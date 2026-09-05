"""
Regression coverage for the readiness-timeout bug: a file that was still being written used to
be retried in a blocking loop for ~10 minutes and then permanently abandoned (no periodic
re-scan or startup sync existed to ever pick it up again). A single ShadowPlay "Record" session
(as opposed to a quick Instant Replay clip) can legitimately stay open and growing for hours, so
this would silently lose real recordings. Fixed by requeuing via a non-blocking timer instead of
looping in place, with a generous 24h cap for a genuinely stuck file rather than a fixed attempt
count.
"""
import os
import sqlite3
import time

import pytest

from fireshare_agent.config.app_config import AppConfig, WatchFolderConfig
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.models import PostUploadAction, UploadResult
from fireshare_agent.pipeline import upload_pipeline
from fireshare_agent.pipeline.activity import PipelineEventKind
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline


def _pipeline(tmp_path, config: AppConfig | None = None) -> UploadPipeline:
    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    return UploadPipeline(manifest, config or AppConfig())


class _FakeUploader:
    """A stand-in uploader whose upload() returns a scripted sequence of results, so a retry
    scenario can be driven without a real Fireshare server."""

    def __init__(self, results: list[UploadResult]) -> None:
        self._results = list(results)
        self.call_count = 0

    def exists_at_destination(self, file) -> bool:
        return False

    def upload(self, file) -> UploadResult:
        self.call_count += 1
        return self._results.pop(0)


def test_not_ready_file_is_requeued_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)

    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    pipeline._in_flight[str(clip)] = time.monotonic()
    try:
        resolved = pipeline._process_candidate(str(clip))

        assert resolved is False  # must not block the worker waiting for readiness
        assert str(clip) in pipeline._in_flight  # stays tracked so it isn't double-enqueued
        with pipeline._pending_retry_timers_lock:
            assert len(pipeline._pending_retry_timers) == 1
    finally:
        pipeline.stop()  # cancels the pending timer


def test_not_ready_file_raises_a_waiting_activity_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)

    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    activities = []
    pipeline.add_activity_listener(activities.append)
    pipeline._in_flight[str(clip)] = time.monotonic()

    try:
        pipeline._process_candidate(str(clip))
        assert activities[-1].event_kind == PipelineEventKind.WAITING
    finally:
        pipeline.stop()


def test_file_stuck_past_the_max_wait_is_finally_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)

    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    activities = []
    pipeline.add_activity_listener(activities.append)
    # Simulate having first seen this file more than the 24h cap ago.
    pipeline._in_flight[str(clip)] = time.monotonic() - (upload_pipeline._MAX_WAIT_FOR_READY_SECONDS + 60)

    try:
        resolved = pipeline._process_candidate(str(clip))

        assert resolved is True  # finally resolved (as a failure), not requeued forever
        assert activities[-1].event_kind == PipelineEventKind.FAILED
        with pipeline._pending_retry_timers_lock:
            assert len(pipeline._pending_retry_timers) == 0
    finally:
        pipeline.stop()


def test_failed_upload_is_retried_via_a_timer_without_blocking(tmp_path, monkeypatch):
    # Regression test: a failed upload attempt used to be retried with a blocking time.sleep()
    # inside the single worker thread, so one failing file (e.g. the server briefly unreachable)
    # stalled every other already-queued file for its entire retry budget. It must instead
    # requeue via a non-blocking timer, like the "file still being written" path already does.
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)

    config = AppConfig(max_retry_attempts=3, retry_backoff_seconds=9999)
    pipeline = _pipeline(tmp_path, config)
    fake_uploader = _FakeUploader([UploadResult.fail("boom")])
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: fake_uploader)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    try:
        started = time.monotonic()
        resolved = pipeline._process_candidate(str(clip))
        elapsed = time.monotonic() - started

        assert resolved is False  # not yet exhausted its retry budget
        assert elapsed < 5  # must not have blocked on the (huge) backoff
        assert fake_uploader.call_count == 1
        with pipeline._retry_state_lock:
            assert str(clip) in pipeline._retry_state
        with pipeline._pending_retry_timers_lock:
            assert len(pipeline._pending_retry_timers) == 1
    finally:
        pipeline.stop()  # cancels the pending retry timer


