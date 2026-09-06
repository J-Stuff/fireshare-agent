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
import threading
import time

import pytest

from fireshare_agent.config.app_config import AppConfig, WatchFolderConfig
from fireshare_agent.manifest import fingerprint
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

    def __init__(self, results: list[UploadResult], progress_steps: list[int] | None = None) -> None:
        self._results = list(results)
        self._progress_steps = list(progress_steps or [])
        self.call_count = 0
        self.received_progress_callback = False

    def exists_at_destination(self, file) -> bool:
        return False

    def upload(self, file, on_progress=None) -> UploadResult:
        self.call_count += 1
        self.received_progress_callback = on_progress is not None
        if on_progress is not None:
            for sent in self._progress_steps:
                on_progress(sent, file.size_bytes)
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

    def upload(self, file, on_progress=None) -> UploadResult:  # pragma: no cover - must never be reached
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


# --- Startup rescan must not pay the 3s readiness sleep for already-uploaded files -------------
#
# is_ready() sleeps unconditionally for its stable-size window, and it used to run *before* the
# manifest lookup. With the default post_upload_action = LEAVE every uploaded file stays in the
# watch folder and is re-walked by sync_now() on every launch, so a few hundred clips meant many
# minutes of pure sleeping (plus a discarded head/tail read each) before the first genuinely new
# file was even looked at. The dedupe check must come first.


def _explode_if_called(path):  # pragma: no cover - the assertion is the point
    raise AssertionError("is_ready() must not be called for an already-handled file")


def test_already_uploaded_file_skips_the_readiness_sleep_entirely(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"already uploaded contents")
    size = clip.stat().st_size
    pipeline._manifest.record_success(fingerprint.compute(str(clip), size), str(clip), size, "web_api")

    monkeypatch.setattr(upload_pipeline, "is_ready", _explode_if_called)
    activities = []
    pipeline.add_activity_listener(activities.append)

    resolved = pipeline._process_candidate(str(clip))

    assert resolved is True
    assert activities[-1].event_kind == PipelineEventKind.SKIPPED_DUPLICATE


def test_file_matched_to_the_server_and_awaiting_review_also_skips_readiness(tmp_path, monkeypatch):
    # record_already_existed(pending_review=True) counts as handled: the file is on the server
    # either way, so a rescan must not re-probe it while the user takes their time deciding.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"on the server already")
    size = clip.stat().st_size
    pipeline._manifest.record_already_existed(
        fingerprint.compute(str(clip), size), str(clip), size, "web_api", pending_review=True
    )

    monkeypatch.setattr(upload_pipeline, "is_ready", _explode_if_called)

    assert pipeline._process_candidate(str(clip)) is True


def test_a_previously_failed_file_still_goes_through_the_readiness_probe(tmp_path, monkeypatch):
    # Only success/already-existed count as handled - a failed row must not be short-circuited,
    # or a retry would never reach the uploader again.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"failed last time")
    size = clip.stat().st_size
    pipeline._manifest.record_failure(
        fingerprint.compute(str(clip), size), str(clip), size, "web_api", "boom"
    )

    calls = []

    def _record_ready(path):
        calls.append(path)
        return True

    monkeypatch.setattr(upload_pipeline, "is_ready", _record_ready)
    fake_uploader = _FakeUploader([UploadResult.ok()])
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: fake_uploader)

    assert pipeline._process_candidate(str(clip)) is True
    assert calls == [str(clip)]
    assert fake_uploader.call_count == 1


def test_a_new_file_is_still_readiness_checked_before_upload(tmp_path, monkeypatch):
    # The pre-check must not become a way to upload a file that is still being written.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"brand new, still growing")

    def _never(*args, **kwargs):  # pragma: no cover - the assertion is the point
        raise AssertionError("a not-ready file must never reach the uploader")

    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", _never)

    pipeline._in_flight[str(clip)] = time.monotonic()
    try:
        assert pipeline._process_candidate(str(clip)) is False
    finally:
        pipeline.stop()


