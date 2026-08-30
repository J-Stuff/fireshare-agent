from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fireshare_agent.models import MediaKind


class PipelineEventKind(str, Enum):
    QUEUED = "queued"
    WAITING = "waiting"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    ALREADY_AT_DESTINATION = "already_at_destination"


@dataclass(frozen=True)
class PipelineActivity:
    path: str
    kind: MediaKind
    event_kind: PipelineEventKind
    message: str | None = None
