"""Run the scenario matrix once against a live FCL and append every episode to results.tsv.

    .venv/bin/python -m a2a_eval.run --reps 3 --label baseline
    .venv/bin/python -m a2a_eval.run --scenarios dinner-counter --reps 1 --label smoke
    .venv/bin/python -m a2a_eval.run --print-prompts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone

import httpx

from .config import load_config
from .domain import PROVIDERS, EpisodeResult, Family, Member, Pair
from .episode import persona, run_episode
from .evidence import scenario_dates
from .fcl import FclRest, preflight
from .ledger import append_row
from .llm import BudgetExceeded, Meter
from .prompts import compose_system, load_prompts
from .scenarios import SCENARIOS


def _parse_pairs(spec: str) -> list[Pair]:
    pairs = []
    for item in spec.split(","):
        husband, _, wife = item.partition(":")
        if husband not in PROVIDERS or wife not in PROVIDERS:
            raise SystemExit(f'bad pair "{item}"; use husband:wife with {"|".join(PROVIDERS)}')
        pairs.append(Pair(husband, wife))  # type: ignore[arg-type]
    return pairs


def _print_prompts(prompts, scenario) -> None:
    d = scenario_dates(datetime.now(timezone.utc))
    member = lambda name: Member(name, "usr_…", "", "agt_…", "")  # noqa: E731
    family = Family("fam_…", member("Ben"), member("Ann"))
    for provider in PROVIDERS:
        print(f"===== {provider} system prompt (wife, {scenario.id}) =====")
        print(compose_system(prompts, provider, None, persona("wife", family, d, scenario.private_notes(d).get("wife"))))
    print(f"\ntool overrides: {', '.join(prompts.tool_overrides) or 'none'}\nprompt hash: {prompts.hash}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default=",".join(s.id for s in SCENARIOS))
    ap.add_argument("--pairs", default="claude:openai,openai:claude", help="husband:wife providers, comma separated")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--print-prompts", action="store_true", help="print the composed system prompts and exit; no FCL or API calls")
    args = ap.parse_args()

    cfg = load_config()
    prompts = load_prompts(cfg.prompts_dir)
    by_id = {s.id: s for s in SCENARIOS}
    unknown = [s for s in args.scenarios.split(",") if s not in by_id]
    if unknown:
        raise SystemExit(f"unknown scenario {unknown}; known: {', '.join(by_id)}")
    scenarios = [by_id[s] for s in args.scenarios.split(",")]
    pairs = _parse_pairs(args.pairs)

    if args.print_prompts:
        _print_prompts(prompts, scenarios[0])
        return

    rest = FclRest(cfg.fcl_base_url)
    await preflight(rest)

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{prompts.hash}"
    run_dir = cfg.runs_dir / run_id
    run_dir.mkdir(parents=True)
    shutil.copytree(cfg.prompts_dir, run_dir / "prompts")
    episodes_path = run_dir / "episodes.jsonl"
    meter = Meter(cfg.prices, cfg.max_calls, cfg.max_usd)
    total = len(scenarios) * len(pairs) * args.reps
    results: list[EpisodeResult] = []

    agents = ", ".join(f"{p}={cfg.models[p] or 'default'} via {cfg.backends[p]}" for p in PROVIDERS)
    print(f"run {run_id}: {total} episodes, {agents}, cap ${cfg.max_usd}")
    async with httpx.AsyncClient() as http:
        try:
            for scenario in scenarios:
                for pair in pairs:
                    for rep in range(1, args.reps + 1):
                        ep = await run_episode(scenario=scenario, pair=pair, rep=rep, rest=rest, cfg=cfg, prompts=prompts, meter=meter, http=http, log_dir=run_dir)
                        results.append(ep)
                        with episodes_path.open("a") as f:
                            f.write(json.dumps({"run_id": run_id, "prompt_hash": prompts.hash, **asdict(ep)}, ensure_ascii=False, default=str) + "\n")
                        append_row(cfg.ledger_path, run_id, prompts.hash, args.label, ep)
                        failed = [f"{c.name}({c.detail.splitlines()[0]})" if c.detail else c.name for c in ep.checks if c.gate and not c.passed]
                        verdict = "PASS" if ep.passed else "FAIL " + " ".join(failed)
                        print(f"[{len(results)}/{total}] {scenario.id} {pair.label()} rep{rep} {verdict} {ep.model_calls} calls ${ep.usd:.3f}")
                        for w in ep.warnings:
                            print(f"  warning: {w}")
        except BudgetExceeded as err:
            print(f"stopped early: {err}")
        finally:
            await rest.aclose()

    cells: dict[str, list[int]] = {}
    for ep in results:
        c = cells.setdefault(f"{ep.scenario} {ep.pair.label()}", [0, 0])
        c[0] += ep.passed
        c[1] += 1
    print(f"\n{'cell':<36} pass")
    for key, (passed, n) in cells.items():
        print(f"{key:<36} {passed}/{n}")
    spend = ", ".join(f"{p} ${meter.usd(p):.2f}" for p in PROVIDERS)
    print(f"\nrun {run_id}: {sum(r.passed for r in results)}/{len(results)} passed, {meter.calls()} model calls, ${meter.usd():.2f} ({spend})")
    print(f"transcripts: {episodes_path}")


if __name__ == "__main__":
    asyncio.run(main())
