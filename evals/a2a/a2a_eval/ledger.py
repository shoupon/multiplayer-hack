"""results.tsv: one row per episode, append-only, committed. The loop's memory across sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .domain import EpisodeResult

COLUMNS = ("run_id", "prompt_hash", "label", "scenario", "husband", "wife", "rep", "pass", "failed_gates", "model_calls", "usd", "tool_calls", "tool_errors", "error")


@dataclass(frozen=True)
class Row:
    run_id: str
    label: str
    scenario: str
    husband: str
    wife: str
    passed: bool
    model_calls: int


def _clean(s: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", s)[:200]


def append_row(path: Path, run_id: str, prompt_hash: str, label: str, ep: EpisodeResult) -> None:
    calls = [c for t in ep.turns for c in t.tool_calls]
    cells = [
        run_id,
        prompt_hash,
        label,
        ep.scenario,
        ep.pair.husband,
        ep.pair.wife,
        str(ep.rep),
        "1" if ep.passed else "0",
        ",".join(c.name for c in ep.checks if c.gate and not c.passed),
        str(ep.model_calls),
        f"{ep.usd:.4f}",
        str(len(calls)),
        str(sum(c.is_error for c in calls)),
        ep.error,
    ]
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write("\t".join(COLUMNS) + "\n")
        f.write("\t".join(_clean(c) for c in cells) + "\n")


def parse_ledger(tsv: str) -> list[Row]:
    lines = [line for line in tsv.splitlines() if line.strip()]
    if not lines:
        return []
    cols = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        f = dict(zip(cols, line.split("\t")))
        rows.append(
            Row(
                run_id=f.get("run_id", ""),
                label=f.get("label", ""),
                scenario=f.get("scenario", ""),
                husband=f.get("husband", ""),
                wife=f.get("wife", ""),
                passed=f.get("pass") == "1",
                model_calls=int(f.get("model_calls") or 0),
            )
        )
    return rows


def read_ledger(path: Path) -> list[Row]:
    return parse_ledger(path.read_text()) if path.exists() else []
