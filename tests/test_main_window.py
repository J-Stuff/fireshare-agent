"""
The main window's decision logic, exercised without a Tcl interpreter.

`entry_matches` and `display_status` are deliberately module-level functions rather than methods
for exactly this reason: filtering and status labelling are what a user notices being wrong, and
neither needs a window to be checked.

The tray assertions below cover the other half of "make this the main dialog": a left-click on
the tray icon has to open it, which on Windows means pystray invoking the menu item marked
`default` - and that item has to be one that is always visible.
"""
from datetime import datetime, timezone

import pytest

from fireshare_agent.manifest.store import (
    STATUS_ALREADY_EXISTED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    ManifestEntry,
)
from fireshare_agent.ui import main_window
from fireshare_agent.ui.main_window import display_status, entry_matches
from fireshare_agent.ui.tray import TrayIcon


def _entry(path=r"C:\clips\HELLDIVERS 2\clip.mp4", status=STATUS_SUCCESS, pending_review=False):
    return ManifestEntry(
        fingerprint="fp", path=path, size_bytes=1000,
        updated_at_utc=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
        method="web_api", status=status, error=None, pending_review=pending_review,
    )


# ------------------------------------------------------------------ history filtering

def test_the_all_filter_keeps_everything():
    for status in (STATUS_SUCCESS, STATUS_FAILED, STATUS_ALREADY_EXISTED):
        assert entry_matches(_entry(status=status), main_window.FILTER_ALL, "")


@pytest.mark.parametrize(
    "selected, status, expected",
    [
        (main_window.FILTER_UPLOADED, STATUS_SUCCESS, True),
        (main_window.FILTER_UPLOADED, STATUS_FAILED, False),
        (main_window.FILTER_UPLOADED, STATUS_ALREADY_EXISTED, False),
        (main_window.FILTER_FAILED, STATUS_FAILED, True),
        (main_window.FILTER_FAILED, STATUS_SUCCESS, False),
    ],
)
def test_status_filters(selected, status, expected):
    assert entry_matches(_entry(status=status), selected, "") is expected


def test_the_review_filter_follows_the_flag_not_the_status():
    """A reviewable row is stored as "already existed"; what makes it reviewable is the flag."""
    assert entry_matches(
        _entry(status=STATUS_ALREADY_EXISTED, pending_review=True), main_window.FILTER_REVIEW, ""
    )
    assert not entry_matches(
        _entry(status=STATUS_ALREADY_EXISTED, pending_review=False), main_window.FILTER_REVIEW, ""
    )


def test_search_matches_the_folder_as_well_as_the_filename():
    """With mirrored per-game subfolders, "HELLDIVERS" is how someone asks for a game's clips."""
    entry = _entry(path=r"C:\clips\HELLDIVERS 2\extraction.mp4")
    assert entry_matches(entry, main_window.FILTER_ALL, "helldivers")
    assert entry_matches(entry, main_window.FILTER_ALL, "extraction")
    assert not entry_matches(entry, main_window.FILTER_ALL, "cyberpunk")


def test_search_ignores_case_and_surrounding_whitespace():
    entry = _entry(path=r"C:\clips\Clip.mp4")
    assert entry_matches(entry, main_window.FILTER_ALL, "  CLIP  ")


def test_an_empty_search_is_not_a_filter():
    assert entry_matches(_entry(), main_window.FILTER_ALL, "   ")


def test_the_filter_and_the_search_are_both_applied():
    failed = _entry(path=r"C:\clips\broken.mp4", status=STATUS_FAILED)
    assert entry_matches(failed, main_window.FILTER_FAILED, "broken")
    assert not entry_matches(failed, main_window.FILTER_FAILED, "working")


# ------------------------------------------------------------------ status labelling

def test_pending_review_outranks_the_stored_status():
    entry = _entry(status=STATUS_ALREADY_EXISTED, pending_review=True)
    assert display_status(entry) == "NEEDS REVIEW"


