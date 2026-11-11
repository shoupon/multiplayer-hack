"""Pure coordination-task state machine. See docs/DESIGN.md section 10.

The caller loads a snapshot, applies one event, and persists the result in one transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, ClassVar, Literal, Optional, Union

TaskState = Literal[
    "CREATED",
    "AWAITING_DELIVERY",
    "AWAITING_RESPONSE",
    "COUNTERPROPOSED",
    "AWAITING_APPROVAL",
    "AGREED",
    "COMPLETED",
    "DECLINED",
    "EXPIRED",
    "CANCELLED",
    "FAILED",
]
TERMINAL: frozenset[str] = frozenset({"COMPLETED", "DECLINED", "EXPIRED", "CANCELLED", "FAILED"})
OPEN: frozenset[str] = frozenset({"AWAITING_DELIVERY", "AWAITING_RESPONSE", "COUNTERPROPOSED"})

Proposal = dict[str, Any]


@dataclass(frozen=True)
class Participant:
    user_id: str
    agent_id: str
    role: Literal["initiator", "invitee"]
    response: Literal["pending", "accepted", "declined"] = "pending"
    responded_version: Optional[int] = None
    delivered: bool = False
    approval_pending: bool = False


@dataclass(frozen=True)
class Task:
    state: TaskState
    proposal: Proposal
    proposal_version: int
    proposal_author_user_id: str
    negotiation_count: int
    max_negotiations: int
    expires_at: datetime
    participants: tuple[Participant, ...]
    outcome: Optional[dict[str, Any]] = None

    def participant(self, user_id: str) -> Optional[Participant]:
        return next((p for p in self.participants if p.user_id == user_id), None)


# Events. `via_approval` marks a parked response that its human approved.


@dataclass(frozen=True)
class Enqueue:
    kind: ClassVar[str] = "enqueue"


@dataclass(frozen=True)
class Delivered:
    user_id: str
    kind: ClassVar[str] = "delivered"


@dataclass(frozen=True)
class Accept:
    user_id: str
    proposal_version: int
    via_approval: bool = False
    kind: ClassVar[str] = "accept"


@dataclass(frozen=True)
class Decline:
    user_id: str
    proposal_version: int
    reason: Optional[str] = None
    via_approval: bool = False
    kind: ClassVar[str] = "decline"


@dataclass(frozen=True)
class Counterpropose:
    user_id: str
    proposal_version: int
    proposal: Proposal = field(default_factory=dict)
    via_approval: bool = False
    kind: ClassVar[str] = "counterpropose"


@dataclass(frozen=True)
class RequestApproval:
    user_id: str
    proposal_version: int
    kind: ClassVar[str] = "request_approval"


@dataclass(frozen=True)
class ApprovalDenied:
    user_id: str
    kind: ClassVar[str] = "approval_denied"


@dataclass(frozen=True)
class Complete:
    user_id: str
    kind: ClassVar[str] = "complete"


@dataclass(frozen=True)
class Cancel:
    user_id: str
    reason: Optional[str] = None
    kind: ClassVar[str] = "cancel"


@dataclass(frozen=True)
class Expire:
    kind: ClassVar[str] = "expire"


@dataclass(frozen=True)
class Fail:
    reason: str
    kind: ClassVar[str] = "fail"


Response = Union[Accept, Decline, Counterpropose]
Event = Union[Enqueue, Delivered, Accept, Decline, Counterpropose, RequestApproval, ApprovalDenied, Complete, Cancel, Expire, Fail]


def event_json(e: Event) -> dict[str, Any]:
    return {"kind": e.kind, **asdict(e)}


ErrorCode = Literal[
    "task_terminal",
    "task_expired",
    "not_participant",
    "not_initiator",
    "stale_version",
    "approval_pending",
    "negotiation_limit",
    "invalid_transition",
]


@dataclass(frozen=True)
class Ok:
    task: Task
    changed: bool = True


@dataclass(frozen=True)
class Err:
    code: ErrorCode
    message: str


Result = Union[Ok, Err]


def new_task(
    *,
    initiator: tuple[str, str],
    invitees: list[tuple[str, str]],
    proposal: Proposal,
    max_negotiations: int,
    expires_at: datetime,
) -> Task:
    """The initiator's proposal counts as their acceptance of version 1."""
    user_id, agent_id = initiator
    return Task(
        state="CREATED",
        proposal=proposal,
        proposal_version=1,
        proposal_author_user_id=user_id,
        negotiation_count=0,
        max_negotiations=max_negotiations,
        expires_at=expires_at,
        participants=(
            Participant(user_id, agent_id, "initiator", "accepted", 1, delivered=True),
            *(Participant(u, a, "invitee") for u, a in invitees),
        ),
    )


