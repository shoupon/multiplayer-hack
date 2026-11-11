"""Human decisions on what agents want to do. Only the named human, with a user credential, decides.

Two kinds share one queue, so an owner sees everything their agents are waiting on in one place:
- task_response: an agent's answer to a task, parked because its grant requires approval.
- permission: an agent asking to act outside FCL on its owner's behalf (see permissions.py).
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from . import audit, tasks
from .auth import AgentPrincipal, Principal, UserPrincipal
from .context import Ctx
from .db import Conn, Row
from .errors import AppError, DeniedAudit, conflict, forbidden, not_found
from .families import active_members
from .messaging import Recipient, insert_message
from .timeutil import from_db, to_db

Decision = Literal["grant", "deny"]


def _task_summary(c: Conn, task_id: Optional[str]) -> Optional[dict[str, Any]]:
    task = c.one("SELECT intent, state, proposal, proposal_version FROM coordination_tasks WHERE id = ?", (task_id,))
    if task is None:
        return None
    return {"intent": task["intent"], "state": task["state"], "current_proposal": json.loads(task["proposal"]),
            "current_version": task["proposal_version"]}


def _view(c: Conn, a: Row) -> dict[str, Any]:
    action = json.loads(a["action"])
    shown = ("scope", "params", "purpose") if a["kind"] == "permission" else ("kind", "proposal_version", "proposal", "reason")
    return {
        "id": a["id"],
        "kind": a["kind"],
        "family_id": a["family_id"],
        "task_id": a["task_id"],
        "status": a["status"],
        "requesting_agent_id": a["requesting_agent_id"],
        "approver_user_id": a["approver_user_id"],
        "action": {k: action.get(k) for k in shown},
        "action_hash": a["action_hash"],
        "task": _task_summary(c, a["task_id"]),
        "expires_at": a["expires_at"],
        "decided_at": a["decided_at"],
        "created_at": a["created_at"],
    }


def list_for(ctx: Ctx, p: UserPrincipal, status: Optional[str]) -> list[dict[str, Any]]:
    with ctx.db.read() as c:
        rows = c.all(
            """SELECT * FROM approval_requests WHERE approver_user_id = ? AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC LIMIT 200""",
            (p.user_id, status, status),
        )
        return [_view(c, a) for a in rows]


def get(ctx: Ctx, p: Principal, approval_id: str) -> dict[str, Any]:
    """The approver and the requesting agent can read a request; everyone else gets 404."""
    with ctx.db.read() as c:
        a = c.one("SELECT * FROM approval_requests WHERE id = ?", (approval_id,))
        mine = a is not None and (
            a["requesting_agent_id"] == p.agent_id if isinstance(p, AgentPrincipal) else a["approver_user_id"] == p.user_id)
        if not mine:
            raise not_found("approval")
        return _view(c, a)


def _decide_task_response(c: Conn, now: Any, a: Row, decision: Decision) -> tuple[str, Optional[AppError]]:
    if decision == "deny":
        tasks.release_parked(c, now, a)
        return "denied", None
    failed = tasks.apply_approved(c, now, a)
    if failed is not None:
        # The task moved on (newer version, terminal state). Approving now would act on stale facts.
        return "superseded", conflict("approval_superseded", f"the task changed since this was requested: {failed.message}")
    return "granted", None


def decide(ctx: Ctx, p: UserPrincipal, approval_id: str, decision: Decision) -> dict[str, Any]:
    now = ctx.clock()
    error = None
    with ctx.db.tx() as c:
        a = c.one("SELECT * FROM approval_requests WHERE id = ?", (approval_id,))
        if a is None:
            raise not_found("approval")
        if a["approver_user_id"] != p.user_id:
            if p.user_id in active_members(c, a["family_id"]):
                raise forbidden("not_approver", "only the named approver can decide this request",
                                DeniedAudit("approval.decide", a["family_id"], "approval", approval_id))
            raise not_found("approval")
        if a["status"] != "pending":
            raise conflict("approval_not_pending", f"approval is {a['status']}")
        if now >= from_db(a["expires_at"]):
            c.run("UPDATE approval_requests SET status = 'expired' WHERE id = ?", (approval_id,))
            error = conflict("approval_expired", "approval expired")
        else:
            if a["kind"] == "task_response":
                status, error = _decide_task_response(c, now, a, decision)
                conversation_id = c.one("SELECT conversation_id FROM coordination_tasks WHERE id = ?",
                                        (a["task_id"],))["conversation_id"]
            else:
                status = "granted" if decision == "grant" else "denied"
                conversation_id = a["conversation_id"]
            c.run(
                """UPDATE approval_requests SET status = ?, decided_at = ?, decided_by_user_id = ?, decided_credential_id = ?
                    WHERE id = ?""",
                (status, to_db(now), p.user_id, p.credential_id, approval_id),
            )
            if status in ("granted", "denied"):
                insert_message(c, now, family_id=a["family_id"], conversation_id=conversation_id, task_id=a["task_id"],
                               message_type=f"approval.{status}", payload={"approval_id": approval_id, "kind": a["kind"]},
                               recipients=[Recipient(p.user_id, a["requesting_agent_id"])],
                               correlation_id=a["task_id"] or approval_id)
            audit.record(c, now, p, f"approval.{decision}", "succeeded" if error is None else "failed",
                         family_id=a["family_id"], resource_type="approval", resource_id=approval_id,
                         reason=error.code if error else None,
                         metadata={"kind": a["kind"], "task_id": a["task_id"], "status": status})
        result = _view(c, c.one("SELECT * FROM approval_requests WHERE id = ?", (approval_id,)))
    if error is not None:
        raise error
    return result
