"""
Regression coverage for the "Sync Now freezes the tray menu" bug: the menu item used to be wired
straight to `pipeline.sync_now`, so the full recursive walk of every watch folder ran on pystray's
single callback thread and the whole menu - Exit included - stopped responding until it finished.
Every other tray callback already handed off immediately.

The fix runs the scan on a short-lived daemon thread. Marshalling it to the UI thread instead
would not have helped: run_on_ui_thread() is root.after(), so the walk would have run inside Tk's
event loop and frozen the settings and activity windows instead.

Also covers the update-check response, reported by J: "Check for Updates Now" looked like it did
nothing whenever an update was actually available.
"""
<<<<<<< HEAD
import logging
=======
>>>>>>> origin/main
import threading

import pytest

from fireshare_agent import app as app_module
from fireshare_agent import updater
from fireshare_agent.app import (
    FireshareAgentApp,
    UpdateCheckResponse,
    decide_update_check_response,
)
from fireshare_agent.config.app_config import AppConfig
<<<<<<< HEAD
from fireshare_agent.models import MediaKind
from fireshare_agent.pipeline.activity import PipelineActivity, PipelineEventKind
=======
>>>>>>> origin/main


class _FakeRoot:
    """Stands in for the CTk root so these tests need no Tcl interpreter. after() runs the callback
    inline, which is what makes a UI-thread hand-off observably *not* a fix for a slow scan."""

    def __init__(self) -> None:
        self.mainloop_entered = False

    def after(self, delay, func) -> None:
        func()

    def mainloop(self) -> None:
        self.mainloop_entered = True


class _FakePipeline:
    """A pipeline whose sync_now() blocks until released, so a caller that fails to hand the work
    off to another thread is caught by a timeout rather than hanging the suite forever."""

    def __init__(self, paused: bool = False) -> None:
        self.is_paused = paused
        self.started = False
        self.release = threading.Event()
        self.scan_entered = threading.Event()
        self.scan_count = 0
        self._count_lock = threading.Lock()

    def start(self) -> None:
        self.started = True

    def sync_now(self) -> None:
        with self._count_lock:
            self.scan_count += 1
        self.scan_entered.set()
        assert self.release.wait(timeout=10), "test failed to release the blocked scan"


class _FakeTray:
    """Captures the callbacks app.run() wires up, so the Sync Now hand-off can be exercised exactly
    as pystray would invoke it."""

    last: "_FakeTray | None" = None

    def __init__(self, **callbacks) -> None:
        self.callbacks = callbacks
        _FakeTray.last = self

    def run(self) -> None:
        pass

    def refresh(self) -> None:
        pass


def _app(pipeline: _FakePipeline, config: AppConfig | None = None) -> FireshareAgentApp:
    """Builds an app instance without running __init__ - that would create a real Tk root, a real
    manifest database in the user's profile, and a real pipeline. Only the attributes the paths
    under test actually touch are populated."""
    instance = FireshareAgentApp.__new__(FireshareAgentApp)
    instance.root = _FakeRoot()
    instance.config = config or AppConfig(auto_check_for_updates=False)
    instance.pipeline = pipeline
    instance.tray = None
    instance._update_info = None
    instance._sync_thread = None
    instance._sync_lock = threading.Lock()
    return instance


def _drain(pipeline: _FakePipeline, instance: FireshareAgentApp) -> None:
    pipeline.release.set()
    if instance._sync_thread is not None:
        instance._sync_thread.join(timeout=10)


def test_sync_now_returns_immediately_instead_of_blocking_its_caller():
    # The bug itself: pystray invokes the menu callback on its own single callback thread, so a
    # callback that only returns once the walk has finished freezes the entire menu.
    pipeline = _FakePipeline()
    instance = _app(pipeline)

    try:
        instance._start_sync()

        assert pipeline.scan_entered.wait(timeout=10), "the scan never started"
        assert pipeline.release.is_set() is False  # still mid-scan...
        # ...and yet _start_sync() has already returned, which is the whole point.
    finally:
        _drain(pipeline, instance)


