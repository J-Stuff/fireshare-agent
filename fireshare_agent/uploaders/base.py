"""
A single, already-configured destination that pending files are handed to. Implementations
must stream file content rather than buffering whole files in memory - ShadowPlay clips can be
multiple gigabytes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from fireshare_agent.models import ConnectionTestResult, PendingFile, UploadResult


class Uploader(ABC):
    @abstractmethod
    def upload(self, file: PendingFile) -> UploadResult:
        ...

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        ...

    @abstractmethod
    def exists_at_destination(self, file: PendingFile) -> bool:
        """Best-effort check for whether this file already exists at the destination, so a lost
        local manifest / fresh install / manually-uploaded file doesn't get duplicated. Must
        never raise - any failure to determine this should return False and let the normal
        upload attempt proceed rather than blocking on an uncertain check."""
        ...
