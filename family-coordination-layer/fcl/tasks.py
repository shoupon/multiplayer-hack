"""Coordination task service: persistence around the pure state machine in `machine.py`.

Every command loads the snapshot, applies one event, and writes the new snapshot, the transition,
the messages it produces, and any follow-up jobs in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, Union

from . import audit, machine
from .auth import AgentPrincipal, Principal
from .context import Ctx
from .db import Conn, Row
from .errors import AppError, DeniedAudit, conflict, forbidden, not_found, unprocessable
from .grants import decide, require_grant
from .ids import new_id
from .jobs import enqueue
from .messaging import Recipient, insert_message, new_conversation
from .policy import AgentActor, Deny
from .proposal import Proposal
from .timeutil import from_db, to_db

ResponseKind = Literal["accept", "decline", "counterpropose", "complete"]
# Responses an approval-gated grant parks for the human. Completing an agreed task commits to nothing new.
PARKABLE: frozenset[str] = frozenset({"accept", "decline", "counterpropose"})

ERROR_STATUS: dict[str, int] = {
    "task_terminal": 409,
    "task_expired": 409,
    "not_participant": 403,
    "not_initiator": 403,
    "stale_version": 409,
    "approval_pending": 409,
    "negotiation_limit": 422,
    "invalid_transition": 409,
}


def machine_error(err: machine.Err) -> AppError:
    return AppError(ERROR_STATUS[err.code], err.code, err.message)


@dataclass(frozen=True)
class Actor:
    kind: Literal["agent", "user", "system"]
    user_id: Optional[str] = None
    agent_id: Optional[str] = None

    @staticmethod
    def of(p: AgentPrincipal) -> "Actor":
        return Actor("agent", p.owner_user_id, p.agent_id)


SYSTEM = Actor("system")


# ---- persistence -----------------------------------------------------------------------


def load(c: Conn, task_id: str) -> tuple[Row, machine.Task]:
    row = c.one("SELECT * FROM coordination_tasks WHERE id = ?", (task_id,))
    if row is None:
        raise not_found("task")
    parts = c.all("SELECT * FROM task_participants WHERE task_id = ? ORDER BY role DESC, rowid", (task_id,))
    snapshot = machine.Task(
        state=row["state"],
        proposal=json.loads(row["proposal"]),
        proposal_version=row["proposal_version"],
        proposal_author_user_id=row["proposal_author_user_id"],
        negotiation_count=row["negotiation_count"],
        max_negotiations=row["max_negotiations"],
        expires_at=from_db(row["expires_at"]),
        outcome=json.loads(row["outcome"]) if row["outcome"] else None,
        participants=tuple(
            machine.Participant(
                user_id=p["user_id"], agent_id=p["agent_id"], role=p["role"], response=p["response"],
                responded_version=p["responded_version"], delivered=bool(p["delivered"]),
                approval_pending=bool(p["approval_pending"]),
            )
            for p in parts
        ),
    )
    return row, snapshot


def save(
    c: Conn,
    now: datetime,
    task_id: str,
    family_id: str,
    before: Optional[machine.Task],
    after: machine.Task,
    event: dict[str, Any],
    actor: Actor,
    idempotency_key: Optional[str] = None,
    req_hash: Optional[str] = None,
    message_id: Optional[str] = None,
) -> int:
    """Writes the snapshot and appends one transition row. Returns the transition id."""
    if before is not None:
        c.run(
            """UPDATE coordination_tasks SET state = ?, proposal = ?, proposal_version = ?, proposal_author_user_id = ?,
                      negotiation_count = ?, outcome = ?, row_version = row_version + 1, updated_at = ?
                WHERE id = ?""",
            (after.state, json.dumps(after.proposal), after.proposal_version, after.proposal_author_user_id,
             after.negotiation_count, json.dumps(after.outcome) if after.outcome else None, to_db(now), task_id),
        )
        old = {p.user_id: p for p in before.participants}
        for p in after.participants:
            if p == old[p.user_id]:
                continue
            responded = p.response != old[p.user_id].response or p.responded_version != old[p.user_id].responded_version
            c.run(
                """UPDATE task_participants SET response = ?, responded_version = ?, delivered = ?, approval_pending = ?,
                          responded_at = CASE WHEN ? THEN ? ELSE responded_at END
                    WHERE task_id = ? AND user_id = ?""",
                (p.response, p.responded_version, int(p.delivered), int(p.approval_pending), int(responded),
                 to_db(now), task_id, p.user_id),
            )
        if after.state in machine.TERMINAL or after.state == "AGREED":
            # Nothing left to approve: close parked responses so they cannot be applied later.
            c.run(
                "UPDATE approval_requests SET status = ? WHERE task_id = ? AND status = 'pending' AND kind = 'task_response'",
                ("expired" if after.state == "EXPIRED" else "superseded", task_id),
            )
    row = c.one(
        """INSERT INTO task_transitions (task_id, family_id, from_state, to_state, event, actor_kind, actor_user_id,
                                         actor_agent_id, idempotency_key, request_hash, message_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (task_id, family_id, before.state if before else None, after.state, json.dumps(event), actor.kind, actor.user_id,
         actor.agent_id, idempotency_key, req_hash, message_id, to_db(now)),
    )
    assert row is not None
    return int(row["id"])


