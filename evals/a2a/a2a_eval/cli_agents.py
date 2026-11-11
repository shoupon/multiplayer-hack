"""Subscription-backed agent drivers: the user's own Claude Code and Codex (ChatGPT) logins instead
of API keys.

Each turn is one non-interactive CLI run that resumes the agent's session. The CLI starts FCL's MCP
server itself and runs the tool loop, so the harness reads the tool calls back from the CLI's JSON
event stream instead of executing them. Both CLIs are cut down to the FCL tools: no shell or file
tools, no user config, hooks, plugins, rules or web search, and an empty temp directory as cwd.
Neither CLI can override tool descriptions, so prompts/tools.json does not apply on this backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .domain import ToolCall
from .llm import Meter, TurnOutput
from .mcp_client import MAX_RESULT_CHARS, McpLaunch

# The CLIs must use the subscription login. An API key in the environment would take precedence.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


class CliError(RuntimeError):
    pass


@dataclass
class CliOptions:
    binary: str
    model: str
    system: str
    launch: McpLaunch
    agent_token: str
    meter: Meter
    max_steps: int
    timeout: float
    workdir: Path
    transcript: TextIO


def _server_shell_args(launch: McpLaunch) -> list[str]:
    command = shlex.join([launch.command, *launch.args])
    return ["-c", f"cd {shlex.quote(str(launch.cwd))} && exec {command}"]


def _server_env(o: CliOptions) -> dict[str, str]:
    return {"FCL_BASE_URL": o.launch.base_url, "FCL_AGENT_TOKEN": o.agent_token}


async def _run(o: CliOptions, cmd: list[str], prompt: str) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=o.workdir,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), o.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CliError(f"{Path(cmd[0]).name} turn timed out after {o.timeout:.0f}s") from None
    stdout = out.decode(errors="replace")
    o.transcript.write(stdout)
    o.transcript.flush()
    return proc.returncode or 0, stdout, err.decode(errors="replace")


def _events(stdout: str) -> Iterator[dict[str, Any]]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return json.dumps(content, ensure_ascii=False)


class ClaudeCliSession:
    """`claude -p` with only the FCL MCP server. The first turn names the session, later turns resume it."""

    TOOL_PREFIX = "mcp__fcl__"

    def __init__(self, o: CliOptions, session_id: str) -> None:
        self.o = o
        self.session_id = session_id
        self.started = False
        self.mcp_config = o.workdir / "mcp.json"
        config = {"mcpServers": {"fcl": {"type": "stdio", "command": "/bin/sh", "args": _server_shell_args(o.launch), "env": _server_env(o)}}}
        fd = os.open(self.mcp_config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)

    async def turn(self, say: str) -> TurnOutput:
        self.o.meter.guard()
        cmd = [
            self.o.binary, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", self.o.model,
            "--setting-sources", "project",
            "--strict-mcp-config", "--mcp-config", str(self.mcp_config),
            "--tools", "",
            "--allowedTools", "mcp__fcl",
            "--system-prompt", self.o.system,
            "--max-turns", str(self.o.max_steps + 1),
            "--resume" if self.started else "--session-id", self.session_id,
        ]  # fmt: skip
        code, stdout, stderr = await _run(self.o, cmd, say)
        self.started = True

        out = TurnOutput()
        pending: dict[str, tuple[str, Any]] = {}
        message_ids: set[str] = set()
        result: dict[str, Any] | None = None
        for ev in _events(stdout):
            kind = ev.get("type")
            if kind == "assistant":
                message = ev.get("message") or {}
                message_ids.add(message.get("id", ""))
                for block in message.get("content") or []:
                    if block.get("type") == "tool_use":
                        pending[block["id"]] = (block["name"].removeprefix(self.TOOL_PREFIX), block.get("input"))
            elif kind == "user":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        name, args = pending.pop(block.get("tool_use_id"), ("?", None))
                        out.tool_calls.append(ToolCall(name, args, _text_of(block.get("content"))[:MAX_RESULT_CHARS], bool(block.get("is_error"))))
            elif kind == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                if info.get("status") not in (None, "allowed"):
                    out.notes.append(f"claude subscription rate limit: {info.get('status')} ({info.get('rateLimitType')})")
            elif kind == "result":
                result = ev

        if result is None:
            raise CliError(f"claude exited {code} without a result event. stderr: {stderr[-800:]}")
        subtype = result.get("subtype", "")
        if subtype == "error_max_turns":
            out.notes.append(f"claude hit --max-turns {self.o.max_steps + 1} and gave no reply")
        elif result.get("is_error"):
            raise CliError(f"claude turn failed ({subtype}): {str(result.get('result'))[:500]} stderr: {stderr[-500:]}")
        out.reply = result.get("result") or ""
        out.model_calls = len(message_ids - {""}) or 1
        usage = result.get("usage") or {}
        cache_read = usage.get("cache_read_input_tokens") or 0
        self.o.meter.add(
            "claude",
            (usage.get("input_tokens") or 0) + cache_read + (usage.get("cache_creation_input_tokens") or 0),
            cache_read,
            usage.get("output_tokens") or 0,
            calls=out.model_calls,
        )
        return out


CODEX_DISABLED_FEATURES = (
    "apps", "browser_use", "browser_use_external", "computer_use", "image_generation", "in_app_browser",
    "in_app_local_automation", "multi_agent", "plugins", "hooks", "goals", "shell_tool", "view_image",
    "tool_suggest", "skill_search", "sleep_tool",
)  # fmt: skip
# code_mode_host stays on: Codex 0.163 routes MCP tool calls through it, and with it disabled the
# FCL tools are unreachable. With shell_tool off and a read-only sandbox it can only call tools.


def _toml_inline_table(values: dict[str, str]) -> str:
    return "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in values.items()) + "}"


class CodexCliSession:
    """`codex exec` with only the FCL MCP server. The first turn starts a thread, later turns resume it.

    Codex keeps its own coding-agent base prompt. The harness prompt goes in as developer
    instructions, which Codex layers on top of it."""

    def __init__(self, o: CliOptions) -> None:
        self.o = o
        self.thread_id: str | None = None

    def _options(self) -> list[str]:
        opts = [
            "--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"',
            "-c", f"developer_instructions={json.dumps(self.o.system)}",
            "-c", 'mcp_servers.fcl.command="/bin/sh"',
            "-c", f"mcp_servers.fcl.args={json.dumps(_server_shell_args(self.o.launch))}",
            "-c", f"mcp_servers.fcl.env={_toml_inline_table(_server_env(self.o))}",
            "-c", 'mcp_servers.fcl.default_tools_approval_mode="approve"',
        ]  # fmt: skip
        for feature in CODEX_DISABLED_FEATURES:
            opts += ["--disable", feature]
        if self.o.model:
            opts += ["-m", self.o.model]
        return opts

    async def turn(self, say: str) -> TurnOutput:
        self.o.meter.guard()
        if self.thread_id:
            cmd = [self.o.binary, "exec", "resume", *self._options(), self.thread_id, "-"]
        else:
            cmd = [self.o.binary, "exec", *self._options(), "-"]
        code, stdout, stderr = await _run(self.o, cmd, say)

        out = TurnOutput()
        messages: list[str] = []
        usage: dict[str, Any] | None = None
        failure = ""
        for ev in _events(stdout):
            kind = ev.get("type")
            if kind == "thread.started" and not self.thread_id:
                self.thread_id = ev.get("thread_id")
            elif kind == "item.completed":
                item = ev.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message":
                    messages.append(item.get("text") or "")
                elif itype == "mcp_tool_call":
                    result = item.get("result") or {}
                    text = _text_of(result.get("content")) if result else (item.get("error") or {}).get("message", "")
                    out.tool_calls.append(ToolCall(item.get("tool", "?"), item.get("arguments"), text[:MAX_RESULT_CHARS], item.get("status") == "failed"))
                elif itype == "error":
                    out.notes.append(f"codex error item: {(item.get('message') or '')[:300]}")
                elif itype not in ("reasoning", "todo_list"):
                    out.notes.append(f"codex used a non-FCL item type {itype!r}")
            elif kind == "turn.completed":
                usage = ev.get("usage") or {}
            elif kind in ("turn.failed", "error"):
                failure = json.dumps(ev.get("error") or ev.get("message") or ev, ensure_ascii=False)[:500]

        if usage is None:
            raise CliError(f"codex exited {code} without completing the turn: {failure or stderr[-800:]}")
        out.reply = messages[-1] if messages else ""
        # Codex does not report model calls. Each round of tool calls costs at least one, plus the reply.
        out.model_calls = len(out.tool_calls) + 1
        self.o.meter.add(
            "openai",
            usage.get("input_tokens") or 0,
            usage.get("cached_input_tokens") or 0,
            (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0),
            calls=out.model_calls,
        )
        return out
