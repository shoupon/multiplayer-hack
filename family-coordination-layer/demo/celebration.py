"""A hackathon celebration dinner, planned by three people's agents over FCL, on one laptop.

Chloe types a goal. Her concierge opens a room (an FCL family) and Chloe invites Alice and Ben, who
approve by joining and connecting their own agents. Each agent knows something the others don't:

- Alice's assistant (MCP) already reads Alice's calendar and knows Alice's budget.
- Ben's assistant (REST companion) knows Ben is vegetarian with a severe peanut allergy, but cannot
  see Ben's calendar. When it receives Alice's and Chloe's free evenings, it asks Ben through FCL for
  read access to that one week, then lines up all three calendars.
- Chloe's concierge (MCP) finds a restaurant that fits everyone, holds a table, gets the other agents
  to agree, asks Chloe to approve the deposit and enter a card, books, and completes the task with
  the booking as evidence. TableBook emails everyone; the decision log is written to disk.

Run: make demo-celebration            (owners' decisions are auto-approved)
     make demo-celebration ARGS=--interactive   (you approve and type the card)
     make viz                         (pick "Hackathon celebration" to replay it step by step)
"""

from __future__ import annotations

import argparse
import asyncio
import email
import email.policy
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from demo.decision_log import DecisionLog
from demo.dinner import ROOT, FclProcess, Rest, Trace, free_port, mcp_agent
from demo.outside import (EVENING_END, EVENING_START, TPE, Calendar, Card, Event, Hold, Outbox, TableBook, at,
                          day_label)

