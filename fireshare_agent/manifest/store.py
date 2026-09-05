"""
Local SQLite-backed record of what has already been uploaded (or permanently failed), so
restarts and manual rescans never re-upload the same file.

Also carries a small `agent_state` key/value table for runtime state that has to outlive a
restart but is not a user-authored setting - currently just the pause flag. That deliberately
does not live in config.json: saving Settings rewrites the whole AppConfig from memory, so a
value the tray can change behind the settings window's back would be clobbered on the next save.

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

CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Values are stored as text rather than integers so this table can hold non-boolean state later
# without a schema change.
_STATE_WATCHING_PAUSED = "watching_paused"


@dataclass(frozen=True)
class ManifestEntry:
    fingerprint: str
    path: str
    size_bytes: int
    updated_at_utc: datetime
    method: str
    status: str
    error: str | None
    pending_review: bool = False


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
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive-only schema upgrades for a database written by an older version. Kept here
        rather than in _SCHEMA because CREATE TABLE IF NOT EXISTS won't add a column to a table
        that already exists - an agent upgraded in place would otherwise fail every query that
        mentions a newer column."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(uploads)")}
        if "pending_review" not in columns:
            conn.execute("ALTER TABLE uploads ADD COLUMN pending_review INTEGER NOT NULL DEFAULT 0")

    def is_already_handled(self, fingerprint: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM uploads WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        # Both a real upload and a confirmed-already-on-the-server hit count as handled; failed
        # entries are retried by the pipeline until its retry budget is exhausted, at which
        # point it stops re-enqueueing them anyway. A row still awaiting the user's review is
        # handled too - the file is on the server either way, only the local copy's fate is
        # still undecided.
        return row is not None and row[0] in (STATUS_SUCCESS, STATUS_ALREADY_EXISTED)

    def record_success(self, fingerprint: str, path: str, size_bytes: int, method: str) -> None:
        self._upsert(fingerprint, path, size_bytes, method, STATUS_SUCCESS, None, pending_review=False)

    def record_already_existed(
        self, fingerprint: str, path: str, size_bytes: int, method: str, pending_review: bool = False
    ) -> None:
        """pending_review=True means this file was matched to a server-side file by name alone,
        so the local copy must not be moved or deleted until the user confirms. See
        WebApiUploader.exists_at_destination for why that match can't be verified exactly."""
        self._upsert(fingerprint, path, size_bytes, method, STATUS_ALREADY_EXISTED, None, pending_review)

    def record_failure(self, fingerprint: str, path: str, size_bytes: int, method: str, error: str) -> None:
        self._upsert(fingerprint, path, size_bytes, method, STATUS_FAILED, error, pending_review=False)

    def _upsert(
        self, fingerprint: str, path: str, size_bytes: int, method: str,
        status: str, error: str | None, pending_review: bool,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO uploads (fingerprint, path, size_bytes, updated_at_utc, method, status, error, pending_review)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    path = excluded.path,
                    size_bytes = excluded.size_bytes,
                    updated_at_utc = excluded.updated_at_utc,
                    method = excluded.method,
                    status = excluded.status,
                    error = excluded.error,
                    pending_review = excluded.pending_review
                """,
                (
                    fingerprint, path, size_bytes, datetime.now(timezone.utc).isoformat(),
                    method, status, error, 1 if pending_review else 0,
                ),
            )

    def get_failed_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM uploads WHERE status = ?", (STATUS_FAILED,)
            ).fetchone()
        return int(row[0])

    def get_pending_review_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM uploads WHERE pending_review = 1").fetchone()
        return int(row[0])

    def get_pending_review(self) -> list[ManifestEntry]:
        """Files matched to a server-side file by name that are still awaiting the user's
        decision about the local copy. Oldest first - this is a to-do list, so whatever has been
        waiting longest belongs at the top."""
        return self._select("WHERE pending_review = 1 ORDER BY updated_at_utc ASC", ())

    def clear_pending_review(self, fingerprint: str) -> None:
        """Records that the user has decided. The row itself stays - it is still the dedupe
        record that stops this file being uploaded again."""
        with self._connection() as conn:
            conn.execute("UPDATE uploads SET pending_review = 0 WHERE fingerprint = ?", (fingerprint,))

    def is_watching_paused(self) -> bool:
        """Whether the user left the agent paused. Read once at startup to restore the pause
        across restarts; defaults to False for a database written before this table existed."""
        return self._get_flag(_STATE_WATCHING_PAUSED, default=False)

    def set_watching_paused(self, paused: bool) -> None:
        self._set_flag(_STATE_WATCHING_PAUSED, paused)

    def _get_flag(self, key: str, default: bool) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
        return default if row is None else row[0] == "1"

    def _set_flag(self, key: str, value: bool) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, "1" if value else "0"),
            )

    def get_recent(self, limit: int = 100) -> list[ManifestEntry]:
        return self._select("ORDER BY updated_at_utc DESC LIMIT ?", (limit,))

    def _select(self, clause: str, params: tuple) -> list[ManifestEntry]:
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT fingerprint, path, size_bytes, updated_at_utc, method, status, error, pending_review
                FROM uploads {clause}
                """,
                params,
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
                pending_review=bool(r[7]),
            )
            for r in rows
        ]
