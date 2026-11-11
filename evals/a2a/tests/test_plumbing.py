"""The tool loop end to end with no FCL and no API spend: a stub MCP server stands in for FCL and
httpx.MockTransport stands in for each provider, so the wire formats are checked exactly."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest

from a2a_eval.config import Price
from a2a_eval.llm import BudgetExceeded, ClaudeSession, Meter, OpenAISession, SessionOptions, budget_exceeded_in
from a2a_eval.mcp_client import FclMcp, McpLaunch

STUB = Path(__file__).with_name("stub_mcp_server.py")
PRICES = {"claude": Price(2, 0.1, 10), "openai": Price(5, 0.5, 30)}


def scripted(responses: list[dict], seen: list[dict]) -> httpx.AsyncClient:
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=next(queue))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def with_stub(tmp_path: Path, body):
    async def go():
        async with AsyncExitStack() as stack:
            errlog = stack.enter_context((tmp_path / "mcp.log").open("w"))
            mcp = await FclMcp.connect(stack, McpLaunch(sys.executable, [str(STUB)], STUB.parent, "http://fcl.invalid"), "fcl_a_test", errlog)
            return await body(mcp)

    return asyncio.run(go())


def options(mcp: FclMcp, http: httpx.AsyncClient, meter: Meter, max_steps: int = 8) -> SessionOptions:
    return SessionOptions("model-x", "key-x", "system prompt", mcp.tools, mcp.call, meter, max_steps, http)


def claude_reply(content: list[dict], input_tokens: int = 100, cache_read: int = 0) -> dict:
    return {"content": content, "usage": {"input_tokens": input_tokens, "output_tokens": 10, "cache_read_input_tokens": cache_read}}


def test_mcp_connector_lists_tools_and_holds_its_own_token(tmp_path):
    async def body(mcp: FclMcp):
        inbox = await mcp.call("fcl_get_inbox", {})
        stale = await mcp.call("fcl_respond_to_proposal", {"task_id": "tsk_1", "kind": "accept", "proposal_version": 2})
        return mcp, inbox, stale

    mcp, inbox, stale = with_stub(tmp_path, body)
    assert {t.name for t in mcp.tools} == {"fcl_get_inbox", "fcl_respond_to_proposal"}
    assert mcp.instructions == "Stub FCL instructions."
    assert "fcl_a_test" in inbox.text and not inbox.is_error
    assert stale.is_error and "stale_version" in stale.text


def test_claude_tool_loop_returns_tool_results_and_meters_cache(tmp_path):
    seen: list[dict] = []
    meter = Meter(PRICES, 100, 100)

    async def body(mcp: FclMcp):
        http = scripted(
            [
                claude_reply([{"type": "text", "text": "Checking."}, {"type": "tool_use", "id": "tu_1", "name": "fcl_get_inbox", "input": {}}], 100, 50),
                claude_reply([{"type": "text", "text": "Ben proposed dinner Friday at 7pm."}], 120),
            ],
            seen,
        )
        return await ClaudeSession(options(mcp, http, meter)).turn("Anything new?")

    out = with_stub(tmp_path, body)
    assert out.reply == "Ben proposed dinner Friday at 7pm."
    assert [c.name for c in out.tool_calls] == ["fcl_get_inbox"] and "fcl_a_test" in out.tool_calls[0].result
    assert seen[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert seen[1]["messages"][-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": out.tool_calls[0].result, "is_error": False}]}
    usage = meter.usage["claude"]
    assert (usage.calls, usage.input_tokens, usage.cached_input_tokens) == (2, 270, 50)


def test_openai_tool_loop_marks_errors_and_starts_with_system(tmp_path):
    seen: list[dict] = []
    meter = Meter(PRICES, 100, 100)
    call = {"id": "c1", "type": "function", "function": {"name": "fcl_respond_to_proposal", "arguments": '{"task_id":"tsk_1","kind":"accept","proposal_version":2}'}}

    async def body(mcp: FclMcp):
        http = scripted(
            [
                {"choices": [{"message": {"content": None, "tool_calls": [call]}}], "usage": {"prompt_tokens": 200, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 100}}},
                {"choices": [{"message": {"content": "The proposal changed. Checking again."}}], "usage": {"prompt_tokens": 250, "completion_tokens": 15}},
            ],
            seen,
        )
        return await OpenAISession(options(mcp, http, meter)).turn("Accept it.")

    out = with_stub(tmp_path, body)
    assert seen[0]["messages"][0] == {"role": "system", "content": "system prompt"}
    assert out.tool_calls[0].is_error and out.tool_calls[0].args["proposal_version"] == 2
    assert seen[1]["messages"][-1]["role"] == "tool" and seen[1]["messages"][-1]["content"].startswith("ERROR:")
    assert meter.usage["openai"].cached_input_tokens == 100


def test_step_limit_ends_a_turn_that_keeps_calling_tools(tmp_path):
    use = claude_reply([{"type": "tool_use", "id": "tu", "name": "fcl_get_inbox", "input": {}}])

    async def body(mcp: FclMcp):
        return await ClaudeSession(options(mcp, scripted([use, use, use], []), Meter(PRICES, 100, 100), max_steps=1)).turn("Loop forever.")

    out = with_stub(tmp_path, body)
    assert out.model_calls == 3
    assert [c.is_error for c in out.tool_calls] == [False, True, True]


def test_budget_cap_stops_before_the_next_model_call(tmp_path):
    use = claude_reply([{"type": "tool_use", "id": "tu", "name": "fcl_get_inbox", "input": {}}])

    async def body(mcp: FclMcp):
        return await ClaudeSession(options(mcp, scripted([use, use], []), Meter(PRICES, 1, 100))).turn("Hi.")

    with pytest.raises(Exception) as caught:
        with_stub(tmp_path, body)
    assert isinstance(budget_exceeded_in(caught.value), BudgetExceeded)
