"""`python -m fcl serve` runs the API and the job worker in one process. `python -m fcl migrate` only migrates."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .context import Ctx
from .db import Database, migrate, open_database


def load_or_create_server_key(path: Path) -> bytes:
    """32 random bytes in a 0600 file next to the database, created on first start."""
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise SystemExit(f"{path} must hold exactly 32 bytes")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def build_ctx(settings: Settings) -> Ctx:
    key = settings.server_key or load_or_create_server_key(settings.db_path.with_name("server.key"))
    return Ctx(open_database(settings.db_path), settings, key)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fcl", description="Family Coordination Layer")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API and the job worker")
    serve.add_argument("--db", type=Path, help="SQLite file (env FCL_DB_PATH, default .data/fcl.db)")
    serve.add_argument("--host", help="bind address (env FCL_HOST, default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (env FCL_PORT, default 8000)")
    serve.add_argument("--allow-loopback-webhooks", action="store_true", help="allow http:// webhooks to this machine")
    mig = sub.add_parser("migrate", help="apply database migrations and exit")
    mig.add_argument("--db", type=Path)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.db:
        settings = replace(settings, db_path=args.db)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "migrate":
        applied = migrate(Database(settings.db_path))
        print("applied: " + ", ".join(applied) if applied else "up to date")
        return

    settings = replace(
        settings,
        host=args.host or settings.host,
        port=args.port or settings.port,
        allow_loopback_webhooks=args.allow_loopback_webhooks or settings.allow_loopback_webhooks,
    )
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.signup_token:
        print(f"WARNING: binding {settings.host}. Signup is unauthenticated in this MVP; anyone who can reach "
              "this address can create accounts. Set FCL_SIGNUP_TOKEN. See docs/security.md.", file=sys.stderr)

    import uvicorn

    from . import worker
    from .api import create_app

    ctx = build_ctx(settings)
    hooks = [lambda: worker.start(ctx)] if settings.run_worker else []
    uvicorn.run(create_app(ctx, hooks), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