def test_failed_upload_succeeds_on_a_later_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)

    config = AppConfig(max_retry_attempts=3, retry_backoff_seconds=9999)
    pipeline = _pipeline(tmp_path, config)
    fake_uploader = _FakeUploader([UploadResult.fail("boom"), UploadResult.ok()])
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: fake_uploader)

    activities = []
    pipeline.add_activity_listener(activities.append)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    try:
        assert pipeline._process_candidate(str(clip)) is False  # first attempt fails

        # Simulate the scheduled timer firing and the worker picking the retry back up, without
        # actually waiting out the backoff.
        resolved = pipeline._process_candidate(str(clip))

        assert resolved is True
        assert fake_uploader.call_count == 2
        assert activities[-1].event_kind == PipelineEventKind.SUCCEEDED
        with pipeline._retry_state_lock:
            assert str(clip) not in pipeline._retry_state
    finally:
        pipeline.stop()


def test_failed_upload_records_failure_after_exhausting_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)

    config = AppConfig(max_retry_attempts=2, retry_backoff_seconds=9999)
    pipeline = _pipeline(tmp_path, config)
    fake_uploader = _FakeUploader([UploadResult.fail("boom"), UploadResult.fail("boom again")])
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: fake_uploader)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    try:
        assert pipeline._process_candidate(str(clip)) is False  # attempt 1/2 fails, retry scheduled
        resolved = pipeline._process_candidate(str(clip))  # attempt 2/2 fails, budget exhausted

        assert resolved is True
        assert pipeline._manifest.get_failed_count() == 1
        with pipeline._retry_state_lock:
            assert str(clip) not in pipeline._retry_state
    finally:
        pipeline.stop()


def test_ready_file_with_no_matching_extension_resolves_immediately(tmp_path):
    pipeline = _pipeline(tmp_path)
    unrelated = tmp_path / "notes.txt"
    unrelated.write_bytes(b"x")

    resolved = pipeline._process_candidate(str(unrelated))

    assert resolved is True


def test_deleted_file_resolves_immediately(tmp_path):
    pipeline = _pipeline(tmp_path)
    missing = tmp_path / "gone.mp4"  # never created

    resolved = pipeline._process_candidate(str(missing))

    assert resolved is True


def test_remote_folder_hint_uses_the_immediate_subfolder_name(tmp_path):
    # Mirrors ShadowPlay's default layout: <capture root>/<game name>/<clip>.mp4
    capture_root = tmp_path / "recordings"
    game_dir = capture_root / "SomeGame"
    game_dir.mkdir(parents=True)
    clip = game_dir / "clip.mp4"
    clip.write_bytes(b"x")

    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(capture_root))])
    pipeline = UploadPipeline(manifest, config)

    assert pipeline._compute_remote_folder_hint(str(clip)) == "SomeGame"


def test_remote_folder_hint_is_none_for_a_file_directly_in_the_watch_root(tmp_path):
    capture_root = tmp_path / "recordings"
    capture_root.mkdir()
    clip = capture_root / "clip.mp4"
    clip.write_bytes(b"x")

    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(capture_root))])
    pipeline = UploadPipeline(manifest, config)

    assert pipeline._compute_remote_folder_hint(str(clip)) is None


def test_remote_folder_hint_is_none_outside_any_watch_folder(tmp_path):
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    clip = other_dir / "clip.mp4"
    clip.write_bytes(b"x")

    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(tmp_path / "recordings"))])
    pipeline = UploadPipeline(manifest, config)

    assert pipeline._compute_remote_folder_hint(str(clip)) is None


