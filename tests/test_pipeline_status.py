"""
Coverage for the pipeline's live status snapshot and its progress reporting - feature-ideas.md
#1, plus the "no videos left to upload" report asked for in GitHub issue #7.

Two things here are easy to get subtly wrong and expensive to notice in production:

  * The event rate. feature-ideas.md flagged it explicitly: the activity listener logs every
    event, so an unthrottled PROGRESS would flood the size-capped agent.log, exactly as WAITING
    once did. So the throttle is asserted, not assumed.
  * The queue arithmetic. `queued_count` is derived by subtracting the active and parked files
    from the in-flight total, across two different locks - a subtraction that must never surface
    a negative count or double-count a file.
"""
import time

import pytest

from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.models import MediaKind, PendingFile, UploadResult
from fireshare_agent.pipeline import upload_pipeline
from fireshare_agent.pipeline.activity import PipelineEventKind
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline


def _pipeline(tmp_path, config: AppConfig | None = None) -> UploadPipeline:
    return UploadPipeline(ManifestStore(str(tmp_path / "manifest.db")), config or AppConfig())


def _recorder(pipeline) -> list:
    events = []
    pipeline.add_activity_listener(events.append)
    return events


class _ProgressUploader:
    """Reports a scripted sequence of byte counts through the callback the pipeline supplies,
    the way the real chunk loop reports one per accepted chunk."""

    def __init__(self, steps, size_bytes, result=None, pause_between=0.0):
        self._steps = steps
        self._size = size_bytes
        self._result = result or UploadResult.ok()
        self._pause = pause_between
        self.saw_callback = False

    def exists_at_destination(self, file) -> bool:
        return False

    def upload(self, file, on_progress=None) -> UploadResult:
        self.saw_callback = on_progress is not None
        for sent in self._steps:
            on_progress(sent, self._size)
            if self._pause:
                time.sleep(self._pause)
        return self._result


# ------------------------------------------------------------------ status snapshot

def test_a_fresh_pipeline_reports_idle(tmp_path):
    status = _pipeline(tmp_path).get_status()
    assert status.is_idle
    assert status.active is None
    assert status.pending_count == 0


def test_queued_waiting_and_active_partition_the_in_flight_set(tmp_path):
    """The three counts must sum to the in-flight total - a file shown as both queued and
    uploading, or missing from both, is a status line the user cannot act on."""
    pipeline = _pipeline(tmp_path)
    for name in ("a.mp4", "b.mp4", "c.mp4", "d.mp4"):
        pipeline._in_flight[str(tmp_path / name)] = time.monotonic()

    pipeline._mark_waiting(str(tmp_path / "a.mp4"), "Still being written")
    pipeline._begin_active(str(tmp_path / "b.mp4"), MediaKind.VIDEO, 1000)

    status = pipeline.get_status()
    assert status.waiting_count == 1
    assert status.queued_count == 2
    assert status.active is not None
    assert status.pending_count == 4


def test_the_queued_count_never_goes_negative(tmp_path):
    """in_flight is read under a different lock from the waiting/active state, so a file
    resolving in between can make the raw arithmetic negative."""
    pipeline = _pipeline(tmp_path)
    pipeline._mark_waiting(str(tmp_path / "gone.mp4"), "Still being written")
    pipeline._begin_active(str(tmp_path / "also-gone.mp4"), MediaKind.VIDEO, 10)

    assert pipeline.get_status().queued_count == 0


def test_a_scan_in_progress_is_reported_and_is_not_idle(tmp_path):
    pipeline = _pipeline(tmp_path)
    with pipeline._status_lock:
        pipeline._scanning = True

    status = pipeline.get_status()
    assert status.scanning
    assert not status.is_idle


def test_the_active_snapshot_is_released_when_the_upload_ends(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline._begin_active(str(tmp_path / "clip.mp4"), MediaKind.VIDEO, 1000)
    assert pipeline.get_status().active is not None

    pipeline._end_active()
    assert pipeline.get_status().active is None


# ------------------------------------------------------------------ progress reporting

def test_progress_updates_the_snapshot_on_every_callback(tmp_path):
    """The snapshot is polled, not pushed, so it takes every update even though the broadcast
    below is throttled - a window that opens mid-chunk should see the current byte count."""
    pipeline = _pipeline(tmp_path)
    path = str(tmp_path / "clip.mp4")
    pipeline._begin_active(path, MediaKind.VIDEO, 1000)

    for sent in (100, 200, 300):
        pipeline._on_upload_progress(path, MediaKind.VIDEO, sent, 1000)

    assert pipeline.get_status().active.bytes_sent == 300


def test_progress_events_are_rate_limited(tmp_path, monkeypatch):
    """feature-ideas.md #1's stated trap: the app logs every activity event, so an event per
    chunk would flood the size-capped agent.log on a large clip."""
    monkeypatch.setattr(upload_pipeline, "_PROGRESS_EVENT_INTERVAL_SECONDS", 60.0)
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)
    path = str(tmp_path / "clip.mp4")
    pipeline._begin_active(path, MediaKind.VIDEO, 1000)

    for sent in (100, 200, 300, 400, 500):
        pipeline._on_upload_progress(path, MediaKind.VIDEO, sent, 1000)

    progress = [e for e in events if e.event_kind == PipelineEventKind.PROGRESS]
    assert len(progress) == 1  # the first; the rest fall inside the interval


