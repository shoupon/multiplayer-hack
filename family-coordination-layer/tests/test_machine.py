from datetime import datetime, timedelta, timezone
from functools import reduce

import pytest

from fcl.machine import (
    Accept,
    ApprovalDenied,
    Cancel,
    Complete,
    Counterpropose,
    Decline,
    Delivered,
    Enqueue,
    Err,
    Expire,
    Ok,
    RequestApproval,
    Task,
    apply,
    new_task,
)

T0 = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
EXPIRES = T0 + timedelta(hours=14)
SEVEN = {"proposed_time": "2026-11-13T19:00:00+08:00", "duration_minutes": 90}
SEVEN_THIRTY = {"proposed_time": "2026-11-13T19:30:00+08:00", "duration_minutes": 90}


def fresh(max_negotiations: int = 5, invitees=(("usr_w", "agt_w"),)) -> Task:
    return new_task(
        initiator=("usr_h", "agt_h"),
        invitees=list(invitees),
        proposal=SEVEN,
        max_negotiations=max_negotiations,
        expires_at=EXPIRES,
    )


def run(task: Task, *events) -> Task:
    def step(acc: Task, e) -> Task:
        r = apply(acc, e, T0)
        if isinstance(r, Err):
            pytest.fail(f"{e.kind}: {r.code}")
        return r.task

    return reduce(step, events, task)


def code(task: Task, event, now=T0) -> str:
    r = apply(task, event, now)
    assert isinstance(r, Err)
    return r.code


def test_dinner_flow_deliver_counterpropose_accept_agree_complete():
    task = run(fresh(), Enqueue())
    assert task.state == "AWAITING_DELIVERY"
    task = run(task, Delivered("usr_w"))
    assert task.state == "AWAITING_RESPONSE"
    task = run(task, Counterpropose("usr_w", 1, SEVEN_THIRTY))
    assert (task.state, task.proposal_version, task.proposal_author_user_id, task.negotiation_count) == (
        "COUNTERPROPOSED", 2, "usr_w", 1,
    )
    task = run(task, Accept("usr_h", 2))
    assert task.state == "AGREED"
    assert task.outcome is not None and task.outcome["proposal"] == SEVEN_THIRTY
    assert run(task, Complete("usr_w")).state == "COMPLETED"


def test_does_not_agree_until_every_participant_accepts():
    task = run(fresh(invitees=(("usr_w", "agt_w"), ("usr_g", "agt_g"))), Enqueue(), Accept("usr_w", 1))
    assert task.state == "AWAITING_DELIVERY"
    assert task.participant("usr_w").response == "accepted"


def test_accepts_a_response_that_arrives_before_the_delivery_receipt():
    assert run(fresh(), Enqueue(), Accept("usr_w", 1)).state == "AGREED"


def test_rejects_response_to_superseded_version():
    task = run(fresh(), Enqueue(), Counterpropose("usr_w", 1, SEVEN_THIRTY))
    assert code(task, Accept("usr_w", 1)) == "stale_version"


def test_repeated_accept_of_same_version_is_a_noop():
    task = run(fresh(), Enqueue())
    assert apply(task, Accept("usr_h", 1), T0) == Ok(task, changed=False)


def test_expiry_rejects_responses_and_fires_only_after_deadline():
    task = run(fresh(), Enqueue())
    assert code(task, Accept("usr_w", 1), EXPIRES) == "task_expired"
    assert code(task, Expire(), T0) == "invalid_transition"
    r = apply(task, Expire(), EXPIRES)
    assert isinstance(r, Ok) and r.task.state == "EXPIRED"


def test_terminal_task_rejects_everything():
    task = run(fresh(), Enqueue(), Decline("usr_w", 1))
    assert task.state == "DECLINED"
    assert code(task, Accept("usr_w", 1)) == "task_terminal"


def test_rejects_non_participants_and_only_initiator_cancels():
    task = run(fresh(), Enqueue())
    assert code(task, Accept("usr_x", 1)) == "not_participant"
    assert code(task, Cancel("usr_w")) == "not_initiator"
    assert run(task, Cancel("usr_h")).state == "CANCELLED"


def test_stops_negotiation_at_the_cap():
    task = run(fresh(max_negotiations=1), Enqueue(), Counterpropose("usr_w", 1, SEVEN_THIRTY))
    assert code(task, Counterpropose("usr_h", 2, SEVEN)) == "negotiation_limit"


def test_parks_response_behind_approval_and_applies_it_only_via_approval():
    task = run(fresh(), Enqueue(), RequestApproval("usr_w", 1))
    assert task.state == "AWAITING_APPROVAL"
    assert code(task, Accept("usr_w", 1)) == "approval_pending"
    task = run(task, Counterpropose("usr_w", 1, SEVEN_THIRTY, via_approval=True))
    assert (task.state, task.proposal_version) == ("COUNTERPROPOSED", 2)


def test_denied_approval_returns_to_the_open_state():
    assert run(fresh(), Enqueue(), RequestApproval("usr_w", 1), ApprovalDenied("usr_w")).state == "AWAITING_RESPONSE"


def test_cannot_complete_a_task_that_is_not_agreed():
    assert code(run(fresh(), Enqueue(), Delivered("usr_w")), Complete("usr_h")) == "invalid_transition"