def test_the_tray_menu_item_is_wired_to_the_non_blocking_hand_off(monkeypatch):
    """Pins the wiring, not just the helper: the regression was a one-line callback assignment in
    run(), so a test of _start_sync() alone would not have caught it."""
    pipeline = _FakePipeline()
    instance = _app(pipeline)
    monkeypatch.setattr(app_module, "TrayIcon", _FakeTray)

    try:
        instance.run()

        on_sync_now = _FakeTray.last.callbacks["on_sync_now"]
        on_sync_now()  # invoked exactly as pystray would, on this thread

        assert pipeline.scan_entered.wait(timeout=10)
        assert pipeline.release.is_set() is False  # returned while the scan is still running
    finally:
        _drain(pipeline, instance)


def test_a_second_sync_is_ignored_while_one_is_still_running():
    pipeline = _FakePipeline()
    instance = _app(pipeline)

    try:
        instance._start_sync()
        assert pipeline.scan_entered.wait(timeout=10)
        first_thread = instance._sync_thread

        instance._start_sync()
        instance._start_sync()

        assert instance._sync_thread is first_thread
        assert pipeline.scan_count == 1
    finally:
        _drain(pipeline, instance)


def test_a_new_sync_can_start_once_the_previous_one_has_finished():
    """The guard must not latch: a user who clicks Sync Now after a scan completes has to get a
    real scan, not a permanently swallowed menu item."""
    pipeline = _FakePipeline()
    instance = _app(pipeline)

    instance._start_sync()
    assert pipeline.scan_entered.wait(timeout=10)
    _drain(pipeline, instance)

    pipeline.release.clear()
    pipeline.scan_entered.clear()
    try:
        instance._start_sync()

        assert pipeline.scan_entered.wait(timeout=10)
        assert pipeline.scan_count == 2
    finally:
        _drain(pipeline, instance)


def test_a_failing_scan_is_logged_and_does_not_latch_the_guard(caplog):
    """The scan thread is the top of its own stack, so an escaping error would reach nothing but
    threading's default hook. It must be caught, reported, and must not block later scans."""
    instance = _app(_FakePipeline())
    calls = []

    def _boom() -> None:
        calls.append("scan")
        raise OSError("the drive went away mid-walk")

    instance.pipeline.sync_now = _boom

    with caplog.at_level("ERROR"):
        instance._start_sync()
        instance._sync_thread.join(timeout=10)

    assert "Rescan failed." in caplog.text

    instance._start_sync()  # the guard released, so a retry is possible
    instance._sync_thread.join(timeout=10)
    assert len(calls) == 2


def test_the_startup_rescan_does_not_delay_the_tray_appearing(monkeypatch):
    """The startup scan used to run inline on the main thread before the tray was even created, so
    a large library meant no tray icon at all for the duration."""
    pipeline = _FakePipeline()
    instance = _app(pipeline)
    monkeypatch.setattr(app_module, "TrayIcon", _FakeTray)

    try:
        instance.run()  # returns only after the tray is built and mainloop is entered

        assert pipeline.scan_entered.wait(timeout=10)
        assert pipeline.release.is_set() is False  # the startup scan is still going...
        assert instance.tray is not None  # ...and the tray already exists
        assert instance.root.mainloop_entered is True
    finally:
        _drain(pipeline, instance)


def test_a_paused_agent_still_skips_the_startup_rescan(monkeypatch):
    """Pause survives a restart, so a paused agent must not come back up and immediately scan -
    moving the scan onto a thread must not quietly reintroduce that."""
    pipeline = _FakePipeline(paused=True)
    instance = _app(pipeline)
    monkeypatch.setattr(app_module, "TrayIcon", _FakeTray)

    instance.run()

    assert instance._sync_thread is None
    assert pipeline.scan_count == 0

    # ...but the manual menu item is still allowed to run while paused.
    try:
        _FakeTray.last.callbacks["on_sync_now"]()
        assert pipeline.scan_entered.wait(timeout=10)
    finally:
        _drain(pipeline, instance)


