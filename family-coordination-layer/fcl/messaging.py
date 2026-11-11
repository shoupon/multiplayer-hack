"""Message envelopes, delivery rows, the inbox, and direct agent-to-agent messages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from . import audit
from .auth import AgentPrincipal, Principal, UserPrincipal
from .context import Ctx
from .db import Conn, Row
from .envelope import DIRECT_TYPES, MESSAGE_TYPES, SCHEMA_VERSION, payload_schema_id, validate_payload
from .errors import DeniedAudit, bad_request, forbidden, not_found, unprocessable
from .grants import decide, require_grant
from .ids import new_id
from .policy import AgentActor, Deny
from .timeutil import to_db


def request_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Recipient:
    user_id: str
    agent_id: str


def new_conversation(c: Conn, now: datetime, family_id: str, subject: Optional[str], agent_id: Optional[str]) -> str:
    conversation_id = new_id("conversation")
    c.run(
        "INSERT INTO conversations (id, family_id, subject, created_by_agent_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, family_id, subject, agent_id, to_db(now)),
    )
    return conversation_id


def insert_message(
    c: Conn,
    now: datetime,
    *,
    family_id: str,
    conversation_id: str,
    message_type: str,
    payload: dict[str, Any],
    recipients: list[Recipient],
    sender: Optional[AgentPrincipal] = None,
    grant_id: Optional[str] = None,
    task_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    req_hash: Optional[str] = None,
    requires_response: bool = False,
    expires_at: Optional[datetime] = None,
) -> str:
    """Writes one envelope and one delivery row per recipient. `sender=None` means FCL itself."""
    message_id = new_id("message")
    canonical = validate_payload(message_type, payload)
    c.run(
        """INSERT INTO messages (id, family_id, conversation_id, task_id, schema_version, type, payload_schema, payload,
                                 sender_kind, sender_user_id, sender_agent_id, sender_credential_id, grant_id,
                                 correlation_id, causation_id, idempotency_key, request_hash, requires_response,
                                 expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id, family_id, conversation_id, task_id, SCHEMA_VERSION, message_type, payload_schema_id(message_type),
            json.dumps(canonical), "agent" if sender else "system", sender.owner_user_id if sender else None,
            sender.agent_id if sender else None, sender.credential_id if sender else None, grant_id,
            correlation_id or message_id, causation_id, idempotency_key, req_hash, int(requires_response),
            to_db(expires_at) if expires_at else None, to_db(now),
        ),
    )
    from . import webhooks  # local import: webhooks builds envelopes with this module

    for r in recipients:
        seq = c.one(
            """INSERT INTO message_deliveries (message_id, family_id, recipient_user_id, recipient_agent_id, created_at)
               VALUES (?, ?, ?, ?, ?) RETURNING seq""",
            (message_id, family_id, r.user_id, r.agent_id, to_db(now)),
        )
        assert seq is not None
        webhooks.enqueue_delivery(c, now, r.agent_id, seq["seq"])
    return message_id


def envelope(m: Row, deliveries: list[Row], include_auth: bool) -> dict[str, Any]:
    """The wire form of a message. Agent-authored content is labelled untrusted for the reader."""
    env: dict[str, Any] = {
        "message_id": m["id"],
        "schema_version": m["schema_version"],
        "family_id": m["family_id"],
        "conversation_id": m["conversation_id"],
        "task_id": m["task_id"],
        "type": m["type"],
        "payload_schema": m["payload_schema"],
        "payload": json.loads(m["payload"]),
        "content_trust": "untrusted" if m["sender_kind"] == "agent" else "system",
        "sender": {"kind": m["sender_kind"], "user_id": m["sender_user_id"], "agent_id": m["sender_agent_id"]},
        "recipients": [
            {"user_id": d["recipient_user_id"], "agent_id": d["recipient_agent_id"], "delivery_status": d["status"]}
            for d in deliveries
        ],
        "correlation_id": m["correlation_id"],
        "causation_id": m["causation_id"],
        "requires_response": bool(m["requires_response"]),
        "expires_at": m["expires_at"],
        "created_at": m["created_at"],
    }
    if include_auth:
        env["idempotency_key"] = m["idempotency_key"]
        env["auth"] = {"credential_id": m["sender_credential_id"], "grant_id": m["grant_id"]}
    return env


