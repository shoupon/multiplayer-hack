"""Human approval of parked agent responses. Only the named human, with a user credential, decides."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from . import audit, tasks
from .auth import UserPrincipal
from .context import Ctx
from .db import Conn, Row
from .errors import DeniedAudit, conflict, forbidden, not_found
from .families import active_members
from .messaging import Recipient, insert_message
from .timeutil import from_db, to_db

Decision = Literal["grant", "deny"]


def _view(c: Conn, a: Row) -> dict[str, Any]:
    task = c.one("SELECT intent, state, proposal, proposal_version FROM coordination_tasks WHERE id = ?", (a["task_id"],))
    action = json.loads(a["action"])
    return {
        "id": a["id"],
        "family_id": a["family_id"],
        "task_id": a["task_id"],
        "status": a["status"],
        "requesting_agent_id": a["requesting_agent_id"],
        "action": {k: action[k] for k in ("kind", "proposal_version", "proposal", "reason")},
        "action_hash": a["action_hash"],
        "task": {"intent": task["intent"], "state": task["state"], "current_proposal": json.loads(task["proposal"]),
                 "current_version": task["proposal_version"]} if task else None,
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
            status = "granted" if decision == "grant" else "denied"
            if decision == "grant":
                failed = tasks.apply_approved(c, now, a)
                if failed is not None:
                    # The task moved on (newer version, terminal state). Approving now would act on stale facts.
                    status = "superseded"
                    error = conflict("approval_superseded", f"the task changed since this was requested: {failed.message}")
            else:
                tasks.release_parked(c, now, a)
            c.run(
                """UPDATE approval_requests SET status = ?, decided_at = ?, decided_by_user_id = ?, decided_credential_id = ?
                    WHERE id = ?""",
                (status, to_db(now), p.user_id, p.credential_id, approval_id),
            )
            if status in ("granted", "denied"):
                task = c.one("SELECT conversation_id FROM coordination_tasks WHERE id = ?", (a["task_id"],))
                insert_message(c, now, family_id=a["family_id"], conversation_id=task["conversation_id"], task_id=a["task_id"],
                               message_type=f"approval.{status}", payload={"approval_id": approval_id},
                               recipients=[Recipient(p.user_id, a["requesting_agent_id"])], correlation_id=a["task_id"])
            audit.record(c, now, p, f"approval.{decision}", "succeeded" if error is None else "failed",
                         family_id=a["family_id"], resource_type="approval", resource_id=approval_id,
                         reason=error.code if error else None, metadata={"task_id": a["task_id"], "status": status})
        result = _view(c, c.one("SELECT * FROM approval_requests WHERE id = ?", (approval_id,)))
    if error is not None:
        raise error
    return result

