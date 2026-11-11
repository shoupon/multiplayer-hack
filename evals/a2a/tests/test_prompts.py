"""The loop tunes prompts against four scenarios. A prompt that names the scenarios' people, times,
or private facts would pass them by memorizing instead of generalizing, so it fails here."""

import re

from a2a_eval.config import EVAL_DIR
from a2a_eval.prompts import FILES

SCENARIO_LITERALS = re.compile(r"\bBen\b|\bAnn\b|7:30|19:30|19:00|gyn|veterans|hospital|dinner|pre-?authori[sz]ed this|SYSTEM NOTICE\]", re.I)


def test_prompts_carry_no_scenario_specifics():
    hits = {
        f: SCENARIO_LITERALS.findall((EVAL_DIR / "prompts" / f).read_text())
        for f in FILES
        if (EVAL_DIR / "prompts" / f).exists()
    }
    assert {f: h for f, h in hits.items() if h} == {}