def _load(c: Conn, message_id: str) -> tuple[Row, list[Row]]:
    m = c.one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if m is None:
        raise not_found("message")
    return m, c.all("SELECT * FROM message_deliveries WHERE message_id = ? ORDER BY seq", (message_id,))


def send_direct(
    ctx: Ctx,
    p: AgentPrincipal,
    *,
    idempotency_key: str,
    req_hash: str,
    family_id: str,
    to_agent_id: str,
    message_type: str,
    payload: dict[str, Any],
    conversation_id: Optional[str],
    correlation_id: Optional[str],
    causation_id: Optional[str],
    requires_response: bool,
    expires_at: Optional[datetime],
) -> tuple[bool, dict[str, Any]]:
    """Returns (created, envelope). A retry with the same key returns the original message."""
    if message_type not in MESSAGE_TYPES:
        raise bad_request("unknown_message_type", f"unknown message type {message_type!r}")
    if message_type not in DIRECT_TYPES:
        raise unprocessable("engine_only_type", f"{message_type} is produced by FCL; use the /v1/tasks or /v1/approvals endpoints")
    try:
        validate_payload(message_type, payload)
    except ValidationError as e:
        raise bad_request("invalid_payload", str(e).splitlines()[0] + ": " + "; ".join(err["msg"] for err in e.errors()[:5]))

    now = ctx.clock()
    with ctx.db.tx() as c:
        existing = c.one("SELECT id, request_hash FROM messages WHERE sender_agent_id = ? AND idempotency_key = ?",
                         (p.agent_id, idempotency_key))
        if existing:
            if existing["request_hash"] != req_hash:
                raise unprocessable("idempotency_key_reused", "this Idempotency-Key was used for a different request")
            m, d = _load(c, existing["id"])
            return False, envelope(m, d, include_auth=True)

        target = c.one("SELECT id, owner_user_id, status FROM agents WHERE id = ?", (to_agent_id,))
        if target is None or target["status"] != "active":
            raise not_found("recipient agent")
        allowed = require_grant(c, now, p, family_id, "agent:message:send", target["owner_user_id"])
        consent = decide(c, now, AgentActor(target["id"], target["owner_user_id"], True), family_id,
                         "agent:message:receive", p.owner_user_id)
        if isinstance(consent, Deny):
            raise forbidden("recipient_not_accepting", "the recipient agent does not accept messages from you",
                            DeniedAudit("agent:message:send", family_id, "agent", to_agent_id))
        if conversation_id is None:
            conversation_id = new_conversation(c, now, family_id, None, p.agent_id)
        elif c.one("SELECT 1 FROM conversations WHERE id = ? AND family_id = ?", (conversation_id, family_id)) is None:
            raise not_found("conversation")
        if causation_id and c.one("SELECT 1 FROM messages WHERE id = ? AND family_id = ?", (causation_id, family_id)) is None:
            raise not_found("causation message")

        message_id = insert_message(
            c, now, family_id=family_id, conversation_id=conversation_id, message_type=message_type, payload=payload,
            recipients=[Recipient(target["owner_user_id"], target["id"])], sender=p, grant_id=allowed.grant.id,
            correlation_id=correlation_id, causation_id=causation_id, idempotency_key=idempotency_key,
            req_hash=req_hash, requires_response=requires_response, expires_at=expires_at,
        )
        audit.record(c, now, p, "message.send", "succeeded", family_id=family_id, resource_type="message",
                     resource_id=message_id, metadata={"type": message_type, "recipient_agent_id": to_agent_id})
        m, d = _load(c, message_id)
        return True, envelope(m, d, include_auth=True)


