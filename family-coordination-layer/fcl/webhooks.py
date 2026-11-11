"""Signed webhook delivery with SSRF protection. See docs/DESIGN.md section 13.

Signing secrets are never stored: each is HMAC(server_key, subscription id + salt), re-derived
when signing. A database copy alone cannot forge deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from . import audit
from .auth import UserPrincipal
from .context import Ctx
from .db import Conn
from .errors import bad_request, conflict, not_found
from .grants import require_own_agent
from .ids import new_id
from .jobs import Retry, enqueue
from .timeutil import to_db

TIMEOUT_SECONDS = 5.0
SIGNATURE_HEADER = "FCL-Signature"
REPLAY_WINDOW_SECONDS = 300


# ---- destination vetting ---------------------------------------------------------------


def _ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool) -> bool:
    if allow_loopback and ip.is_loopback:
        return True
    return ip.is_global and not ip.is_multicast


def vet_url(url: str, allow_loopback: bool) -> tuple[str, str, int]:
    """Syntactic checks. Returns (scheme, host, port). Plain http only to loopback in development."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise bad_request("invalid_webhook_url", "webhook URL must be https")
    if parts.username or parts.password:
        raise bad_request("invalid_webhook_url", "webhook URL must not contain credentials")
    if not parts.hostname:
        raise bad_request("invalid_webhook_url", "webhook URL needs a host")
    if parts.scheme == "http" and not allow_loopback:
        raise bad_request("invalid_webhook_url", "webhook URL must be https")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def resolve_vetted(scheme: str, host: str, port: int, allow_loopback: bool) -> str:
    """Resolves the host and returns one address, but only if every address it resolves to is allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise bad_request("invalid_webhook_url", f"cannot resolve {host}: {e}")
    addrs = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addrs:
        raise bad_request("invalid_webhook_url", f"cannot resolve {host}")
    for ip in addrs:
        if not _ip_allowed(ip, allow_loopback):
            raise bad_request("webhook_destination_blocked", f"{host} resolves to a non-public address")
        if scheme == "http" and not ip.is_loopback:
            raise bad_request("invalid_webhook_url", "plain http is only allowed to loopback")
    return str(addrs[0])


# ---- signing ---------------------------------------------------------------------------


def derive_secret(server_key: bytes, subscription_id: str, salt: str) -> str:
    return "whsec_" + hmac.new(server_key, f"webhook:{subscription_id}:{salt}".encode(), hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, now: int, tolerance: int = REPLAY_WINDOW_SECONDS) -> bool:
    """Receiver-side check, shipped for agents written in Python and used by the tests and demo."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
    except (ValueError, KeyError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header)


# ---- subscriptions ---------------------------------------------------------------------


def subscribe(ctx: Ctx, p: UserPrincipal, agent_id: str, url: str) -> dict[str, Any]:
    allow_loopback = ctx.settings.allow_loopback_webhooks
    scheme, host, port = vet_url(url, allow_loopback)
    resolve_vetted(scheme, host, port, allow_loopback)
    now = ctx.clock()
    with ctx.db.tx() as c:
        require_own_agent(c, p.user_id, agent_id)
        if c.one("SELECT 1 FROM webhook_subscriptions WHERE agent_id = ? AND status = 'active'", (agent_id,)):
            raise conflict("webhook_exists", "this agent already has an active webhook; delete it first")
        subscription_id, salt = new_id("webhook"), secrets.token_hex(16)
        c.run(
            "INSERT INTO webhook_subscriptions (id, agent_id, url, secret_salt, created_at) VALUES (?, ?, ?, ?, ?)",
            (subscription_id, agent_id, url, salt, to_db(now)),
        )
        c.run(
            """INSERT INTO agent_transports (agent_id, kind, status, created_at) VALUES (?, 'webhook', 'active', ?)
               ON CONFLICT (agent_id, kind) DO UPDATE SET status = 'active', last_success_at = NULL, last_failure_at = NULL""",
            (agent_id, to_db(now)),
        )
        audit.record(c, now, p, "webhook.subscribe", "succeeded", resource_type="webhook", resource_id=subscription_id,
                     metadata={"agent_id": agent_id, "host": host})
    return {"id": subscription_id, "agent_id": agent_id, "url": url,
            "secret": derive_secret(ctx.server_key, subscription_id, salt), "signature_header": SIGNATURE_HEADER}


