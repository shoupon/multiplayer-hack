from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .domain import Provider

EVAL_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class Config:
    fcl_base_url: str
    fcl_dir: Path
    mcp_command: str
    mcp_args: list[str]
    models: dict[Provider, str]
    prices: dict[Provider, Price]
    max_tool_steps: int
    max_calls: int
    max_usd: float
    prompts_dir: Path
    runs_dir: Path
    ledger_path: Path


_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")


# Keys stay in os.environ and are read only where a request is built. Never log them.
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        m = _ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        key, raw = m.groups()
        os.environ.setdefault(key, re.sub(r"^(['\"])(.*)\1$", r"\2", raw))


def _num(name: str, fallback: float) -> float:
    raw = os.environ.get(name, "")
    return float(raw) if raw else fallback


def api_key(provider: Provider) -> str:
    name = "ANTHROPIC_API_KEY" if provider == "claude" else "OPENAI_API_KEY"
    key = os.environ.get(name)
    if not key:
        raise RuntimeError(f"{name} is not set. Export it or put it in evals/a2a/.env.local.")
    return key


def load_config() -> Config:
    _load_env_file(EVAL_DIR / ".env.local")
    command, *args = shlex.split(os.environ.get("FCL_MCP_COMMAND", ".venv/bin/python -m fcl.mcp_server"))
    return Config(
        fcl_base_url=os.environ.get("FCL_BASE_URL", "http://127.0.0.1:8000"),
        fcl_dir=Path(os.environ.get("FCL_DIR", str(EVAL_DIR.parent.parent))).resolve(),
        mcp_command=command,
        mcp_args=args,
        models={
            "claude": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            "openai": os.environ.get("OPENAI_MODEL", "chat-latest"),
        },
        # USD per million tokens, from the provider pricing pages on 2026-11-11. Override when a model changes.
        prices={
            "claude": Price(_num("CLAUDE_PRICE_IN", 2), _num("CLAUDE_PRICE_CACHED", 0.1), _num("CLAUDE_PRICE_OUT", 10)),
            "openai": Price(_num("OPENAI_PRICE_IN", 5), _num("OPENAI_PRICE_CACHED", 0.5), _num("OPENAI_PRICE_OUT", 30)),
        },
        max_tool_steps=int(_num("MAX_TOOL_STEPS", 8)),
        max_calls=int(_num("MAX_MODEL_CALLS", 400)),
        max_usd=_num("MAX_RUN_USD", 12),
        prompts_dir=EVAL_DIR / "prompts",
        runs_dir=EVAL_DIR / "runs",
        ledger_path=EVAL_DIR / "results.tsv",
    )
