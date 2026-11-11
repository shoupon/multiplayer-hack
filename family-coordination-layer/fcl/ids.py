"""Opaque, prefixed identifiers. The prefix makes a misplaced id fail validation loudly."""

from __future__ import annotations

import re
import secrets
from typing import NewType

UserId = NewType("UserId", str)
FamilyId = NewType("FamilyId", str)
InvitationId = NewType("InvitationId", str)
AgentId = NewType("AgentId", str)
CredentialId = NewType("CredentialId", str)
GrantId = NewType("GrantId", str)
ConversationId = NewType("ConversationId", str)
MessageId = NewType("MessageId", str)
TaskId = NewType("TaskId", str)
ApprovalId = NewType("ApprovalId", str)
WebhookId = NewType("WebhookId", str)
LinkId = NewType("LinkId", str)
RoomId = NewType("RoomId", str)

PREFIX = {
    "user": "usr",
    "family": "fam",
    "invitation": "inv",
    "agent": "agt",
    "credential": "crd",
    "grant": "grt",
    "conversation": "cnv",
    "message": "msg",
    "task": "tsk",
    "approval": "apr",
    "webhook": "whk",
    "link": "lnk",
    "room": "rom",
}


def new_id(kind: str) -> str:
    return f"{PREFIX[kind]}_{secrets.token_hex(12)}"


def id_pattern(kind: str) -> str:
    """Regex for Pydantic `Field(pattern=...)` at the HTTP boundary."""
    return rf"^{PREFIX[kind]}_[0-9a-f]{{24}}$"


def is_id(kind: str, value: str) -> bool:
    return re.fullmatch(id_pattern(kind), value) is not None