def test_known_statuses_get_readable_labels():
    assert display_status(_entry(status=STATUS_SUCCESS)) == "UPLOADED"
    assert display_status(_entry(status=STATUS_FAILED)) == "FAILED"
    assert display_status(_entry(status=STATUS_ALREADY_EXISTED)) == "ON SERVER"


def test_an_unknown_status_falls_back_to_its_own_name():
    """A manifest written by a future version must still render rather than raise."""
    assert display_status(_entry(status="quarantined")) == "QUARANTINED"


def test_every_label_fits_the_history_column():
    """The history is monospace with a fixed-width status column; a longer label would push the
    size and path columns out of alignment for that one row."""
    labels = set(main_window._STATUS_DISPLAY.values()) | {main_window._REVIEW_DISPLAY}
    assert all(len(label) < main_window._COL_STATUS for label in labels)


# ------------------------------------------------------------------ tray wiring

def _tray(**overrides):
    callbacks = dict(
        on_open_settings=lambda: None,
        on_open_main_window=lambda: None,
        on_sync_now=lambda: None,
        on_toggle_pause=lambda: None,
        is_paused=lambda: False,
        has_failures=lambda: False,
        on_exit=lambda: None,
    )
    callbacks.update(overrides)
    return TrayIcon(**callbacks)


def test_a_left_click_on_the_tray_icon_opens_the_main_window():
    """pystray's Windows backend calls the icon on WM_LBUTTONUP, and the icon invokes whichever
    menu item is marked default. Without one, a left-click does nothing at all."""
    opened = []
    tray = _tray(on_open_main_window=lambda: opened.append(True))

    defaults = [item for item in tray.icon.menu.items if item.default]
    assert len(defaults) == 1, "exactly one default item, or the click target is ambiguous"

    defaults[0](tray.icon)
    assert opened == [True]


def test_the_default_item_is_never_hidden():
    """The conditional entries above it (update available, files to review) disappear when they
    do not apply. A default nobody can reach is no default at all."""
    tray = _tray(has_update=lambda: False, pending_review_count=lambda: 0)
    default = next(item for item in tray.icon.menu.items if item.default)
    assert default.visible


def test_the_tooltip_callback_drives_the_icon_title():
    tray = _tray(tooltip=lambda: "Fireshare Agent - uploading clip.mp4 (43%)")
    assert tray.icon.title == "Fireshare Agent - uploading clip.mp4 (43%)"


def test_the_tooltip_falls_back_to_the_pause_state_when_none_is_supplied():
    assert _tray(is_paused=lambda: True).icon.title == "Fireshare Agent (paused)"
    assert _tray(is_paused=lambda: False).icon.title == "Fireshare Agent"


def test_refresh_tooltip_updates_only_the_title():
    """Progress moves about once a second; going through the full refresh would rebuild the icon
    bitmap and the native menu each time."""
    text = ["Fireshare Agent"]
    tray = _tray(tooltip=lambda: text[0])
    text[0] = "Fireshare Agent - uploading clip.mp4 (43%)"

    tray.refresh_tooltip()

    assert tray.icon.title == "Fireshare Agent - uploading clip.mp4 (43%)"


# ------------------------------------------------------------------ share-link affordance

def test_only_files_that_reached_the_server_are_linkable():
    """A FAILED row has nothing to link to. Offering Copy Link for one would promise a link that
    cannot exist - and the id it would need is only created for files Fireshare actually has."""
    assert STATUS_SUCCESS in main_window._LINKABLE_STATUSES
    assert STATUS_ALREADY_EXISTED in main_window._LINKABLE_STATUSES
    assert STATUS_FAILED not in main_window._LINKABLE_STATUSES


def test_a_row_awaiting_review_is_still_linkable():
    """"Needs review" is about the fate of the *local* copy. The file is on the server either
    way, so its link is exactly as valid as any other."""
    entry = _entry(status=STATUS_ALREADY_EXISTED, pending_review=True)
    assert entry.status in main_window._LINKABLE_STATUSES
