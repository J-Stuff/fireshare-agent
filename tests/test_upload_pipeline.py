"""
Regression coverage for the readiness-timeout bug: a file that was still being written used to
be retried in a blocking loop for ~10 minutes and then permanently abandoned (no periodic
re-scan or startup sync existed to ever pick it up again). A single ShadowPlay "Record" session
(as opposed to a quick Instant Replay clip) can legitimately stay open and growing for hours, so
this would silently lose real recordings. Fixed by requeuing via a non-blocking timer instead of
looping in place, with a generous 24h cap for a genuinely stuck file rather than a fixed attempt
count.
"""
import time

from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.pipeline import upload_pipeline
from fireshare_agent.pipeline.activity import PipelineEventKind
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline


def _pipeline(tmp_path) -> UploadPipeline:
    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    return UploadPipeline(manifest, AppConfig())


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
