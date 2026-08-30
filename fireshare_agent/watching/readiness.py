"""
Point-in-time check for "is this file finished being written and safe to read".

Combines a cheap stable-size pre-check with the authoritative exclusive-open probe (via the
Win32 CreateFile API, requesting zero sharing), so the pipeline doesn't hammer the OS with open
attempts while a long ShadowPlay recording is still growing. Callers own the retry cadence -
this only answers "ready right now?".
"""
from __future__ import annotations

import os
import time

import pywintypes
import win32file

_STABLE_CHECK_INTERVAL_SECONDS = 3.0


def is_ready(path: str) -> bool:
    size_before = _try_get_size(path)
    if size_before is None or size_before == 0:
        return False

    time.sleep(_STABLE_CHECK_INTERVAL_SECONDS)

    size_after = _try_get_size(path)
    if size_after is None or size_after != size_before:
        return False

    return _can_open_exclusively(path)


def _try_get_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _can_open_exclusively(path: str) -> bool:
    handle = None
    try:
        handle = win32file.CreateFile(
            path,
            win32file.GENERIC_READ,
            0,  # no sharing: fails with a sharing violation if another process still holds it
            None,
            win32file.OPEN_EXISTING,
            win32file.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        return True
    except pywintypes.error:
        # Sharing violation (or file vanished mid-check): treat as still locked/unready.
        return False
    finally:
        if handle is not None:
            handle.Close()
