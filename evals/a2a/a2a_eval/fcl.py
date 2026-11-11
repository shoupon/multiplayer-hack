"""FCL REST client for fixture setup, harness-driven events, and scoring.

Every FCL request shape the harness depends on lives in this module, written against
docs/DESIGN.md section 12. When the built API differs, the first run fails here with the
endpoint and the keys it actually got, and this module is the only one to fix.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .domain import Family, Member, Pair, Role, TaskView


class FclHttpError(Exception):
    def __init__(self, status: int, body: str, label: str) -> None:
        super().__init__(f"{label} -> {status}: {body[:300]}")
        self.status = status


class FclRest:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30)

    async def request(self, method: str, path: str, *, token: str | None = None, body: Any = None, idempotent: bool = False) -> Any:
        headers: dict[str, str] = {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        if idempotent:
            headers["idempotency-key"] = str(uuid.uuid4())
        res = await self._http.request(method, f"/v1{path}", headers=headers, json=body)
        if res.is_error:
            raise FclHttpError(res.status_code, res.text, f"{method} /v1{path}")
        return res.json() if res.content else None

    async def aclose(self) -> None:
        await self._http.aclose()


def _at(value: Any, path: str) -> Any:
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _need(value: Any, path: str, label: str) -> str:
    v = _at(value, path)
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        return str(v)
    keys = ", ".join(value) if isinstance(value, dict) else type(value).__name__
    raise RuntimeError(f'FCL contract mismatch: {label} has no "{path}" (got: {keys}). Fix evals/a2a/a2a_eval/fcl.py.')


def _items(value: Any, label: str) -> list[Any]:
    items = _at(value, "items")
    if isinstance(items, list):
        return items
    raise RuntimeError(f'FCL contract mismatch: {label} has no "items" list. Fix evals/a2a/a2a_eval/fcl.py.')


async def preflight(rest: FclRest) -> None:
    try:
        await rest.request("GET", "/me")
    except FclHttpError as err:
        if err.status == 401:
            return
        raise RuntimeError(f"FCL at {rest.base_url} answered GET /v1/me with {err}") from err
    except httpx.HTTPError as err:
        raise RuntimeError(f"FCL is not reachable at {rest.base_url}: {err}") from err
    raise RuntimeError("GET /v1/me answered without a token. Refusing to run against an FCL that skips authentication.")


NAMES: dict[Role, str] = {"husband": "Ben", "wife": "Ann"}
AGENT_ACTIONS = (
    "agent:message:send",
    "agent:message:receive",
    "family:members:discover",
    "task:propose",
    "task:respond",
)


async def _create_user(rest: FclRest, role: Role) -> tuple[str, str]:
    user = await rest.request("POST", "/users", body={"display_name": NAMES[role]})
    return _need(user, "id", "POST /v1/users"), _need(user, "token", "POST /v1/users")


async def _register_agent(rest: FclRest, name: str, user_token: str, provider: str) -> tuple[str, str]:
    agent = await rest.request(
        "POST",
        "/agents",
        token=user_token,
        body={
            "display_name": f"{name}'s assistant",
            "provider": provider,
            "capabilities": ["receive_message", "propose_schedule", "request_approval"],
            "transports": ["inbox", "mcp"],
        },
    )
    return _need(agent, "id", "POST /v1/agents"), _need(agent, "token", "POST /v1/agents")


async def setup_family(rest: FclRest, pair: Pair, *, wife_needs_approval: bool) -> Family:
    """A fresh family per episode: two users, one family, one agent each, every grant the dinner flow needs."""
    users = {role: await _create_user(rest, role) for role in ("husband", "wife")}
    h_token = users["husband"][1]
    family = await rest.request("POST", "/families", token=h_token, body={"name": f"eval-{uuid.uuid4().hex[:8]}"})
    family_id = _need(family, "id", "POST /v1/families")
    invitation = await rest.request("POST", f"/families/{family_id}/invitations", token=h_token, body={})
    await rest.request("POST", "/invitations/accept", token=users["wife"][1], body={"code": _need(invitation, "code", "POST invitations")})

    members: dict[Role, Member] = {}
    for role in ("husband", "wife"):
        user_id, user_token = users[role]
        provider = "anthropic" if pair[role] == "claude" else "openai"
        agent_id, agent_token = await _register_agent(rest, NAMES[role], user_token, provider)
        members[role] = Member(NAMES[role], user_id, user_token, agent_id, agent_token)
        for action in AGENT_ACTIONS:
            await rest.request(
                "POST",
                "/grants",
                token=user_token,
                body={
                    "agent_id": agent_id,
                    "family_id": family_id,
                    "action": action,
                    "requires_approval": role == "wife" and action == "task:respond" and wife_needs_approval,
                },
            )
    return Family(family_id, members["husband"], members["wife"])


async def list_tasks(rest: FclRest, user_token: str) -> list[TaskView]:
    tasks: list[TaskView] = []
    for summary in _items(await rest.request("GET", "/tasks", token=user_token), "GET /v1/tasks"):
        task_id = _need(summary, "id", "GET /v1/tasks item")
        raw = await rest.request("GET", f"/tasks/{task_id}", token=user_token)
        tasks.append(TaskView(task_id, _need(raw, "state", "GET /v1/tasks/{id}"), _at(raw, "proposal"), raw))
    return tasks


async def send_text(rest: FclRest, family: Family, sender: Member, recipient: Member, text: str) -> None:
    """Plays a compromised or injected sender. The text reaches the recipient's inbox through FCL's
    normal authorized path, exactly as a real agent's message would."""
    await rest.request(
        "POST",
        "/messages",
        token=sender.agent_token,
        idempotent=True,
        body={"family_id": family.family_id, "recipient": {"agent_id": recipient.agent_id}, "type": "message.text", "payload": {"text": text}},
    )


async def approve_pending(rest: FclRest, member: Member) -> int:
    """Plays the human tapping "approve" in the companion app, with a user token as FCL requires."""
    pending = _items(await rest.request("GET", "/approvals?status=pending", token=member.user_token), "GET /v1/approvals")
    for approval in pending:
        approval_id = _need(approval, "id", "GET /v1/approvals item")
        await rest.request("POST", f"/approvals/{approval_id}/decisions", token=member.user_token, body={"decision": "grant"})
    return len(pending)
