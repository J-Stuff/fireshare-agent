import sqlite3

from fireshare_agent.manifest.store import ManifestStore

# The exact schema shipped before the review queue existed. Kept verbatim so the in-place upgrade
# path is tested against what is actually on an existing user's disk, not against a paraphrase.
_PRE_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    fingerprint TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
"""


def test_unknown_fingerprint_is_not_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    assert store.is_already_handled("nope") is False


def test_recorded_success_is_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", r"C:\clip.mp4", 1234, "web_api")

    assert store.is_already_handled("fp1") is True


def test_recorded_failure_is_not_treated_as_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_failure("fp1", r"C:\clip.mp4", 1234, "web_api", "network error")

    assert store.is_already_handled("fp1") is False
    assert store.get_failed_count() == 1


def test_success_after_failure_overwrites_status(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_failure("fp1", r"C:\clip.mp4", 1234, "web_api", "network error")
    store.record_success("fp1", r"C:\clip.mp4", 1234, "web_api")

    assert store.is_already_handled("fp1") is True
    assert store.get_failed_count() == 0


def test_get_recent_orders_newest_first(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", "a.mp4", 1, "web_api")
    store.record_success("fp2", "b.mp4", 2, "web_api")

    recent = store.get_recent()
    assert [e.fingerprint for e in recent] == ["fp2", "fp1"]


def test_pending_review_round_trip(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_already_existed("fp1", r"C:\clip.mp4", 1234, "web_api", pending_review=True)

    pending = store.get_pending_review()
    assert [e.fingerprint for e in pending] == ["fp1"]
    assert pending[0].pending_review is True
    assert store.get_pending_review_count() == 1
    # Awaiting review is still "handled" - it must not be re-uploaded in the meantime.
    assert store.is_already_handled("fp1") is True

    store.clear_pending_review("fp1")

    assert store.get_pending_review() == []
    assert store.get_pending_review_count() == 0
    assert store.is_already_handled("fp1") is True  # the dedupe record itself survives


def test_already_existed_does_not_queue_for_review_by_default(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_already_existed("fp1", r"C:\clip.mp4", 1234, "web_api")

    assert store.get_pending_review_count() == 0


def test_opens_a_database_written_before_the_review_column_existed(tmp_path):
    # An agent upgraded in place meets a table that already exists, so CREATE TABLE IF NOT EXISTS
    # is a no-op and will not add the new column - every query mentioning it would fail with
    # "no such column" until the additive migration runs.
    db_path = tmp_path / "manifest.db"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.executescript(_PRE_REVIEW_SCHEMA)
        conn.execute(
            "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fp1", r"C:\old_clip.mp4", 99, "2026-01-01T00:00:00+00:00", "web_api", "success", None),
        )
    conn.close()

    store = ManifestStore(str(db_path))

    assert store.is_already_handled("fp1") is True  # pre-existing history survives the upgrade
    assert store.get_pending_review_count() == 0
    assert store.get_recent()[0].pending_review is False


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "manifest.db"
    ManifestStore(str(db_path))
    store = ManifestStore(str(db_path))  # second open must not try to re-add the column

    store.record_already_existed("fp1", r"C:\clip.mp4", 1, "web_api", pending_review=True)
    assert store.get_pending_review_count() == 1
