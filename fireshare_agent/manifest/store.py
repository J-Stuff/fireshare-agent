"""
Local SQLite-backed record of what has already been uploaded (or permanently failed), so
restarts and manual rescans never re-upload the same file.

Opens a fresh connection per call rather than sharing one across threads - the watcher, upload
worker, and UI can all touch this from different threads and SQLite connections aren't safe to
share across threads without extra locking. Call volume here is low (one write per upload
attempt), so the per-call connection overhead is irrelevant.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fireshare_agent.config.store import app_data_dir

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ALREADY_EXISTED = "already_existed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    fingerprint TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);
CREATE INDEX IF NOT EXISTS idx_uploads_updated_at ON uploads(updated_at_utc DESC);
"""


@dataclass(frozen=True)
class ManifestEntry:
    fingerprint: str
    path: str
    size_bytes: int
    updated_at_utc: datetime
    method: str
    status: str
    error: str | None


class ManifestStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(app_data_dir() / "manifest.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # `with sqlite3.Connection(...) as conn:` only commits/rolls back the transaction on
        # exit - it does NOT close the connection, so a bare `with self._connect() as conn:`
        # per call leaked a connection object every time (relying entirely on CPython's
        # refcounting GC to eventually close it). This wraps both concerns properly.
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def is_already_handled(self, fingerprint: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM uploads WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        # Both a real upload and a confirmed-already-on-the-server hit count as handled; failed
        # entries are retried by the pipeline until its retry budget is exhausted, at which
        # point it stops re-enqueueing them anyway.
        return row is not None and row[0] in (STATUS_SUCCESS, STATUS_ALREADY_EXISTED)

    def record_success(self, fingerprint: str, path: str, size_bytes: int, method: str) -> None:
        self._upsert(fingerprint, path, size_bytes, method, STATUS_SUCCESS, None)

    def record_already_existed(self, fingerprint: str, path: str, size_bytes: int, method: str) -> None:
        self._upsert(fingerprint, path, size_bytes, method, STATUS_ALREADY_EXISTED, None)

    def record_failure(self, fingerprint: str, path: str, size_bytes: int, method: str, error: str) -> None:
        self._upsert(fingerprint, path, size_bytes, method, STATUS_FAILED, error)

    def _upsert(self, fingerprint: str, path: str, size_bytes: int, method: str, status: str, error: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO uploads (fingerprint, path, size_bytes, updated_at_utc, method, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    path = excluded.path,
                    size_bytes = excluded.size_bytes,
                    updated_at_utc = excluded.updated_at_utc,
                    method = excluded.method,
                    status = excluded.status,
                    error = excluded.error
                """,
                (fingerprint, path, size_bytes, datetime.now(timezone.utc).isoformat(), method, status, error),
            )

    def get_failed_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM uploads WHERE status = ?", (STATUS_FAILED,)
            ).fetchone()
        return int(row[0])

    def get_recent(self, limit: int = 100) -> list[ManifestEntry]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT fingerprint, path, size_bytes, updated_at_utc, method, status, error
                FROM uploads ORDER BY updated_at_utc DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            ManifestEntry(
                fingerprint=r[0],
                path=r[1],
                size_bytes=r[2],
                updated_at_utc=datetime.fromisoformat(r[3]),
                method=r[4],
                status=r[5],
                error=r[6],
            )
            for r in rows
        ]
