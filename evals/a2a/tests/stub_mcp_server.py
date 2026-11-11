"""Stand-in for FCL's MCP server, for plumbing tests only. Echoes the agent token it was launched with
so a test can prove each agent's connector holds its own credential."""

import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("fcl-stub", instructions="Stub FCL instructions.")


@server.tool()
def fcl_get_inbox() -> str:
    """Read this agent's FCL inbox."""
    return f"inbox for {os.environ.get('FCL_AGENT_TOKEN', '?')}: proposal tsk_1 v1 dinner 2026-11-13T19:00:00+08:00"


@server.tool()
def fcl_respond_to_proposal(task_id: str, kind: str, proposal_version: int) -> str:
    """Respond to a coordination proposal."""
    if proposal_version != 1:
        raise ToolError("stale_version: the task is at proposal_version 1")
    return f"{kind} recorded on {task_id}"


if __name__ == "__main__":
    server.run()
