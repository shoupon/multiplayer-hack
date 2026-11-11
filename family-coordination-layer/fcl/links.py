"""Monitor links: one URL an owner gives their agent. Reading it is safe; only a claim issues a token."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from . import audit
from .auth import AgentPrincipal, UserPrincipal
from .context import Ctx
from .db import Conn, Row
from .errors import AppError, not_found
from .grants import require_own_agent
from .identity import insert_credential
from .ids import new_id
from .timeutil import from_db, to_db
from .tokens import hash_secret

CODE_PATTERN = r"^[A-Za-z0-9_-]{32}$"
LinkState = Literal["open", "claimed", "expired", "revoked"]


@dataclass(frozen=True)
class Link:
    id: str
    agent_id: str
    owner_user_id: str
    state: LinkState
    expires_at: str
    claimed_credential_id: Optional[str]


def _state(r: Row, now: datetime) -> LinkState:
    if r["revoked_at"] or r["agent_status"] != "active":
        return "revoked"
    if from_db(r["expires_at"]) <= now:
        return "expired"
    return "claimed" if r["claimed_at"] else "open"


def _link(r: Row, now: datetime) -> Link:
    return Link(r["id"], r["agent_id"], r["owner_user_id"], _state(r, now), r["expires_at"], r["claimed_credential_id"])


def _find(c: Conn, now: datetime, code: str) -> Link:
    r = c.one(
        """SELECT l.*, a.status AS agent_status FROM agent_links l JOIN agents a ON a.id = l.agent_id
            WHERE l.code_hash = ?""",
        (hash_secret(code),),
    )
    if r is None:
        raise not_found("link")
    return _link(r, now)


def already_claimed() -> AppError:
    return AppError(410, "link_claimed", "this link already issued its token; ask the owner for a new link")


def create(ctx: Ctx, p: UserPrincipal, agent_id: str, ttl_hours: int) -> tuple[dict[str, Any], str]:
    """Returns (link view, code). The code is shown once, inside the URL."""
    now = ctx.clock()
    code = secrets.token_urlsafe(24)
    link_id = new_id("link")
    expires_at = to_db(now + timedelta(hours=ttl_hours))
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        c.run(
            """INSERT INTO agent_links (id, agent_id, owner_user_id, code_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (link_id, agent_id, p.user_id, hash_secret(code), to_db(now), expires_at),
        )
        audit.record(c, now, p, "agent.link.create", "succeeded", resource_type="link", resource_id=link_id,
                     metadata={"agent_id": agent_id, "ttl_hours": ttl_hours})
    return {"id": link_id, "agent_id": agent_id, "state": "open", "expires_at": expires_at}, code


def revoke(ctx: Ctx, p: UserPrincipal, agent_id: str, link_id: str) -> None:
    """Stops further reads and claims. A token already claimed stays valid until its credential is revoked."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        n = c.run(
            "UPDATE agent_links SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND agent_id = ? AND owner_user_id = ?",
            (to_db(now), link_id, agent_id, p.user_id),
        )
        if n == 0:
            raise not_found("link")
        audit.record(c, now, p, "agent.link.revoke", "succeeded", resource_type="link", resource_id=link_id,
                     metadata={"agent_id": agent_id})


def resolve(c: Conn, now: datetime, code: str) -> Link:
    """For GET: an open or claimed link can still be read. Unknown, expired, and revoked look the same."""
    link = _find(c, now, code)
    if link.state in ("expired", "revoked"):
        raise not_found("link")
    return link


def claim(ctx: Ctx, code: str) -> dict[str, str]:
    """Issues a fresh agent token exactly once per link."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        link = resolve(c, now, code)
        if link.state == "claimed":
            raise already_claimed()
        credential_id, token = insert_credential(c, "agent", link.agent_id, f"link:{link.id}", now)
        c.run("UPDATE agent_links SET claimed_at = ?, claimed_credential_id = ? WHERE id = ?",
              (to_db(now), credential_id, link.id))
        audit.record(c, now, AgentPrincipal(link.agent_id, link.owner_user_id, True, credential_id), "agent.link.claim",
                     "succeeded", resource_type="link", resource_id=link.id, metadata={"credential_id": credential_id})
    return {"agent_id": link.agent_id, "credential_id": credential_id, "token": token}
