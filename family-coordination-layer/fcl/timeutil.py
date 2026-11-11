"""One timestamp format everywhere: UTC, microseconds, trailing Z. Text order equals time order."""

from __future__ import annotations

from datetime import datetime, timezone

FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).strftime(FORMAT)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, FORMAT).replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
