"""Core domain types shared across the watcher, pipeline, and uploaders."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"


class PostUploadAction(str, Enum):
    LEAVE = "leave"
    MOVE_TO_SUBFOLDER = "move_to_subfolder"
    DELETE = "delete"


@dataclass(frozen=True)
class PendingFile:
    path: str
    kind: MediaKind
    size_bytes: int
    # The file's subfolder relative to whichever configured watch folder contains it (e.g.
    # "HELLDIVERS 2" for .../captures/HELLDIVERS 2/clip.mp4), forward-slash normalized. None if
    # the file sits directly in the watch folder's root with no subfolder to mirror.
    remote_folder_hint: str | None = None


@dataclass(frozen=True)
class UploadResult:
    success: bool
    error_message: str | None = None

    @staticmethod
    def ok() -> "UploadResult":
        return UploadResult(True)

    @staticmethod
    def fail(message: str) -> "UploadResult":
        return UploadResult(False, message)


@dataclass(frozen=True)
class ConnectionTestResult:
    success: bool
    message: str

    @staticmethod
    def ok(message: str) -> "ConnectionTestResult":
        return ConnectionTestResult(True, message)

    @staticmethod
    def fail(message: str) -> "ConnectionTestResult":
        return ConnectionTestResult(False, message)
