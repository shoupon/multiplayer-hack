"""Pure, deterministic authorization. Never reads message content. See docs/DESIGN.md section 8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Union

Action = Literal[
    "agent:message:send",
    "agent:message:receive",
    "family:members:discover",
    "task:propose",
    "task:respond",
]
DELEGABLE_ACTIONS: tuple[Action, ...] = (
    "agent:message:send",
    "agent:message:receive",
    "family:members:discover",
    "task:propose",
    "task:respond",
)
# Only these may carry requires_approval in the MVP.
APPROVABLE_ACTIONS: frozenset[str] = frozenset({"task:respond"})

DenyReason = Literal["agent_disabled", "actor_not_member", "target_not_member", "no_grant"]


@dataclass(frozen=True)
class Grant:
    id: str
    agent_id: str
    family_id: str
    action: str
    target_user_id: str | None
    requires_approval: bool
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class AgentActor:
    agent_id: str
    owner_user_id: str
    active: bool


@dataclass(frozen=True)
class Question:
    actor: AgentActor
    family_id: str
    action: Action
    target_user_id: str | None
    active_members: frozenset[str]
    grants: tuple[Grant, ...]
    now: datetime


@dataclass(frozen=True)
class Allow:
    grant: Grant

    @property
    def requires_approval(self) -> bool:
        return self.grant.requires_approval


@dataclass(frozen=True)
class Deny:
    reason: DenyReason


Decision = Union[Allow, Deny]


def _matches(g: Grant, q: Question) -> bool:
    return (
        g.agent_id == q.actor.agent_id
        and g.family_id == q.family_id
        and g.action == q.action
        and (g.target_user_id is None or g.target_user_id == q.target_user_id)
        and g.revoked_at is None
        and (g.expires_at is None or g.expires_at > q.now)
    )


def _precedence(g: Grant) -> int:
    """Most specific first; on a tie, the stricter grant (requires approval) wins."""
    return (2 if g.target_user_id is not None else 0) + (1 if g.requires_approval else 0)


def authorize(q: Question) -> Decision:
    if not q.actor.active:
        return Deny("agent_disabled")
    if q.actor.owner_user_id not in q.active_members:
        return Deny("actor_not_member")
    if q.target_user_id is not None and q.target_user_id not in q.active_members:
        return Deny("target_not_member")
    candidates = sorted((g for g in q.grants if _matches(g, q)), key=_precedence, reverse=True)
    return Allow(candidates[0]) if candidates else Deny("no_grant")
