from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent

MAX_RESULT_CHARS = 8_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool


@dataclass(frozen=True)
class McpLaunch:
    command: str
    args: list[str]
    cwd: Path
    base_url: str


class FclMcp:
    """One FCL MCP server process per agent, holding exactly that agent's token, the way an
    assistant's connector would. The child gets a minimal environment, never the LLM API keys."""

    def __init__(self, session: ClientSession, tools: list[ToolSpec], instructions: str | None) -> None:
        self._session = session
        self.tools = tools
        self.instructions = instructions

    @classmethod
    async def connect(cls, stack: AsyncExitStack, launch: McpLaunch, agent_token: str, errlog: TextIO) -> FclMcp:
        params = StdioServerParameters(
            command=launch.command,
            args=launch.args,
            cwd=str(launch.cwd),
            env={**get_default_environment(), "FCL_BASE_URL": launch.base_url, "FCL_AGENT_TOKEN": agent_token},
        )
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        tools: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            listed = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            tools += [ToolSpec(t.name, t.description or "", dict(t.input_schema)) for t in listed.tools]
            cursor = listed.next_cursor
            if not cursor:
                break
        return cls(session, tools, init.instructions)

    async def call(self, name: str, args: Any) -> ToolResult:
        try:
            res = await self._session.call_tool(name, args if isinstance(args, dict) else {})
        except Exception as err:  # a tool failure is evidence for the model, never a harness crash
            return ToolResult(f"Tool call failed: {err}", True)
        if not isinstance(res, CallToolResult):
            return ToolResult(f"Tool returned {type(res).__name__}, which this harness does not support.", True)
        text = "\n".join(c.text if isinstance(c, TextContent) else f"[{c.type} content]" for c in res.content)
        return ToolResult(text[:MAX_RESULT_CHARS], bool(res.is_error))
