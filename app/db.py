"""SQLite ⇄ Postgres abstraction.

The rest of the app speaks one small SQL dialect: ``?`` placeholders, ``TEXT``
timestamps and JSON stored as text. :class:`Database` translates that dialect
for Postgres when ``DATABASE_URL`` is set, so callers never branch on the
backend.

Connections are opened per call rather than kept in a pool: serverless
instances are reused across requests and per-call connections keep that safe,
including for SQLite where a long-lived handle would be bound to whichever
thread happened to create it.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app import settings

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        tool TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        mimetype TEXT NOT NULL DEFAULT 'application/octet-stream',
        size INTEGER NOT NULL DEFAULT 0,
        path TEXT,
        data BLOB,
        meta TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS documents_batch_idx ON documents (batch_id)",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        status TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs (batch_id)",
)


class Database:
    """A tiny SQL runner shared by both backends."""

    def __init__(self, url: str = "", sqlite_path: Path | str | None = None) -> None:
        self.url = (url or settings.database_url).strip()
        self.is_postgres = self.url.startswith(("postgres://", "postgresql://"))
        self.sqlite_path = Path(sqlite_path or settings.sqlite_path)

    # ------------------------------------------------------------------ connect
    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.sqlite_path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    # -------------------------------------------------------------------- reads
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return [_as_dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------- writes
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        items = [tuple(row) for row in rows]
        if not items:
            return 0
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.executemany(self._sql(sql), items)
            else:
                cur.executemany(sql, items)
            return cur.rowcount

    # -------------------------------------------------------------------- setup
    def init_schema(self) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            for statement in SCHEMA:
                sql = statement
                if self.is_postgres:
                    sql = sql.replace("BLOB", "BYTEA")
                cur.execute(sql)

    def drop_schema(self) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            for table in ("jobs", "documents", "batches"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")


def _as_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def reset_db(url: str = "", sqlite_path: Path | str | None = None) -> Database:
    """Rebuild the singleton; used by tests and by the admin maintenance route."""
    global _db
    _db = Database(url, sqlite_path)
    return _db


def init_db() -> Database:
    db = get_db()
    db.init_schema()
    return db
