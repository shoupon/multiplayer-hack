"""Dinner coordination between two personal agents (the brief's MVP scenario), on one laptop.

- FCL runs as its own process (`python -m fcl serve`) on a fresh SQLite file.
- The husband's agent talks to FCL only through the MCP stdio server, as an MCP assistant would.
- The wife's companion agent talks REST, receives pushes on a local signed webhook, and needs her
  approval before it answers on her behalf.
- The FCL process is killed and restarted while the wife is offline; nothing is lost.

Both agents are scripted; every call they make is real. `make demo` prints the story,
`make viz` records the same run and draws it step by step in a browser.
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
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fcl.webhooks import SIGNATURE_HEADER, verify

ROOT = Path(__file__).resolve().parents[1]
FRIDAY_7PM = "2026-11-13T19:00:00+08:00"
FRIDAY_730PM = "2026-11-13T19:30:00+08:00"
ACTIONS = ["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]

Lane = Literal["husband", "husband_agent", "fcl", "companion", "wife"]
LANE_NAMES: dict[str, str] = {"husband": "Husband", "husband_agent": "Husband's agent", "fcl": "FCL",
                              "companion": "Companion", "wife": "Wife"}


class Trace:
    """Receives the demo's story. This one prints narration; demo.viz records every call too."""

    def section(self, title: str) -> None:
        print(f"\n== {title}", flush=True)

    def __init__(self, names: Optional[dict[str, str]] = None) -> None:
        self.names = names or LANE_NAMES

    def note(self, lane: str, text: str, detail: Any = None) -> None:
        print(f"  {self.names[lane]:<18} {text}", flush=True)

    def call(self, frm: str, via: str, label: str, request: Any, status: int, response: Any, to: str = "fcl") -> None:
        pass

    def push(self, label: str, status: int, verified: bool, body: Any) -> None:
        pass

    def server(self, up: bool, text: str) -> None:
        self.note("fcl", text)

    def watch(self, base_url: str, token: str, task_id: str) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FclProcess:
    def __init__(self, db: Path, port: int, log: Path):
        self.db, self.port, self.log = db, port, log
        self.proc: Optional[subprocess.Popen[bytes]] = None
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

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)  # a crash, not a graceful shutdown
            self.proc.wait()


class Rest:
    """A REST caller acting as one lane, with that lane's bearer token."""

    def __init__(self, trace: Trace, lane: str, base_url: str, token: Optional[str] = None):
        self.trace, self.lane, self.base_url, self.token = trace, lane, base_url, token

    def __call__(self, method: str, path: str, json_body: Any = None, idem: Optional[str] = None, **params: Any) -> Any:
        headers = {}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if idem:
            headers["idempotency-key"] = idem
        r = httpx.request(method, self.base_url + path, json=json_body, params=params or None, headers=headers, timeout=10)
        body = r.json() if r.content else None
        label = f"{method} {path}" + (f"?{httpx.QueryParams(params)}" if params else "")
        self.trace.call(self.lane, "REST", label, json_body, r.status_code, body)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code} {r.text}")
        return body


class WebhookReceiver:
    """The companion agent's push endpoint. Verifies every signature before trusting a delivery."""

    def __init__(self, trace: Trace) -> None:
        self.secret = ""
        self.received: list[tuple[str, bool]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers["content-length"]))
                ok = verify(receiver.secret, self.headers.get(SIGNATURE_HEADER, ""), raw, int(time.time()))
                body = json.loads(raw)
                receiver.received.append((body["message"]["type"], ok))
                self.send_response(204 if ok else 401)
                self.end_headers()
                trace.push(body["message"]["type"], 204 if ok else 401, ok, body)

            def log_message(self, *_: Any) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}/fcl"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def wait_for(self, count: int, timeout: float = 10) -> None:
        deadline = time.time() + timeout
        while len(self.received) < count and time.time() < deadline:
            time.sleep(0.1)

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()