def view(c: Conn, task_id: str, with_history: bool = False) -> dict[str, Any]:
    row, t = load(c, task_id)
    out: dict[str, Any] = {
        "id": row["id"],
        "family_id": row["family_id"],
        "conversation_id": row["conversation_id"],
        "intent": row["intent"],
        "state": t.state,
        "proposal": t.proposal,
        "proposal_version": t.proposal_version,
        "proposal_author_user_id": t.proposal_author_user_id,
        "negotiation_count": t.negotiation_count,
        "max_negotiations": t.max_negotiations,
        "expires_at": row["expires_at"],
        "outcome": t.outcome,
        "initiator": {"user_id": row["initiator_user_id"], "agent_id": row["initiator_agent_id"]},
        "participants": [
            {"user_id": p.user_id, "agent_id": p.agent_id, "role": p.role, "response": p.response,
             "responded_version": p.responded_version, "delivered": p.delivered, "approval_pending": p.approval_pending}
            for p in t.participants
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_history:
        out["history"] = [
            {"id": h["id"], "from_state": h["from_state"], "to_state": h["to_state"], "event": json.loads(h["event"]),
             "actor": {"kind": h["actor_kind"], "user_id": h["actor_user_id"], "agent_id": h["actor_agent_id"]},
             "message_id": h["message_id"], "created_at": h["created_at"]}
            for h in c.all("SELECT * FROM task_transitions WHERE task_id = ? ORDER BY id", (task_id,))
        ]
    return out


def _others(t: machine.Task, user_id: Optional[str]) -> list[Recipient]:
    return [Recipient(p.user_id, p.agent_id) for p in t.participants if p.user_id != user_id]


def _everyone(t: machine.Task) -> list[Recipient]:
    return _others(t, None)


def _notify_state(c: Conn, now: datetime, row: Row, t: machine.Task, note: Optional[str] = None,
                  recipients: Optional[list[Recipient]] = None) -> None:
    """System message announcing a state everyone should know about (AGREED, EXPIRED, reminders)."""
    payload: dict[str, Any] = {"state": t.state, "proposal_version": t.proposal_version}
    if note:
        payload["note"] = note
    insert_message(c, now, family_id=row["family_id"], conversation_id=row["conversation_id"], task_id=row["id"],
                   message_type="task.status", payload=payload, recipients=recipients or _everyone(t),
                   correlation_id=row["id"])


def _schedule_reminder(c: Conn, now: datetime, task_id: str, version: int, expires_at: datetime) -> None:
    halfway = now + (expires_at - now) / 2
    enqueue(c, now, "task.reminder", {"task_id": task_id, "proposal_version": version}, halfway,
            dedupe_key=f"reminder:{task_id}:{version}")


# ---- commands --------------------------------------------------------------------------


def create(
    ctx: Ctx,
    p: AgentPrincipal,
    *,
    idempotency_key: str,
    req_hash: str,
    family_id: str,
    invitee_agent_ids: list[str],
    intent: str,
    proposal: Proposal,
    expires_at: datetime,
) -> tuple[bool, dict[str, Any]]:
    """Returns (created, task). A retry with the same Idempotency-Key returns the original task."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        existing = c.one("SELECT id, request_hash FROM coordination_tasks WHERE initiator_agent_id = ? AND idempotency_key = ?",
                         (p.agent_id, idempotency_key))
        if existing:
            if existing["request_hash"] != req_hash:
                raise unprocessable("idempotency_key_reused", "this Idempotency-Key was used for a different request")
            return False, view(c, existing["id"])

        if expires_at <= now:
            raise unprocessable("invalid_expiry", "expires_at must be in the future")
        if expires_at > now + timedelta(seconds=ctx.settings.max_task_ttl_seconds):
            raise unprocessable("invalid_expiry", f"expires_at must be within {ctx.settings.max_task_ttl_seconds // 3600} hours")
        if len(set(invitee_agent_ids)) != len(invitee_agent_ids):
            raise unprocessable("duplicate_invitee", "each invitee agent may appear once")

        invitees: list[tuple[str, str]] = []
        grant_id = None
        for agent_id in invitee_agent_ids:
            target = c.one("SELECT id, owner_user_id, status FROM agents WHERE id = ?", (agent_id,))
            if target is None or target["status"] != "active":
                raise not_found("invitee agent")
            if target["owner_user_id"] == p.owner_user_id or target["owner_user_id"] in {u for u, _ in invitees}:
                raise unprocessable("duplicate_participant", "each participant must be a different person")
            grant_id = require_grant(c, now, p, family_id, "task:propose", target["owner_user_id"]).grant.id
            consent = decide(c, now, AgentActor(target["id"], target["owner_user_id"], True), family_id,
                             "agent:message:receive", p.owner_user_id)
            if isinstance(consent, Deny):
                raise forbidden("recipient_not_accepting", "an invitee agent does not accept requests from you",
                                DeniedAudit("task:propose", family_id, "agent", agent_id))
            invitees.append((target["owner_user_id"], target["id"]))

        task_id = new_id("task")
        conversation_id = new_conversation(c, now, family_id, intent, p.agent_id)
        snapshot = machine.new_task(initiator=(p.owner_user_id, p.agent_id), invitees=invitees,
                                    proposal=proposal.as_json(), max_negotiations=ctx.settings.max_negotiations,
                                    expires_at=expires_at)
        c.run(
            """INSERT INTO coordination_tasks (id, family_id, conversation_id, intent, state, proposal, proposal_version,
                       proposal_author_user_id, negotiation_count, max_negotiations, initiator_user_id, initiator_agent_id,
                       idempotency_key, request_hash, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, family_id, conversation_id, intent, snapshot.state, json.dumps(snapshot.proposal), p.owner_user_id,
             snapshot.max_negotiations, p.owner_user_id, p.agent_id, idempotency_key, req_hash, to_db(expires_at),
             to_db(now), to_db(now)),
        )
        for part in snapshot.participants:
            c.run(
                """INSERT INTO task_participants (task_id, family_id, user_id, agent_id, role, response, responded_version,
                                                  responded_at, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, family_id, part.user_id, part.agent_id, part.role, part.response, part.responded_version,
                 to_db(now) if part.role == "initiator" else None, int(part.delivered)),
            )
        actor = Actor.of(p)
        save(c, now, task_id, family_id, None, snapshot,
             {"kind": "create", "intent": intent, "proposal": snapshot.proposal, "invitees": invitee_agent_ids},
             actor, idempotency_key, req_hash)

        enqueued = machine.apply(snapshot, machine.Enqueue(), now)
        assert isinstance(enqueued, machine.Ok)
        message_id = insert_message(
            c, now, family_id=family_id, conversation_id=conversation_id, task_id=task_id,
            message_type="coordination.proposal",
            payload={"intent": intent, "proposal_version": 1, "proposal": snapshot.proposal,
                     "expires_at": expires_at.isoformat()},
            recipients=_others(snapshot, p.owner_user_id), sender=p, grant_id=grant_id, correlation_id=task_id,
            requires_response=True, expires_at=expires_at,
        )
        save(c, now, task_id, family_id, snapshot, enqueued.task, machine.event_json(machine.Enqueue()), SYSTEM,
             message_id=message_id)
        enqueue(c, now, "task.expire", {"task_id": task_id}, expires_at, dedupe_key=f"expire:{task_id}")
        _schedule_reminder(c, now, task_id, 1, expires_at)
        audit.record(c, now, p, "task.create", "succeeded", family_id=family_id, resource_type="task", resource_id=task_id,
                     metadata={"intent": intent, "invitees": len(invitees)})
        return True, view(c, task_id)


def _participant_or_404(t: machine.Task, p: AgentPrincipal) -> machine.Participant:
    part = t.participant(p.owner_user_id)
    if part is None or part.agent_id != p.agent_id:
        raise not_found("task")
    return part


def _event_for(kind: ResponseKind, user_id: str, version: int, proposal: Optional[dict[str, Any]],
               reason: Optional[str], via_approval: bool = False,
               evidence: Optional[dict[str, Any]] = None) -> machine.Event:
    if kind == "accept":
        return machine.Accept(user_id, version, via_approval)
    if kind == "decline":
        return machine.Decline(user_id, version, reason, via_approval)
    if kind == "counterpropose":
        assert proposal is not None
        return machine.Counterpropose(user_id, version, proposal, via_approval)
    assert evidence is not None
    return machine.Complete(user_id, evidence)


def _announce(c: Conn, now: datetime, row: Row, before: machine.Task, after: machine.Task, event: machine.Event,
              sender: AgentPrincipal, grant_id: Optional[str]) -> Optional[str]:
    """Sends the coordination message a response produces. Returns its id."""

    def send(message_type: str, payload: dict[str, Any], requires_response: bool = False) -> str:
        return insert_message(c, now, family_id=row["family_id"], conversation_id=row["conversation_id"], task_id=row["id"],
                              message_type=message_type, payload=payload, recipients=_others(after, sender.owner_user_id),
                              sender=sender, grant_id=grant_id, correlation_id=row["id"],
                              requires_response=requires_response, expires_at=after.expires_at if requires_response else None)

    message_id: Optional[str] = None
    if isinstance(event, machine.Counterpropose):
        message_id = send("coordination.counterproposal",
                          {"intent": row["intent"], "proposal_version": after.proposal_version, "proposal": after.proposal,
                           "expires_at": after.expires_at.isoformat()}, requires_response=True)
    elif isinstance(event, machine.Accept):
        message_id = send("coordination.accept", {"proposal_version": event.proposal_version})
    elif isinstance(event, machine.Decline):
        message_id = send("coordination.decline", {"proposal_version": event.proposal_version, "reason": event.reason})
    if after.state == "AGREED" and before.state != "AGREED":
        _notify_state(c, now, row, after, note="all participants accepted")
    if after.state == "COMPLETED":
        insert_message(c, now, family_id=row["family_id"], conversation_id=row["conversation_id"], task_id=row["id"],
                       message_type="task.completed", payload={"outcome": after.outcome or {}},
                       recipients=_everyone(after), correlation_id=row["id"])
    return message_id


@dataclass(frozen=True)
class Failure:
    """A command error that must be raised only after its transaction commits (e.g. lazy expiry)."""

    error: AppError


def _expire_now(c: Conn, now: datetime, row: Row, t: machine.Task) -> machine.Task:
    r = machine.apply(t, machine.Expire(), now)
    assert isinstance(r, machine.Ok)
    save(c, now, row["id"], row["family_id"], t, r.task, machine.event_json(machine.Expire()), SYSTEM)
    _notify_state(c, now, row, r.task, note="the proposal expired")
    return r.task


def respond(
    ctx: Ctx,
    p: AgentPrincipal,
    task_id: str,
    *,
    idempotency_key: str,
    req_hash: str,
    kind: ResponseKind,
    proposal_version: int,
    proposal: Optional[Proposal],
    reason: Optional[str],
    evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = ctx.clock()
    outcome: Union[dict[str, Any], Failure]
    with ctx.db.tx() as c:
        row, t = load(c, task_id)
        part = _participant_or_404(t, p)
        prior = c.one("SELECT event, request_hash FROM task_transitions WHERE task_id = ? AND actor_agent_id = ? AND idempotency_key = ?",
                      (task_id, p.agent_id, idempotency_key))
        if prior:
            if prior["request_hash"] != req_hash:
                raise unprocessable("idempotency_key_reused", "this Idempotency-Key was used for a different request")
            stored = json.loads(prior["event"])
            return {"result": stored.get("result", "applied"), "approval_id": stored.get("approval_id"),
                    "task": view(c, task_id)}

        if t.state not in machine.TERMINAL and now >= t.expires_at:
            _expire_now(c, now, row, t)
            outcome = Failure(conflict("task_expired", "task expired"))
        else:
            target = t.proposal_author_user_id if t.proposal_author_user_id != part.user_id else row["initiator_user_id"]
            allowed = require_grant(c, now, p, row["family_id"], "task:respond", target)
            proposal_json = proposal.as_json() if proposal else None
            if allowed.requires_approval and kind in PARKABLE:
                outcome = _park(c, now, row, t, p, kind, proposal_version, proposal_json, reason, allowed.grant.id,
                                idempotency_key, req_hash)
            else:
                event = _event_for(kind, part.user_id, proposal_version, proposal_json, reason, evidence=evidence)
                r = machine.apply(t, event, now)
                if isinstance(r, machine.Err):
                    raise machine_error(r)
                result = "applied" if r.changed else "noop"
                message_id = _announce(c, now, row, t, r.task, event, p, allowed.grant.id) if r.changed else None
                save(c, now, task_id, row["family_id"], t, r.task, {**machine.event_json(event), "result": result},
                     Actor.of(p), idempotency_key, req_hash, message_id)
                if isinstance(event, machine.Counterpropose):
                    _schedule_reminder(c, now, task_id, r.task.proposal_version, t.expires_at)
                audit.record(c, now, p, f"task.{kind}", "succeeded", family_id=row["family_id"], resource_type="task",
                             resource_id=task_id, metadata={"proposal_version": proposal_version, "result": result})
                outcome = {"result": result, "approval_id": None, "task": view(c, task_id)}
    if isinstance(outcome, Failure):
        raise outcome.error
    return outcome


def _park(c: Conn, now: datetime, row: Row, t: machine.Task, p: AgentPrincipal, kind: str, version: int,
          proposal: Optional[dict[str, Any]], reason: Optional[str], grant_id: str,
          idempotency_key: str, req_hash: str) -> dict[str, Any]:
    """Holds the exact response for the agent's human to approve. Nothing is sent to other participants yet."""
    event = machine.RequestApproval(p.owner_user_id, version)
    r = machine.apply(t, event, now)
    if isinstance(r, machine.Err):
        raise machine_error(r)
    action = {"task_id": row["id"], "kind": kind, "proposal_version": version, "proposal": proposal, "reason": reason,
              "grant_id": grant_id, "credential_id": p.credential_id}
    action_hash = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
    approval_id = new_id("approval")
    c.run(
        """INSERT INTO approval_requests (id, family_id, task_id, approver_user_id, requesting_agent_id, action,
                                          action_hash, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (approval_id, row["family_id"], row["id"], p.owner_user_id, p.agent_id, json.dumps(action), action_hash,
         row["expires_at"], to_db(now)),
    )
    save(c, now, row["id"], row["family_id"], t, r.task,
         {**machine.event_json(event), "response_kind": kind, "result": "pending_approval", "approval_id": approval_id},
         Actor.of(p), idempotency_key, req_hash)
    audit.record(c, now, p, "approval.request", "succeeded", family_id=row["family_id"], resource_type="approval",
                 resource_id=approval_id, metadata={"task_id": row["id"], "response_kind": kind})
    return {"result": "pending_approval", "approval_id": approval_id, "task": view(c, row["id"])}


def apply_approved(c: Conn, now: datetime, approval: Row) -> Optional[machine.Err]:
    """Applies a parked response exactly as approved. Returns the machine error if the task moved on."""
    action = json.loads(approval["action"])
    row, t = load(c, approval["task_id"])
    agent = c.one("SELECT owner_user_id FROM agents WHERE id = ?", (approval["requesting_agent_id"],))
    assert agent is not None
    sender = AgentPrincipal(approval["requesting_agent_id"], agent["owner_user_id"], True, action["credential_id"])
    event = _event_for(action["kind"], agent["owner_user_id"], action["proposal_version"], action["proposal"],
                       action["reason"], via_approval=True)
    r = machine.apply(t, event, now)
    if isinstance(r, machine.Err):
        return r
    message_id = _announce(c, now, row, t, r.task, event, sender, action["grant_id"])
    save(c, now, row["id"], row["family_id"], t, r.task,
         {**machine.event_json(event), "result": "applied", "approval_id": approval["id"]},
         Actor("user", approval["approver_user_id"]), message_id=message_id)
    if isinstance(event, machine.Counterpropose):
        _schedule_reminder(c, now, row["id"], r.task.proposal_version, t.expires_at)
    return None


def release_parked(c: Conn, now: datetime, approval: Row) -> None:
    """A denied approval puts the participant back to responding."""
    row, t = load(c, approval["task_id"])
    agent = c.one("SELECT owner_user_id FROM agents WHERE id = ?", (approval["requesting_agent_id"],))
    assert agent is not None
    event = machine.ApprovalDenied(agent["owner_user_id"])
    r = machine.apply(t, event, now)
    if isinstance(r, machine.Ok):
        save(c, now, row["id"], row["family_id"], t, r.task, {**machine.event_json(event), "approval_id": approval["id"]},
             Actor("user", approval["approver_user_id"]))


def cancel(ctx: Ctx, p: AgentPrincipal, task_id: str, reason: Optional[str]) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        row, t = load(c, task_id)
        part = _participant_or_404(t, p)
        invitee = next(q.user_id for q in t.participants if q.role == "invitee")
        allowed = require_grant(c, now, p, row["family_id"], "task:propose", invitee)
        event = machine.Cancel(part.user_id, reason)
        r = machine.apply(t, event, now)
        if isinstance(r, machine.Err):
            raise machine_error(r)
        payload = {"reason": reason} if reason else {}
        message_id = insert_message(c, now, family_id=row["family_id"], conversation_id=row["conversation_id"],
                                    task_id=task_id, message_type="coordination.cancel", payload=payload,
                                    recipients=_others(t, part.user_id), sender=p, grant_id=allowed.grant.id,
                                    correlation_id=task_id)
        save(c, now, task_id, row["family_id"], t, r.task, machine.event_json(event), Actor.of(p), message_id=message_id)
        audit.record(c, now, p, "task.cancel", "succeeded", family_id=row["family_id"], resource_type="task", resource_id=task_id)
        return view(c, task_id)


def mark_delivered(c: Conn, now: datetime, task_id: str, user_id: str) -> None:
    """Delivery receipt from the inbox or a webhook. Never changes business acceptance."""
    row, t = load(c, task_id)
    event = machine.Delivered(user_id)
    r = machine.apply(t, event, now)
    if isinstance(r, machine.Ok) and r.changed:
        save(c, now, task_id, row["family_id"], t, r.task, machine.event_json(event), SYSTEM)


# ---- queries ---------------------------------------------------------------------------


def _visible(c: Conn, p: Principal, task_id: str) -> bool:
    if isinstance(p, AgentPrincipal):
        q = "SELECT 1 FROM task_participants WHERE task_id = ? AND agent_id = ?"
        return c.one(q, (task_id, p.agent_id)) is not None
    return c.one("SELECT 1 FROM task_participants WHERE task_id = ? AND user_id = ?", (task_id, p.user_id)) is not None


def get(ctx: Ctx, p: Principal, task_id: str) -> dict[str, Any]:
    with ctx.db.read() as c:
        if not _visible(c, p, task_id):
            raise not_found("task")
        return view(c, task_id, with_history=True)


def list_for(ctx: Ctx, p: Principal, state: Optional[str], limit: int) -> list[dict[str, Any]]:
    column, value = ("agent_id", p.agent_id) if isinstance(p, AgentPrincipal) else ("user_id", p.user_id)
    with ctx.db.read() as c:
        rows = c.all(
            f"""SELECT t.id FROM coordination_tasks t JOIN task_participants tp ON tp.task_id = t.id
                 WHERE tp.{column} = ? AND (? IS NULL OR t.state = ?) ORDER BY t.updated_at DESC LIMIT ?""",
            (value, state, state, limit),
        )
        return [view(c, r["id"]) for r in rows]


# ---- jobs ------------------------------------------------------------------------------


def expire_job(ctx: Ctx, payload: dict[str, Any]) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        row, t = load(c, payload["task_id"])
        if t.state in machine.TERMINAL:
            return
        if now < t.expires_at:
            enqueue(c, now, "task.expire", payload, t.expires_at)  # fired early (clock skew): try again on time
            return
        _expire_now(c, now, row, t)


def reminder_job(ctx: Ctx, payload: dict[str, Any]) -> None:
    """Nudges participants who have not answered the current version. Stale reminders do nothing."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        row, t = load(c, payload["task_id"])
        if t.state in machine.TERMINAL or t.state == "AGREED" or t.proposal_version != payload["proposal_version"]:
            return
        waiting = [Recipient(q.user_id, q.agent_id) for q in t.participants if q.response == "pending" and not q.approval_pending]
        if waiting:
            _notify_state(c, now, row, t, note=f"awaiting your response before {row['expires_at']}", recipients=waiting)

