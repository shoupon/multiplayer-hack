"""Keep or revert a prompt change: compare two runs from results.tsv.

    .venv/bin/python -m a2a_eval.compare_cli <baseline run_id> [<candidate run_id> | latest]

Exit 0 means ACCEPT, 3 means REJECT.
"""

from __future__ import annotations

import sys

from .compare import verdict
from .config import load_config
from .ledger import read_ledger


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 64
    rows = read_ledger(load_config().ledger_path)
    run_ids = list(dict.fromkeys(r.run_id for r in rows))
    resolve = lambda rid: (run_ids[-1] if run_ids else "") if rid == "latest" else rid  # noqa: E731
    base_id, cand_id = resolve(argv[0]), resolve(argv[1] if len(argv) > 1 else "latest")
    base = [r for r in rows if r.run_id == base_id]
    cand = [r for r in rows if r.run_id == cand_id]
    if not base or not cand:
        print(f"no ledger rows for {base_id if not base else cand_id}. Known runs: {', '.join(run_ids)}", file=sys.stderr)
        return 64

    v = verdict(base, cand)
    print(f"baseline  {base_id}\ncandidate {cand_id}\n")
    for c in v.cells:
        print(f"{c.key:<36} {c.base.passed}/{c.base.total} -> {c.cand.passed}/{c.cand.total}")
    print(f"\nVERDICT {'ACCEPT' if v.accept else 'REJECT'}: {'; '.join(v.reasons)}")
    return 0 if v.accept else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