@asynccontextmanager
async def mcp_agent(trace: Trace, base_url: str, token: str, log: Path,
                    lane: str = "husband_agent") -> AsyncIterator[Callable[..., Awaitable[dict[str, Any]]]]:
    """Starts the FCL MCP server over stdio and returns a tool caller, as an MCP assistant host would."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "fcl.mcp_server"], cwd=str(ROOT),
                                   env={**os.environ, "FCL_BASE_URL": base_url, "FCL_AGENT_TOKEN": token})
    with log.open("a") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, **args: Any) -> dict[str, Any]:
                result = await session.call_tool(tool, args)
                text = "".join(getattr(c, "text", "") for c in result.content)
                body = text if result.is_error else (result.structured_content or json.loads(text))
                trace.call(lane, "MCP", tool, args, 500 if result.is_error else 200, body)
                if result.is_error:
                    raise SystemExit(f"{tool} failed: {text}")
                return body

            yield call


async def run(trace: Trace) -> Path:
    data = ROOT / ".data" / f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    data.mkdir(parents=True)
    fcl = FclProcess(data / "fcl.db", free_port(), data / "server.log")
    receiver: Optional[WebhookReceiver] = None
    try:
        fcl.start()
        trace.server(True, f"started on {fcl.base_url}, pid {fcl.pid}, fresh SQLite file")
        receiver = await _story(trace, fcl, data)
    finally:
        fcl.kill()
        if receiver is not None:
            receiver.close()
    return data


async def _story(trace: Trace, fcl: FclProcess, data: Path) -> WebhookReceiver:
    base = fcl.base_url

    trace.section("Setup: two people, one family, two agents from different providers")
    husband_user = Rest(trace, "husband", base)("POST", "/v1/users", {"display_name": "Husband"})
    wife_user = Rest(trace, "wife", base)("POST", "/v1/users", {"display_name": "Wife"})
    husband, wife = Rest(trace, "husband", base, husband_user["token"]), Rest(trace, "wife", base, wife_user["token"])
    family = husband("POST", "/v1/families", {"name": "The Lins"})
    invite = husband("POST", f"/v1/families/{family['id']}/invitations", {})
    wife("POST", "/v1/invitations/accept", {"code": invite["code"]})
    husband_agent = husband("POST", "/v1/agents", {
        "display_name": "Husband's assistant", "provider": "custom-mcp", "transports": ["mcp"],
        "capabilities": ["propose_schedule", "receive_message"],
        "grants": [{"family_id": family["id"], "action": a} for a in ACTIONS]})
    trace.note("husband", f"registered agent {husband_agent['id']}; it may propose and answer on his behalf")
    wife_agent = wife("POST", "/v1/agents", {
        "display_name": "Wife's companion", "provider": "fcl-companion", "transports": ["inbox"],
        "capabilities": ["receive_message", "request_approval"],
        "grants": [{"family_id": family["id"], "action": a, "requires_approval": a == "task:respond"} for a in ACTIONS]})
    trace.note("wife", f"registered agent {wife_agent['id']}; its answers need her approval (task:respond)")
    companion = Rest(trace, "companion", base, wife_agent["token"])

    async with mcp_agent(trace, base, husband_agent["token"], data / "mcp.log") as husband_mcp:
        trace.section("1-5. Husband asks his assistant to arrange dinner Friday at 7 PM")
        trace.note("husband", "\"Set up dinner with my wife Friday at 7.\" (scripted; no LLM in this demo)")
        found = (await husband_mcp("fcl_list_family_agents", family_id=family["id"]))["items"]
        target = next(a for a in found if a["owner"]["user_id"] == wife_user["id"])
        trace.note("husband_agent", f"discovered {target['display_name']} ({target['availability']['mode']} delivery)")
        expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        proposal = dict(family_id=family["id"], invitee_agent_ids=[target["id"]], intent="schedule_dinner",
                        proposed_time=FRIDAY_7PM, duration_minutes=90, expires_at=expires, idempotency_key="dinner-friday")
        task = await husband_mcp("fcl_create_coordination_task", **proposal)
        trace.watch(base, husband_user["token"], task["id"])
        retry = await husband_mcp("fcl_create_coordination_task", **proposal)
        assert retry["id"] == task["id"], "retry must not create a second task"
        trace.note("fcl", f"task {task['id']} is {task['state']}; the retried call returned the same task")

        trace.section("Wife's companion is offline. FCL crashes and restarts")
        fcl.kill()
        trace.server(False, "process killed with SIGKILL while the proposal waits undelivered")
        fcl.start()
        trace.server(True, f"restarted, pid {fcl.pid}, same SQLite file; the proposal is still queued")

        trace.section("6-8. Wife's companion comes online, reads the proposal, wants 7:30 PM")
        [item] = companion("GET", f"/v1/agents/{wife_agent['id']}/inbox", after=0)["items"]
        message = item["message"]
        trace.note("companion", f"inbox: {message['type']} for {message['payload']['proposal']['proposed_time']} "
                                f"(content_trust: {message['content_trust']})")
        receiver = WebhookReceiver(trace)
        hook = wife("POST", "/v1/webhooks", {"agent_id": wife_agent["id"], "url": receiver.url})
        receiver.secret = hook["secret"]
        trace.note("companion", "subscribed a signed webhook for further updates")
        answer = companion("POST", f"/v1/tasks/{task['id']}/responses",
                           {"kind": "counterpropose", "proposal_version": 1,
                            "proposal": {"proposed_time": FRIDAY_730PM, "duration_minutes": 90}}, idem="counter-730")
        trace.note("fcl", f"result {answer['result']}: task is {answer['task']['state']} until the wife decides")

        [approval] = wife("GET", "/v1/approvals", status="pending")["items"]
        trace.note("wife", f"sees approval {approval['id']}: {approval['action']['kind']} to "
                           f"{approval['action']['proposal']['proposed_time']}")
        wife("POST", f"/v1/approvals/{approval['id']}/decisions", {"decision": "grant"})
        trace.note("wife", "approves that exact counterproposal; FCL applies it as version 2")
        receiver.wait_for(1)

        trace.section("9-12. The counterproposal reaches the husband's agent; he accepts")
        page = await husband_mcp("fcl_get_inbox", after=0, acknowledge=True)
        counter = page["items"][-1]["message"]
        trace.note("husband_agent", f"inbox: {counter['type']} v{counter['payload']['proposal_version']} "
                                    f"for {counter['payload']['proposal']['proposed_time']}")
        accepted = await husband_mcp("fcl_respond_to_proposal", task_id=task["id"], kind="accept",
                                     proposal_version=counter["payload"]["proposal_version"], idempotency_key="accept-v2")
        trace.note("fcl", f"result {accepted['result']}: task is {accepted['task']['state']}")

    receiver.wait_for(3)
    trace.section("13-14. Both people inspect the shared outcome")
    his, hers = husband("GET", f"/v1/tasks/{task['id']}"), wife("GET", f"/v1/tasks/{task['id']}")
    assert his["state"] == hers["state"] == "AGREED"
    assert his["outcome"] == hers["outcome"] and his["outcome"]["proposal"]["proposed_time"] == FRIDAY_730PM
    outcome = his["outcome"]["proposal"]
    trace.note("fcl", f"both views agree on {outcome['proposed_time']} for {outcome['duration_minutes']} min",
               detail=his["history"])
    for t in his["history"]:
        actor = t["actor"]["agent_id"] or t["actor"]["user_id"] or t["actor"]["kind"]
        print(f"  {'history':<18} {(t['from_state'] or '-'):<18} -> {t['to_state']:<18} {t['event']['kind']:<17} by {actor}")
    pushed = ", ".join(f"{kind} ({'signature ok' if ok else 'BAD SIGNATURE'})" for kind, ok in receiver.received)
    trace.note("companion", f"webhook pushes: {pushed}")
    assert receiver.received and all(ok for _, ok in receiver.received)
    return receiver


def main() -> None:
    data = asyncio.run(run(Trace()))
    print(f"\nDemo passed. Database and server log: {data}")


if __name__ == "__main__":
    main()
