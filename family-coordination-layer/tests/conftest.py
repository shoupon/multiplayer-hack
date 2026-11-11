from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from fcl.api import create_app
from fcl.config import Settings
from fcl.context import Ctx
from fcl.db import Database, open_database

T0 = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Api:
    """Test client that takes a bearer token per call and returns (status, json)."""

    def __init__(self, client: TestClient):
        self.client = client

    def call(self, method: str, path: str, token: Optional[str] = None, json: Any = None,
             idem: Optional[str] = None, params: Optional[dict] = None) -> tuple[int, Any]:
        headers = {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        if idem:
            headers["idempotency-key"] = idem
        r = self.client.request(method, path, headers=headers, json=json, params=params)
        return r.status_code, (r.json() if r.content else None)

    def get(self, path: str, token: Optional[str] = None, **params: Any) -> tuple[int, Any]:
        return self.call("GET", path, token, params=params or None)

    def post(self, path: str, token: Optional[str] = None, json: Any = None, idem: Optional[str] = None) -> tuple[int, Any]:
        return self.call("POST", path, token, json=json if json is not None else {}, idem=idem)

    def delete(self, path: str, token: Optional[str] = None) -> tuple[int, Any]:
        return self.call("DELETE", path, token)

    def ok(self, method: str, path: str, token: Optional[str] = None, **kw: Any) -> Any:
        status, body = self.call(method, path, token, **kw)
        assert status < 300, (status, body)
        return body


@dataclass
class Env:
    api: Api
    ctx: Ctx
    clock: FakeClock
    db_path: Path


def make_ctx(db_path: Path, clock: FakeClock, **settings: Any) -> Ctx:
    s = Settings(db_path=db_path, run_worker=False, **settings)
    return Ctx(open_database(db_path), s, b"\x07" * 32, clock)


@pytest.fixture
def env(tmp_path: Path):
    clock = FakeClock()
    ctx = make_ctx(tmp_path / "fcl.db", clock)
    with TestClient(create_app(ctx)) as client:
        yield Env(Api(client), ctx, clock, tmp_path / "fcl.db")
    ctx.db.close()


ALL_AGENT_ACTIONS = ["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]


@dataclass
class Member:
    user_id: str
    user_token: str
    agent_id: str = ""
    agent_token: str = ""


@dataclass
class Family:
    id: str
    husband: Member
    wife: Member
    extra: dict[str, Member] = field(default_factory=dict)


def signup(api: Api, name: str) -> Member:
    body = api.ok("POST", "/v1/users", json={"display_name": name})
    return Member(body["id"], body["token"])


def join(api: Api, admin: Member, family_id: str, member: Member) -> None:
    inv = api.ok("POST", f"/v1/families/{family_id}/invitations", admin.user_token, json={})
    api.ok("POST", "/v1/invitations/accept", member.user_token, json={"code": inv["code"]})


def register(api: Api, m: Member, family_id: str, provider: str, grants: list[dict]) -> None:
    body = api.ok("POST", "/v1/agents", m.user_token, json={
        "display_name": f"{provider} agent", "provider": provider,
        "capabilities": ["receive_message", "propose_schedule"], "transports": ["inbox"],
        "grants": [{"family_id": family_id, **g} for g in grants],
    })
    m.agent_id, m.agent_token = body["id"], body["token"]


def family_grants(wife_needs_approval: bool = False) -> tuple[list[dict], list[dict]]:
    husband = [{"action": a} for a in ALL_AGENT_ACTIONS]
    wife = [{"action": a, "requires_approval": wife_needs_approval and a == "task:respond"} for a in ALL_AGENT_ACTIONS]
    return husband, wife


@pytest.fixture
def family(env: Env) -> Family:
    return build_family(env.api)


def build_family(api: Api, wife_needs_approval: bool = False) -> Family:
    husband, wife = signup(api, "Husband"), signup(api, "Wife")
    fam = api.ok("POST", "/v1/families", husband.user_token, json={"name": "The Lins"})
    join(api, husband, fam["id"], wife)
    hg, wg = family_grants(wife_needs_approval)
    register(api, husband, fam["id"], "custom-mcp", hg)
    register(api, wife, fam["id"], "companion", wg)
    return Family(fam["id"], husband, wife)


def run_jobs(env: Env) -> int:
    from fcl import worker

    return worker.run_due(env.ctx)


def inbox(env: Env, m: Member, after: int = 0) -> list[dict]:
    return env.api.ok("GET", f"/v1/agents/{m.agent_id}/inbox", m.agent_token, params={"after": after})["items"]


def text(env: Env, sender: Member, to: Member, family_id: str, body: str, idem: str) -> tuple[int, Any]:
    return env.api.post("/v1/messages", sender.agent_token,
                        {"family_id": family_id, "to_agent_id": to.agent_id, "type": "message.text", "payload": {"text": body}},
                        idem=idem)


DINNER_AT_7 = {"proposed_time": "2026-11-13T19:00:00+08:00", "duration_minutes": 90}
DINNER_AT_730 = {"proposed_time": "2026-11-13T19:30:00+08:00", "duration_minutes": 90}


def propose_dinner(env: Env, fam: Family, idem: str = "dinner-1", hours: int = 10) -> dict:
    status, task = env.api.post("/v1/tasks", fam.husband.agent_token, {
        "family_id": fam.id, "invitees": [fam.wife.agent_id], "intent": "schedule_dinner",
        "proposal": DINNER_AT_7, "expires_at": (env.clock.now + timedelta(hours=hours)).isoformat(),
    }, idem=idem)
    assert status in (200, 201), task
    return task


def respond(env: Env, m: Member, task_id: str, kind: str, version: int, idem: str, proposal: Optional[dict] = None) -> tuple[int, Any]:
    body: dict[str, Any] = {"kind": kind, "proposal_version": version}
    if proposal is not None:
        body["proposal"] = proposal
    return env.api.post(f"/v1/tasks/{task_id}/responses", m.agent_token, body, idem=idem)
