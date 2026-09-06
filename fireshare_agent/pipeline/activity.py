from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fireshare_agent.models import MediaKind


class PipelineEventKind(str, Enum):
    QUEUED = "queued"
    WAITING = "waiting"
    UPLOADING = "uploading"
    PROGRESS = "progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    ALREADY_AT_DESTINATION = "already_at_destination"
    IDLE = "idle"


# Events that carry no single file - IDLE describes the pipeline as a whole, so its `path` is
# empty and consumers that render a filename must not assume there is one.
FILELESS_EVENT_KINDS = frozenset({PipelineEventKind.IDLE})


@dataclass(frozen=True)
class PipelineActivity:
    path: str
    kind: MediaKind
    event_kind: PipelineEventKind
    message: str | None = None
    # Only populated on PROGRESS. Kept as raw byte counts rather than a precomputed percentage so
    # consumers can render whichever they want (a bar, "1.8 GB of 4.1 GB", a tray tooltip) without
    # the pipeline having to guess.
    bytes_sent: int | None = None
    total_bytes: int | None = None

    @property
    def percent(self) -> float | None:
        """0-100, or None when this event carries no progress information.

        Clamped rather than trusted: a chunked upload's final part can push the running total
        marginally past the size that was measured before the transfer started."""
        if self.bytes_sent is None or not self.total_bytes:
            return None
        return max(0.0, min(100.0, self.bytes_sent / self.total_bytes * 100.0))
