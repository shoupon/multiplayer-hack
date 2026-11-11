"""Database-backed job queue and the worker thread that drains it.

Jobs are written in the same transaction as the state change that needs them (the outbox), so a
committed change always has its follow-up work queued, and a rolled-back change never does.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .context import Ctx
from .db import Conn
from .timeutil import to_db

log = logging.getLogger("fcl.jobs")

LEASE = timedelta(minutes=2)

Handler = Callable[[Ctx, dict[str, Any]], None]


class Retry(Exception):
    """Raised by a handler to retry later with backoff."""


def enqueue(c: Conn, now: datetime, kind: str, payload: dict[str, Any], run_at: datetime,
            dedupe_key: Optional[str] = None, max_attempts: int = 8) -> None:
    """Idempotent when `dedupe_key` is set: a second enqueue of the same key is a no-op."""
    c.run(
        """INSERT INTO scheduled_jobs (kind, payload, run_at, max_attempts, dedupe_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (dedupe_key) DO NOTHING""",
        (kind, json.dumps(payload), to_db(run_at), max_attempts, dedupe_key, to_db(now), to_db(now)),
    )


def backoff(attempts: int) -> timedelta:
    """Exponential with jitter: ~2s, 4s, 8s ... capped at 10 minutes."""
    base = min(2 ** attempts, 600)
    return timedelta(seconds=base * (0.5 + random.random() / 2))


def _claim(ctx: Ctx) -> Optional[dict[str, Any]]:
    now = ctx.clock()
    with ctx.db.tx() as c:
        job = c.one(
            """SELECT * FROM scheduled_jobs
                WHERE (status = 'pending' AND run_at <= ?) OR (status = 'running' AND locked_until <= ?)
                ORDER BY run_at, id LIMIT 1""",
            (to_db(now), to_db(now)),
        )
        if job is None:
            return None
        c.run(
            "UPDATE scheduled_jobs SET status = 'running', attempts = attempts + 1, locked_until = ?, updated_at = ? WHERE id = ?",
            (to_db(now + LEASE), to_db(now), job["id"]),
        )
        return {**job, "attempts": job["attempts"] + 1}


def _finish(ctx: Ctx, job: dict[str, Any], error: Optional[str]) -> bool:
    """Records the outcome. Returns True when the job just went to the dead-letter state."""
    now = ctx.clock()
    with ctx.db.tx() as c:
        if error is None:
            c.run("UPDATE scheduled_jobs SET status = 'done', locked_until = NULL, updated_at = ? WHERE id = ?",
                  (to_db(now), job["id"]))
        elif job["attempts"] >= job["max_attempts"]:
            c.run("UPDATE scheduled_jobs SET status = 'dead', locked_until = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                  (error[:1000], to_db(now), job["id"]))
            return True
        else:
            c.run(
                """UPDATE scheduled_jobs SET status = 'pending', locked_until = NULL, last_error = ?, run_at = ?,
                          updated_at = ? WHERE id = ?""",
                (error[:1000], to_db(now + backoff(job["attempts"])), to_db(now), job["id"]),
            )
    return False


def run_due(ctx: Ctx, handlers: dict[str, Handler], on_dead: Optional[dict[str, Handler]] = None, max_jobs: int = 100) -> int:
    """Runs due jobs until none are left (or `max_jobs`). Returns how many ran."""
    ran = 0
    while ran < max_jobs:
        job = _claim(ctx)
        if job is None:
            break
        ran += 1
        handler = handlers.get(job["kind"])
        payload = json.loads(job["payload"])
        try:
            if handler is None:
                raise RuntimeError(f"no handler for job kind {job['kind']!r}")
            handler(ctx, payload)
            dead = _finish(ctx, job, None)
        except Retry as e:
            dead = _finish(ctx, job, str(e) or "retry")
        except Exception as e:  # a failing job must never kill the worker
            log.warning("job %s (%s) failed: %s", job["id"], job["kind"], e)
            dead = _finish(ctx, job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        if dead and on_dead and job["kind"] in on_dead:
            on_dead[job["kind"]](ctx, payload)
    return ran


def start_worker(ctx: Ctx, handlers: dict[str, Handler], on_dead: Optional[dict[str, Handler]] = None) -> Callable[[], None]:
    """Starts the worker thread; returns a function that stops it."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                run_due(ctx, handlers, on_dead)
            except Exception:
                log.exception("worker iteration failed")
            stop.wait(ctx.settings.worker_interval_seconds)

    thread = threading.Thread(target=loop, name="fcl-worker", daemon=True)
    thread.start()

    def halt() -> None:
        stop.set()
        thread.join(timeout=5)

    return halt