def _settle(task: Task) -> Task:
    """Open states are a projection of participant data, so they cannot drift from it."""
    if task.state in TERMINAL or task.state == "AGREED":
        return task
    if any(p.approval_pending for p in task.participants):
        state: TaskState = "AWAITING_APPROVAL"
    elif task.proposal_version > 1:
        state = "COUNTERPROPOSED"
    elif not all(p.delivered for p in task.participants):
        state = "AWAITING_DELIVERY"
    else:
        state = "AWAITING_RESPONSE"
    return replace(task, state=state)


def _with(task: Task, user_id: str, **changes: Any) -> Task:
    return replace(
        task,
        participants=tuple(replace(p, **changes) if p.user_id == user_id else p for p in task.participants),
    )


def _respond(task: Task, p: Participant, e: Union[Response, RequestApproval], now: datetime) -> Result:
    if e.proposal_version != task.proposal_version:
        return Err("stale_version", f"response is for version {e.proposal_version}, current is {task.proposal_version}")
    via_approval = getattr(e, "via_approval", False)
    if (
        isinstance(e, Accept)
        and p.response == "accepted"
        and p.responded_version == task.proposal_version
        and not p.approval_pending
    ):
        return Ok(task, changed=False)
    if p.approval_pending and not via_approval:
        return Err("approval_pending", "a response from this participant awaits approval")
    if via_approval and not p.approval_pending:
        return Err("invalid_transition", "no approval is pending for this participant")
    if task.state not in OPEN and task.state != "AWAITING_APPROVAL":
        return Err("invalid_transition", f"cannot {e.kind} a task in {task.state}")

    base = _with(task, p.user_id, delivered=True, approval_pending=False)
    if isinstance(e, RequestApproval):
        return Ok(_settle(_with(base, p.user_id, approval_pending=True)))
    if isinstance(e, Decline):
        return Ok(replace(_with(base, p.user_id, response="declined", responded_version=e.proposal_version), state="DECLINED"))
    if isinstance(e, Accept):
        nxt = _with(base, p.user_id, response="accepted", responded_version=e.proposal_version)
        if all(q.response == "accepted" and q.responded_version == nxt.proposal_version for q in nxt.participants):
            outcome = {"proposal": nxt.proposal, "proposal_version": nxt.proposal_version, "agreed_at": now.isoformat()}
            return Ok(replace(nxt, state="AGREED", outcome=outcome))
        return Ok(_settle(nxt))
    # Counterpropose
    if task.negotiation_count >= task.max_negotiations:
        return Err("negotiation_limit", f"negotiation limit of {task.max_negotiations} counterproposals reached")
    version = task.proposal_version + 1
    return Ok(
        _settle(
            replace(
                base,
                proposal=e.proposal,
                proposal_version=version,
                proposal_author_user_id=p.user_id,
                negotiation_count=task.negotiation_count + 1,
                participants=tuple(
                    replace(q, response="accepted", responded_version=version)
                    if q.user_id == p.user_id
                    else replace(q, response="pending", responded_version=None)
                    for q in base.participants
                ),
            )
        )
    )


def apply(task: Task, event: Event, now: datetime) -> Result:
    if task.state in TERMINAL:
        return Err("task_terminal", f"task is {task.state}")
    if isinstance(event, Fail):
        return Ok(replace(task, state="FAILED"))
    if isinstance(event, Expire):
        if now >= task.expires_at:
            return Ok(replace(task, state="EXPIRED"))
        return Err("invalid_transition", "task has not expired")
    if now >= task.expires_at:
        return Err("task_expired", "task expired")
    if isinstance(event, Enqueue):
        if task.state != "CREATED":
            return Err("invalid_transition", "task already enqueued")
        return Ok(_settle(replace(task, state="AWAITING_DELIVERY")))

    p = task.participant(event.user_id)
    if p is None:
        return Err("not_participant", "actor is not a participant")
    if isinstance(event, Delivered):
        if p.delivered:
            return Ok(task, changed=False)
        return Ok(_settle(_with(task, p.user_id, delivered=True)))
    if isinstance(event, Cancel):
        if p.role != "initiator":
            return Err("not_initiator", "only the initiator can cancel")
        return Ok(replace(task, state="CANCELLED"))
    if isinstance(event, Complete):
        if task.state != "AGREED":
            return Err("invalid_transition", f"cannot complete a task in {task.state}")
        return Ok(replace(task, state="COMPLETED"))
    if isinstance(event, ApprovalDenied):
        if not p.approval_pending:
            return Err("invalid_transition", "no approval is pending for this participant")
        return Ok(_settle(_with(task, p.user_id, approval_pending=False)))
    return _respond(task, p, event, now)
