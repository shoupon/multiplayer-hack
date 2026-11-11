"""Rooms: one shareable code that lets people's agents join a fresh family to coordinate one goal.

A room has a brief (agreed up front), member agents (one per person, joined through the code), and at most
one coordinator agent, owned by the person who created the room. The coordinator moves the room from
`waiting` to `live` to `done` and writes the summary the room page shows.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from . import audit
from .agents import insert_agent
from .auth import AgentPrincipal, UserPrincipal
from .context import Ctx
from .db import Conn
from .errors import DeniedAudit, conflict, forbidden, not_found
from .grants import insert_grant
from .identity import insert_credential
from .ids import new_id
from .messaging import Recipient, insert_message, new_conversation
from .policy import DELEGABLE_ACTIONS
from .timeutil import from_db, to_db
from .tokens import hash_secret

CODE_PATTERN = r"^[A-Za-z0-9_-]{32}$"
Status = Literal["waiting", "live", "done"]


@dataclass(frozen=True)
class Room:
    id: str
    family_id: str
    created_by: str
    intent: str
    max_members: int
    expires_at: str
    brief: dict[str, str]
    status: Status
    summary: Optional[dict[str, Any]]
    coordinator_agent_id: Optional[str]


def _room(r: Any) -> Room:
    return Room(r["id"], r["family_id"], r["created_by"], r["intent"], r["max_members"], r["expires_at"],
                json.loads(r["brief"]), r["status"], json.loads(r["summary"]) if r["summary"] else None,
                r["coordinator_agent_id"])


def create(ctx: Ctx, p: UserPrincipal, name: str, intent: str, ttl_hours: int, max_members: int,
           brief: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Creates the room's family with the caller as admin. Returns (room view, code)."""
    now = ctx.clock()
    code = secrets.token_urlsafe(24)
    room_id, family_id = new_id("room"), new_id("family")
    expires_at = to_db(now + timedelta(hours=ttl_hours))
    with ctx.db.tx() as c:
        c.run("INSERT INTO families (id, name, created_by, created_at) VALUES (?, ?, ?, ?)", (family_id, name, p.user_id, to_db(now)))
        c.run("INSERT INTO family_memberships (family_id, user_id, role, joined_at) VALUES (?, ?, 'admin', ?)",
              (family_id, p.user_id, to_db(now)))
        c.run(
            """INSERT INTO rooms (id, family_id, created_by, intent, code_hash, max_members, created_at, expires_at, brief)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_id, family_id, p.user_id, intent, hash_secret(code), max_members, to_db(now), expires_at, json.dumps(brief)),
        )
        audit.record(c, now, p, "room.create", "succeeded", family_id=family_id, resource_type="room", resource_id=room_id,
                     metadata={"intent": intent, "max_members": max_members})
    return {"id": room_id, "family_id": family_id, "intent": intent, "max_members": max_members, "expires_at": expires_at}, code


def resolve(c: Conn, now: datetime, code: str) -> Room:
    """Unknown, expired, and revoked codes look the same."""
    r = c.one("SELECT * FROM rooms WHERE code_hash = ?", (hash_secret(code),))
    if r is None or r["revoked_at"] or from_db(r["expires_at"]) <= now:
        raise not_found("room")
    return _room(r)


def _grant_all(c: Conn, now: datetime, user_id: str, agent_id: str, family_id: str) -> None:
    for action in DELEGABLE_ACTIONS:
        insert_grant(c, now, user_id, agent_id, family_id, action)


def join(ctx: Ctx, code: str, display_name: str, agent_name: str, provider: str) -> dict[str, Any]:
    """One step: a new user joins the room's family, with an agent granted every family action."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        room = resolve(c, now, code)
        members = c.one("SELECT count(*) AS n FROM family_memberships WHERE family_id = ? AND status = 'active'", (room.family_id,))
        if members and members["n"] >= room.max_members:
            raise conflict("room_full", f"this room holds at most {room.max_members} members")
        user_id = new_id("user")
        c.run("INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)", (user_id, display_name, to_db(now)))
        _, user_token = insert_credential(c, "user", user_id, f"room:{room.id}", now)
        c.run("INSERT INTO family_memberships (family_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
              (room.family_id, user_id, to_db(now)))
        agent_id = insert_agent(c, now, user_id, agent_name, provider, ["propose_schedule", "receive_message"], ["inbox"])
        _grant_all(c, now, user_id, agent_id, room.family_id)
        agent_credential, agent_token = insert_credential(c, "agent", agent_id, f"room:{room.id}", now)
        audit.record(c, now, AgentPrincipal(agent_id, user_id, True, agent_credential), "room.join", "succeeded",
                     family_id=room.family_id, resource_type="room", resource_id=room.id)
        if room.coordinator_agent_id:
            insert_message(c, now, family_id=room.family_id,
                           conversation_id=new_conversation(c, now, room.family_id, "room", None),
                           message_type="room.member_joined", payload={"room_id": room.id, "agent_id": agent_id},
                           recipients=[Recipient(room.created_by, room.coordinator_agent_id)])
    return {"room_id": room.id, "family_id": room.family_id, "user_id": user_id, "user_token": user_token,
            "agent_id": agent_id, "agent_token": agent_token}


def add_coordinator(ctx: Ctx, p: UserPrincipal, code: str, agent_id: Optional[str] = None) -> dict[str, Any]:
    """The room's creator sets the one coordinator. With no `agent_id`, FCL creates a coordinator agent that acts
    for the creator and returns its token once. With `agent_id`, an agent already in the room becomes the
    coordinator and keeps its own token."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        room = resolve(c, now, code)
        if room.created_by != p.user_id:
            raise forbidden("not_room_creator", "only the person who created the room can set its coordinator",
                            DeniedAudit("room.coordinator", room.family_id, "room", room.id))
        if room.coordinator_agent_id and room.coordinator_agent_id != agent_id:
            raise conflict("coordinator_exists", "this room already has a coordinator")
        out: dict[str, Any] = {"room_id": room.id, "family_id": room.family_id}
        if agent_id:
            if not any(a["agent_id"] == agent_id for a in agents(c, room)):
                raise not_found("agent in this room")
        else:
            agent_id = insert_agent(c, now, p.user_id, "Room coordinator", "fcl-orchestrator",
                                    ["coordinate", "propose_schedule", "receive_message"], ["inbox"])
            _grant_all(c, now, p.user_id, agent_id, room.family_id)
            out["credential_id"], out["token"] = insert_credential(c, "agent", agent_id, f"room-coordinator:{room.id}", now)
        c.run("UPDATE rooms SET coordinator_agent_id = ? WHERE id = ?", (agent_id, room.id))
        audit.record(c, now, p, "room.coordinator", "succeeded", family_id=room.family_id, resource_type="room",
                     resource_id=room.id, metadata={"agent_id": agent_id})
    return {**out, "agent_id": agent_id}


def set_status(ctx: Ctx, p: AgentPrincipal, room_id: str, status: Status, summary: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Only the room's coordinator moves the room along. `done` needs a summary; the others clear nothing."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        r = c.one("SELECT * FROM rooms WHERE id = ?", (room_id,))
        if r is None or r["coordinator_agent_id"] != p.agent_id:
            raise not_found("room")
        if status == "done" and summary is None:
            raise conflict("summary_required", "set a summary when the room is done")
        c.run("UPDATE rooms SET status = ?, summary = COALESCE(?, summary) WHERE id = ?",
              (status, json.dumps(summary) if summary is not None else None, room_id))
        audit.record(c, now, p, "room.status", "succeeded", family_id=r["family_id"], resource_type="room",
                     resource_id=room_id, metadata={"status": status})
        return {"room_id": room_id, "status": status}


# ---- what the room page and the briefing show ----


def agents(c: Conn, room: Room) -> list[dict[str, Any]]:
    """Every agent acting in the room: the coordinator first, then members in the order they joined."""
    rows = c.all(
        """SELECT a.id, a.display_name, a.provider, a.created_at, u.display_name AS owner
             FROM agents a JOIN users u ON u.id = a.owner_user_id
            WHERE a.status = 'active' AND a.id IN (SELECT DISTINCT agent_id FROM delegation_grants WHERE family_id = ?)
            ORDER BY a.created_at, a.id""",
        (room.family_id,),
    )
    out = [{"agent_id": r["id"], "name": r["display_name"], "owner": r["owner"], "provider": r["provider"],
            "joined_at": r["created_at"], "role": "coordinator" if r["id"] == room.coordinator_agent_id else "member"}
           for r in rows]
    return sorted(out, key=lambda a: a["role"] != "coordinator")


def _proposal_text(p: dict[str, Any]) -> str:
    parts = [p.get("location"), p.get("proposed_time"), f"{p['duration_minutes']} min" if p.get("duration_minutes") else None,
             p.get("note")]
    return " · ".join(str(x) for x in parts if x)


def _task_line(e: dict[str, Any], to_state: str) -> Optional[str]:
    kind = e.get("kind")
    pending = e.get("result") == "pending_approval"
    if kind == "create":
        return "Proposed: " + _proposal_text(e.get("proposal") or {})
    if kind == "accept":
        return f"Asked its owner to approve accepting v{e['proposal_version']}" if pending else f"Accepted v{e['proposal_version']}"
    if kind == "decline":
        return "Declined" + (f": {e['reason']}" if e.get("reason") else "")
    if kind == "counterpropose":
        return ("Asked its owner to approve a counterproposal" if pending else
                "Counterproposed: " + _proposal_text(e.get("proposal") or {}))
    if kind == "complete":
        return "Marked the plan done · " + ", ".join(f"{k}: {v}" for k, v in (e.get("evidence") or {}).items())
    if kind in ("expire", "cancel", "fail"):
        return f"Plan {to_state.lower()}"
    return None


def state(c: Conn, room: Room) -> dict[str, Any]:
    """The room as people see it: who joined, what the agents said and did, and the outcome."""
    family = c.one("SELECT name FROM families WHERE id = ?", (room.family_id,))
    members = agents(c, room)
    events: list[dict[str, Any]] = [
        {"at": a["joined_at"], "kind": "join", "agent_id": a["agent_id"]} for a in members]
    for m in c.all(
        """SELECT m.sender_agent_id, m.correlation_id, min(m.created_at) AS at, m.payload,
                  json_group_array(d.recipient_agent_id) AS recipients
             FROM messages m JOIN message_deliveries d ON d.message_id = m.id
            WHERE m.family_id = ? AND m.type = 'message.text'
            GROUP BY m.sender_agent_id, m.correlation_id, m.payload""",
        (room.family_id,),
    ):
        events.append({"at": m["at"], "kind": "message", "agent_id": m["sender_agent_id"],
                       "to": json.loads(m["recipients"]), "text": json.loads(m["payload"])["text"]})
    for t in c.all(
        """SELECT tt.task_id, tt.from_state, tt.to_state, tt.event, tt.actor_agent_id, tt.created_at
             FROM task_transitions tt WHERE tt.family_id = ? ORDER BY tt.id""",
        (room.family_id,),
    ):
        line = _task_line(json.loads(t["event"]), t["to_state"])
        if line and t["actor_agent_id"]:
            events.append({"at": t["created_at"], "kind": "task", "agent_id": t["actor_agent_id"], "task_id": t["task_id"],
                           "text": line})
        if t["to_state"] == "AGREED" and t["from_state"] != "AGREED":
            events.append({"at": t["created_at"], "kind": "sys", "task_id": t["task_id"],
                           "text": "Every agent accepted: the plan is agreed"})
    for s in c.all(
        """SELECT created_at, json_extract(metadata, '$.status') AS status FROM audit_events
            WHERE resource_type = 'room' AND resource_id = ? AND action = 'room.status' ORDER BY id""",
        (room.id,),
    ):
        text = {"live": "Coordinator started", "done": "Coordination finished", "waiting": "Waiting for agents"}[s["status"]]
        events.append({"at": s["created_at"], "kind": "sys", "text": text})
    events.sort(key=lambda e: e["at"])
    tasks = []
    for t in c.all(
        """SELECT t.id, t.state, t.intent, t.proposal, t.proposal_version, t.initiator_agent_id, t.outcome,
                  (SELECT json_group_array(json_object('agent_id', p.agent_id, 'response', p.response,
                                                       'responded_version', p.responded_version))
                     FROM task_participants p WHERE p.task_id = t.id) AS participants
             FROM coordination_tasks t WHERE t.family_id = ? ORDER BY t.created_at""",
        (room.family_id,),
    ):
        tasks.append({"task_id": t["id"], "state": t["state"], "intent": t["intent"], "proposal": json.loads(t["proposal"]),
                      "proposal_version": t["proposal_version"], "initiator_agent_id": t["initiator_agent_id"],
                      "participants": json.loads(t["participants"]),
                      "outcome": json.loads(t["outcome"]) if t["outcome"] else None})
    return {
        "room": {"id": room.id, "family_id": room.family_id, "topic": family["name"] if family else "",
                 "brief": room.brief, "status": room.status, "max_members": room.max_members,
                 "expires_at": room.expires_at, "coordinator_agent_id": room.coordinator_agent_id},
        "agents": members,
        "events": events,
        "tasks": tasks,
        "summary": room.summary,
    }
