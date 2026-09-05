"""
Prevents two copies of the agent running at once - e.g. "start with Windows" plus a manual
double-launch, or a stuck old process - which would otherwise run two independent
watchers/upload pipelines against the same Fireshare account and the same manifest database.

Uses a named Win32 mutex rather than a lock file: Windows releases it automatically when the
owning process exits or is killed, so a crash never leaves a stale lock behind the way a lock
file would (which needs its own staleness detection to get the same guarantee).
"""
from __future__ import annotations

import win32api
import win32event
import winerror

_MUTEX_NAME = "FireshareAgent-SingleInstance"

# Keeps every acquired handle alive for the life of the process - the lock is held only as long
# as the handle stays open.
_held_handles: list = []


def acquire(name: str = _MUTEX_NAME) -> bool:
    """Returns True if this process now holds the named lock (no other instance holds it), or
    False if another instance already does."""
    handle = win32event.CreateMutex(None, False, name)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    _held_handles.append(handle)
    return not already_running
