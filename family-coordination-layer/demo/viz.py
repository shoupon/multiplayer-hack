"""Step-by-step visualizer for the FCL demos, served on localhost.

`make viz` (or `python -m demo.viz`) serves a page at http://127.0.0.1:8001. Pick a scenario
(the hackathon celebration or the two-agent dinner); each press of "Run live demo" runs it
against a fresh FCL server process and records every REST call, MCP tool call, webhook push,
mock outside-service call and server crash, with the task's state after each one. The page
replays that recording as a sequence diagram. Open /#dinner or /#celebration to preselect one.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from demo import celebration, dinner

PAGE = Path(__file__).with_name("viz.html")
SECRET = re.compile(r"^(fcl_u_|fcl_a_|fcl_inv_|whsec_|tok_)")


@dataclass(frozen=True)
class Scenario:
    """One demo the page can run: its lanes (left to right) and the copy around the diagram."""

    id: str
    title: str
    blurb: str
    lanes: list[dict[str, str]]
    vias: list[str]
    task_hint: str
    passed: str
    run: Callable[[dinner.Trace], Awaitable[Path]] = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "run"}


def lane(id: str, name: str, sub: str, tone: str, kind: str, mark: str) -> dict[str, str]:
    return {"id": id, "name": name, "sub": sub, "tone": tone, "kind": kind, "mark": mark}


FCL_LANE = lane("fcl", "FCL", "coordination server · SQLite", "fcl", "server", "FCL")
SCENARIOS: dict[str, Scenario] = {s.id: s for s in (
    Scenario(
        "celebration", "Hackathon celebration dinner",
        "Chloe types a goal. Three people's agents gather calendars, budgets and allergies over FCL, one asks its "
        "owner for calendar access, and Chloe's concierge books and pays only after Chloe approves. Agents are "
        "scripted; every arrow is a real call. Empty inbox and approval polls are hidden.",
        [lane("chloe", "Chloe", "host · user token", "p1", "person", "C"),
         lane("concierge", "Chloe's concierge", "agent · MCP over stdio", "p1", "agent", "AI"),
         FCL_LANE,
         lane("alice_agent", "Alice's assistant", "agent · MCP over stdio", "p2", "agent", "AI"),
         lane("alice", "Alice", "guest · user token", "p2", "person", "A"),
         lane("ben_agent", "Ben's assistant", "agent · REST", "p3", "agent", "AI"),
         lane("ben", "Ben", "guest · approver", "p3", "person", "B"),
         lane("outside", "Outside world", "calendars · TableBook · email (mocks)", "ext", "server", "EXT")],
        ["REST", "MCP", "mock"],
        "No task yet. It appears when Chloe's concierge proposes a restaurant to the other agents.",
        "Run passed. The table is booked, everyone got the email, and the task is COMPLETED with the booking as evidence.",
        celebration.run,
    ),
    Scenario(
        "dinner", "Dinner coordination",
        "Two personal agents from different providers agree on dinner. Both agents are scripted. Every arrow is a "
        "real call to a live FCL server started on this laptop for the run.",
        [lane("husband", "Husband", "person · user token", "p1", "person", "H"),
         lane("husband_agent", "Husband's assistant", "agent · MCP over stdio", "p1", "agent", "AI"),
         FCL_LANE,
         lane("companion", "Wife's companion", "agent · REST + webhook", "p2", "agent", "AI"),
         lane("wife", "Wife", "person · approver", "p2", "person", "W")],
        ["REST", "MCP", "webhook"],
        "No task yet. It appears when the husband's agent proposes dinner.",
        "Run passed. Both people see AGREED at 19:30.",
        dinner.run,
    ),
)}


def redact(value: Any) -> Any:
    """Tokens, invitation codes and webhook secrets are shown by prefix only."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and SECRET.match(value):
        return value[:12] + "…"
    return value