@pytest.mark.parametrize("recursive", [True, False])
def test_a_scan_in_progress_bails_out_when_the_pipeline_is_stopped(tmp_path, recursive):
    """A real scan can now overlap with Exit, since it is no longer finished before the tray is
    built. It must stop walking rather than enqueueing work for a worker that has already gone."""
    from fireshare_agent.config.app_config import WatchFolderConfig
    from fireshare_agent.manifest.store import ManifestStore
    from fireshare_agent.pipeline.upload_pipeline import UploadPipeline

    (tmp_path / "clip.mp4").write_bytes(b"x")
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(tmp_path), recursive=recursive)])
    pipeline = UploadPipeline(ManifestStore(str(tmp_path / "manifest.db")), config)

    enqueued = []
    pipeline._enqueue = enqueued.append
    pipeline._stop_event.set()

    pipeline.sync_now()

    assert enqueued == []


# --- A manual update check owes the user a definite answer ------------------------------------
#
# Reported by J: "Check for Updates Now" appeared to do nothing when an update WAS available. The
# two outcomes were handled asymmetrically - "no update" got a modal window, while "update
# available" got only a tray balloon, and that balloon was gated on show_upload_notifications, the
# per-upload notification toggle. Turning that off is entirely reasonable for a background
# uploader, and it meant the interesting outcome produced no feedback of any kind.


def test_manual_check_with_an_update_offers_to_install_it():
    assert decide_update_check_response(
        update_available=True, user_initiated=True, announce_automatic_updates=False,
    ) is UpdateCheckResponse.OFFER_UPDATE


def test_manual_check_with_an_update_is_not_gated_on_a_notification_setting():
    """The heart of the report: the only reason the button looked broken was an unrelated toggle.
    A user-initiated check answers regardless of every notification preference."""
    for announce in (True, False):
        assert decide_update_check_response(
            update_available=True, user_initiated=True, announce_automatic_updates=announce,
        ) is UpdateCheckResponse.OFFER_UPDATE


def test_manual_check_with_no_update_says_so():
    assert decide_update_check_response(
        update_available=False, user_initiated=True, announce_automatic_updates=False,
    ) is UpdateCheckResponse.ALREADY_CURRENT


def test_automatic_check_with_an_update_stays_unobtrusive():
    """A modal on launch would be an ambush, so the startup check keeps the balloon."""
    assert decide_update_check_response(
        update_available=True, user_initiated=False, announce_automatic_updates=True,
    ) is UpdateCheckResponse.ANNOUNCE_UPDATE


def test_automatic_check_with_no_update_says_nothing():
    assert decide_update_check_response(
        update_available=False, user_initiated=False, announce_automatic_updates=True,
    ) is UpdateCheckResponse.NOTHING


def test_automatic_check_stays_silent_when_update_announcements_are_off():
    assert decide_update_check_response(
        update_available=True, user_initiated=False, announce_automatic_updates=False,
    ) is UpdateCheckResponse.NOTHING