def test_a_mid_write_file_does_not_match_the_finished_file_fingerprint(tmp_path, monkeypatch):
    # The safety property the reordering rests on: the size is part of the fingerprint string,
    # so a partially written file cannot collide with the completed file's manifest row.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"the complete finished recording")
    size = clip.stat().st_size
    pipeline._manifest.record_success(fingerprint.compute(str(clip), size), str(clip), size, "web_api")

    clip.write_bytes(b"the complete")  # rewind to a partially written state
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)

    pipeline._in_flight[str(clip)] = time.monotonic()
    try:
        # Not short-circuited as a duplicate: it falls through to the readiness path unchanged.
        assert pipeline._process_candidate(str(clip)) is False
    finally:
        pipeline.stop()


def test_an_unreadable_file_falls_through_instead_of_failing(tmp_path, monkeypatch):
    # A file the recorder still holds open exclusively raises PermissionError (an OSError) from
    # the pre-check's open(). That must be absorbed here - letting it reach the worker's generic
    # catch would mark a live recording permanently FAILED.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"held open by the recorder")

    def _locked(path, size_bytes):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(upload_pipeline.fingerprint, "compute", _locked)
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)

    activities = []
    pipeline.add_activity_listener(activities.append)
    pipeline._in_flight[str(clip)] = time.monotonic()
    try:
        assert pipeline._process_candidate(str(clip)) is False
        assert activities[-1].event_kind == PipelineEventKind.WAITING
    finally:
        pipeline.stop()


def test_a_zero_byte_file_is_left_to_the_readiness_probe(tmp_path):
    # A recorder that has created the file but written nothing yet: nothing worth hashing, and
    # is_ready() already rejects size 0 on its own.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")

    assert pipeline._try_compute_fingerprint(str(clip)) is None


def test_a_vanished_file_yields_no_fingerprint_rather_than_raising(tmp_path):
    pipeline = _pipeline(tmp_path)

    assert pipeline._try_compute_fingerprint(str(tmp_path / "gone.mp4")) is None


def test_the_fingerprint_is_not_recomputed_when_the_size_held_steady(tmp_path, monkeypatch):
    # The pre-check's hash is reused after a passing readiness probe, so a new file costs one
    # head/tail read in total rather than two.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"a brand new recording")

    real_compute = fingerprint.compute
    calls = []

    def _counted(path, size_bytes):
        calls.append(path)
        return real_compute(path, size_bytes)

    monkeypatch.setattr(upload_pipeline.fingerprint, "compute", _counted)
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: _FakeUploader([UploadResult.ok()]))

    assert pipeline._process_candidate(str(clip)) is True
    assert len(calls) == 1


def test_the_fingerprint_is_recomputed_when_the_file_grew_during_the_probe(tmp_path, monkeypatch):
    # If the file changed size between the pre-check and a passing readiness probe, the stored
    # fingerprint must describe what was actually uploaded, not the earlier partial state.
    pipeline = _pipeline(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"partial")

    def _grow_then_ready(path):
        clip.write_bytes(b"partial plus the rest of the recording")
        return True

    monkeypatch.setattr(upload_pipeline, "is_ready", _grow_then_ready)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: _FakeUploader([UploadResult.ok()]))

    assert pipeline._process_candidate(str(clip)) is True

    final_fp = fingerprint.compute(str(clip), clip.stat().st_size)
    assert pipeline._manifest.is_already_handled(final_fp) is True


# --- Retry timers must not accumulate ---------------------------------------------------------
#
# _pending_retry_timers exists only so stop() can cancel what is still pending, but nothing used
# to take entries back out of it. A single multi-hour "Record" session, rechecked every 15s while
# it is still being written, left roughly 240 dead Timer objects behind per hour - each wrapping a
# Thread - for as long as the agent stayed running.


def _drain_queue(pipeline) -> list[str]:
    drained = []
    while not pipeline._queue.empty():
        drained.append(pipeline._queue.get_nowait())
    return drained


