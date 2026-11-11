from __future__ import annotations

from dataclasses import dataclass

from .ledger import Row


@dataclass(frozen=True)
class Tally:
    passed: int
    total: int

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass(frozen=True)
class Cell:
    key: str
    base: Tally
    cand: Tally


@dataclass(frozen=True)
class Verdict:
    accept: bool
    reasons: list[str]
    cells: list[Cell]


def _key(r: Row) -> str:
    return f"{r.scenario} {r.husband}->{r.wife}"


def _tally(rows: list[Row]) -> Tally:
    return Tally(sum(r.passed for r in rows), len(rows))


def _mean_calls(rows: list[Row]) -> float:
    return sum(r.model_calls for r in rows) / max(len(rows), 1)


def verdict(base: list[Row], cand: list[Row]) -> Verdict:
    """Keep/revert rule for one loop iteration. LLM runs are noisy at small n, so a cell may lose at
    most one episode's worth of pass rate. The candidate must then beat the baseline overall, or tie
    it while costing at least 10% fewer model calls per episode."""
    keys = sorted({_key(r) for r in [*base, *cand]})
    cells = [Cell(k, _tally([r for r in base if _key(r) == k]), _tally([r for r in cand if _key(r) == k])) for k in keys]

    reasons = []
    for c in cells:
        if c.base.total and not c.cand.total:
            reasons.append(f"candidate did not run {c.key}")
        elif c.base.total and c.cand.rate < c.base.rate - 1 / c.cand.total - 1e-9:
            reasons.append(f"regression in {c.key}: {c.base.passed}/{c.base.total} -> {c.cand.passed}/{c.cand.total}")
    if reasons:
        return Verdict(False, reasons, cells)

    b, k = _tally(base).rate, _tally(cand).rate
    if k > b:
        return Verdict(True, [f"pass rate {b:.0%} -> {k:.0%}"], cells)
    if k == b and _mean_calls(cand) <= 0.9 * _mean_calls(base):
        return Verdict(True, [f"same pass rate, model calls/episode {_mean_calls(base):.1f} -> {_mean_calls(cand):.1f}"], cells)
    return Verdict(False, [f"no improvement: pass rate {b:.0%} -> {k:.0%}"], cells)
