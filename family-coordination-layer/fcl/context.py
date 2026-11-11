from __future__ import annotations

from dataclasses import dataclass, field

from .config import Clock, Settings
from .db import Database
from .timeutil import utcnow


@dataclass
class Ctx:
    """Everything a service needs: the database, the clock, settings, and the secret-encryption key."""

    db: Database
    settings: Settings
    encryption_key: bytes
    clock: Clock = field(default=utcnow)