def _require_own_inbox(p: AgentPrincipal, agent_id: str, action: str) -> None:
    if agent_id != p.agent_id:
        raise forbidden("not_your_inbox", "agents can only use their own inbox", DeniedAudit(action, None, "agent", agent_id))


def inbox(ctx: Ctx, p: AgentPrincipal, agent_id: str, after: int, limit: int, unacknowledged: bool = False) -> dict[str, Any]:
    """
    Deliveries after the cursor, oldest first. Fetching marks them delivered.
    `unacknowledged` returns only deliveries not yet acknowledged, so a restarted agent needs no cursor.
    """
    _require_own_inbox(p, agent_id, "inbox.read")
    now = ctx.clock()
    from . import tasks  # local import: tasks sends messages through this module

    with ctx.db.tx() as c:
        rows = c.all(
            """SELECT * FROM message_deliveries
                WHERE recipient_agent_id = ? AND seq > ? AND (? = 0 OR status != 'acknowledged')
                ORDER BY seq LIMIT ?""",
            (agent_id, after, int(unacknowledged), limit),
        )
        items = []
        for d in rows:
            if d["status"] in ("pending", "failed"):
                c.run("UPDATE message_deliveries SET status = 'delivered', delivered_at = ? WHERE seq = ?", (to_db(now), d["seq"]))
                d = {**d, "status": "delivered"}
            m, deliveries = _load(c, d["message_id"])
            if m["task_id"] and m["type"] in ("coordination.proposal", "coordination.counterproposal"):
                tasks.mark_delivered(c, now, m["task_id"], d["recipient_user_id"])
            items.append({"cursor": d["seq"], "delivery_status": d["status"],
                          "message": envelope(m, [x if x["seq"] != d["seq"] else d for x in deliveries], include_auth=False)})
    return {"items": items, "next_cursor": items[-1]["cursor"] if items else after}


def inbox_count(ctx: Ctx, p: AgentPrincipal, agent_id: str) -> dict[str, int]:
    """How many deliveries await acknowledgement. Changes nothing, so a watcher can poll it freely."""
    _require_own_inbox(p, agent_id, "inbox.read")
    with ctx.db.read() as c:
        row = c.one(
            "SELECT count(*) AS n FROM message_deliveries WHERE recipient_agent_id = ? AND status != 'acknowledged'",
            (agent_id,),
        )
    assert row is not None
    return {"unacknowledged": row["n"]}


def acknowledge(ctx: Ctx, p: AgentPrincipal, agent_id: str, through: int) -> int:
    _require_own_inbox(p, agent_id, "inbox.ack")
    now = ctx.clock()
    with ctx.db.tx() as c:
        return c.run(
            """UPDATE message_deliveries SET status = 'acknowledged', acknowledged_at = ?,
                      delivered_at = COALESCE(delivered_at, ?)
                WHERE recipient_agent_id = ? AND seq <= ? AND status != 'acknowledged'""",
            (to_db(now), to_db(now), agent_id, through),
        )


def get_message(ctx: Ctx, p: Principal, message_id: str) -> dict[str, Any]:
    """Visible to the sender and recipient agents, and to the humans who own them."""
    with ctx.db.read() as c:
        m, deliveries = _load(c, message_id)
        if isinstance(p, AgentPrincipal):
            is_sender = m["sender_agent_id"] == p.agent_id
            is_recipient = any(d["recipient_agent_id"] == p.agent_id for d in deliveries)
        else:
            assert isinstance(p, UserPrincipal)
            is_sender = m["sender_user_id"] == p.user_id and m["sender_kind"] == "agent"
            is_recipient = any(d["recipient_user_id"] == p.user_id for d in deliveries)
        if not (is_sender or is_recipient):
            raise not_found("message")
        return envelope(m, deliveries, include_auth=is_sender)