def test_sync_now_skips_the_post_upload_subfolder(tmp_path, monkeypatch):
    # Regression test: after a "move to subfolder" upload, that subfolder sits inside the same
    # recursively-watched tree. Without pruning it, every rescan (Sync Now, or the startup sync)
    # would walk back into it and re-hash every already-uploaded file there forever.
    capture_root = tmp_path / "recordings"
    game_dir = capture_root / "SomeGame"
    uploaded_dir = game_dir / "Uploaded"
    uploaded_dir.mkdir(parents=True)
    (game_dir / "new_clip.mp4").write_bytes(b"x")
    (uploaded_dir / "old_clip.mp4").write_bytes(b"x")

    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(capture_root))], move_to_subfolder_name="Uploaded")
    pipeline = UploadPipeline(manifest, config)

    enqueued = []
    monkeypatch.setattr(pipeline, "_enqueue", enqueued.append)

    pipeline.sync_now()

    assert enqueued == [str(game_dir / "new_clip.mp4")]


class _AlreadyThereUploader:
    """An uploader that claims every file is already on the server, to drive the
    exists_at_destination path."""

    def exists_at_destination(self, file) -> bool:
        return True

    def upload(self, file) -> UploadResult:  # pragma: no cover - must never be reached
        raise AssertionError("upload() must not be called for a file already at the destination")


def _queue_one_for_review(tmp_path, monkeypatch, post_upload_action: str):
    """Runs a clip through the pipeline with an uploader that reports it already exists, and
    returns (pipeline, clip_path, review_entry)."""
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)

    config = AppConfig(post_upload_action=post_upload_action, move_to_subfolder_name="Uploaded")
    pipeline = _pipeline(tmp_path, config)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: _AlreadyThereUploader())

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    assert pipeline._process_candidate(str(clip)) is True
    pending = pipeline.get_pending_review()
    assert len(pending) == 1
    return pipeline, clip, pending[0]


@pytest.mark.parametrize("action", [PostUploadAction.DELETE.value, PostUploadAction.MOVE_TO_SUBFOLDER.value])
def test_inferred_server_match_never_touches_the_local_file(tmp_path, monkeypatch, action):
    # The core data-loss regression: exists_at_destination() matches on filename alone, so acting
    # on it automatically could move - or with the delete action configured, destroy - a local
    # file that was never actually uploaded. It must only ever be queued for review.
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, action)

    assert clip.exists()
    assert entry.pending_review is True
    assert pipeline.pending_review_count == 1
    # Still deduped: it must not be re-uploaded while awaiting review.
    assert pipeline._manifest.is_already_handled(entry.fingerprint) is True


def test_review_keep_leaves_the_file_and_clears_the_queue(tmp_path, monkeypatch):
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, PostUploadAction.DELETE.value)

    outcome = pipeline.resolve_pending_review(entry, PostUploadAction.LEAVE)

    assert outcome.resolved is True
    assert clip.exists()
    assert pipeline.pending_review_count == 0


def test_review_delete_removes_the_file(tmp_path, monkeypatch):
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, PostUploadAction.LEAVE.value)

    outcome = pipeline.resolve_pending_review(entry, PostUploadAction.DELETE)

    assert outcome.resolved is True
    assert not clip.exists()
    assert pipeline.pending_review_count == 0


def test_review_move_relocates_the_file_to_the_configured_subfolder(tmp_path, monkeypatch):
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, PostUploadAction.LEAVE.value)

    outcome = pipeline.resolve_pending_review(entry, PostUploadAction.MOVE_TO_SUBFOLDER)

    assert outcome.resolved is True
    assert not clip.exists()
    assert (tmp_path / "Uploaded" / "clip.mp4").exists()
    assert "Uploaded" in outcome.message
    assert pipeline.pending_review_count == 0


def test_review_of_an_already_deleted_file_resolves_instead_of_erroring(tmp_path, monkeypatch):
    # The user may deal with the file in Explorer before getting to the review queue; the entry
    # is then obsolete rather than failed, and must not be stranded in the list forever.
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, PostUploadAction.LEAVE.value)
    clip.unlink()

    outcome = pipeline.resolve_pending_review(entry, PostUploadAction.DELETE)

    assert outcome.resolved is True
    assert "no longer on disk" in outcome.message
    assert pipeline.pending_review_count == 0


