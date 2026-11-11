"""Bearer-token authentication. Identity never comes from a request body."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Union

from .context import Ctx
from .timeutil import to_db
from .tokens import hash_secret, token_kind


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    credential_id: str
    kind: str = "user"


@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    owner_user_id: str
    agent_active: bool
    credential_id: str
    kind: str = "agent"


Principal = Union[UserPrincipal, AgentPrincipal]

_BEARER = re.compile(r"^Bearer (\S+)$")


def acting_user_id(p: Principal) -> str:
    """The human a principal ultimately acts for."""
    return p.user_id if isinstance(p, UserPrincipal) else p.owner_user_id


def resolve_principal(ctx: Ctx, header: str | None) -> Principal | None:
    m = _BEARER.match(header or "")
    if not m or token_kind(m.group(1)) is None:
        return None
    now = ctx.clock()
    with ctx.db.tx() as c:
        row = c.one(
            """SELECT c.id, c.principal_kind, c.user_id, c.agent_id, a.owner_user_id, a.status AS agent_status
                 FROM credentials c
                 LEFT JOIN agents a ON a.id = c.agent_id
                 JOIN users u ON u.id = COALESCE(c.user_id, a.owner_user_id)
                WHERE c.token_hash = ? AND c.revoked_at IS NULL AND (c.expires_at IS NULL OR c.expires_at > ?)
                  AND u.deleted_at IS NULL""",
            (hash_secret(m.group(1)), to_db(now)),
        )
        if row is None:
            return None
        stale = to_db(now - timedelta(minutes=1))
        c.run(
            "UPDATE credentials SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
            (to_db(now), row["id"], stale),
        )
        if row["agent_id"]:
            c.run(
                "UPDATE agents SET last_seen_at = ? WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
                (to_db(now), row["agent_id"], stale),
            )
    if row["principal_kind"] == "user":
        return UserPrincipal(row["user_id"], row["id"])
    return AgentPrincipal(row["agent_id"], row["owner_user_id"], row["agent_status"] == "active", row["id"])