def unsubscribe(ctx: Ctx, p: UserPrincipal, subscription_id: str) -> None:
    now = ctx.clock()
    with ctx.db.tx() as c:
        row = c.one(
            """SELECT w.agent_id FROM webhook_subscriptions w JOIN agents a ON a.id = w.agent_id
                WHERE w.id = ? AND a.owner_user_id = ? AND w.status = 'active'""",
            (subscription_id, p.user_id),
        )
        if row is None:
            raise not_found("webhook")
        c.run("UPDATE webhook_subscriptions SET status = 'disabled', disabled_at = ? WHERE id = ?", (to_db(now), subscription_id))
        c.run("UPDATE agent_transports SET status = 'disabled' WHERE agent_id = ? AND kind = 'webhook'", (row["agent_id"],))
        audit.record(c, now, p, "webhook.unsubscribe", "succeeded", resource_type="webhook", resource_id=subscription_id)


def enqueue_delivery(c: Conn, now: datetime, agent_id: str, seq: int) -> None:
    """Called inside the transaction that creates the delivery row (the outbox)."""
    if c.one("SELECT 1 FROM webhook_subscriptions WHERE agent_id = ? AND status = 'active'", (agent_id,)):
        enqueue(c, now, "webhook.deliver", {"seq": seq}, now, dedupe_key=f"webhook:{seq}")


# ---- delivery job ----------------------------------------------------------------------


def _post_pinned(url: str, ip: str, host: str, body: bytes, headers: dict[str, str]) -> int:
    """POSTs to the vetted IP while keeping the original Host header and TLS server name."""
    parts = urlsplit(url)
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc += f":{parts.port}"
    target = parts._replace(netloc=netloc).geturl()
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        r = client.post(target, content=body, headers={**headers, "host": parts.netloc},
                        extensions={"sni_hostname": host})
    return r.status_code


def deliver(ctx: Ctx, payload: dict[str, Any]) -> None:
    from . import messaging, tasks  # local import: both modules call into this one

    seq = int(payload["seq"])
    with ctx.db.read() as c:
        d = c.one("SELECT * FROM message_deliveries WHERE seq = ?", (seq,))
        sub = c.one("SELECT * FROM webhook_subscriptions WHERE agent_id = ? AND status = 'active'",
                    (d["recipient_agent_id"],)) if d else None
        if d is None or sub is None or d["status"] == "acknowledged":
            return
        m = c.one("SELECT * FROM messages WHERE id = ?", (d["message_id"],))
        deliveries = c.all("SELECT * FROM message_deliveries WHERE message_id = ? ORDER BY seq", (d["message_id"],))
    assert m is not None
    body = json.dumps({"cursor": seq, "message": messaging.envelope(m, deliveries, include_auth=False)}).encode()

    allow_loopback = ctx.settings.allow_loopback_webhooks
    now = ctx.clock()
    secret = derive_secret(ctx.server_key, sub["id"], sub["secret_salt"])
    headers = {"content-type": "application/json", SIGNATURE_HEADER: sign(secret, int(now.timestamp()), body),
               "FCL-Delivery": str(seq), "FCL-Message-Id": m["id"]}
    error: Optional[str] = None
    try:
        scheme, host, port = vet_url(sub["url"], allow_loopback)
        ip = resolve_vetted(scheme, host, port, allow_loopback)
        status = _post_pinned(sub["url"], ip, host, body, headers)
        if not 200 <= status < 300:
            error = f"receiver answered {status}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    done = ctx.clock()
    with ctx.db.tx() as c:
        if error is None:
            c.run("UPDATE message_deliveries SET status = 'delivered', delivered_at = ? WHERE seq = ? AND status = 'pending'",
                  (to_db(done), seq))
            c.run("UPDATE agent_transports SET last_success_at = ? WHERE agent_id = ? AND kind = 'webhook'",
                  (to_db(done), d["recipient_agent_id"]))
            if m["task_id"] and m["type"] in ("coordination.proposal", "coordination.counterproposal"):
                tasks.mark_delivered(c, done, m["task_id"], d["recipient_user_id"])
        else:
            c.run("UPDATE message_deliveries SET last_error = ? WHERE seq = ?", (error[:500], seq))
            c.run("UPDATE agent_transports SET last_failure_at = ? WHERE agent_id = ? AND kind = 'webhook'",
                  (to_db(done), d["recipient_agent_id"]))
    if error is not None:
        raise Retry(error)


def delivery_dead(ctx: Ctx, payload: dict[str, Any]) -> None:
    """Out of attempts. The message stays readable in the inbox; only the push channel gave up."""
    with ctx.db.tx() as c:
        c.run("UPDATE message_deliveries SET status = 'failed' WHERE seq = ? AND status = 'pending'", (int(payload["seq"]),))
