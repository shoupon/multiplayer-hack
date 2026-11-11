"""Append-only audit log. Metadata carries identifiers and enums only, never message text."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional, Union

from .auth import AgentPrincipal, Principal, UserPrincipal
from .context import Ctx
from .db import Conn
from .timeutil import to_db

Scalar = Union[str, int, bool, None]


def record(
    c: Conn,
    now: datetime,
    principal: Optional[Principal],
    action: str,
    outcome: str,
    *,
    family_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict[str, Scalar]] = None,
) -> None:
    if isinstance(principal, AgentPrincipal):
        kind, user_id, agent_id = "agent", principal.owner_user_id, principal.agent_id
    elif isinstance(principal, UserPrincipal):
        kind, user_id, agent_id = "user", principal.user_id, None
    else:
        kind, user_id, agent_id = "anonymous", None, None
    c.run(
        """INSERT INTO audit_events (family_id, actor_kind, actor_user_id, actor_agent_id, credential_id, action,
                                     resource_type, resource_id, outcome, reason, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            family_id, kind, user_id, agent_id, principal.credential_id if principal else None, action,
            resource_type, resource_id, outcome, reason, json.dumps(metadata or {}), to_db(now),
        ),
    )


def list_for_user(ctx: Ctx, user_id: str, after: int, limit: int) -> list[dict[str, Any]]:
    """Everything a user did, directly or through any of their agents."""
    with ctx.db.read() as c:
        rows = c.all(
            """SELECT id, family_id, actor_kind, actor_agent_id, action, resource_type, resource_id, outcome,
                      reason, metadata, created_at
                 FROM audit_events WHERE actor_user_id = ? AND id > ? ORDER BY id LIMIT ?""",
            (user_id, after, limit),
        )
    for r in rows:
        r["metadata"] = json.loads(r["metadata"])
    return rows
