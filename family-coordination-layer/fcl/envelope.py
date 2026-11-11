"""Message types, their payload models, and who may originate each. See docs/DESIGN.md section 9.

`direct`: agents send it through POST /v1/messages.
`engine`: only the task or approval engine produces it, so each state change has one writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .proposal import INTENT_PATTERN, Proposal

SCHEMA_VERSION = "1.0"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Text(_Payload):
    text: str = Field(min_length=1, max_length=4000)


class InformationRequest(_Payload):
    topic: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    data: Optional[dict[str, Any]] = None


class InformationResponse(_Payload):
    """`answer` is for people; `data` is the same answer in a shape another agent can parse (still untrusted)."""

    topic: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=4000)
    data: Optional[dict[str, Any]] = None


class ProposalPayload(_Payload):
    intent: str = Field(pattern=INTENT_PATTERN)
    proposal_version: int = Field(ge=1)
    proposal: Proposal
    expires_at: AwareDatetime


class VersionPayload(_Payload):
    proposal_version: int = Field(ge=1)


class DeclinePayload(_Payload):
    proposal_version: int = Field(ge=1)
    reason: Optional[str] = Field(default=None, max_length=500)


class ReasonPayload(_Payload):
    reason: Optional[str] = Field(default=None, max_length=500)


class ApprovalRequestPayload(_Payload):
    approval_id: str
    summary: str = Field(max_length=500)


class ApprovalDecisionPayload(_Payload):
    approval_id: str
    kind: Optional[Literal["task_response", "permission"]] = None


class TaskStatusPayload(_Payload):
    state: str
    proposal_version: int = Field(ge=1)
    note: Optional[str] = Field(default=None, max_length=500)


class TaskCompletedPayload(_Payload):
    outcome: dict[str, Any]


class TaskFailedPayload(_Payload):
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class MessageType:
    origin: Literal["direct", "engine"]
    payload: type[_Payload]


MESSAGE_TYPES: dict[str, MessageType] = {
    "message.text": MessageType("direct", Text),
    "information.request": MessageType("direct", InformationRequest),
    "information.response": MessageType("direct", InformationResponse),
    "coordination.proposal": MessageType("engine", ProposalPayload),
    "coordination.counterproposal": MessageType("engine", ProposalPayload),
    "coordination.accept": MessageType("engine", VersionPayload),
    "coordination.decline": MessageType("engine", DeclinePayload),
    "coordination.cancel": MessageType("engine", ReasonPayload),
    "approval.request": MessageType("engine", ApprovalRequestPayload),
    "approval.granted": MessageType("engine", ApprovalDecisionPayload),
    "approval.denied": MessageType("engine", ApprovalDecisionPayload),
    "task.status": MessageType("engine", TaskStatusPayload),
    "task.completed": MessageType("engine", TaskCompletedPayload),
    "task.failed": MessageType("engine", TaskFailedPayload),
}
DIRECT_TYPES = tuple(t for t, m in MESSAGE_TYPES.items() if m.origin == "direct")


def payload_schema_id(message_type: str) -> str:
    return f"fcl.{message_type}/1"


def validate_payload(message_type: str, payload: Any) -> dict[str, Any]:
    """Parses a payload against its registered model and returns canonical JSON."""
    model = MESSAGE_TYPES[message_type].payload.model_validate(payload)
    return model.model_dump(mode="json", exclude_none=True)
