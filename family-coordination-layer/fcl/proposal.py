"""The negotiable body of a coordination task."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

INTENT_PATTERN = r"^[a-z][a-z0-9_.]{0,63}$"


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposed_time: Optional[AwareDatetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=24 * 60)
    location: Optional[str] = Field(default=None, min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _not_empty(self) -> "Proposal":
        if not self.model_dump(exclude_none=True):
            raise ValueError("proposal must not be empty")
        return self

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
