from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import audit
from .auth import UserPrincipal
from .context import Ctx
from .db import Conn
from .ids import new_id
from .timeutil import to_db
from .tokens import TokenKind, issue_token


def insert_credential(c: Conn, kind: TokenKind, owner_id: str, label: Optional[str], now: datetime) -> tuple[str, str]:
    """Returns (credential_id, token). The token is shown once."""
    credential_id = new_id("credential")
    token, token_hash = issue_token(kind)
    c.run(
        """INSERT INTO credentials (id, principal_kind, user_id, agent_id, token_hash, label, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (credential_id, kind, owner_id if kind == "user" else None, owner_id if kind == "agent" else None,
         token_hash, label, to_db(now)),
    )
    return credential_id, token


def create_user(ctx: Ctx, display_name: str) -> dict[str, Any]:
    """MVP bootstrap: unauthenticated signup. See docs/security.md before exposing the server."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        user_id = new_id("user")
        c.run("INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)", (user_id, display_name, to_db(now)))
        credential_id, token = insert_credential(c, "user", user_id, "signup", now)
        audit.record(c, now, UserPrincipal(user_id, credential_id), "user.create", "succeeded",
                     resource_type="user", resource_id=user_id)
    return {"id": user_id, "display_name": display_name, "token": token}


def me(ctx: Ctx, user_id: str) -> dict[str, Any]:
    with ctx.db.read() as c:
        user = c.one("SELECT id, display_name, created_at FROM users WHERE id = ?", (user_id,))
        families = c.all(
            """SELECT f.id, f.name, m.role FROM family_memberships m JOIN families f ON f.id = m.family_id
                WHERE m.user_id = ? AND m.status = 'active' ORDER BY m.joined_at""",
            (user_id,),
        )
        agents = c.all(
            "SELECT id, display_name, provider, status FROM agents WHERE owner_user_id = ? ORDER BY created_at",
            (user_id,),
        )
    return {**(user or {}), "families": families, "agents": agents}
