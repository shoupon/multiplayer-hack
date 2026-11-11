from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domain import PROVIDERS, Provider

Backend = Literal["cli", "api"]

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
    backends: dict[Provider, Backend]
    models: dict[Provider, str]
    prices: dict[Provider, Price]
    max_tool_steps: int
    max_calls: int
    max_usd: float
    prompts_dir: Path
    runs_dir: Path
    ledger_path: Path
    claude_bin: str
    codex_bin: str
    cli_turn_timeout: float


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


def _backend(provider: Provider) -> Backend:
    name = f"{provider.upper()}_BACKEND"
    value = os.environ.get(name, "cli")
    if value not in ("cli", "api"):
        raise RuntimeError(f'{name} must be "cli" (subscription login) or "api" (API key), not "{value}"')
    return value  # type: ignore[return-value]


def _default_claude_bin() -> str:
    # The native install. A wrapper earlier on PATH (cmux, for one) can inject hooks into every run.
    native = Path.home() / ".local/bin/claude"
    return str(native) if native.exists() else "claude"


def load_config() -> Config:
    _load_env_file(EVAL_DIR / ".env.local")
    command, *args = shlex.split(os.environ.get("FCL_MCP_COMMAND", ".venv/bin/python -m fcl.mcp_server"))
    backends = {p: _backend(p) for p in PROVIDERS}
    free = Price(0, 0, 0)
    return Config(
        fcl_base_url=os.environ.get("FCL_BASE_URL", "http://127.0.0.1:8000"),
        fcl_dir=Path(os.environ.get("FCL_DIR", str(EVAL_DIR.parent.parent / "family-coordination-layer"))).resolve(),
        mcp_command=command,
        mcp_args=args,
        backends=backends,
        models={
            "claude": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            # Empty on the cli backend means the model Codex picks for the signed-in ChatGPT account.
            "openai": os.environ.get("OPENAI_MODEL", "chat-latest" if backends["openai"] == "api" else ""),
        },
        # USD per million tokens, from the provider pricing pages on 2026-11-11. Override when a model
        # changes. The cli backend draws on the subscription, so it meters tokens but no dollars.
        prices={
            "claude": free if backends["claude"] == "cli" else Price(_num("CLAUDE_PRICE_IN", 2), _num("CLAUDE_PRICE_CACHED", 0.1), _num("CLAUDE_PRICE_OUT", 10)),
            "openai": free if backends["openai"] == "cli" else Price(_num("OPENAI_PRICE_IN", 5), _num("OPENAI_PRICE_CACHED", 0.5), _num("OPENAI_PRICE_OUT", 30)),
        },
        max_tool_steps=int(_num("MAX_TOOL_STEPS", 8)),
        max_calls=int(_num("MAX_MODEL_CALLS", 400)),
        max_usd=_num("MAX_RUN_USD", 12),
        prompts_dir=EVAL_DIR / "prompts",
        runs_dir=EVAL_DIR / "runs",
        ledger_path=EVAL_DIR / "results.tsv",
        claude_bin=os.environ.get("CLAUDE_BIN", _default_claude_bin()),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        cli_turn_timeout=_num("CLI_TURN_TIMEOUT", 300),
    )
