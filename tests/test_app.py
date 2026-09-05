"""
Regression coverage for the "Sync Now freezes the tray menu" bug: the menu item used to be wired
straight to `pipeline.sync_now`, so the full recursive walk of every watch folder ran on pystray's
single callback thread and the whole menu - Exit included - stopped responding until it finished.
Every other tray callback already handed off immediately.

The fix runs the scan on a short-lived daemon thread. Marshalling it to the UI thread instead
would not have helped: run_on_ui_thread() is root.after(), so the walk would have run inside Tk's
event loop and frozen the settings and activity windows instead.
"""
import threading

import pytest

from fireshare_agent import app as app_module
from fireshare_agent.app import FireshareAgentApp
from fireshare_agent.config.app_config import AppConfig


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
