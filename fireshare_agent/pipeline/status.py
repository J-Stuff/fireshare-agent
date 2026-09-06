"""
An immutable snapshot of what the pipeline is doing right now.

Built entirely from in-memory state - no database, no disk - because the main window polls it
once a second for as long as it is open. Anything that needs a query (lifetime totals, the
review queue) is read from the manifest on a much slower cadence instead.

Derived numbers (percent, throughput, ETA) live here rather than in the window so they can be
tested without a Tcl interpreter, and so the tray tooltip and the window can never disagree
about what "43%" means.
"""
from __future__ import annotations

from dataclasses import dataclass

from fireshare_agent.models import MediaKind

# Throughput over the first moment of a transfer is meaningless - the first chunk of a 4 GB clip
# can land in well under a second and imply a rate the connection cannot hold - and an ETA built
# on it swings wildly. Below this, the window shows the bar and byte counts but no rate/ETA.
_MIN_RATE_SAMPLE_SECONDS = 2.0


@dataclass(frozen=True)
class ActiveUpload:
    """The one file currently being transferred. The pipeline uploads strictly one at a time,
    so this is a single value rather than a list."""

    path: str
    kind: MediaKind
    size_bytes: int
    bytes_sent: int
    elapsed_seconds: float

    @property
    def fraction(self) -> float:
        """0.0-1.0, for a progress bar. A zero/unknown size reports 0 rather than dividing by it -
        the file is still being uploaded, we just cannot say how far along it is."""
        if self.size_bytes <= 0:
            return 0.0
        return max(0.0, min(1.0, self.bytes_sent / self.size_bytes))

    @property
    def percent(self) -> float:
        return self.fraction * 100.0

    @property
    def bytes_per_second(self) -> float | None:
        """Average throughput so far, or None while the sample is too short to mean anything.

        Deliberately an average over the whole transfer rather than an instantaneous rate: chunk
        boundaries make the instantaneous number lurch between "stalled" and "impossibly fast",
        and for an ETA the average is both steadier and more honest."""
        if self.elapsed_seconds < _MIN_RATE_SAMPLE_SECONDS or self.bytes_sent <= 0:
            return None
        return self.bytes_sent / self.elapsed_seconds

    @property
    def eta_seconds(self) -> float | None:
        rate = self.bytes_per_second
        if not rate:
            return None
        return max(0, self.size_bytes - self.bytes_sent) / rate


@dataclass(frozen=True)
class PipelineStatus:
    paused: bool
    # A rescan (Sync Now, or the one at startup) walking the watch folders. Tracked separately
    # from the queue because a scan of a large library can run for a while having enqueued
    # nothing yet, and reporting that as "idle" would be a lie the user acts on.
    scanning: bool
    active: ActiveUpload | None
    # Files accepted and awaiting their turn - excludes the active one and anything parked in
    # `waiting_count`, so the three numbers sum to the real in-flight total.
    queued_count: int
    # Files the pipeline is holding rather than working: still being written by the recorder, or
    # sitting out a retry backoff. They are not stuck, and they are not progress either.
    waiting_count: int
    waiting_note: str | None = None
    uploaded_this_session: int = 0
    failed_this_session: int = 0

    @property
    def pending_count(self) -> int:
        return self.queued_count + self.waiting_count + (1 if self.active else 0)

    @property
    def is_idle(self) -> bool:
        """Nothing left to do - the condition issue #7 asks the log to announce. A scan in
        progress is not idle even with an empty queue: it may be about to fill one."""
        return not self.scanning and self.pending_count == 0