class Recorder(dinner.Trace):
    """Records the run as events. Each event carries the task as it stood right after it."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.task_url: Optional[str] = None
        self.task_auth: dict[str, str] = {}
        self.task: Optional[dict[str, Any]] = None

    def _refresh(self) -> None:
        if self.task_url is None:
            return
        try:
            r = httpx.get(self.task_url, headers=self.task_auth, timeout=2)
            if r.status_code == 200:
                self.task = r.json()
        except httpx.HTTPError:
            pass  # the server is down between crash and restart; keep the last known state

    def _add(self, event: dict[str, Any], refresh: bool = False) -> None:
        with self.lock:
            if refresh:
                self._refresh()
            event.update(seq=len(self.events) + 1, t=round(time.monotonic() - self.t0, 3), task=self.task)
            self.events.append(redact(event))

    def section(self, title: str) -> None:
        self._add({"kind": "section", "title": title})

    def note(self, lane: str, text: str, detail: Any = None) -> None:
        self._add({"kind": "note", "lane": lane, "text": text, "detail": detail})

    def call(self, frm: str, via: str, label: str, request: Any, status: int, response: Any, to: str = "fcl") -> None:
        self._add({"kind": "call", "from": frm, "to": to, "via": via, "label": label,
                   "request": request, "status": status, "response": response}, refresh=to == "fcl")

    def push(self, label: str, status: int, verified: bool, body: Any) -> None:
        self._add({"kind": "push", "from": "fcl", "to": "companion", "via": "webhook", "label": label,
                   "status": status, "verified": verified, "request": body, "response": None}, refresh=True)

    def server(self, up: bool, text: str) -> None:
        self._add({"kind": "server", "lane": "fcl", "up": up, "text": text}, refresh=up)

    def watch(self, base_url: str, token: str, task_id: str) -> None:
        with self.lock:
            self.task_url, self.task_auth = f"{base_url}/v1/tasks/{task_id}", {"authorization": f"Bearer {token}"}
            self._refresh()
            if self.events and self.events[-1]["task"] is None:
                self.events[-1]["task"] = self.task  # the call that created the task


@dataclass
class Run:
    id: str
    scenario: Scenario
    recorder: Recorder = field(default_factory=Recorder)
    status: Literal["running", "passed", "failed"] = "running"
    error: Optional[str] = None
    data_dir: Optional[str] = None

    def work(self) -> None:
        try:
            self.data_dir = str(asyncio.run(self.scenario.run(self.recorder)))
            self.status = "passed"
        except BaseException as e:  # the demo signals failure with SystemExit and AssertionError
            self.error = f"{type(e).__name__}: {e}"
            self.status = "failed"

    def page(self, after: int) -> dict[str, Any]:
        with self.recorder.lock:
            events = self.recorder.events[after:]
        return {"id": self.id, "scenario": self.scenario.id, "status": self.status, "error": self.error,
                "data_dir": self.data_dir, "events": events}


def create_app() -> FastAPI:
    app = FastAPI(title="FCL demo visualizer", docs_url=None, redoc_url=None)
    runs: dict[str, Run] = {}
    latest: list[Run] = []
    start_lock = threading.Lock()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/scenarios")
    def scenarios() -> list[dict[str, Any]]:
        return [s.public() for s in SCENARIOS.values()]

    @app.post("/api/runs", status_code=201)
    def start(scenario: str = Query(next(iter(SCENARIOS)))) -> dict[str, Any]:
        if scenario not in SCENARIOS:
            raise HTTPException(404, {"code": "unknown_scenario"})
        with start_lock:
            if latest and latest[-1].status == "running":
                raise HTTPException(409, {"code": "run_in_progress", "id": latest[-1].id,
                                          "scenario": latest[-1].scenario.id})
            run = Run(id=uuid.uuid4().hex[:12], scenario=SCENARIOS[scenario])
            runs[run.id] = run
            latest.append(run)
        threading.Thread(target=run.work, name=f"demo-{run.id}", daemon=True).start()
        return {"id": run.id}

    @app.get("/api/runs/latest")
    def last(after: int = 0, scenario: Optional[str] = None) -> Optional[dict[str, Any]]:
        mine = [r for r in latest if scenario is None or r.scenario.id == scenario]
        return mine[-1].page(after) if mine else None

    @app.get("/api/runs/{run_id}")
    def get(run_id: str, after: int = 0) -> dict[str, Any]:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, {"code": "not_found"})
        return run.page(after)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m demo.viz", description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    print(f"FCL demo visualizer: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
