from dataclasses import replace
from datetime import datetime, timezone
from itertools import count

from fcl.policy import AgentActor, Allow, Deny, Grant, Question, authorize

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
_ids = count()


def grant(**over) -> Grant:
    base = Grant(f"grt_{next(_ids)}", "agt_h", "fam_1", "task:propose", None, False, None, None)
    return replace(base, **over)


def ask(**over) -> Question:
    base = Question(
        actor=AgentActor("agt_h", "usr_h", True),
        family_id="fam_1",
        action="task:propose",
        target_user_id="usr_w",
        active_members=frozenset({"usr_h", "usr_w", "usr_g"}),
        grants=(grant(),),
        now=NOW,
    )
    return replace(base, **over)


def test_allows_with_family_wide_grant():
    d = authorize(ask())
    assert isinstance(d, Allow) and not d.requires_approval


def test_denies_without_grant_for_the_action():
    assert authorize(ask(action="agent:message:send")) == Deny("no_grant")


def test_denies_grant_aimed_at_a_different_target():
    assert authorize(ask(grants=(grant(target_user_id="usr_g"),))) == Deny("no_grant")


def test_denies_revoked_and_expired_grants():
    assert authorize(ask(grants=(grant(revoked_at=NOW),))) == Deny("no_grant")
    assert authorize(ask(grants=(grant(expires_at=NOW),))) == Deny("no_grant")


def test_denies_grant_from_another_family():
    assert authorize(ask(grants=(grant(family_id="fam_2"),))) == Deny("no_grant")


def test_denies_when_actor_owner_or_target_left_the_family():
    assert authorize(ask(active_members=frozenset({"usr_w"}))) == Deny("actor_not_member")
    assert authorize(ask(active_members=frozenset({"usr_h"}))) == Deny("target_not_member")


def test_denies_disabled_agent_even_with_grants():
    assert authorize(ask(actor=AgentActor("agt_h", "usr_h", False))) == Deny("agent_disabled")


def test_prefers_target_specific_grant_over_family_wide():
    specific = grant(target_user_id="usr_w", requires_approval=True)
    d = authorize(ask(grants=(grant(), specific)))
    assert isinstance(d, Allow) and d.grant == specific and d.requires_approval


def test_on_a_tie_picks_the_grant_that_requires_approval():
    strict = grant(requires_approval=True)
    d = authorize(ask(grants=(grant(), strict)))
    assert isinstance(d, Allow) and d.grant == strict
