"""
Turning pipeline numbers into the sentences the main window and the tray tooltip show.

Deliberately free of any Tk import: this is where the window's actual wording lives, and keeping
it here means "what does the agent say when the queue drains" is a plain function call in a test
rather than something you have to stand up a Tcl interpreter and read a label to find out.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from fireshare_agent.pipeline.status import PipelineStatus

_UNITS = ("B", "KB", "MB", "GB", "TB")

# The four states the header can be in. The window maps these onto colours; keeping them as names
# rather than colours is what lets this module stay UI-toolkit-free.
TONE_IDLE = "idle"
TONE_BUSY = "busy"
TONE_PAUSED = "paused"
TONE_WARNING = "warning"


def format_bytes(num_bytes: int | float | None) -> str:
    """1024-based with the familiar KB/MB/GB labels - the same convention Windows Explorer uses,
    which is where the user just saw the size of the file they are watching upload."""
    if num_bytes is None:
        return "-"
    size = float(max(0, num_bytes))
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            # Whole bytes never want a decimal point ("512 B", not "512.0 B"), and neither does a
            # number that has grown back past three digits ("1013 MB").
            if unit == "B" or size >= 100:
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def format_rate(bytes_per_second: float | None) -> str | None:
    """None (rather than "0 B/s") when the sample is too short to mean anything, so callers can
    leave the rate out of the line entirely instead of printing a number they'd have to caveat."""
    if not bytes_per_second:
        return None
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | None) -> str | None:
    """A coarse, human duration for an ETA. Precision here is false confidence - the estimate is
    an average over a transfer whose throughput moves - so it rounds hard and never shows
    seconds once there are minutes to show."""
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    if seconds < 10:
        return "a few seconds"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{max(1, minutes)}m"
    hours = seconds / 3600
    return f"{hours:.1f}h" if hours < 10 else f"{int(hours)}h"


def format_timestamp(moment: datetime) -> str:
    """A stored UTC timestamp rendered in the user's own timezone.

    The manifest records `datetime.now(timezone.utc)`, and printing that verbatim - as the old
    activity view did - told a user in UTC+10 that a clip they uploaded a minute ago went up ten
    hours ago. Naive values are assumed to be UTC, which is what every row this app has ever
    written actually is."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class StatusSummary:
    """What the window's status card says right now."""

    headline: str
    detail: str
    tone: str
    # None when there is nothing to show a bar for. 0.0-1.0 otherwise; the bar is shown even at
    # 0.0, because "starting" is information.
    fraction: float | None = None


def summarize(status: PipelineStatus) -> StatusSummary:
    """The single source of truth for what state the agent is in, in words.

    Order matters and is not arbitrary. Paused wins over everything because a paused agent that
    says "uploading" is actively misleading about why nothing is happening. An active transfer
    wins over a scan because it is the more specific thing to report. Idle is last, so it can only
    be reached once every other explanation has been ruled out - which is what makes "no files
    left to upload" (issue #7) a claim worth trusting."""
    active = status.active

    if status.paused:
        detail = "Not watching for new captures. Resume to pick up where it left off."
        if active is not None:
            # Pause stops new work being taken on; it does not abort a transfer already in
            # flight. Saying so is the difference between a user waiting calmly and one killing
            # the app mid-upload because it "ignored" the pause.
            detail = f"Finishing {_name(active.path)} first - pause takes effect after it."
        return StatusSummary("Paused", detail, TONE_PAUSED)

    if active is not None:
        pieces = [f"{format_bytes(active.bytes_sent)} of {format_bytes(active.size_bytes)}"]
        rate = format_rate(active.bytes_per_second)
        if rate:
            pieces.append(rate)
        eta = format_duration(active.eta_seconds)
        if eta:
            pieces.append(f"{eta} left")
        queued = _describe_backlog(status)
        if queued:
            pieces.append(queued)
        return StatusSummary(
            headline=f"Uploading {_name(active.path)}",
            detail="  ·  ".join(pieces),
            tone=TONE_BUSY,
            fraction=active.fraction,
        )

    if status.scanning:
        return StatusSummary(
            "Scanning watch folders...",
            "Looking for captures that haven't been uploaded yet.",
            TONE_BUSY,
        )

    if status.waiting_count and not status.queued_count:
        # Nothing is being transferred and nothing is queued behind it - everything left is
        # parked. Reported as its own state rather than as "uploading", because from the user's
        # side the agent is deliberately doing nothing and they are owed the reason.
        return StatusSummary(
            _plural(status.waiting_count, "file", "files") + " waiting",
            status.waiting_note or "Waiting before the next attempt.",
            TONE_WARNING,
        )

    if status.queued_count or status.waiting_count:
        return StatusSummary(
            _plural(status.pending_count, "file", "files") + " to upload",
            status.waiting_note or "Working through the queue.",
            TONE_BUSY,
        )

    return StatusSummary(
        "No files left to upload",
        _idle_detail(status),
        TONE_IDLE,
    )


def _idle_detail(status: PipelineStatus) -> str:
    if status.uploaded_this_session or status.failed_this_session:
        parts = []
        if status.uploaded_this_session:
            parts.append(f"{status.uploaded_this_session} uploaded")
        if status.failed_this_session:
            parts.append(f"{status.failed_this_session} failed")
        return f"Watching for new captures. This session: {', '.join(parts)}."
    return "Watching for new captures."


def _describe_backlog(status: PipelineStatus) -> str | None:
    parts = []
    if status.queued_count:
        parts.append(f"{status.queued_count} queued")
    if status.waiting_count:
        parts.append(f"{status.waiting_count} waiting")
    return " and ".join(parts) if parts else None


def tray_tooltip(status: PipelineStatus) -> str:
    """The tray icon's hover text. Windows truncates a notification-icon tooltip at 127
    characters, and it is read at a glance, so this is the summary boiled down to one line -
    percentage included, since that is the whole reason someone hovers a tray icon mid-upload."""
    if status.paused:
        return "Fireshare Agent (paused)"
    active = status.active
    if active is not None:
        return f"Fireshare Agent - uploading {_name(active.path)} ({active.percent:.0f}%)"
    if status.scanning:
        return "Fireshare Agent - scanning watch folders"
    if status.pending_count:
        return f"Fireshare Agent - {status.pending_count} file(s) pending"
    return "Fireshare Agent"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _name(path: str) -> str:
    return os.path.basename(path) or path
