"""
Coverage for the wording the main window and the tray tooltip actually show.

The point of `ui/formatting.py` being Tk-free is that these can be asserted directly. The two
things worth guarding hardest are both from GitHub issue #7 and feature-ideas.md #1: that "no
files left to upload" is only ever said when it is true, and that a transfer in flight reports a
percentage rather than a silent twenty minutes.
"""
from datetime import datetime, timezone

import pytest

from fireshare_agent.models import MediaKind
from fireshare_agent.pipeline.status import ActiveUpload, PipelineStatus
from fireshare_agent.ui import formatting


def _active(bytes_sent=0, size_bytes=1000, elapsed=10.0, path=r"C:\clips\clip.mp4"):
    return ActiveUpload(
        path=path, kind=MediaKind.VIDEO, size_bytes=size_bytes,
        bytes_sent=bytes_sent, elapsed_seconds=elapsed,
    )


def _status(**overrides):
    base = dict(paused=False, scanning=False, active=None, queued_count=0, waiting_count=0)
    base.update(overrides)
    return PipelineStatus(**base)


# ------------------------------------------------------------------ byte / duration formatting

@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1024 * 150, "150 KB"),          # past three digits the decimal is noise
        (1024 ** 3, "1.0 GB"),
        (4 * 1024 ** 3, "4.0 GB"),
    ],
)
def test_format_bytes(value, expected):
    assert formatting.format_bytes(value) == expected


def test_format_bytes_of_none_is_a_dash_not_a_crash():
    # Reached whenever a size is genuinely unknown; a window must still render.
    assert formatting.format_bytes(None) == "-"


def test_format_rate_of_no_sample_is_none_rather_than_zero():
    """None lets the caller drop the rate from the line entirely. "0 B/s" during the first second
    of a healthy upload reads as a stall."""
    assert formatting.format_rate(None) is None
    assert formatting.format_rate(0) is None


def test_format_duration_rounds_hard():
    assert formatting.format_duration(3) == "a few seconds"
    assert formatting.format_duration(45) == "45s"
    assert formatting.format_duration(200) == "3m"
    assert formatting.format_duration(7200) == "2.0h"


def test_format_timestamp_converts_stored_utc_to_local():
    """The manifest stores UTC. The old activity view printed it verbatim, so a user outside UTC
    was told every upload happened hours from when it did."""
    moment = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    assert formatting.format_timestamp(moment) == moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_format_timestamp_treats_a_naive_value_as_utc():
    naive = datetime(2026, 9, 6, 12, 0, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert formatting.format_timestamp(naive) == formatting.format_timestamp(aware)


# ------------------------------------------------------------------ active upload derivations

def test_percent_is_clamped_past_the_measured_size():
    """A chunked upload's running total can overshoot the size measured before the transfer
    began. A progress bar must not be asked to render 104%."""
    assert _active(bytes_sent=1040, size_bytes=1000).fraction == 1.0


def test_rate_and_eta_are_withheld_until_the_sample_is_long_enough():
    early = _active(bytes_sent=500, size_bytes=1000, elapsed=0.4)
    assert early.bytes_per_second is None
    assert early.eta_seconds is None


def test_eta_is_derived_from_the_average_rate():
    upload = _active(bytes_sent=500, size_bytes=1000, elapsed=10.0)
    assert upload.bytes_per_second == 50.0
    assert upload.eta_seconds == 10.0


def test_an_unknown_size_reports_no_progress_rather_than_dividing_by_zero():
    assert _active(bytes_sent=100, size_bytes=0).fraction == 0.0


# ------------------------------------------------------------------ summarize()

def test_an_empty_queue_reports_nothing_left_to_upload():
    """Issue #7, item 2."""
    summary = formatting.summarize(_status())
    assert summary.headline == "No files left to upload"
    assert summary.tone == formatting.TONE_IDLE
    assert summary.fraction is None


def test_the_idle_line_mentions_what_the_session_actually_did():
    summary = formatting.summarize(_status(uploaded_this_session=3, failed_this_session=1))
    assert "3 uploaded" in summary.detail
    assert "1 failed" in summary.detail


def test_a_scan_in_progress_is_never_reported_as_idle():
    """An empty queue during a rescan means "we have not looked yet", not "there is nothing".
    Saying otherwise invites the user to close the app mid-scan."""
    summary = formatting.summarize(_status(scanning=True))
    assert summary.headline != "No files left to upload"
    assert "Scanning" in summary.headline


def test_an_active_upload_reports_progress_and_a_bar():
    summary = formatting.summarize(_status(active=_active(bytes_sent=500, size_bytes=1000, elapsed=10.0)))
    assert summary.headline == "Uploading clip.mp4"
    assert summary.fraction == 0.5
    assert "50 B/s" in summary.detail
    assert "left" in summary.detail


def test_the_backlog_is_named_alongside_the_active_upload():
    summary = formatting.summarize(
        _status(active=_active(), queued_count=2, waiting_count=1)
    )
    assert "2 queued" in summary.detail
    assert "1 waiting" in summary.detail


def test_pause_outranks_everything_else():
    """A paused agent that says "uploading" is actively misleading about why nothing is
    happening."""
    summary = formatting.summarize(_status(paused=True, queued_count=5))
    assert summary.headline == "Paused"
    assert summary.tone == formatting.TONE_PAUSED


def test_pausing_mid_transfer_says_the_transfer_finishes_first():
    """Pause stops new work being taken on; it does not abort a transfer in flight. A user who is
    not told that kills the app instead of waiting."""
    summary = formatting.summarize(_status(paused=True, active=_active()))
    assert "clip.mp4" in summary.detail


def test_everything_parked_is_reported_as_waiting_not_as_progress():
    summary = formatting.summarize(_status(waiting_count=1, waiting_note="Still being written"))
    assert "waiting" in summary.headline
    assert summary.detail == "Still being written"
    assert summary.tone == formatting.TONE_WARNING


# ------------------------------------------------------------------ tray tooltip

def test_the_tooltip_carries_the_percentage():
    """The whole reason to hover a tray icon during a long upload."""
    status = _status(active=_active(bytes_sent=430, size_bytes=1000))
    assert formatting.tray_tooltip(status) == "Fireshare Agent - uploading clip.mp4 (43%)"


def test_the_tooltip_says_paused_when_paused():
    assert formatting.tray_tooltip(_status(paused=True)) == "Fireshare Agent (paused)"


def test_the_tooltip_is_always_a_single_line():
    """A tray tooltip renders one line; a newline in it would silently truncate everything after
    it, and paths are attacker-free but not newline-free on every filesystem."""
    for status in (
        _status(),
        _status(paused=True),
        _status(scanning=True),
        _status(queued_count=3),
        _status(active=_active()),
    ):
        assert "\n" not in formatting.tray_tooltip(status)
