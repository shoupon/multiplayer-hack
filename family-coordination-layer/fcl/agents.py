"""Agent registry, credentials, and discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from . import audit
from .auth import AgentPrincipal, Principal, UserPrincipal, acting_user_id
from .context import Ctx
from .db import Conn
from .errors import not_found
from .families import require_member
from .grants import insert_grant, require_grant, require_own_agent
from .identity import insert_credential
from .ids import new_id
from .policy import Action
from .timeutil import to_db

DeclaredTransport = Literal["inbox", "mcp"]


@dataclass(frozen=True)
class GrantSpec:
    family_id: str
    action: Action
    target_user_id: Optional[str] = None
    requires_approval: bool = False
    expires_at: Optional[datetime] = None


def register_agent(
    ctx: Ctx,
    p: UserPrincipal,
    display_name: str,
    provider: str,
    capabilities: list[str],
    transports: list[DeclaredTransport],
    grants: list[GrantSpec],
) -> dict[str, Any]:
    now = ctx.clock()
    caps = sorted(set(capabilities))
    kinds = sorted({"inbox", *transports})  # every agent can poll its inbox
    with ctx.db.tx() as c:
        agent_id = new_id("agent")
        c.run(
            "INSERT INTO agents (id, owner_user_id, display_name, provider, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, p.user_id, display_name, provider, to_db(now)),
        )
        for cap in caps:
            c.run("INSERT INTO agent_capabilities (agent_id, capability) VALUES (?, ?)", (agent_id, cap))
        for kind in kinds:
            c.run("INSERT INTO agent_transports (agent_id, kind, created_at) VALUES (?, ?, ?)", (agent_id, kind, to_db(now)))
        created = [
            insert_grant(c, now, p.user_id, agent_id, g.family_id, g.action, g.target_user_id, g.requires_approval, g.expires_at)
            for g in grants
        ]
        credential_id, token = insert_credential(c, "agent", agent_id, "initial", now)
        audit.record(c, now, p, "agent.register", "succeeded", resource_type="agent", resource_id=agent_id,
                     metadata={"provider": provider, "grants": len(created)})
    return {"id": agent_id, "owner_user_id": p.user_id, "display_name": display_name, "provider": provider,
            "capabilities": caps, "transports": kinds, "grants": created, "credential_id": credential_id, "token": token}


def issue_credential(ctx: Ctx, p: UserPrincipal, agent_id: str, label: Optional[str]) -> dict[str, str]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        credential_id, token = insert_credential(c, "agent", agent_id, label, now)
        audit.record(c, now, p, "agent.credential.issue", "succeeded", resource_type="credential", resource_id=credential_id,
                     metadata={"agent_id": agent_id})
    return {"id": credential_id, "token": token}


def revoke_credential(ctx: Ctx, p: UserPrincipal, agent_id: str, credential_id: str) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        n = c.run(
            "UPDATE credentials SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND agent_id = ?",
            (to_db(now), credential_id, agent_id),
        )
        if n == 0:
            raise not_found("credential")
        audit.record(c, now, p, "agent.credential.revoke", "succeeded", resource_type="credential", resource_id=credential_id,
                     metadata={"agent_id": agent_id})


def disable_agent(ctx: Ctx, p: UserPrincipal, agent_id: str) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        c.run("UPDATE agents SET status = 'disabled' WHERE id = ?", (agent_id,))
        audit.record(c, now, p, "agent.disable", "succeeded", resource_type="agent", resource_id=agent_id)


def visible_agents(c: Conn, now: datetime, viewer: str, family_id: str, only_agent: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Agents in a family the viewer may see: the viewer's own agents, plus other members' agents
    whose owners granted `agent:message:receive` to the viewer or to the whole family.
    Never returns endpoints, credentials, or grants.
    """
    rows = c.all(
        """SELECT a.id, a.owner_user_id, u.display_name AS owner_display_name, a.display_name, a.provider, a.last_seen_at,
                  (SELECT json_group_array(capability) FROM (SELECT capability FROM agent_capabilities
                     WHERE agent_id = a.id ORDER BY capability)) AS capabilities,
                  (SELECT json_group_array(kind) FROM (SELECT kind FROM agent_transports
                     WHERE agent_id = a.id AND status = 'active' ORDER BY kind)) AS transports,
                  EXISTS (SELECT 1 FROM agent_transports t WHERE t.agent_id = a.id AND t.kind = 'webhook'
                            AND t.status = 'active' AND t.last_success_at IS NOT NULL
                            AND (t.last_failure_at IS NULL OR t.last_success_at > t.last_failure_at)) AS webhook_healthy
             FROM agents a
             JOIN users u ON u.id = a.owner_user_id
             JOIN family_memberships m ON m.user_id = a.owner_user_id AND m.family_id = ? AND m.status = 'active'
            WHERE a.status = 'active'
              AND (? IS NULL OR a.id = ?)
              AND (a.owner_user_id = ? OR EXISTS (
                    SELECT 1 FROM delegation_grants g
                     WHERE g.agent_id = a.id AND g.family_id = ? AND g.action = 'agent:message:receive'
                       AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > ?)
                       AND (g.target_user_id IS NULL OR g.target_user_id = ?)))
            ORDER BY a.created_at""",
        (family_id, only_agent, only_agent, viewer, family_id, to_db(now), viewer),
    )
    return [
        {
            "id": r["id"],
            "owner": {"user_id": r["owner_user_id"], "display_name": r["owner_display_name"]},
            "display_name": r["display_name"],
            "provider": r["provider"],
            "provider_verified": False,
            "capabilities": json.loads(r["capabilities"]),
            "transports": json.loads(r["transports"]),
            "availability": {"mode": "push" if r["webhook_healthy"] else "poll", "last_seen_at": r["last_seen_at"]},
        }
        for r in rows
    ]


def _require_discovery(c: Conn, now: datetime, p: Principal, family_id: str) -> None:
    """Users need membership. Agents additionally need a `family:members:discover` grant."""
    require_member(c, family_id, acting_user_id(p))
    if isinstance(p, AgentPrincipal):
        require_grant(c, now, p, family_id, "family:members:discover", None)


def discover(ctx: Ctx, p: Principal, family_id: str) -> list[dict[str, Any]]:
    now = ctx.clock()
    with ctx.db.read() as c:
        _require_discovery(c, now, p, family_id)
        return visible_agents(c, now, acting_user_id(p), family_id)


def capabilities(ctx: Ctx, p: Principal, family_id: str, agent_id: str) -> dict[str, Any]:
    now = ctx.clock()
    with ctx.db.read() as c:
        _require_discovery(c, now, p, family_id)
        found = visible_agents(c, now, acting_user_id(p), family_id, agent_id)
    if not found:
        raise not_found("agent")
    return found[0]
