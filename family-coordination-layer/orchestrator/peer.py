"""A scripted family member for trying the orchestrator against a local FCL server.

    python orchestrator/peer.py seed      # two people, one family, two agents; prints the orchestrator's link
    python orchestrator/peer.py propose   # Mom's agent proposes dinner to the orchestrator
    python orchestrator/peer.py show      # what Mom's agent sees: its inbox and its tasks

The orchestrator belongs to "Me". The peer is "Mom's agent". State (tokens for the scripted side only) is kept in
.data/orchestrator-peer.json, never in the orchestrator's directories.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

BASE = os.environ.get("FCL_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
STATE = Path(__file__).resolve().parents[1] / ".data" / "orchestrator-peer.json"
ACTIONS = ["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]
FRIDAY_7PM = "2026-11-13T19:00:00+08:00"


def call(method: str, path: str, token: Optional[str] = None, body: Any = None, idem: Optional[str] = None,
         **params: Any) -> Any:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if idem:
        headers["idempotency-key"] = idem
    if os.environ.get("FCL_SIGNUP_TOKEN") and path == "/v1/users":
        headers["x-signup-token"] = os.environ["FCL_SIGNUP_TOKEN"]
    r = httpx.request(method, BASE + path, json=body, params=params or None, headers=headers, timeout=10)
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code} {r.text}")
    return r.json() if r.content else None


def seed() -> None:
    me = call("POST", "/v1/users", body={"display_name": "Me"})
    mom = call("POST", "/v1/users", body={"display_name": "Mom"})
    family = call("POST", "/v1/families", me["token"], {"name": "Home"})
    invite = call("POST", f"/v1/families/{family['id']}/invitations", me["token"], {})
    call("POST", "/v1/invitations/accept", mom["token"], {"code": invite["code"]})
    grants = [{"family_id": family["id"], "action": a} for a in ACTIONS]
    orchestrator = call("POST", "/v1/agents", me["token"], {
        "display_name": "Family orchestrator", "provider": "claude-code", "transports": ["mcp"],
        "capabilities": ["propose_schedule", "receive_message"], "grants": grants})
    peer = call("POST", "/v1/agents", mom["token"], {
        "display_name": "Mom's agent", "provider": "script", "transports": ["inbox"],
        "capabilities": ["propose_schedule", "receive_message"], "grants": grants})
    link = call("POST", f"/v1/agents/{orchestrator['id']}/links", me["token"], {"ttl_hours": 24})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"family_id": family["id"], "me_token": me["token"], "orchestrator_id": orchestrator["id"],
                                 "peer_id": peer["id"], "peer_token": peer["token"]}, indent=2))
    STATE.chmod(0o600)
    print(f"family {family['id']}: orchestrator {orchestrator['id']}, Mom's agent {peer['id']}")
    print(f"orchestrator link (give it to setup.sh): {link['url']}")


def propose() -> None:
    s = json.loads(STATE.read_text())
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    task = call("POST", "/v1/tasks", s["peer_token"], {
        "family_id": s["family_id"], "invitees": [s["orchestrator_id"]], "intent": "schedule_dinner",
        "expires_at": expires, "proposal": {"proposed_time": FRIDAY_7PM, "duration_minutes": 90,
                                            "location": "Mom's place", "note": "Dinner Friday at 7?"}},
        idem=f"peer-dinner-{int(datetime.now().timestamp())}")
    print(f"Mom's agent proposed task {task['id']}: dinner {FRIDAY_7PM}, state {task['state']}")


def show() -> None:
    s = json.loads(STATE.read_text())
    inbox = call("GET", f"/v1/agents/{s['peer_id']}/inbox", s["peer_token"], unacknowledged="true")
    for item in inbox["items"]:
        m = item["message"]
        print(f"inbox: {m['type']} from {m.get('sender', {}).get('agent_id')}: {json.dumps(m['payload'])}")
    for t in call("GET", "/v1/tasks", s["peer_token"])["items"]:
        print(f"task {t['id']} {t['intent']} {t['state']} v{t['proposal_version']}: {json.dumps(t['proposal'])}")


if __name__ == "__main__":
    {"seed": seed, "propose": propose, "show": show}[sys.argv[1] if len(sys.argv) > 1 else "show"]()
