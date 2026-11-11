from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["husband", "wife"]
Provider = Literal["claude", "openai"]
ROLES: tuple[Role, ...] = ("husband", "wife")
PROVIDERS: tuple[Provider, ...] = ("claude", "openai")
OTHER: dict[Role, Role] = {"husband": "wife", "wife": "husband"}


@dataclass(frozen=True)
class Pair:
    husband: Provider
    wife: Provider

    def __getitem__(self, role: Role) -> Provider:
        return self.husband if role == "husband" else self.wife

    def label(self) -> str:
        return f"{self.husband}->{self.wife}"


@dataclass
class ToolCall:
    name: str
    args: Any
    result: str
    is_error: bool


@dataclass
class Turn:
    role: Role
    provider: Provider
    say: str
    tool_calls: list[ToolCall]
    reply: str
    model_calls: int


@dataclass(frozen=True)
class Member:
    name: str
    user_id: str
    user_token: str
    agent_id: str
    agent_token: str


@dataclass(frozen=True)
class Family:
    family_id: str
    husband: Member
    wife: Member

    def __getitem__(self, role: Role) -> Member:
        return self.husband if role == "husband" else self.wife


@dataclass
class TaskView:
    id: str
    state: str
    proposal: Any
    raw: Any


@dataclass
class Evidence:
    turns: list[Turn]
    tasks: list[TaskView]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    gate: bool
    detail: str = ""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class EpisodeResult:
    scenario: str
    pair: Pair
    rep: int
    passed: bool
    checks: list[Check]
    turns: list[Turn]
    tasks: list[TaskView]
    model_calls: int
    usd: float
    error: str = ""
    warnings: list[str] = field(default_factory=list)
