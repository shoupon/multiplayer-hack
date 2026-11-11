"""Delegation grants: storage, and the bridge from database facts to the pure policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import audit
from .auth import AgentPrincipal, UserPrincipal
from .context import Ctx
from .db import Conn, Row
from .errors import DeniedAudit, forbidden, not_found, unprocessable
from .families import active_members, require_member
from .ids import new_id
from .policy import APPROVABLE_ACTIONS, Action, AgentActor, Allow, Decision, Deny, Grant, Question, authorize
from .timeutil import from_db, to_db


def _grant(r: Row) -> Grant:
    return Grant(
        id=r["id"],
        agent_id=r["agent_id"],
        family_id=r["family_id"],
        action=r["action"],
        target_user_id=r["target_user_id"],
        requires_approval=bool(r["requires_approval"]),
        expires_at=from_db(r["expires_at"]) if r["expires_at"] else None,
        revoked_at=from_db(r["revoked_at"]) if r["revoked_at"] else None,
    )


def actor_of(p: AgentPrincipal) -> AgentActor:
    return AgentActor(p.agent_id, p.owner_user_id, p.agent_active)


def decide(c: Conn, now: datetime, actor: AgentActor, family_id: str, action: Action, target_user_id: Optional[str]) -> Decision:
    """Loads the facts the pure policy needs, then asks it."""
    rows = c.all(
        "SELECT * FROM delegation_grants WHERE agent_id = ? AND family_id = ? AND action = ? AND revoked_at IS NULL",
        (actor.agent_id, family_id, action),
    )
    return authorize(
        Question(actor, family_id, action, target_user_id, active_members(c, family_id), tuple(map(_grant, rows)), now)
    )


def require_grant(c: Conn, now: datetime, p: AgentPrincipal, family_id: str, action: Action, target_user_id: Optional[str]) -> Allow:
    """Raises 403 (recorded in the audit log) unless the agent may perform the action."""
    d = decide(c, now, actor_of(p), family_id, action, target_user_id)
    if isinstance(d, Deny):
        raise forbidden(d.reason, f"agent is not authorized for {action}", DeniedAudit(action, family_id, "user", target_user_id))
    return d


def view(r: Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "agent_id": r["agent_id"],
        "family_id": r["family_id"],
        "action": r["action"],
        "target_user_id": r["target_user_id"],
        "requires_approval": bool(r["requires_approval"]),
        "expires_at": r["expires_at"],
        "revoked_at": r["revoked_at"],
        "created_at": r["created_at"],
    }


def insert_grant(
    c: Conn,
    now: datetime,
    owner_user_id: str,
    agent_id: str,
    family_id: str,
    action: Action,
    target_user_id: Optional[str] = None,
    requires_approval: bool = False,
    expires_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if requires_approval and action not in APPROVABLE_ACTIONS:
        raise unprocessable("approval_not_supported", f"requires_approval is only supported for: {', '.join(sorted(APPROVABLE_ACTIONS))}")
    require_member(c, family_id, owner_user_id)
    if target_user_id is not None and target_user_id not in active_members(c, family_id):
        raise unprocessable("target_not_member", "target user is not an active member of this family")
    row = c.one(
        """INSERT INTO delegation_grants (id, agent_id, grantor_user_id, family_id, action, target_user_id,
                                          requires_approval, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
        (new_id("grant"), agent_id, owner_user_id, family_id, action, target_user_id, int(requires_approval),
         to_db(expires_at) if expires_at else None, to_db(now)),
    )
    assert row is not None
    return view(row)


def require_own_agent(c: Conn, owner_user_id: str, agent_id: str) -> None:
    if c.one("SELECT 1 FROM agents WHERE id = ? AND owner_user_id = ?", (agent_id, owner_user_id)) is None:
        raise not_found("agent")


def create_grant(ctx: Ctx, p: UserPrincipal, agent_id: str, family_id: str, action: Action,
                 target_user_id: Optional[str], requires_approval: bool, expires_at: Optional[datetime]) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        g = insert_grant(c, now, p.user_id, agent_id, family_id, action, target_user_id, requires_approval, expires_at)
        audit.record(c, now, p, "grant.create", "succeeded", family_id=family_id, resource_type="grant", resource_id=g["id"],
                     metadata={"agent_id": agent_id, "grant_action": action, "target_user_id": target_user_id,
                               "requires_approval": requires_approval})
    return g


def list_grants(ctx: Ctx, p: UserPrincipal, agent_id: str) -> list[dict[str, Any]]:
    with ctx.db.read() as c:
        require_own_agent(c, p.user_id, agent_id)
        return [view(r) for r in c.all("SELECT * FROM delegation_grants WHERE agent_id = ? ORDER BY created_at", (agent_id,))]


def revoke_grant(ctx: Ctx, p: UserPrincipal, grant_id: str) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        row = c.one(
            """UPDATE delegation_grants SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND grantor_user_id = ? RETURNING family_id""",
            (to_db(now), grant_id, p.user_id),
        )
        if row is None:
            raise not_found("grant")
        audit.record(c, now, p, "grant.revoke", "succeeded", family_id=row["family_id"], resource_type="grant", resource_id=grant_id)
