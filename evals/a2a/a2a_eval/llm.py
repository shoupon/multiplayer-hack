"""API-key agent drivers with identical behavior: one human message in, a tool loop over FCL's MCP
tools, one reply out. Only the provider wire format differs. cli_agents.py has the subscription
drivers, which implement the same AgentSession protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import Price
from .domain import PROVIDERS, Provider, ToolCall, Usage
from .mcp_client import ToolResult, ToolSpec


class BudgetExceeded(Exception):
    pass


def budget_exceeded_in(err: BaseException) -> BudgetExceeded | None:
    """anyio task groups inside the MCP client wrap exceptions raised in their body in an exception group."""
    if isinstance(err, BudgetExceeded):
        return err
    for inner in getattr(err, "exceptions", ()):
        found = budget_exceeded_in(inner)
        if found:
            return found
    return None


def cost_usd(u: Usage, p: Price) -> float:
    uncached = u.input_tokens - u.cached_input_tokens
    return (uncached * p.input + u.cached_input_tokens * p.cached_input + u.output_tokens * p.output) / 1_000_000


class Meter:
    def __init__(self, prices: dict[Provider, Price], max_calls: int, max_usd: float) -> None:
        self.prices = prices
        self.max_calls = max_calls
        self.max_usd = max_usd
        self.usage: dict[Provider, Usage] = {p: Usage() for p in PROVIDERS}

    def add(self, provider: Provider, input_tokens: int, cached_input_tokens: int, output_tokens: int, calls: int = 1) -> None:
        u = self.usage[provider]
        u.calls += calls
        u.input_tokens += input_tokens
        u.cached_input_tokens += cached_input_tokens
        u.output_tokens += output_tokens

    def usd(self, provider: Provider | None = None) -> float:
        providers = [provider] if provider else list(PROVIDERS)
        return sum(cost_usd(self.usage[p], self.prices[p]) for p in providers)

    def calls(self) -> int:
        return sum(u.calls for u in self.usage.values())

    def guard(self) -> None:
        if self.calls() >= self.max_calls:
            raise BudgetExceeded(f"model call cap {self.max_calls} reached")
        if self.usd() >= self.max_usd:
            raise BudgetExceeded(f"spend cap ${self.max_usd} reached (${self.usd():.2f})")


@dataclass
class TurnOutput:
    tool_calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""
    model_calls: int = 0
    notes: list[str] = field(default_factory=list)


class AgentSession(Protocol):
    async def turn(self, say: str) -> TurnOutput: ...


@dataclass
class SessionOptions:
    model: str
    api_key: str
    system: str
    tools: list[ToolSpec]
    execute: Callable[[str, Any], Awaitable[ToolResult]]
    meter: Meter
    max_steps: int
    http: httpx.AsyncClient


RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
STEP_LIMIT = ToolResult("Tool step limit reached for this turn. Answer your principal now without more tool calls.", True)


async def _post_json(http: httpx.AsyncClient, url: str, headers: dict[str, str], body: Any, label: str) -> Any:
    for attempt in range(1, 6):
        res = await http.post(url, headers=headers, json=body, timeout=120)
        if res.is_success:
            return res.json()
        if attempt < 5 and res.status_code in RETRYABLE:
            await asyncio.sleep(2 * 2 ** (attempt - 1))
            continue
        raise RuntimeError(f"{label} {res.status_code}: {res.text[:500]}")


def _schema(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "object", **{k: v for k, v in tool.input_schema.items() if k != "$schema"}}


class ClaudeSession:
    def __init__(self, o: SessionOptions) -> None:
        self.o = o
        self.messages: list[dict[str, Any]] = []
        self.tools = [
            {"name": t.name, "description": t.description, "input_schema": _schema(t)}
            | ({"cache_control": {"type": "ephemeral"}} if i == len(o.tools) - 1 else {})
            for i, t in enumerate(o.tools)
        ]

    def _push_user(self, blocks: list[dict[str, Any]]) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].extend(blocks)
        else:
            self.messages.append({"role": "user", "content": blocks})

    async def turn(self, say: str) -> TurnOutput:
        self._push_user([{"type": "text", "text": say}])
        out = TurnOutput()
        for step in range(self.o.max_steps + 2):
            self.o.meter.guard()
            res = await _post_json(
                self.o.http,
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": self.o.api_key, "anthropic-version": "2023-06-01"},
                {
                    "model": self.o.model,
                    "max_tokens": 2048,
                    "system": [{"type": "text", "text": self.o.system, "cache_control": {"type": "ephemeral"}}],
                    "tools": self.tools,
                    "messages": self.messages,
                },
                "anthropic",
            )
            out.model_calls += 1
            usage = res["usage"]
            cache_read = usage.get("cache_read_input_tokens") or 0
            self.o.meter.add(
                "claude",
                usage["input_tokens"] + cache_read + (usage.get("cache_creation_input_tokens") or 0),
                cache_read,
                usage["output_tokens"],
            )
            content: list[dict[str, Any]] = res["content"]
            self.messages.append({"role": "assistant", "content": content})
            out.reply = "\n".join(b["text"] for b in content if b["type"] == "text")
            uses = [b for b in content if b["type"] == "tool_use"]
            if not uses:
                break
            results = []
            for use in uses:
                r = await self.o.execute(use["name"], use["input"]) if step < self.o.max_steps else STEP_LIMIT
                out.tool_calls.append(ToolCall(use["name"], use["input"], r.text, r.is_error))
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": r.text, "is_error": r.is_error})
            self._push_user(results)
        return out


def _parse_arguments(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparseable_arguments": raw}


class OpenAISession:
    def __init__(self, o: SessionOptions) -> None:
        self.o = o
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": o.system}]
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": _schema(t)}} for t in o.tools]

    async def turn(self, say: str) -> TurnOutput:
        self.messages.append({"role": "user", "content": say})
        out = TurnOutput()
        for step in range(self.o.max_steps + 2):
            self.o.meter.guard()
            res = await _post_json(
                self.o.http,
                "https://api.openai.com/v1/chat/completions",
                {"authorization": f"Bearer {self.o.api_key}"},
                {"model": self.o.model, "messages": self.messages, "tools": self.tools, "max_completion_tokens": 4096},
                "openai",
            )
            out.model_calls += 1
            usage = res["usage"]
            self.o.meter.add(
                "openai",
                usage["prompt_tokens"],
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                usage["completion_tokens"],
            )
            msg = res["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content")} | ({"tool_calls": calls} if calls else {}))
            out.reply = msg.get("content") or ""
            if not calls:
                break
            for call in calls:
                args = _parse_arguments(call["function"]["arguments"])
                r = await self.o.execute(call["function"]["name"], args) if step < self.o.max_steps else STEP_LIMIT
                out.tool_calls.append(ToolCall(call["function"]["name"], args, r.text, r.is_error))
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": f"ERROR: {r.text}" if r.is_error else r.text})
        return out