DEFAULT_GOAL = "find a restaurant and dinner so we can celebrate winning compile hackathon for next week"
ACTIONS = ["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]
TEST_CARD = "4242 4242 4242 4242"
LANES = {"chloe": "Chloe", "concierge": "Chloe's concierge", "fcl": "FCL", "alice_agent": "Alice's assistant",
         "alice": "Alice", "ben_agent": "Ben's assistant", "ben": "Ben", "outside": "Outside world"}


class HideEmptyPolls(Trace):
    """Agents poll their inbox and owners their approvals every round. Empty polls would bury the story."""

    def __init__(self, inner: Trace):
        super().__init__(LANES)
        self.inner = inner

    def section(self, title: str) -> None:
        self.inner.section(title)

    def note(self, lane: str, text: str, detail: Any = None) -> None:
        self.inner.note(lane, text, detail)

    def call(self, frm: str, via: str, label: str, request: Any, status: int, response: Any, to: str = "fcl") -> None:
        if not (isinstance(response, dict) and response.get("items") == []):
            self.inner.call(frm, via, label, request, status, response, to)

    def server(self, up: bool, text: str) -> None:
        self.inner.server(up, text)

    def watch(self, base_url: str, token: str, task_id: str) -> None:
        self.inner.watch(base_url, token, task_id)


def days(ds: Any) -> str:
    return ", ".join(day_label(d) for d in sorted(ds)) or "none"


def whose(names: Any) -> str:
    return " and ".join(f"{n}'s" for n in names)


def nt(minor: int) -> str:
    return f"NT${minor // 100:,}"


def parse_days(values: Any, first: date, last: date) -> list[date]:
    """Dates from another agent are untrusted: keep only well-formed dates inside the week that was asked about."""
    out = set()
    for v in values if isinstance(values, list) else []:
        try:
            d = date.fromisoformat(str(v))
        except ValueError:
            continue
        if first <= d <= last:
            out.add(d)
    return sorted(out)


@dataclass(frozen=True)
class Brief:
    goal: str
    occasion: str
    first: date
    last: date

    @property
    def window(self) -> dict[str, str]:
        return {"window_start": datetime.combine(self.first, time.min, TPE).isoformat(),
                "window_end": datetime.combine(self.last + timedelta(days=1), time.min, TPE).isoformat()}


def draft_brief(goal: str, today: date) -> Brief:
    found = re.search(r"celebrate (.+?)(?: for | next |$)", goal)
    occasion = found.group(1) if found else "together"
    first = today + timedelta(days=7 - today.weekday()) if "next week" in goal else today + timedelta(days=1)
    return Brief(goal, occasion, first, first + timedelta(days=6))


# ---- how each agent reaches FCL ---------------------------------------------------------


class Fcl(Protocol):
    async def inbox(self, after: int) -> tuple[list[dict[str, Any]], int]: ...
    async def send(self, family_id: str, to: str, message_type: str, payload: dict[str, Any], idem: str) -> Any: ...
    async def propose(self, family_id: str, invitees: list[str], intent: str, proposal: dict[str, Any], expires_at: str,
                      idem: str) -> dict[str, Any]: ...
    async def respond(self, task_id: str, kind: str, version: int, idem: str,
                      evidence: Optional[dict[str, Any]] = None, reason: Optional[str] = None) -> dict[str, Any]: ...
    async def ask_owner(self, family_id: str, scope: str, params: dict[str, Any], purpose: str, expires_at: str,
                        idem: str, task_id: Optional[str] = None) -> dict[str, Any]: ...
    async def approval(self, approval_id: str) -> dict[str, Any]: ...


class McpFcl:
    """An agent whose assistant host speaks MCP: every call is an FCL MCP tool call."""

    def __init__(self, call: Callable[..., Any]):
        self.call = call

    async def inbox(self, after: int) -> tuple[list[dict[str, Any]], int]:
        page = await self.call("fcl_get_inbox", after=after, limit=50, acknowledge=True)
        return page["items"], page["next_cursor"]

    async def send(self, family_id: str, to: str, message_type: str, payload: dict[str, Any], idem: str) -> Any:
        return await self.call("fcl_send_message", family_id=family_id, to_agent_id=to, message_type=message_type,
                               payload=payload, idempotency_key=idem)

    async def propose(self, family_id: str, invitees: list[str], intent: str, proposal: dict[str, Any], expires_at: str,
                      idem: str) -> dict[str, Any]:
        return await self.call("fcl_create_coordination_task", family_id=family_id, invitee_agent_ids=invitees,
                               intent=intent, expires_at=expires_at, idempotency_key=idem, **proposal)

    async def respond(self, task_id: str, kind: str, version: int, idem: str,
                      evidence: Optional[dict[str, Any]] = None, reason: Optional[str] = None) -> dict[str, Any]:
        extra = {k: v for k, v in (("evidence", evidence), ("reason", reason)) if v is not None}
        return await self.call("fcl_respond_to_proposal", task_id=task_id, kind=kind, proposal_version=version,
                               idempotency_key=idem, **extra)

    async def ask_owner(self, family_id: str, scope: str, params: dict[str, Any], purpose: str, expires_at: str,
                        idem: str, task_id: Optional[str] = None) -> dict[str, Any]:
        extra = {"task_id": task_id} if task_id else {}
        return await self.call("fcl_request_permission", family_id=family_id, scope=scope, params=params,
                               purpose=purpose, expires_at=expires_at, idempotency_key=idem, **extra)

    async def approval(self, approval_id: str) -> dict[str, Any]:
        return await self.call("fcl_get_approval", approval_id=approval_id)


class RestFcl:
    """A companion agent that calls the FCL REST API directly."""

    def __init__(self, rest: Rest, agent_id: str):
        self.rest, self.agent_id = rest, agent_id

    async def inbox(self, after: int) -> tuple[list[dict[str, Any]], int]:
        page = self.rest("GET", f"/v1/agents/{self.agent_id}/inbox", after=after, limit=50)
        if page["items"]:
            self.rest("POST", f"/v1/agents/{self.agent_id}/inbox/ack", {"through": page["next_cursor"]})
        return page["items"], page["next_cursor"]

    async def send(self, family_id: str, to: str, message_type: str, payload: dict[str, Any], idem: str) -> Any:
        return self.rest("POST", "/v1/messages", {"family_id": family_id, "to_agent_id": to, "type": message_type,
                                                  "payload": payload}, idem=idem)

    async def propose(self, family_id: str, invitees: list[str], intent: str, proposal: dict[str, Any], expires_at: str,
                      idem: str) -> dict[str, Any]:
        return self.rest("POST", "/v1/tasks", {"family_id": family_id, "invitees": invitees, "intent": intent,
                                               "proposal": proposal, "expires_at": expires_at}, idem=idem)

    async def respond(self, task_id: str, kind: str, version: int, idem: str,
                      evidence: Optional[dict[str, Any]] = None, reason: Optional[str] = None) -> dict[str, Any]:
        body = {"kind": kind, "proposal_version": version, "evidence": evidence, "reason": reason}
        return self.rest("POST", f"/v1/tasks/{task_id}/responses", {k: v for k, v in body.items() if v is not None},
                         idem=idem)

    async def ask_owner(self, family_id: str, scope: str, params: dict[str, Any], purpose: str, expires_at: str,
                        idem: str, task_id: Optional[str] = None) -> dict[str, Any]:
        body = {"family_id": family_id, "scope": scope, "params": params, "purpose": purpose, "expires_at": expires_at,
                "task_id": task_id}
        return self.rest("POST", "/v1/approvals", {k: v for k, v in body.items() if v is not None}, idem=idem)

    async def approval(self, approval_id: str) -> dict[str, Any]:
        return self.rest("GET", f"/v1/approvals/{approval_id}")


# ---- the agents ------------------------------------------------------------------------


def describe(m: dict[str, Any]) -> str:
    t, p = m["type"], m["payload"]
    if t == "information.request":
        return f'asks about {p["topic"]}: {p["question"]}'
    if t == "information.response":
        return f'answers {p["topic"]}: {p["answer"]}'
    if t in ("coordination.proposal", "coordination.counterproposal"):
        return f'proposes v{p["proposal_version"]}: {p["proposal"].get("proposed_time")} at {p["proposal"].get("location")}'
    if t == "coordination.accept":
        return f'accepts v{p["proposal_version"]}'
    if t == "coordination.decline":
        return f'declines v{p["proposal_version"]}: {p.get("reason")}'
    if t.startswith("approval."):
        return f'{t} ({p.get("kind")}) {p["approval_id"]}'
    if t == "task.status":
        return f'task is {p["state"]}' + (f' ({p["note"]})' if p.get("note") else "")
    if t == "task.completed":
        return f'task completed with evidence {p["outcome"].get("completion", {}).get("evidence")}'
    return t


class Agent:
    def __init__(self, name: str, fcl: Fcl, family_id: str, brief: Brief, log: DecisionLog, names: dict[str, str]):
        self.name, self.fcl, self.family_id, self.brief, self.log, self.names = name, fcl, family_id, brief, log, names
        self.cursor = 0

    def note(self, text: str, channel: Any = "local", ref: str = "") -> None:
        self.log.note(self.name, channel, text, ref)

    def outside(self, label: str, request: Any, response: Any) -> None:
        self.log.outside(self.name, label, request, response)

    async def step(self) -> bool:
        items, self.cursor = await self.fcl.inbox(self.cursor)
        for item in items:
            m = item["message"]
            sender = self.names.get(m["sender"]["agent_id"] or "", "FCL")
            self.log.note(sender, "fcl", f"to {self.name}: {describe(m)}", m["message_id"], quiet=True)
            await self.handle(m)
        return bool(items)

    async def handle(self, m: dict[str, Any]) -> None:
        raise NotImplementedError

    async def reply(self, m: dict[str, Any], answer: str, data: dict[str, Any]) -> None:
        payload = {"topic": m["payload"]["topic"], "answer": answer, "data": data}
        await self.fcl.send(self.family_id, m["sender"]["agent_id"], "information.response", payload,
                            idem=f"re-{m['message_id']}")

    async def answer_proposal(self, m: dict[str, Any], problems: list[str]) -> None:
        p = m["payload"]
        kind = "decline" if problems else "accept"
        await self.fcl.respond(m["task_id"], kind, p["proposal_version"], idem=f"{kind}-{m['message_id']}",
                               reason="; ".join(problems) or None)
        self.note(f"declined: {'; '.join(problems)}" if problems else "accepted the proposal", "fcl", m["task_id"])


class CalendarAssistant(Agent):
    """Alice's assistant: already connected to Alice's calendar, and knows Alice's budget."""

    def __init__(self, *args: Any, calendar: Calendar, budget: int):
        super().__init__(*args)
        self.calendar, self.budget = calendar, budget

    def free(self) -> list[date]:
        free = self.calendar.free_evenings(self.brief.first, self.brief.last)
        self.outside("Alice's calendar: free evenings", {"from": self.brief.first.isoformat(), "to": self.brief.last.isoformat()},
                     [d.isoformat() for d in free])
        return free

    async def handle(self, m: dict[str, Any]) -> None:
        t, p = m["type"], m["payload"]
        if t == "information.request" and p["topic"] == "dinner_preferences":
            free = self.free()
            self.note(f"checked Alice's calendar: free {days(free)}", "outside")
            await self.reply(m, f"Alice is free {days(free)} evenings, budget up to NT${self.budget:,} per person, "
                                f"no food restrictions.",
                             {"available_evenings": [d.isoformat() for d in free], "budget_per_person": self.budget,
                              "dietary": [], "allergies": []})
            self.note("shared Alice's free evenings and budget, not what is on the calendar", "fcl")
        elif t == "coordination.proposal":
            when = datetime.fromisoformat(p["proposal"]["proposed_time"]).astimezone(TPE)
            fits = when.date() in self.free() and EVENING_START <= when.time() <= EVENING_END
            await self.answer_proposal(m, [] if fits else [f"Alice is not free {day_label(when.date())} {when:%H:%M}"])


class Companion(Agent):
    """Ben's companion: knows Ben's food needs and budget. Needs Ben's permission to read Ben's calendar."""

    def __init__(self, *args: Any, calendar: Calendar, tablebook: TableBook):
        super().__init__(*args)
        self.calendar, self.tablebook = calendar, tablebook
        self.prefs = {"budget_per_person": 1200, "dietary": ["vegetarian"], "allergies": ["peanut"]}
        self.asks: dict[str, tuple[dict[str, Any], dict[str, list[date]]]] = {}
        self.all_free: set[date] = set()

    async def handle(self, m: dict[str, Any]) -> None:
        t, p = m["type"], m["payload"]
        if t == "information.request" and p["topic"] == "dinner_preferences":
            await self.reply(m, "Ben is vegetarian with a severe peanut allergy; budget up to NT$1,200 per person. "
                                "I can't see Ben's calendar yet. Send me the evenings that work for the others and "
                                "I'll ask Ben to let me check.",
                             {**self.prefs, "calendar": "needs_permission"})
            self.note("shared Ben's food needs and budget; no calendar access yet", "fcl")
        elif t == "information.request" and p["topic"] == "availability_check":
            await self.ask_for_calendar(m)
        elif t == "approval.granted" and p["approval_id"] in self.asks:
            await self.align(p["approval_id"])
        elif t == "approval.denied" and p["approval_id"] in self.asks:
            request, _ = self.asks.pop(p["approval_id"])
            await self.reply(request, "Ben didn't share the calendar. Please ask Ben directly.", {"evenings_free": {}})
        elif t == "coordination.proposal":
            await self.check_proposal(m)

    async def ask_for_calendar(self, m: dict[str, Any]) -> None:
        shared = (m["payload"].get("data") or {}).get("evenings_free") or {}
        others = {str(name): parse_days(v, self.brief.first, self.brief.last) for name, v in shared.items()}
        self.note("received the others' free evenings: " + "; ".join(f"{n} {days(d)}" for n, d in others.items()))
        overlap = set.intersection(*(set(d) for d in others.values())) if others else set()
        purpose = (f"{' and '.join(others)} can make {days(overlap)} for the dinner celebrating {self.brief.occasion}. "
                   f"I need to see when you're busy {day_label(self.brief.first)} to {day_label(self.brief.last)} to "
                   f"line up all three calendars. Free/busy only; nothing else is shared.")
        expires = (datetime.now(TPE) + timedelta(hours=1)).isoformat()
        a = await self.fcl.ask_owner(self.family_id, "calendar.read", self.brief.window, purpose, expires,
                                     idem=f"calendar-{m['message_id']}")
        self.asks[a["id"]] = (m, others)
        self.note("asked Ben for read access to Ben's calendar for that one week", "fcl", a["id"])

    async def align(self, approval_id: str) -> None:
        request, others = self.asks.pop(approval_id)
        a = await self.fcl.approval(approval_id)
        window = a["action"]["params"]
        allowed = (a["status"] == "granted" and a["action"]["scope"] == "calendar.read"
                   and datetime.fromisoformat(window["window_start"]) <= datetime.combine(self.brief.first, time.min, TPE)
                   and datetime.fromisoformat(window["window_end"]) > datetime.combine(self.brief.last, time.min, TPE))
        assert allowed, "never read the calendar outside what Ben granted"
        free = self.calendar.free_evenings(self.brief.first, self.brief.last)
        self.outside("Ben's calendar: free evenings", {"from": self.brief.first.isoformat(), "to": self.brief.last.isoformat(),
                                                       "approval_id": approval_id}, [d.isoformat() for d in free])
        self.note(f"Ben allowed it; read Ben's calendar for that week only: free {days(free)}", "outside", approval_id)
        self.all_free = set(free).intersection(*(set(d) for d in others.values()))
        self.note(f"lined up all three calendars: {days(self.all_free)} works for everyone")
        await self.reply(request, f"Ben is free {days(free)}. Evenings that work for all three: {days(self.all_free)}.",
                         {"evenings_free": {"Ben": [d.isoformat() for d in free]},
                          "all_three": [d.isoformat() for d in sorted(self.all_free)]})

    async def check_proposal(self, m: dict[str, Any]) -> None:
        proposal = m["payload"]["proposal"]
        when = datetime.fromisoformat(proposal["proposed_time"]).astimezone(TPE)
        found = re.search(r"tablebook:([a-z0-9-]+)", proposal.get("location", ""))
        problems = [] if when.date() in self.all_free else [f"Ben is not free {day_label(when.date())}"]
        if found is None:
            problems.append("no TableBook listing to check allergens against")
        else:
            r = self.tablebook.restaurant(found.group(1))
            self.outside(f"TableBook: listing {r.id}", None, {"name": r.name, "vegetarian_mains": r.vegetarian_mains,
                                                               "kitchen_allergens": sorted(r.kitchen_allergens),
                                                               "price_pp": r.price_pp})
            self.note(f"checked {r.name} with TableBook directly instead of trusting the proposal text: kitchen "
                      f"allergens {', '.join(sorted(r.kitchen_allergens)) or 'none'}, vegetarian mains "
                      f"{'yes' if r.vegetarian_mains else 'no'}", "outside")
            if "peanut" in r.kitchen_allergens:
                problems.append(f"{r.name}'s kitchen uses peanuts")
            if not r.vegetarian_mains:
                problems.append(f"{r.name} has no vegetarian mains")
            if r.price_pp > self.prefs["budget_per_person"]:
                problems.append(f"{r.name} is over Ben's budget")
        await self.answer_proposal(m, problems)


class Concierge(Agent):
    """Chloe's concierge: runs the plan, manages Chloe's own calendar, books and pays only with Chloe's approval."""

    def __init__(self, *args: Any, calendar: Calendar, tablebook: TableBook, guests: dict[str, str],
                 contacts: list[tuple[str, str]], watch: Callable[[str], None]):
        super().__init__(*args)
        self.calendar, self.tablebook, self.guests, self.contacts, self.watch = calendar, tablebook, guests, contacts, watch
        self.prefs: dict[str, dict[str, Any]] = {"Chloe": {"budget_per_person": 2000, "dietary": [], "allergies": []}}
        self.evenings: dict[str, list[date]] = {}
        self.alignment_sent = False
        self.hold: Optional[Hold] = None
        self.why = ""
        self.task: Optional[dict[str, Any]] = None
        self.payment_approval: Optional[str] = None
        self.card: Optional[Card] = None
        self.booking: Any = None

    def agent_of(self, person: str) -> str:
        return next(agent_id for agent_id, name in self.guests.items() if name == person)

    async def kickoff(self) -> None:
        self.evenings["Chloe"] = self.calendar.free_evenings(self.brief.first, self.brief.last)
        self.outside("Chloe's calendar: free evenings", {"from": self.brief.first.isoformat(), "to": self.brief.last.isoformat()},
                     [d.isoformat() for d in self.evenings["Chloe"]])
        self.note(f"Chloe's calendar (already connected): free {days(self.evenings['Chloe'])}", "outside")
        for agent_id, person in self.guests.items():
            await self.fcl.send(self.family_id, agent_id, "information.request", {
                "topic": "dinner_preferences",
                "question": f"Planning a dinner to celebrate {self.brief.occasion}, {day_label(self.brief.first)} to "
                            f"{day_label(self.brief.last)}. Which evenings (18:00 to 21:30) is {person} free, what "
                            f"budget per person, and any food preferences or allergies?",
                "data": {**self.brief.window, "asks": ["available_evenings", "budget_per_person", "dietary", "allergies"]},
            }, idem=f"prefs-{agent_id}")
        self.note(f"asked {whose(self.guests.values())} agents for free evenings, budget and food needs", "fcl")

    async def handle(self, m: dict[str, Any]) -> None:
        t, p = m["type"], m["payload"]
        person = self.guests.get(m["sender"]["agent_id"] or "")
        if t == "information.response" and person and p["topic"] == "dinner_preferences":
            self.take_prefs(person, p.get("data") or {})
        elif t == "information.response" and person and p["topic"] == "availability_check":
            shared = (p.get("data") or {}).get("evenings_free") or {}
            self.evenings[person] = parse_days(shared.get(person), self.brief.first, self.brief.last)
        elif t == "task.status" and p["state"] == "AGREED":
            await self.ask_to_book()
        elif t == "approval.granted" and p["approval_id"] == self.payment_approval:
            await self.book()
        elif t == "approval.denied" and p["approval_id"] == self.payment_approval:
            self.note("Chloe said no to the deposit; letting the hold lapse")
        await self.advance()

    def take_prefs(self, person: str, data: dict[str, Any]) -> None:
        budget = data.get("budget_per_person")
        self.prefs[person] = {
            "budget_per_person": budget if isinstance(budget, int) and budget > 0 else 10**9,
            "dietary": [str(x) for x in data.get("dietary") or []],
            "allergies": [str(x) for x in data.get("allergies") or []],
        }
        if "available_evenings" in data:
            self.evenings[person] = parse_days(data["available_evenings"], self.brief.first, self.brief.last)

    async def advance(self) -> None:
        if len(self.prefs) < 1 + len(self.guests) or self.hold is not None:
            return
        missing = [person for person in self.prefs if person not in self.evenings]
        if missing and not self.alignment_sent:
            self.alignment_sent = True
            overlap = set.intersection(*(set(d) for d in self.evenings.values()))
            self.note(f"{' and '.join(self.evenings)} are both free {days(overlap)}; "
                      f"{whose(missing)} agent can't see the calendar yet")
            shared = {name: [d.isoformat() for d in ds] for name, ds in self.evenings.items()}
            for person in missing:
                await self.fcl.send(self.family_id, self.agent_of(person), "information.request", {
                    "topic": "availability_check",
                    "question": f"These evenings work for {' and '.join(shared)}. Which of the week work for {person}?",
                    "data": {**self.brief.window, "evenings_free": shared},
                }, idem=f"align-{person}")
                self.note(f"sent {whose(shared)} free evenings to {person}'s agent", "fcl")
        elif not missing:
            await self.find_and_propose()

    async def find_and_propose(self) -> None:
        self.log.stage(4, "The agents settle time and place, then the concierge asks Chloe to book")
        common = sorted(set.intersection(*(set(d) for d in self.evenings.values())))
        party = len(self.prefs)
        cap = min(p["budget_per_person"] for p in self.prefs.values())
        vegetarian = any("vegetarian" in p["dietary"] for p in self.prefs.values())
        avoid = set().union(*(set(p["allergies"]) for p in self.prefs.values()))
        self.note(f"evenings all three are free: {days(common)}")
        self.note(f"criteria: party of {party}, at most NT${cap:,} per person"
                  + (", vegetarian mains" if vegetarian else "")
                  + (f", no {' or '.join(sorted(avoid))} in the kitchen" if avoid else ""))
        candidates = []
        for day in common:
            found = self.tablebook.search(day)
            self.outside(f"TableBook: search {day_label(day)}", {"date": day.isoformat(), "party": party},
                         [{"restaurant": r.name, "free_seatings": [s.strftime("%H:%M") for s in seatings]} for r, seatings in found])
            for r, seatings in found:
                problems = []
                if r.price_pp > cap:
                    problems.append(f"NT${r.price_pp:,} per person is over budget")
                if vegetarian and not r.vegetarian_mains:
                    problems.append("no vegetarian mains")
                if avoid & r.kitchen_allergens:
                    problems.append(f"kitchen uses {', '.join(sorted(avoid & r.kitchen_allergens))}")
                if not seatings:
                    problems.append(f"no table {day_label(day)}")
                self.note(f"{r.name} ({r.cuisine}, {r.rating}★, NT${r.price_pp:,} pp): "
                          f"{'; '.join(problems) if problems else 'fits'}", "outside")
                if not problems:
                    candidates.append((r, seatings[0]))
        candidates.sort(key=lambda c: (-c[0].rating, c[0].price_pp, c[1]))
        if not candidates:
            self.note("nothing fits; would ask the room to widen the dates or the budget")
            return
        (r, start), *others = candidates
        self.hold = h = self.tablebook.hold(r.id, start, party)
        self.outside(f"TableBook: hold {r.name}", {"restaurant": r.id, "start": start.isoformat(), "party": party},
                     {"hold_id": h.id, "deposit": nt(h.deposit_minor), "expires_at": h.expires_at.isoformat()})
        when = f"{day_label(start.date())} {start:%H:%M}"
        self.why = (f"the only evening all three are free, vegetarian mains and no peanuts in the kitchen, "
                    f"NT${r.price_pp:,} per person is under everyone's budget"
                    + (f", and it rates higher than {others[0][0].name}" if others else ""))
        self.note(f"suggest {r.name}, {when}: {self.why}. Held the table until {h.expires_at:%H:%M} "
                  f"(deposit {nt(h.deposit_minor)})", "outside", h.id)
        self.task = await self.fcl.propose(self.family_id, list(self.guests), "celebration_dinner", {
            "proposed_time": start.isoformat(), "duration_minutes": 120,
            "location": f"{r.name}, {r.address} (tablebook:{r.id})",
            "note": f"Why: {self.why}. Deposit {nt(h.deposit_minor)}, table held until {h.expires_at:%H:%M}.",
        }, expires_at=h.expires_at.isoformat(), idem="dinner-proposal")
        self.watch(self.task["id"])
        self.note(f"proposed {r.name}, {when} to {whose(self.guests.values())} agents", "fcl", self.task["id"])

    async def ask_to_book(self) -> None:
        assert self.hold is not None and self.task is not None
        h, r = self.hold, self.hold.restaurant
        purpose = (f"Book {r.name}, {day_label(h.start.date())} {h.start:%H:%M}, party of {h.party}. Why: {self.why}. "
                   f"Pay the {nt(h.deposit_minor)} deposit to confirm.")
        a = await self.fcl.ask_owner(self.family_id, "payment.charge",
                                     {"payee": f"{r.name} via TableBook", "amount_minor": h.deposit_minor,
                                      "currency": "TWD", "reference": h.id},
                                     purpose, h.expires_at.isoformat(), idem=f"pay-{h.id}", task_id=self.task["id"])
        self.payment_approval = a["id"]
        self.note("everyone agreed; asked Chloe to approve the deposit and enter a card", "fcl", a["id"])

    async def book(self) -> None:
        assert self.hold is not None and self.task is not None and self.payment_approval is not None
        h = self.hold
        a = await self.fcl.approval(self.payment_approval)
        params = a["action"]["params"]
        assert a["status"] == "granted" and a["action"]["scope"] == "payment.charge"
        assert params["amount_minor"] == h.deposit_minor and params["reference"] == h.id, "pay exactly what Chloe approved"
        assert self.card is not None, "Chloe hands the card token to her own concierge, never through FCL"
        self.log.stage(5, "Booked: everyone gets the email, and the decision log is written")
        booking = self.tablebook.confirm(h.id, self.card.token, h.deposit_minor, a["id"], self.contacts)
        pay = {"hold_id": h.id, "card_token": self.card.token, "amount": nt(h.deposit_minor), "idempotency_key": a["id"]}
        self.outside("TableBook: confirm and pay", pay, {"reference": booking.reference, "receipt": booking.receipt_id})
        self.log.note("Outside world", "outside", f"TableBook emailed {', '.join(n for n, _ in self.contacts)}: "
                                                  f"\"{self.tablebook.outbox.sent[-1].name}\" and two more, each with an .ics invite")
        self.note(f"paid the {nt(booking.charged_minor)} deposit (card ending {booking.card_last4}); "
                  f"booking {booking.reference}", "outside", booking.receipt_id)
        again = self.tablebook.confirm(h.id, self.card.token, h.deposit_minor, a["id"], self.contacts)
        self.outside("TableBook: confirm and pay (retry)", pay, {"reference": again.reference, "receipt": again.receipt_id})
        assert again == booking
        self.note("retried the payment with the same approval id: same booking, no second charge", "outside")
        evidence = {"booking_reference": booking.reference, "receipt_id": booking.receipt_id,
                    "deposit": nt(booking.charged_minor), "payment_approval_id": a["id"]}
        done = await self.fcl.respond(self.task["id"], "complete", self.task["proposal_version"],
                                      idem="complete-dinner", evidence=evidence)
        self.note(f"marked the task {done['task']['state']} with the booking as evidence", "fcl", self.task["id"])
        self.booking = booking


# ---- the people ------------------------------------------------------------------------


class Phone:
    """An owner's FCL app. Only a user credential can decide, so this is the only way an approval happens."""

    def __init__(self, person: str, rest: Rest, log: DecisionLog, names: dict[str, str], interactive: bool,
                 pay: Optional[tuple[TableBook, Callable[[Card], None]]] = None):
        self.person, self.rest, self.log, self.names, self.interactive, self.pay = person, rest, log, names, interactive, pay
        self.cards_typed: list[str] = []

    def ask(self, question: str, default: str) -> str:
        if self.interactive:
            return input(f"  {self.person + ':':<18} {question} [{default}] ").strip() or default
        print(f"  {self.person + ':':<18} {question} {default}  (auto; run with --interactive to decide)")
        return default

    def review(self) -> bool:
        pending = self.rest("GET", "/v1/approvals", status="pending")["items"]
        for a in pending:
            self.decide(a)
        return bool(pending)

    def decide(self, a: dict[str, Any]) -> None:
        act, asker = a["action"], self.names[a["requesting_agent_id"]]
        params = act.get("params") or {}
        if act.get("scope") == "calendar.read":
            first = datetime.fromisoformat(params["window_start"]).date()
            last = datetime.fromisoformat(params["window_end"]).date() - timedelta(days=1)
            ask, summary = f"Read your calendar, {day_label(first)} to {day_label(last)}, free/busy only?", "calendar access"
        elif act.get("scope") == "payment.charge":
            ask, summary = f"Pay {nt(params['amount_minor'])} to {params['payee']}?", f"{nt(params['amount_minor'])} deposit"
        else:
            ask, summary = f"Approve {asker}'s {act.get('kind')}?", str(act.get("kind"))
        self.log.note(self.person, "fcl", f"phone: {asker} asks \"{ask}\" Why: {act.get('purpose') or 'not given'}", a["id"])
        allowed = self.ask("Allow?", "y").lower().startswith("y")
        if allowed and self.pay is not None and act.get("scope") == "payment.charge":
            tablebook, hand_over = self.pay
            while True:
                number = self.ask("Card number (TableBook payment page):", TEST_CARD)
                try:
                    card = tablebook.tokenize_card(number)
                    break
                except ValueError as e:
                    if not self.interactive:
                        raise
                    print(f"  {'':<18} {e}; try again")
            self.cards_typed.append(number.replace(" ", ""))
            self.log.outside(self.person, "TableBook: card field", {"number": f"•••• {card.last4}"},
                             {"token": card.token, "last4": card.last4})
            hand_over(card)
            self.log.note(self.person, "owner", f"entered a card on TableBook's payment page (ending {card.last4}); only "
                                                "a TableBook token goes to Chloe's concierge, and FCL sees neither")
        self.rest("POST", f"/v1/approvals/{a['id']}/decisions", {"decision": "grant" if allowed else "deny"})
        self.log.note(self.person, "owner", f"{'allowed' if allowed else 'denied'} {asker}: {summary}", a["id"])


async def settle(agents: list[Agent], phones: list[Phone], rounds: int = 50) -> None:
    """Runs everyone until nothing is left to read or decide."""
    for _ in range(rounds):
        moved = [await a.step() for a in agents] + [ph.review() for ph in phones]
        if not any(moved):
            return
    raise SystemExit("the agents did not settle")


def calendars(week: date) -> dict[str, Calendar]:
    def busy(offset: int, start: str, end: str, title: str) -> Event:
        day = week + timedelta(days=offset)
        return Event(at(day, start), at(day, end), title)

    return {
        "Alice": Calendar("Alice", [busy(0, "19:00", "20:30", "Yoga"), busy(2, "18:30", "20:00", "Parent-teacher night"),
                                    busy(6, "18:00", "21:00", "Dinner at Mom's")]),
        "Ben": Calendar("Ben", [busy(1, "19:00", "21:00", "Rust meetup"), busy(3, "19:30", "22:00", "Board game night"),
                                busy(4, "18:00", "22:00", "Team offsite dinner")]),
        "Chloe": Calendar("Chloe", [busy(0, "18:00", "21:00", "Demo-day prep"), busy(1, "18:30", "20:30", "Climbing"),
                                    busy(3, "19:00", "20:00", "Investor call")]),
    }


async def run(trace: Trace, goal: str = DEFAULT_GOAL, interactive: bool = False) -> Path:
    data = ROOT / ".data" / f"celebration-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    data.mkdir(parents=True)
    fcl = FclProcess(data / "fcl.db", free_port(), data / "server.log")
    try:
        fcl.start()
        trace.server(True, f"started on {fcl.base_url}, pid {fcl.pid}, fresh SQLite file")
        await _story(HideEmptyPolls(trace), fcl, data, goal, interactive)
    finally:
        fcl.kill()
    return data


async def _story(trace: Trace, fcl: FclProcess, data: Path, goal: str, interactive: bool) -> None:
    def rest_as(lane: str, token: Optional[str] = None) -> Rest:
        return Rest(trace, lane, fcl.base_url, token)

    log = DecisionLog(trace, {name: lane for lane, name in LANES.items()})
    log.stage(1, "Chloe types a goal")
    log.note("Chloe", "owner", f'"{goal}"')
    brief = draft_brief(goal, datetime.now(TPE).date())
    log.note("Chloe's concierge", "local", f"brief: dinner to celebrate {brief.occasion}, "
                                           f"{day_label(brief.first)} to {day_label(brief.last)}; needs everyone's "
                                           f"free evenings, budget and food needs")

    log.stage(2, "The room starts: Chloe invites Alice and Ben, and they approve")
    people = {name: rest_as(name.lower())("POST", "/v1/users", {"display_name": name}) for name in ("Chloe", "Alice", "Ben")}
    rest = {name: rest_as(name.lower(), u["token"]) for name, u in people.items()}
    room = rest["Chloe"]("POST", "/v1/families", {"name": f"Celebrate {brief.occasion}"})
    log.note("Chloe", "owner", f"opened the room \"{room['name']}\"", room["id"])
    registered = {}
    for name, display, provider, transport in (("Chloe", "Chloe's concierge", "custom-mcp", "mcp"),
                                               ("Alice", "Alice's assistant", "custom-mcp", "mcp"),
                                               ("Ben", "Ben's assistant", "fcl-companion", "inbox")):
        if name != "Chloe":
            invite = rest["Chloe"]("POST", f"/v1/families/{room['id']}/invitations", {})
            log.note("Chloe", "owner", f"sent {name} an invite (code {invite['code'][:14]}…)", invite["id"])
            rest[name]("POST", "/v1/invitations/accept", {"code": invite["code"]})
        registered[name] = rest[name]("POST", "/v1/agents", {
            "display_name": display, "provider": provider, "transports": [transport],
            "capabilities": ["receive_message", "propose_schedule"],
            "grants": [{"family_id": room["id"], "action": a} for a in ACTIONS]})
        if name != "Chloe":
            log.note(name, "owner", f"accepted and connected {display} ({provider} over {transport}); it may read and "
                                    f"answer room messages and respond to proposals, nothing more")
    names = {r["id"]: r["display_name"] for r in registered.values()}
    guests = {registered["Alice"]["id"]: "Alice", registered["Ben"]["id"]: "Ben"}
    contacts = [(name, f"{name.lower()}@example.com") for name in ("Chloe", "Alice", "Ben")]

    outbox = Outbox(data / "outbox")
    tablebook = TableBook(outbox)
    cal = calendars(brief.first)

    async with mcp_agent(trace, fcl.base_url, registered["Chloe"]["token"], data / "mcp-chloe.log", "concierge") as chloe_mcp, \
            mcp_agent(trace, fcl.base_url, registered["Alice"]["token"], data / "mcp-alice.log", "alice_agent") as alice_mcp:
        common = (room["id"], brief, log, names)
        concierge = Concierge("Chloe's concierge", McpFcl(chloe_mcp), *common, calendar=cal["Chloe"],
                              tablebook=tablebook, guests=guests, contacts=contacts,
                              watch=lambda task_id: trace.watch(fcl.base_url, people["Chloe"]["token"], task_id))
        alice = CalendarAssistant("Alice's assistant", McpFcl(alice_mcp), *common, calendar=cal["Alice"], budget=1500)
        ben = Companion("Ben's assistant", RestFcl(rest_as("ben_agent", registered["Ben"]["token"]), registered["Ben"]["id"]),
                        *common, calendar=cal["Ben"], tablebook=tablebook)
        phones = [Phone("Ben", rest["Ben"], log, names, interactive),
                  Phone("Chloe", rest["Chloe"], log, names, interactive,
                        pay=(tablebook, lambda card: setattr(concierge, "card", card)))]

        log.stage(3, "Agents join and check their owners' calendars and preferences in the background")
        await concierge.kickoff()
        await settle([concierge, alice, ben], phones)

    assert concierge.booking is not None, "the dinner was not booked"
    task_id = concierge.task["id"] if concierge.task else ""
    views = {name: rest[name]("GET", f"/v1/tasks/{task_id}") for name in rest}
    assert {v["state"] for v in views.values()} == {"COMPLETED"}
    assert cal["Ben"].reads == 1 and len(tablebook.charges) == 1 and len(outbox.sent) == 3
    fcl.kill()
    stored = b"".join(p.read_bytes() for p in data.glob("fcl.db*"))
    secrets_typed = [n.encode() for n in phones[1].cards_typed] + [concierge.card.token.encode() if concierge.card else b""]
    assert all(s and s not in stored for s in secrets_typed), "neither the card number nor its token may reach FCL"

    h, b = concierge.hold, concierge.booking
    assert h is not None
    mail = email.message_from_bytes(outbox.sent[1].read_bytes(), policy=email.policy.default)
    print(f"\n  Alice's inbox · From: {mail['From']}\n  Subject: {mail['Subject']}")
    body = mail.get_body(preferencelist=("plain",))
    assert body is not None
    print("  " + body.get_content().strip().replace("\n", "\n  "))
    log.print()
    page = log.write_html(data / "decision-log.html", f"Dinner to celebrate {brief.occasion}", [
        ("Goal", goal), ("Where", f"{h.restaurant.name}, {h.restaurant.address}"),
        ("When", f"{day_label(h.start.date())}, {h.start:%H:%M}, party of {h.party}"),
        ("Booking", f"{b.reference}, deposit {nt(b.charged_minor)} (card ending {b.card_last4})"),
        ("FCL task", f"{task_id}, {views['Chloe']['state']}"),
    ])
    print(f"\nDemo passed. Decision log: {page}\nEmails: {outbox.folder}\nDatabase and server log: {data}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--interactive", action="store_true", help="approve requests and type the card yourself")
    args = parser.parse_args()
    asyncio.run(run(Trace(LANES), args.goal, args.interactive))


if __name__ == "__main__":
    main()
