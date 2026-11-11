"""Dinner coordination between two personal agents (the brief's MVP scenario), on one laptop.

- FCL runs as its own process (`python -m fcl serve`) on a fresh SQLite file.
- The husband's agent talks to FCL only through the MCP stdio server, as an MCP assistant would.
- The wife's companion agent talks REST, receives pushes on a local signed webhook, and needs her
  approval before it answers on her behalf.
- The FCL process is killed and restarted while the wife is offline; nothing is lost.

Run: make demo
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Awaitable

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fcl.webhooks import SIGNATURE_HEADER, verify

ROOT = Path(__file__).resolve().parents[1]
FRIDAY_7PM = "2026-11-13T19:00:00+08:00"
FRIDAY_730PM = "2026-11-13T19:30:00+08:00"
ACTIONS = ["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]


def say(who: str, text: str) -> None:
    print(f"  {who:<18} {text}", flush=True)


def heading(text: str) -> None:
    print(f"\n== {text}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FclProcess:
    def __init__(self, db: Path, port: int, log: Path):
        self.db, self.port, self.log = db, port, log
        self.proc: subprocess.Popen[bytes] | None = None
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        env = {**os.environ, "FCL_WORKER_INTERVAL_SECONDS": "0.2"}
        with self.log.open("ab") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "fcl", "serve", "--db", str(self.db), "--port", str(self.port),
                 "--allow-loopback-webhooks"],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base_url}/v1/me", timeout=1).status_code == 401:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise SystemExit(f"FCL did not start; see {self.log}")

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)  # a crash, not a graceful shutdown
        self.proc.wait()


class Rest:
    """A REST caller holding one bearer token."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url, self.token = base_url, token

    def __call__(self, method: str, path: str, json_body: Any = None, idem: str | None = None, **params: Any) -> Any:
        headers = {}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if idem:
            headers["idempotency-key"] = idem
        r = httpx.request(method, self.base_url + path, json=json_body, params=params or None, headers=headers, timeout=10)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code} {r.text}")
        return r.json() if r.content else None


class WebhookReceiver:
    """The companion agent's push endpoint. Verifies every signature before trusting a delivery."""

    def __init__(self) -> None:
        self.secret = ""
        self.received: list[tuple[str, bool]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["content-length"]))
                ok = verify(receiver.secret, self.headers.get(SIGNATURE_HEADER, ""), body, int(time.time()))
                receiver.received.append((json.loads(body)["message"]["type"], ok))
                self.send_response(204 if ok else 401)
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}/fcl"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def wait_for(self, count: int, timeout: float = 10) -> None:
        deadline = time.time() + timeout
        while len(self.received) < count and time.time() < deadline:
            time.sleep(0.1)


