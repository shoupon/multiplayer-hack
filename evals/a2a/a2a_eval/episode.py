from __future__ import annotations

import shutil
import tempfile
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import httpx

from .cli_agents import ClaudeCliSession, CliOptions, CodexCliSession
from .config import Config, api_key
from .domain import OTHER, ROLES, Check, EpisodeResult, Evidence, Family, Pair, Role, Turn
from .evidence import Dates, scenario_dates
from .fcl import FclRest, list_tasks, setup_family
from .llm import AgentSession, ClaudeSession, Meter, OpenAISession, SessionOptions, budget_exceeded_in
from .mcp_client import FclMcp, McpLaunch
from .prompts import PromptSet, apply_tool_overrides, compose_system
from .scenarios import HarnessAction, Scenario

RELATION: dict[Role, str] = {"husband": "wife", "wife": "husband"}


def persona(role: Role, family: Family, d: Dates, private_note: str | None) -> str:
    me, other = family[role], family[OTHER[role]]
    lines = [
        "## Your principal",
        f"You are the personal assistant of {me.name}. {me.name}'s {RELATION[role]} is {other.name}, who has their own assistant.",
        f"Today is {d.weekday} {d.today}, Asia/Taipei.",
        f"FCL context: family_id {family.family_id}, your agent_id {me.agent_id}, {me.name}'s user_id {me.user_id}.",
    ]
    return "\n".join([*lines, private_note] if private_note else lines)


def _leaf(err: BaseException) -> BaseException:
    inner = getattr(err, "exceptions", ())
    return _leaf(inner[0]) if inner else err


async def run_episode(
    *,
    scenario: Scenario,
    pair: Pair,
    rep: int,
    rest: FclRest,
    cfg: Config,
    prompts: PromptSet,
    meter: Meter,
    http: httpx.AsyncClient,
    log_dir: Path,
) -> EpisodeResult:
    d = scenario_dates(datetime.now(timezone.utc))
    usd_before, calls_before = meter.usd(), meter.calls()
    family = await setup_family(rest, pair, wife_needs_approval=scenario.wife_needs_approval)
    notes = scenario.private_notes(d)
    turns: list[Turn] = []
    warnings: list[str] = []
    error = ""
    episode_name = f"{scenario.id}-{pair.husband}-{pair.wife}-rep{rep}"
    errlog_path = log_dir / f"{episode_name}.mcp.log"

    async def make_agent(stack: AsyncExitStack, role: Role, errlog: TextIO) -> AgentSession:
        launch = McpLaunch(cfg.mcp_command, cfg.mcp_args, cfg.fcl_dir, cfg.fcl_base_url)
        mcp = await FclMcp.connect(stack, launch, family[role].agent_token, errlog)
        provider = pair[role]
        system = compose_system(prompts, provider, mcp.instructions, persona(role, family, d, notes.get(role)))
        if cfg.backends[provider] == "cli":
            if prompts.tool_overrides and role == "husband":
                warnings.append("tools.json is not applied on the cli backend; the CLIs read tool descriptions from the server")
            workdir = Path(tempfile.mkdtemp(prefix="fcl-agent-"))
            stack.callback(shutil.rmtree, workdir, ignore_errors=True)
            cli = CliOptions(
                binary=cfg.claude_bin if provider == "claude" else cfg.codex_bin,
                model=cfg.models[provider],
                system=system,
                launch=launch,
                agent_token=family[role].agent_token,
                meter=meter,
                max_steps=cfg.max_tool_steps,
                timeout=cfg.cli_turn_timeout,
                workdir=workdir,
                transcript=stack.enter_context((log_dir / f"{episode_name}.{role}.{provider}.jsonl").open("w")),
            )
            return ClaudeCliSession(cli, str(uuid.uuid4())) if provider == "claude" else CodexCliSession(cli)
        tools, unknown = apply_tool_overrides(mcp.tools, prompts.tool_overrides)
        if unknown:
            warnings.append(f"tools.json overrides tools the server does not expose: {', '.join(unknown)}")
        options = SessionOptions(
            model=cfg.models[provider],
            api_key=api_key(provider),
            system=system,
            tools=tools,
            execute=mcp.call,
            meter=meter,
            max_steps=cfg.max_tool_steps,
            http=http,
        )
        return ClaudeSession(options) if provider == "claude" else OpenAISession(options)

    with errlog_path.open("w") as errlog:
        try:
            async with AsyncExitStack() as stack:
                agents = {role: await make_agent(stack, role, errlog) for role in ROLES}
                for step in scenario.steps(d):
                    if isinstance(step, HarnessAction):
                        await step.run(rest, family)
                        continue
                    out = await agents[step.who].turn(step.text)
                    warnings.extend(f"{step.who}: {n}" for n in out.notes)
                    turns.append(Turn(step.who, pair[step.who], step.text, out.tool_calls, out.reply, out.model_calls))
        except Exception as err:
            budget = budget_exceeded_in(err)
            if budget:
                raise budget from None
            leaf = _leaf(err)
            errlog.flush()
            error = f"{type(leaf).__name__}: {leaf}\nMCP stderr tail:\n{errlog_path.read_text()[-2000:]}"

    tasks = await list_tasks(rest, family.husband.user_token)
    checks: list[Check] = [Check("episode_completed", False, True, error[:500])] if error else scenario.checks(Evidence(turns, tasks), d)
    return EpisodeResult(
        scenario=scenario.id,
        pair=pair,
        rep=rep,
        passed=all(c.passed for c in checks if c.gate),
        checks=checks,
        turns=turns,
        tasks=tasks,
        model_calls=meter.calls() - calls_before,
        usd=meter.usd() - usd_before,
        error=error,
        warnings=warnings,
    )
