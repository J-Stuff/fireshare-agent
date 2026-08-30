"""
Cheap dedupe fingerprint: file size plus a hash of the first and last chunk of the file.

Deliberately avoids hashing the full file - ShadowPlay clips can be multiple gigabytes and a
full SHA-256 pass would be wasted CPU/disk I/O for a dedupe check that only needs to
distinguish "have I seen this exact file before".
"""
from __future__ import annotations

import hashlib

_SAMPLE_BYTES = 1024 * 1024  # 1MB from each end


def compute(path: str, size_bytes: int) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(min(_SAMPLE_BYTES, size_bytes))
        sha256.update(head)

        if size_bytes > _SAMPLE_BYTES:
            tail_length = min(_SAMPLE_BYTES, size_bytes - _SAMPLE_BYTES)
            f.seek(-tail_length, 2)  # SEEK_END
            tail = f.read(tail_length)
            sha256.update(tail)

    return f"{size_bytes}-{sha256.hexdigest()}"