class _RecordingApp:
    """Captures which branch _on_update_check_result dispatches into, without a real dialog."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tray = None
        self._update_info = None
        self.offered = 0
        self.notifications: list[str] = []

    def confirm_and_apply_update(self) -> None:
        self.offered += 1

    def _notify(self, message: str) -> None:
        self.notifications.append(message)


def _dispatch(info, notify_if_none: bool, config: AppConfig, monkeypatch) -> _RecordingApp:
    instance = _RecordingApp(config)
    shown = []
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *a, **k: shown.append(a))
    FireshareAgentApp._on_update_check_result(instance, info, notify_if_none)
    instance.info_dialogs = shown
    return instance


def test_the_dispatch_matches_the_decision_for_a_manual_check(monkeypatch):
    """Pins the wiring as well as the decision function - the reported bug was in the dispatch."""
    info = updater.UpdateInfo(
        version="9.9.9", tag="v9.9.9", download_url="u", checksum_url="c", notes_url="n",
    )
    # show_upload_notifications off is exactly the configuration that used to produce silence.
    config = AppConfig(show_upload_notifications=False, auto_check_for_updates=False)

    instance = _dispatch(info, notify_if_none=True, config=config, monkeypatch=monkeypatch)

    assert instance.offered == 1
    assert instance.notifications == []
    assert instance.info_dialogs == []


def test_a_manual_check_with_no_update_still_shows_the_modal(monkeypatch):
    config = AppConfig(show_upload_notifications=False, auto_check_for_updates=False)

    instance = _dispatch(None, notify_if_none=True, config=config, monkeypatch=monkeypatch)

    assert instance.offered == 0
    assert len(instance.info_dialogs) == 1


def test_an_automatic_check_never_opens_a_dialog(monkeypatch):
    info = updater.UpdateInfo(
        version="9.9.9", tag="v9.9.9", download_url="u", checksum_url="c", notes_url="n",
    )
    config = AppConfig(show_upload_notifications=False, auto_check_for_updates=True)

    instance = _dispatch(info, notify_if_none=False, config=config, monkeypatch=monkeypatch)

    assert instance.offered == 0
    assert instance.info_dialogs == []
    assert len(instance.notifications) == 1
    assert "9.9.9" in instance.notifications[0]
<<<<<<< HEAD


# ------------------------------------------------------------------------ activity log levels
#
# feature-ideas.md #1 named the trap directly: the activity listener logs every event, so a
# per-chunk PROGRESS would flood the size-capped agent.log - the same trap WAITING already fell
# into. Under the app's INFO root logger, DEBUG here means those lines are never written at all.


def test_progress_events_are_logged_at_debug():
    level, *_ = app_module._log_line_for(
        PipelineActivity(
            path="clip.mp4", kind=MediaKind.VIDEO, event_kind=PipelineEventKind.PROGRESS,
            bytes_sent=50, total_bytes=100,
        )
    )
    assert level == logging.DEBUG


def test_waiting_events_are_still_logged_at_debug():
    level, *_ = app_module._log_line_for(
        PipelineActivity(path="clip.mp4", kind=MediaKind.VIDEO, event_kind=PipelineEventKind.WAITING)
    )
    assert level == logging.DEBUG


def test_outcomes_are_logged_at_info():
    for kind in (PipelineEventKind.SUCCEEDED, PipelineEventKind.FAILED, PipelineEventKind.IDLE):
        level, *_ = app_module._log_line_for(
            PipelineActivity(path="clip.mp4", kind=MediaKind.VIDEO, event_kind=kind)
        )
        assert level == logging.INFO


def test_a_progress_line_carries_the_percentage():
    level, fmt, *args = app_module._log_line_for(
        PipelineActivity(
            path="clip.mp4", kind=MediaKind.VIDEO, event_kind=PipelineEventKind.PROGRESS,
            bytes_sent=43, total_bytes=100,
        )
    )
    assert (fmt % tuple(args)) == "progress: clip.mp4 (43%)"


def test_an_idle_line_does_not_print_a_stray_empty_path():
    """IDLE describes the pipeline, not a file, so its path is empty. Rendered through the normal
    format it would leave a gap where every other line has a filename."""
    level, fmt, *args = app_module._log_line_for(
        PipelineActivity(
            path="", kind=MediaKind.VIDEO, event_kind=PipelineEventKind.IDLE,
            message="No files left to upload",
        )
    )
    assert (fmt % tuple(args)) == "idle (No files left to upload)"
=======
>>>>>>> origin/main
