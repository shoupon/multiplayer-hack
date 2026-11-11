from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Literal, Optional

from . import audit
from .auth import UserPrincipal
from .context import Ctx
from .db import Conn
from .errors import DeniedAudit, conflict, forbidden, not_found
from .ids import new_id
from .timeutil import to_db
from .tokens import hash_secret

Role = Literal["admin", "member"]


def active_members(c: Conn, family_id: str) -> frozenset[str]:
    rows = c.all("SELECT user_id FROM family_memberships WHERE family_id = ? AND status = 'active'", (family_id,))
    return frozenset(r["user_id"] for r in rows)


def _role(c: Conn, family_id: str, user_id: str) -> Optional[Role]:
    row = c.one(
        "SELECT role FROM family_memberships WHERE family_id = ? AND user_id = ? AND status = 'active'",
        (family_id, user_id),
    )
    return row["role"] if row else None


def require_member(c: Conn, family_id: str, user_id: str) -> Role:
    """Non-members get 404, not 403, so family ids cannot be probed."""
    role = _role(c, family_id, user_id)
    if role is None:
        raise not_found("family")
    return role


def create_family(ctx: Ctx, p: UserPrincipal, name: str) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        family_id = new_id("family")
        c.run("INSERT INTO families (id, name, created_by, created_at) VALUES (?, ?, ?, ?)", (family_id, name, p.user_id, to_db(now)))
        c.run(
            "INSERT INTO family_memberships (family_id, user_id, role, joined_at) VALUES (?, ?, 'admin', ?)",
            (family_id, p.user_id, to_db(now)),
        )
        audit.record(c, now, p, "family.create", "succeeded", family_id=family_id, resource_type="family", resource_id=family_id)
    return {"id": family_id, "name": name}


def get_family(ctx: Ctx, user_id: str, family_id: str) -> dict[str, Any]:
    with ctx.db.read() as c:
        require_member(c, family_id, user_id)
        family = c.one("SELECT id, name, created_at FROM families WHERE id = ?", (family_id,)) or {}
        members = c.all(
            """SELECT m.user_id, u.display_name, m.role, m.joined_at
                 FROM family_memberships m JOIN users u ON u.id = m.user_id
                WHERE m.family_id = ? AND m.status = 'active' ORDER BY m.joined_at""",
            (family_id,),
        )
    return {**family, "members": members}


def create_invitation(ctx: Ctx, p: UserPrincipal, family_id: str, role: Role, ttl_hours: int) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        if require_member(c, family_id, p.user_id) != "admin":
            raise forbidden("admin_required", "only family admins can invite", DeniedAudit("invitation.create", family_id))
        invitation_id = new_id("invitation")
        code = "fcl_inv_" + secrets.token_urlsafe(24)
        expires_at = now + timedelta(hours=ttl_hours)
        c.run(
            """INSERT INTO family_invitations (id, family_id, created_by, code_hash, role, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (invitation_id, family_id, p.user_id, hash_secret(code), role, to_db(expires_at), to_db(now)),
        )
        audit.record(c, now, p, "invitation.create", "succeeded", family_id=family_id, resource_type="invitation", resource_id=invitation_id)
    return {"id": invitation_id, "family_id": family_id, "role": role, "code": code, "expires_at": to_db(expires_at)}


def accept_invitation(ctx: Ctx, p: UserPrincipal, code: str) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        # Single use: the conditional UPDATE claims the invitation atomically.
        inv = c.one(
            """UPDATE family_invitations SET accepted_by = ?, accepted_at = ?
                WHERE code_hash = ? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                RETURNING id, family_id, role""",
            (p.user_id, to_db(now), hash_secret(code), to_db(now)),
        )
        if inv is None:
            raise not_found("invitation")
        c.run(
            """INSERT INTO family_memberships (family_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (family_id, user_id) DO UPDATE
                 SET status = 'active', role = excluded.role, removed_at = NULL, joined_at = excluded.joined_at""",
            (inv["family_id"], p.user_id, inv["role"], to_db(now)),
        )
        audit.record(c, now, p, "invitation.accept", "succeeded", family_id=inv["family_id"], resource_type="invitation", resource_id=inv["id"])
    return {"family_id": inv["family_id"], "role": inv["role"]}


def remove_member(ctx: Ctx, p: UserPrincipal, family_id: str, target: str) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        my_role = require_member(c, family_id, p.user_id)
        if target != p.user_id and my_role != "admin":
            raise forbidden("admin_required", "only admins can remove other members",
                            DeniedAudit("member.remove", family_id, "user", target))
        target_role = _role(c, family_id, target)
        if target_role is None:
            raise not_found("member")
        if target_role == "admin":
            admins = c.one(
                "SELECT count(*) AS n FROM family_memberships WHERE family_id = ? AND role = 'admin' AND status = 'active'",
                (family_id,),
            )
            if admins and admins["n"] <= 1:
                raise conflict("last_admin", "a family needs at least one admin")
        c.run(
            "UPDATE family_memberships SET status = 'removed', removed_at = ? WHERE family_id = ? AND user_id = ?",
            (to_db(now), family_id, target),
        )
        audit.record(c, now, p, "member.remove", "succeeded", family_id=family_id, resource_type="user", resource_id=target)
