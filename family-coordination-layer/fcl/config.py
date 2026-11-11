"""Settings come from environment variables, parsed once at startup."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .timeutil import utcnow

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path(".data/fcl.db")
    host: str = "127.0.0.1"
    port: int = 8000
    server_key: bytes | None = None
    allow_loopback_webhooks: bool = False
    max_negotiations: int = 5
    max_task_ttl_seconds: int = 14 * 24 * 3600
    worker_interval_seconds: float = 1.0
    run_worker: bool = True
    body_limit_bytes: int = 64 * 1024
    rate_limit_per_minute: int = 600

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        key = e.get("FCL_SERVER_KEY")
        return Settings(
            db_path=Path(e.get("FCL_DB_PATH", ".data/fcl.db")),
            host=e.get("FCL_HOST", "127.0.0.1"),
            port=int(e.get("FCL_PORT", "8000")),
            server_key=base64.b64decode(key) if key else None,
            allow_loopback_webhooks=e.get("FCL_ALLOW_LOOPBACK_WEBHOOKS", "false") == "true",
            max_negotiations=int(e.get("FCL_MAX_NEGOTIATIONS", "5")),
            max_task_ttl_seconds=int(e.get("FCL_MAX_TASK_TTL_HOURS", str(14 * 24))) * 3600,
            worker_interval_seconds=float(e.get("FCL_WORKER_INTERVAL_SECONDS", "1.0")),
            run_worker=e.get("FCL_RUN_WORKER", "true") == "true",
            rate_limit_per_minute=int(e.get("FCL_RATE_LIMIT_PER_MINUTE", "600")),
        )