def test_review_failure_leaves_the_entry_queued_for_another_try(tmp_path, monkeypatch):
    # A locked file is exactly the case the user can fix and retry, so the entry must survive.
    pipeline, clip, entry = _queue_one_for_review(tmp_path, monkeypatch, PostUploadAction.LEAVE.value)

    def _boom(path, action):
        raise OSError("file is in use by another process")

    monkeypatch.setattr(pipeline, "perform_local_action", _boom)
    outcome = pipeline.resolve_pending_review(entry, PostUploadAction.DELETE)

    assert outcome.resolved is False
    assert "in use" in outcome.message
    assert pipeline.pending_review_count == 1  # still awaiting a decision


def test_successful_upload_still_applies_the_post_upload_action(tmp_path, monkeypatch):
    # Guard against over-correcting: only the *inferred* match is held back. A real, confirmed
    # upload must still honour the configured action.
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)

    config = AppConfig(post_upload_action=PostUploadAction.DELETE.value)
    pipeline = _pipeline(tmp_path, config)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: _FakeUploader([UploadResult.ok()]))

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")

    assert pipeline._process_candidate(str(clip)) is True
    assert not clip.exists()
    assert pipeline.pending_review_count == 0


def test_remote_folder_hint_normalizes_nested_paths_to_forward_slashes(tmp_path):
    capture_root = tmp_path / "recordings"
    nested_dir = capture_root / "Game" / "Highlights"
    nested_dir.mkdir(parents=True)
    clip = nested_dir / "clip.mp4"
    clip.write_bytes(b"x")

    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(capture_root))])
    pipeline = UploadPipeline(manifest, config)

    hint = pipeline._compute_remote_folder_hint(str(clip))
    assert hint == "Game/Highlights"
    assert os.sep not in hint or os.sep == "/"


def test_saving_settings_does_not_resume_a_paused_pipeline(tmp_path):
    """The user-visible shape of the folder_watcher pause-reset bug: pause from the tray, open
    Settings, save anything at all, and watching used to silently resume while the tray icon and
    menu label both went on insisting the agent was paused."""
    watched = tmp_path / "clips"
    watched.mkdir()
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(watched))])
    pipeline = _pipeline(tmp_path, config)

    pipeline.pause()
    assert pipeline.is_paused is True

    pipeline.update_config(AppConfig(watch_folders=[WatchFolderConfig(path=str(watched))]))

    try:
        assert pipeline.is_paused is True
    finally:
        pipeline.stop()


def test_pausing_persists_across_a_restart(tmp_path):
    """A pause the user set from the tray has to outlive the app: previously it lived only in the
    FolderWatcherService instance, so any restart came back up watching."""
    manifest_path = str(tmp_path / "manifest.db")
    config = AppConfig()

    first_run = UploadPipeline(ManifestStore(manifest_path), config)
    first_run.pause()

    restarted = UploadPipeline(ManifestStore(manifest_path), config)
    assert restarted.is_paused is True


def test_resuming_persists_across_a_restart(tmp_path):
    manifest_path = str(tmp_path / "manifest.db")
    config = AppConfig()

    first_run = UploadPipeline(ManifestStore(manifest_path), config)
    first_run.pause()
    first_run.resume()

    assert UploadPipeline(ManifestStore(manifest_path), config).is_paused is False


def test_a_pipeline_with_no_stored_state_starts_unpaused(tmp_path):
    assert _pipeline(tmp_path).is_paused is False


def test_pause_still_applies_when_it_cannot_be_persisted(tmp_path, monkeypatch):
    """The toggle runs on the tray's callback thread. A database problem must not propagate out of
    it, and must not leave the agent watching while the tray reports it as paused - losing the
    state at the next restart is the acceptable failure, lying about it now is not."""
    manifest = ManifestStore(str(tmp_path / "manifest.db"))

    def _boom(paused: bool) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(manifest, "set_watching_paused", _boom)
    pipeline = UploadPipeline(manifest, AppConfig())
    pipeline.pause()

    assert pipeline.is_paused is True
