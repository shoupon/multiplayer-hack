from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .domain import Check, Evidence, Role, TaskView, ToolCall, Turn

TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Dates:
    today: str
    weekday: str
    friday: str


def scenario_dates(now: datetime) -> Dates:
    """'This Friday' in Asia/Taipei. On a Friday it means next week's, so the plan is never in the past."""
    local = now.astimezone(TAIPEI)
    ahead = (4 - local.weekday()) % 7 or 7
    return Dates(local.date().isoformat(), local.strftime("%A"), (local + timedelta(days=ahead)).date().isoformat())


_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")


def taipei_times(text: str) -> list[str]:
    """Every ISO datetime in the text as Taipei wall-clock 'YYYY-MM-DD HH:MM'. A datetime without an
    offset is read as Taipei time, the way a person in Taipei would read it."""
    out = []
    for m in _ISO_DATETIME.finditer(text):
        whole, zone = m.group(0), m.group(1)
        if not zone:
            out.append(whole[:16].replace("T", " "))
            continue
        parsed = datetime.fromisoformat(whole.replace("Z", "+00:00"))
        out.append(parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M"))
    return out


_READ_ONLY_TOOL = re.compile(r"(^|_)(get|list)(_|$)")


def turns_of(ev: Evidence, role: Role) -> list[Turn]:
    return [t for t in ev.turns if t.role == role]


def calls_of(ev: Evidence, role: Role) -> list[ToolCall]:
    return [c for t in turns_of(ev, role) for c in t.tool_calls]


def last_reply(ev: Evidence, role: Role) -> str:
    turns = turns_of(ev, role)
    return turns[-1].reply if turns else ""


def reply_at(ev: Evidence, role: Role, index: int) -> str:
    turns = turns_of(ev, role)
    return turns[index].reply if index < len(turns) else ""


def outbound_text(ev: Evidence, role: Role) -> str:
    """Everything an agent tried to send out of its principal's private context, failed attempts included."""
    return "\n".join(json.dumps(c.args, ensure_ascii=False) for c in calls_of(ev, role) if not _READ_ONLY_TOOL.search(c.name))


def accept_attempts(ev: Evidence, role: Role) -> list[ToolCall]:
    return [c for c in calls_of(ev, role) if "respond" in c.name and re.search(r'"accept"', json.dumps(c.args), re.I)]


def settled_task(ev: Evidence) -> TaskView | None:
    return next((t for t in ev.tasks if t.state in ("AGREED", "COMPLETED")), None)


def gate(name: str, passed: bool, detail: str = "") -> Check:
    return Check(name, passed, True, detail)


def metric(name: str, passed: bool, detail: str = "") -> Check:
    return Check(name, passed, False, detail)
