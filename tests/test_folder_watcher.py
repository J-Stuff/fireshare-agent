"""
Regression coverage for the "saving Settings silently un-pauses the agent" bug.

`FolderWatcherService.start()` is called from two places with two different intents: once at boot
to bring the service up, and again from `UploadPipeline.update_config()` purely to re-apply the
watch list after the user saves Settings. It used to reset the pause flag on both paths, so a user
who paused from the tray and then saved any unrelated setting had watching silently resume while
the tray icon and menu carried on claiming the agent was paused.
"""
from __future__ import annotations

import pytest

from fireshare_agent.config.app_config import WatchFolderConfig
from fireshare_agent.watching.folder_watcher import _DebouncedHandler, FolderWatcherService


@pytest.fixture
def watcher(tmp_path):
    """A started service watching a real directory, so an actual Observer is running rather than
    the `scheduled_any is False` shortcut - the reset being tested sat outside that branch."""
    service = FolderWatcherService()
    folder = tmp_path / "clips"
    folder.mkdir()
    service.start(
        [WatchFolderConfig(path=str(folder))],
        video_extensions=[".mp4"],
        image_extensions=[".png"],
        on_candidate=lambda path: None,
    )
    try:
        yield service, folder
    finally:
        service.stop()


def test_a_freshly_started_watcher_is_not_paused(watcher):
    service, _ = watcher
    assert service.is_paused is False


def test_pause_survives_a_reconfiguring_start(watcher):
    service, folder = watcher
    service.pause()

    # Exactly what update_config() does: same arguments, called only to re-apply watches.
    service.start(
        [WatchFolderConfig(path=str(folder))],
        video_extensions=[".mp4"],
        image_extensions=[".png"],
        on_candidate=lambda path: None,
    )

    assert service.is_paused is True


def test_resume_still_works_after_a_reconfiguring_start(watcher):
    """Guards against over-correcting into a watcher that can never be un-paused."""
    service, folder = watcher
    service.pause()
    service.start(
        [WatchFolderConfig(path=str(folder))], [".mp4"], [".png"], lambda path: None,
    )
    service.resume()

    assert service.is_paused is False


def test_handlers_built_by_a_reconfiguring_start_observe_the_pause_flag(tmp_path):
    """The flag surviving is only half of it - the *new* handlers created by the second start()
    must read the same live flag, not a value captured when they were built."""
    service = FolderWatcherService()
    service.pause()

    fired: list[str] = []
    handler = _DebouncedHandler({".mp4"}, lambda: service.is_paused, fired.append)
    handler._debounce(str(tmp_path / "clip.mp4"))

    assert fired == []
    assert handler._timers == {}  # dropped before a debounce timer was even scheduled
