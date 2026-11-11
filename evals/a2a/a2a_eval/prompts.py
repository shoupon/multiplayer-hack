"""The prompt surfaces under iteration. Everything the loop may edit lives in prompts/:

shared.md   guidance every agent gets, whatever its provider (ships as: FCL MCP server instructions)
claude.md   Claude-only addendum (ships as: what a Claude user pastes into project instructions)
openai.md   ChatGPT-only addendum (ships as: ChatGPT custom instructions)
tools.json  per-tool description overrides (ships as: FCL MCP tool descriptions)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from .domain import Provider
from .mcp_client import ToolSpec

FILES = ("shared.md", "claude.md", "openai.md", "tools.json")


@dataclass(frozen=True)
class PromptSet:
    shared: str
    overlay: dict[Provider, str]
    tool_overrides: dict[str, dict[str, str]]
    hash: str


def load_prompts(directory: Path) -> PromptSet:
    contents = {f: (directory / f).read_text() if (directory / f).exists() else "" for f in FILES}
    digest = hashlib.sha256("\0".join(f"{f}\0{contents[f]}" for f in FILES).encode()).hexdigest()[:10]
    return PromptSet(
        shared=contents["shared.md"],
        overlay={"claude": contents["claude.md"], "openai": contents["openai.md"]},
        tool_overrides=json.loads(contents["tools.json"]) if contents["tools.json"].strip() else {},
        hash=digest,
    )


def compose_system(p: PromptSet, provider: Provider, server_instructions: str | None, persona: str) -> str:
    server = f"## Instructions from the FCL connector\n\n{server_instructions.strip()}" if server_instructions and server_instructions.strip() else ""
    return "\n\n".join(s.strip() for s in (p.shared, p.overlay[provider], server, persona) if s.strip())


def apply_tool_overrides(tools: list[ToolSpec], overrides: dict[str, dict[str, str]]) -> tuple[list[ToolSpec], list[str]]:
    names = {t.name for t in tools}
    patched = [replace(t, description=overrides.get(t.name, {}).get("description", t.description)) for t in tools]
    return patched, [n for n in overrides if n not in names]
