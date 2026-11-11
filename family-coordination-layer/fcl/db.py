"""SQLite access: one connection per thread, explicit write transactions, migrations."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Sequence

if sqlite3.sqlite_version_info < (3, 35, 0):
    raise RuntimeError(f"FCL needs SQLite 3.35+ for RETURNING; found {sqlite3.sqlite_version}")

Row = dict[str, Any]


class Conn:
    """Thin wrapper so services read rows as dicts and never touch the raw cursor API."""

    def __init__(self, raw: sqlite3.Connection):
        self.raw = raw

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return [dict(r) for r in self.raw.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        r = self.raw.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def run(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self.raw.execute(sql, params).rowcount

    def script(self, sql: str) -> None:
        # sqlite3.executescript() commits any open transaction first, so run statement by statement.
        for stmt in split_statements(sql):
            self.raw.execute(stmt)


def split_statements(sql: str) -> list[str]:
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement: {buf[:80]}")
    return statements


def _connect(path: Path) -> sqlite3.Connection:
    raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode = WAL")
    raw.execute("PRAGMA synchronous = NORMAL")
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 10000")
    return raw


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _raw(self) -> sqlite3.Connection:
        raw = getattr(self._local, "raw", None)
        if raw is None:
            raw = _connect(self.path)
            self._local.raw = raw
            with self._lock:
                self._all.append(raw)
        return raw

    @contextmanager
    def tx(self) -> Iterator[Conn]:
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front, so commands serialize."""
        raw = self._raw()
        if raw.in_transaction:
            raise RuntimeError("nested transaction")
        raw.execute("BEGIN IMMEDIATE")
        try:
            yield Conn(raw)
        except BaseException:
            raw.execute("ROLLBACK")
            raise
        raw.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[Conn]:
        """Read transaction: one consistent snapshot across several queries."""
        raw = self._raw()
        if raw.in_transaction:
            raise RuntimeError("nested transaction")
        raw.execute("BEGIN")
        try:
            yield Conn(raw)
        finally:
            raw.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            for raw in self._all:
                raw.close()
            self._all.clear()
        self._local = threading.local()


def migrate(db: Database) -> list[str]:
    """Applies pending migrations in order inside one write transaction. Repeat runs are no-ops."""
    files = sorted(
        (f for f in resources.files("fcl.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with db.tx() as c:
        c.run("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r["name"] for r in c.all("SELECT name FROM schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            c.script(f.read_text())
            c.run("INSERT INTO schema_migrations (name, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))", (f.name,))
            applied.append(f.name)
    return applied


def open_database(path: Path) -> Database:
    db = Database(path)
    migrate(db)
    return db