@asynccontextmanager
async def mcp_agent(base_url: str, token: str, log: Path) -> AsyncIterator[Callable[..., Awaitable[dict[str, Any]]]]:
    """Starts the FCL MCP server over stdio and returns a tool caller, as an MCP assistant host would."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "fcl.mcp_server"], cwd=str(ROOT),
                                   env={**os.environ, "FCL_BASE_URL": base_url, "FCL_AGENT_TOKEN": token})
    with log.open("a") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, **args: Any) -> dict[str, Any]:
                result = await session.call_tool(tool, args)
                text = "".join(getattr(c, "text", "") for c in result.content)
                if result.is_error:
                    raise SystemExit(f"{tool} failed: {text}")
                return result.structured_content or json.loads(text)

            yield call


async def run() -> None:
    data = ROOT / ".data" / f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    data.mkdir(parents=True)
    fcl = FclProcess(data / "fcl.db", free_port(), data / "server.log")
    fcl.start()
    anon = Rest(fcl.base_url)

    heading("Setup: two people, one family, two agents from different providers")
    husband_user = anon("POST", "/v1/users", {"display_name": "Husband"})
    wife_user = anon("POST", "/v1/users", {"display_name": "Wife"})
    husband, wife = Rest(fcl.base_url, husband_user["token"]), Rest(fcl.base_url, wife_user["token"])
    family = husband("POST", "/v1/families", {"name": "The Lins"})
    invite = husband("POST", f"/v1/families/{family['id']}/invitations", {})
    wife("POST", "/v1/invitations/accept", {"code": invite["code"]})
    husband_agent = husband("POST", "/v1/agents", {
        "display_name": "Husband's assistant", "provider": "custom-mcp", "transports": ["mcp"],
        "capabilities": ["propose_schedule", "receive_message"],
        "grants": [{"family_id": family["id"], "action": a} for a in ACTIONS]})
    wife_agent = wife("POST", "/v1/agents", {
        "display_name": "Wife's companion", "provider": "fcl-companion", "transports": ["inbox"],
        "capabilities": ["receive_message", "request_approval"],
        "grants": [{"family_id": family["id"], "action": a, "requires_approval": a == "task:respond"} for a in ACTIONS]})
    companion = Rest(fcl.base_url, wife_agent["token"])
    say("FCL", f"family {family['id']}, server pid {fcl.proc.pid if fcl.proc else '?'}")
    say("Husband", f"agent {husband_agent['id']} via MCP (provider label: custom-mcp)")
    say("Wife", f"agent {wife_agent['id']} via REST (provider label: fcl-companion); her answers need her approval")

    async with mcp_agent(fcl.base_url, husband_agent["token"], data / "mcp.log") as husband_mcp:
        heading("1-5. Husband asks his assistant to arrange dinner Friday at 7 PM")
        found = (await husband_mcp("fcl_list_family_agents", family_id=family["id"]))["items"]
        target = next(a for a in found if a["owner"]["user_id"] == wife_user["id"])
        say("Husband's agent", f"discovered {target['display_name']} ({target['availability']['mode']} delivery)")
        expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        task = await husband_mcp("fcl_create_coordination_task", family_id=family["id"], invitee_agent_ids=[target["id"]],
                                 intent="schedule_dinner", proposed_time=FRIDAY_7PM, duration_minutes=90,
                                 expires_at=expires, idempotency_key="dinner-friday")
        retry = await husband_mcp("fcl_create_coordination_task", family_id=family["id"], invitee_agent_ids=[target["id"]],
                                  intent="schedule_dinner", proposed_time=FRIDAY_7PM, duration_minutes=90,
                                  expires_at=expires, idempotency_key="dinner-friday")
        assert retry["id"] == task["id"], "retry must not create a second task"
        say("FCL", f"task {task['id']} is {task['state']}; the retried call returned the same task")

        heading("Wife's companion is offline. FCL crashes and restarts")
        fcl.kill()
        say("FCL", "process killed (SIGKILL)")
        fcl.start()
        say("FCL", f"restarted, pid {fcl.proc.pid if fcl.proc else '?'}, same SQLite file")

        heading("6-8. Wife's companion comes online, reads the proposal, wants 7:30 PM")
        inbox = companion("GET", f"/v1/agents/{wife_agent['id']}/inbox", after=0)
        [item] = inbox["items"]
        message = item["message"]
        say("Companion", f"inbox: {message['type']} for {message['payload']['proposal']['proposed_time']}")
        receiver = WebhookReceiver()
        hook = wife("POST", "/v1/webhooks", {"agent_id": wife_agent["id"], "url": receiver.url})
        receiver.secret = hook["secret"]
        say("Companion", "subscribed a signed webhook for further updates")
        answer = companion("POST", f"/v1/tasks/{task['id']}/responses",
                           {"kind": "counterpropose", "proposal_version": 1,
                            "proposal": {"proposed_time": FRIDAY_730PM, "duration_minutes": 90}}, idem="counter-730")
        say("FCL", f"result {answer['result']}: task is {answer['task']['state']} until the wife decides")

        [approval] = wife("GET", "/v1/approvals", status="pending")["items"]
        say("Wife", f"sees approval {approval['id']}: {approval['action']['kind']} to "
                    f"{approval['action']['proposal']['proposed_time']}")
        wife("POST", f"/v1/approvals/{approval['id']}/decisions", {"decision": "grant"})
        say("Wife", "approves that exact counterproposal")

        heading("9-12. The counterproposal reaches the husband's agent; he accepts")
        page = await husband_mcp("fcl_get_inbox", after=0, acknowledge=True)
        counter = page["items"][-1]["message"]
        say("Husband's agent", f"inbox: {counter['type']} v{counter['payload']['proposal_version']} "
                               f"for {counter['payload']['proposal']['proposed_time']}")
        accepted = await husband_mcp("fcl_respond_to_proposal", task_id=task["id"], kind="accept",
                                     proposal_version=counter["payload"]["proposal_version"], idempotency_key="accept-v2")
        say("FCL", f"result {accepted['result']}: task is {accepted['task']['state']}")

    heading("13-14. Both people inspect the shared outcome")
    receiver.wait_for(3)
    his, hers = husband("GET", f"/v1/tasks/{task['id']}"), wife("GET", f"/v1/tasks/{task['id']}")
    assert his["state"] == hers["state"] == "AGREED"
    assert his["outcome"] == hers["outcome"] and his["outcome"]["proposal"]["proposed_time"] == FRIDAY_730PM
    say("Both", f"agreed on {his['outcome']['proposal']['proposed_time']} for {his['outcome']['proposal']['duration_minutes']} min")
    for t in his["history"]:
        actor = t["actor"]["agent_id"] or t["actor"]["user_id"] or t["actor"]["kind"]
        say("history", f"{(t['from_state'] or '-'):<18} -> {t['to_state']:<18} {t['event']['kind']:<17} by {actor}")
    pushed = ", ".join(f"{kind} ({'signature ok' if ok else 'BAD SIGNATURE'})" for kind, ok in receiver.received)
    say("Companion", f"webhook pushes: {pushed}")
    assert receiver.received and all(ok for _, ok in receiver.received)

    fcl.kill()
    print(f"\nDemo passed. Database and server log: {data}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
