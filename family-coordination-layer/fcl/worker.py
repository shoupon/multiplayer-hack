"""Job kinds and their handlers, in one registry."""

from __future__ import annotations

from typing import Callable

from . import jobs, tasks, webhooks
from .context import Ctx

HANDLERS: dict[str, jobs.Handler] = {
    "task.expire": tasks.expire_job,
    "task.reminder": tasks.reminder_job,
    "webhook.deliver": webhooks.deliver,
}
ON_DEAD: dict[str, jobs.Handler] = {
    "webhook.deliver": webhooks.delivery_dead,
}


def run_due(ctx: Ctx) -> int:
    return jobs.run_due(ctx, HANDLERS, ON_DEAD)


def start(ctx: Ctx) -> Callable[[], None]:
    return jobs.start_worker(ctx, HANDLERS, ON_DEAD)