def test_the_final_chunk_always_raises_an_event(tmp_path, monkeypatch):
    """Whatever the throttle, the transfer reaching 100% has to be broadcast - otherwise a bar
    can sit at 80% until the SUCCEEDED event happens to arrive."""
    monkeypatch.setattr(upload_pipeline, "_PROGRESS_EVENT_INTERVAL_SECONDS", 60.0)
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)
    path = str(tmp_path / "clip.mp4")
    pipeline._begin_active(path, MediaKind.VIDEO, 1000)

    pipeline._on_upload_progress(path, MediaKind.VIDEO, 100, 1000)   # emitted (first)
    pipeline._on_upload_progress(path, MediaKind.VIDEO, 500, 1000)   # throttled
    pipeline._on_upload_progress(path, MediaKind.VIDEO, 1000, 1000)  # final - always emitted

    progress = [e for e in events if e.event_kind == PipelineEventKind.PROGRESS]
    assert [e.bytes_sent for e in progress] == [100, 1000]
    assert progress[-1].percent == 100.0


def test_a_callback_for_a_superseded_file_is_ignored(tmp_path):
    """A slow callback from an upload the pipeline has already moved past must not overwrite the
    snapshot for the file it is now working on."""
    pipeline = _pipeline(tmp_path)
    pipeline._begin_active(str(tmp_path / "current.mp4"), MediaKind.VIDEO, 1000)

    pipeline._on_upload_progress(str(tmp_path / "stale.mp4"), MediaKind.VIDEO, 999, 1000)

    assert pipeline.get_status().active.bytes_sent == 0


def test_the_pipeline_hands_a_progress_callback_to_the_uploader(tmp_path, monkeypatch):
    """End to end through _process_candidate: the uploader is given a callback, and what it
    reports reaches the pipeline's listeners."""
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 1000)
    uploader = _ProgressUploader(steps=[1000], size_bytes=1000)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: uploader)

    assert pipeline._process_candidate(str(clip)) is True
    assert uploader.saw_callback
    assert any(e.event_kind == PipelineEventKind.PROGRESS and e.percent == 100.0 for e in events)


def test_the_active_snapshot_is_cleared_even_if_the_uploader_raises(tmp_path, monkeypatch):
    """A window still showing a progress bar for a file nothing is working on is worse than
    showing no bar at all."""
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: True)
    pipeline = _pipeline(tmp_path)

    class _Exploding:
        def exists_at_destination(self, file):
            return False

        def upload(self, file, on_progress=None):
            raise RuntimeError("connection reset")

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    monkeypatch.setattr(pipeline, "_get_or_create_uploader", lambda: _Exploding())

    with pytest.raises(RuntimeError):
        pipeline._process_candidate(str(clip))

    assert pipeline.get_status().active is None


# ------------------------------------------------------------------ idle announcement

def test_draining_the_queue_announces_that_nothing_is_left(tmp_path):
    """Issue #7, item 2 - the report the log owes the user when the work is done."""
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)
    pipeline._begin_active(str(tmp_path / "clip.mp4"), MediaKind.VIDEO, 10)
    pipeline._end_active()

    pipeline._announce_idle_if_drained()

    idle = [e for e in events if e.event_kind == PipelineEventKind.IDLE]
    assert len(idle) == 1
    assert idle[0].message == "No files left to upload"


def test_the_idle_announcement_is_made_once_per_batch_not_once_per_file(tmp_path):
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)
    pipeline._begin_active(str(tmp_path / "clip.mp4"), MediaKind.VIDEO, 10)
    pipeline._end_active()

    for _ in range(5):
        pipeline._announce_idle_if_drained()

    assert len([e for e in events if e.event_kind == PipelineEventKind.IDLE]) == 1


def test_nothing_is_announced_while_files_are_still_in_flight(tmp_path):
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)
    pipeline._begin_active(str(tmp_path / "clip.mp4"), MediaKind.VIDEO, 10)
    pipeline._end_active()
    pipeline._in_flight[str(tmp_path / "next.mp4")] = time.monotonic()

    pipeline._announce_idle_if_drained()

    assert not [e for e in events if e.event_kind == PipelineEventKind.IDLE]


def test_a_scan_that_finds_nothing_still_reports_that_it_found_nothing(tmp_path):
    """The startup rescan of an already-uploaded library queues no work at all. Without re-arming
    the announcement, the user pressing Sync Now would get no answer whatsoever."""
    pipeline = _pipeline(tmp_path)
    events = _recorder(pipeline)

    pipeline.sync_now()  # no watch folders configured; walks nothing

    idle = [e for e in events if e.event_kind == PipelineEventKind.IDLE]
    assert len(idle) == 1
    assert not pipeline.get_status().scanning


def test_the_waiting_note_explains_why_nothing_is_moving(tmp_path, monkeypatch):
    """A file held back for readiness is not stuck, and the window has to be able to say so."""
    monkeypatch.setattr(upload_pipeline, "is_ready", lambda path: False)
    pipeline = _pipeline(tmp_path)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    pipeline._in_flight[str(clip)] = time.monotonic()
    try:
        assert pipeline._process_candidate(str(clip)) is False
        status = pipeline.get_status()
        assert status.waiting_count == 1
        assert status.waiting_note == "Still being written"
        assert not status.is_idle
    finally:
        pipeline.stop()


def test_stopping_clears_the_status_state(tmp_path):
    """stop() cancels the retry timers; the status the UI polls has to come down with them, or a
    window left open through a shutdown keeps reporting work that is no longer scheduled."""
    pipeline = _pipeline(tmp_path)
    pipeline._mark_waiting(str(tmp_path / "a.mp4"), "Still being written")
    pipeline._begin_active(str(tmp_path / "b.mp4"), MediaKind.VIDEO, 10)

    pipeline.stop()

    status = pipeline.get_status()
    assert status.active is None
    assert status.waiting_count == 0