def test_a_finished_timer_is_pruned_by_the_next_schedule(tmp_path):
    pipeline = _pipeline(tmp_path)
    try:
        pipeline._schedule_requeue("first.mp4", 0.0)
        pipeline._pending_retry_timers[0].join(timeout=5)

        pipeline._schedule_requeue("second.mp4", 0.0)

        with pipeline._pending_retry_timers_lock:
            assert len(pipeline._pending_retry_timers) == 1
    finally:
        pipeline.stop()


def test_a_long_running_recording_does_not_pile_up_dead_timers(tmp_path):
    """The actual leak: one file that is still being written gets requeued over and over, and every
    one of those timers used to be retained for the lifetime of the process."""
    pipeline = _pipeline(tmp_path)
    try:
        for _ in range(50):
            pipeline._schedule_requeue("recording.mp4", 0.0)
            pipeline._pending_retry_timers[-1].join(timeout=5)

        with pipeline._pending_retry_timers_lock:
            # Only the most recent one, which was appended after the last prune.
            assert len(pipeline._pending_retry_timers) == 1
        assert len(_drain_queue(pipeline)) == 50  # every retry still actually happened
    finally:
        pipeline.stop()


def test_still_pending_timers_are_kept_so_stop_can_cancel_them(tmp_path):
    """Pruning must only ever drop timers that have already run. Dropping a live one would leave
    stop() with no reference to cancel it by."""
    pipeline = _pipeline(tmp_path)
    try:
        for i in range(5):
            pipeline._schedule_requeue(f"clip{i}.mp4", 9999)

        with pipeline._pending_retry_timers_lock:
            pending = list(pipeline._pending_retry_timers)
        assert len(pending) == 5
    finally:
        pipeline.stop()

    assert all(timer.finished.is_set() for timer in pending)  # stop() reached every one
    # stop() also puts its own None sentinel in to unblock the worker; nothing else should be there.
    assert [item for item in _drain_queue(pipeline) if item is not None] == []


def test_concurrent_scheduling_never_drops_a_live_timer(tmp_path, monkeypatch):
    """A Timer that has not been start()ed yet reports is_alive() == False. So pruning on append is
    only safe if nothing can observe the list between the append and the start - otherwise one
    scheduler prunes another's brand-new timer and stop() can never cancel it.

    The slow start widens that window from a few microseconds to something deterministic. Patching
    threading.Timer itself is process-wide, but nothing else here builds one, and monkeypatch puts
    it back."""

    class _SlowStartTimer(threading.Timer):
        def start(self) -> None:
            time.sleep(0.02)
            super().start()

    monkeypatch.setattr(upload_pipeline.threading, "Timer", _SlowStartTimer)

    pipeline = _pipeline(tmp_path)
    schedulers = [
        threading.Thread(target=pipeline._schedule_requeue, args=(f"clip{i}.mp4", 9999))
        for i in range(8)
    ]
    try:
        for thread in schedulers:
            thread.start()
        for thread in schedulers:
            thread.join(timeout=10)

        with pipeline._pending_retry_timers_lock:
            assert len(pipeline._pending_retry_timers) == 8
    finally:
        pipeline.stop()


def test_pruning_does_not_disturb_a_retry_that_is_still_waiting(tmp_path):
    """The prune walks the list while a real retry is pending in it; that retry must still fire."""
    pipeline = _pipeline(tmp_path)
    try:
        pipeline._schedule_requeue("slow.mp4", 0.3)
        for _ in range(5):
            pipeline._schedule_requeue("quick.mp4", 0.0)
            pipeline._pending_retry_timers[-1].join(timeout=5)

        with pipeline._pending_retry_timers_lock:
            slow = next(t for t in pipeline._pending_retry_timers if t.args == ("slow.mp4",))
        slow.join(timeout=5)

        assert "slow.mp4" in _drain_queue(pipeline)
    finally:
        pipeline.stop()


def test_scheduling_after_stop_adds_nothing(tmp_path):
    """stop() clears the list; a late retry landing afterwards must not repopulate it."""
    pipeline = _pipeline(tmp_path)
    pipeline.stop()

    pipeline._schedule_requeue("late.mp4", 9999)

    with pipeline._pending_retry_timers_lock:
        assert pipeline._pending_retry_timers == []
