"""Permission requests: an agent asks its own owner before it acts outside FCL on the owner's behalf.

FCL never touches the calendar or the card. It records exactly what was asked, lets only the owner
decide with a user credential, and tells the agent. The approval id is the agent's proof, and it is
the idempotency key an agent should hand to the outside service so an approved action runs once.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import audit
from .auth import AgentPrincipal
from .context import Ctx
from .errors import bad_request, not_found, unprocessable
from .families import require_member
from .ids import new_id
from .messaging import new_conversation, request_hash
from .timeutil import to_db

MAX_TTL = timedelta(hours=24)
MAX_CALENDAR_WINDOW = timedelta(days=31)


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarRead(_Scope):
    """Read the owner's busy/free times inside one window. Nothing outside the window."""

    window_start: AwareDatetime
    window_end: AwareDatetime

    @model_validator(mode="after")
    def _window(self) -> "CalendarRead":
        if not self.window_start < self.window_end <= self.window_start + MAX_CALENDAR_WINDOW:
            raise ValueError("window_end must be after window_start and at most 31 days later")
        return self


class PaymentCharge(_Scope):
    """Charge the owner's payment method once, for at most this amount, to this payee."""

    payee: str = Field(min_length=1, max_length=200)
    amount_minor: int = Field(gt=0, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reference: str = Field(min_length=1, max_length=200)


SCOPES: dict[str, type[_Scope]] = {"calendar.read": CalendarRead, "payment.charge": PaymentCharge}


def request(ctx: Ctx, p: AgentPrincipal, *, idempotency_key: str, family_id: str, scope: str, params: dict[str, Any],
            purpose: str, expires_at: datetime, task_id: Optional[str]) -> tuple[bool, str]:
    """Returns (created, approval_id). A retry with the same key returns the original request."""
    if scope not in SCOPES:
        raise bad_request("unknown_scope", f"unknown permission scope {scope!r}; known: {', '.join(SCOPES)}")
    try:
        canonical = SCOPES[scope].model_validate(params).model_dump(mode="json")
    except ValidationError as e:
        raise bad_request("invalid_params", "; ".join(err["msg"] for err in e.errors()[:5]))
    now = ctx.clock()
    if not now < expires_at <= now + MAX_TTL:
        raise bad_request("invalid_expiry", "expires_at must be in the future and at most 24 hours away")
    req_hash = request_hash({"family_id": family_id, "scope": scope, "params": canonical, "purpose": purpose,
                             "expires_at": expires_at.isoformat(), "task_id": task_id})

    with ctx.db.tx() as c:
        prior = c.one("SELECT id, request_hash FROM approval_requests WHERE requesting_agent_id = ? AND idempotency_key = ?",
                      (p.agent_id, idempotency_key))
        if prior:
            if prior["request_hash"] != req_hash:
                raise unprocessable("idempotency_key_reused", "this Idempotency-Key was used for a different request")
            return False, prior["id"]

        require_member(c, family_id, p.owner_user_id)
        if task_id is not None:
            task = c.one(
                """SELECT t.conversation_id FROM coordination_tasks t
                     JOIN task_participants tp ON tp.task_id = t.id AND tp.agent_id = ?
                    WHERE t.id = ? AND t.family_id = ?""",
                (p.agent_id, task_id, family_id),
            )
            if task is None:
                raise not_found("task")
            conversation_id = task["conversation_id"]
        else:
            conversation_id = new_conversation(c, now, family_id, f"permission: {scope}", p.agent_id)

        action = {"scope": scope, "params": canonical, "purpose": purpose, "credential_id": p.credential_id}
        approval_id = new_id("approval")
        c.run(
            """INSERT INTO approval_requests (id, family_id, kind, task_id, conversation_id, approver_user_id,
                                              requesting_agent_id, action, action_hash, expires_at, idempotency_key,
                                              request_hash, created_at)
               VALUES (?, ?, 'permission', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (approval_id, family_id, task_id, conversation_id, p.owner_user_id, p.agent_id, json.dumps(action),
             request_hash(action), to_db(expires_at), idempotency_key, req_hash, to_db(now)),
        )
        audit.record(c, now, p, "permission.request", "succeeded", family_id=family_id, resource_type="approval",
                     resource_id=approval_id, metadata={"scope": scope, "task_id": task_id})
        return True, approval_id
