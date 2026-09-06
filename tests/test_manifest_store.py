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


def test_pause_state_defaults_to_not_paused(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    assert store.is_watching_paused() is False


def test_pause_state_round_trips_through_a_reopen(tmp_path):
    db_path = str(tmp_path / "manifest.db")
    ManifestStore(db_path).set_watching_paused(True)

    assert ManifestStore(db_path).is_watching_paused() is True


def test_pause_state_can_be_cleared(tmp_path):
    db_path = str(tmp_path / "manifest.db")
    store = ManifestStore(db_path)
    store.set_watching_paused(True)
    store.set_watching_paused(False)

    assert ManifestStore(db_path).is_watching_paused() is False
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM agent_state").fetchone()
    assert rows[0] == 1  # upserted in place rather than accumulating a row per toggle


def test_agent_state_table_is_created_in_a_database_that_predates_it(tmp_path):
    # agent_state arrived after the review column, so an existing user's database has neither.
    # A new *table* needs no _migrate() entry - unlike ALTER TABLE, CREATE TABLE IF NOT EXISTS
    # really does apply to an already-populated database - and this pins that.
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

    assert store.is_watching_paused() is False
    store.set_watching_paused(True)
    assert store.is_watching_paused() is True
    assert store.is_already_handled("fp1") is True  # upgrade did not disturb existing history


# --------------------------------------------------------------------------- revision & stats
#
# The main window auto-refreshes its history every five seconds (GitHub issue #7). Rebuilding a
# two-hundred-row view on every tick would throw away the user's scroll position and selection,
# so the window compares `revision` and skips the rebuild when nothing has changed. That makes
# "every write bumps it" a correctness property of the refresh, not an incidental detail.


def test_the_revision_starts_at_a_value_that_can_be_compared(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    assert isinstance(store.revision, int)


def test_every_kind_of_write_bumps_the_revision(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))

    seen = [store.revision]
    store.record_success("fp1", "a.mp4", 1, "web_api")
    seen.append(store.revision)
    store.record_failure("fp2", "b.mp4", 1, "web_api", "boom")
    seen.append(store.revision)
    store.record_already_existed("fp3", "c.mp4", 1, "web_api", pending_review=True)
    seen.append(store.revision)
    store.clear_pending_review("fp3")
    seen.append(store.revision)

    assert seen == sorted(seen) and len(set(seen)) == len(seen)


def test_reading_does_not_bump_the_revision(tmp_path):
    """Otherwise the window would rebuild on every tick regardless - the check would be reading
    its own writes."""
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", "a.mp4", 1, "web_api")

    before = store.revision
    store.get_recent(10)
    store.get_stats()
    store.get_pending_review()
    store.is_already_handled("fp1")

    assert store.revision == before


def test_stats_count_each_status_separately(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", "a.mp4", 100, "web_api")
    store.record_success("fp2", "b.mp4", 200, "web_api")
    store.record_failure("fp3", "c.mp4", 300, "web_api", "boom")
    store.record_already_existed("fp4", "d.mp4", 400, "web_api", pending_review=True)

    stats = store.get_stats()
    assert (stats.uploaded, stats.failed, stats.already_on_server) == (2, 1, 1)
    assert stats.pending_review == 1
    assert stats.total == 4


def test_bytes_uploaded_counts_only_what_was_actually_transferred(tmp_path):
    """A file matched to one already on the server was never sent. Counting it would inflate a
    number the user reads as "how much of my upstream this has spent"."""
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", "a.mp4", 100, "web_api")
    store.record_already_existed("fp2", "b.mp4", 999_999, "web_api")
    store.record_failure("fp3", "c.mp4", 555_555, "web_api", "boom")

    assert store.get_stats().bytes_uploaded == 100


def test_stats_on_an_empty_manifest_are_zeroes_not_an_error(tmp_path):
    stats = ManifestStore(str(tmp_path / "manifest.db")).get_stats()
    assert (stats.uploaded, stats.failed, stats.already_on_server, stats.bytes_uploaded) == (0, 0, 0, 0)
