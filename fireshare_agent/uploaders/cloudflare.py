"""
Detects whether a Fireshare instance is served through Cloudflare.

This matters because Cloudflare caps a single proxied request body (100MB on Free/Pro/Business),
and this agent's whole chunked-upload design exists to stay under that. A user who raises the chunk
size past the cap gets uploads that fail with an opaque 413 from an edge they may not know is in
front of their server - so the setting is worth checking against reality rather than only warning
about it in a caption.

Detection is header-based and needs no authentication: Cloudflare stamps `cf-ray` on every response
it proxies, including the 401/403/404 an unauthenticated probe gets back.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

# https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#upload-limits
UPLOAD_LIMITS_DOC_URL = (
    "https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#upload-limits"
)

# Cloudflare's cap on a single proxied request body for Free/Pro/Business plans. Enterprise can be
# raised, which is one reason this produces a warning rather than a hard limit.
MAX_UPLOAD_MB = 100

# The chunk ceiling we actually advise. A chunk is not the whole request body - the multipart
# envelope adds the field names, boundaries, and the filename on top - so this leaves headroom
# rather than sitting exactly on the cap.
SAFE_CHUNK_MB = 95

# `cf-ray` is present on everything Cloudflare proxies. `cf-cache-status` is near-universal too but
# can be absent on some non-cacheable responses, so it is a secondary signal. Checked
# case-insensitively via requests' CaseInsensitiveDict.
_MARKER_HEADERS = ("cf-ray", "cf-cache-status")


def is_behind_cloudflare(base_url: str, verify: bool = True, timeout: float = 8.0) -> bool | None:
    """True/False when the answer is known, None when it could not be determined (offline, DNS
    failure, TLS rejection, timeout). None is deliberately distinct from False: "we could not tell"
    must not be presented to the user as "you are not behind Cloudflare"."""
    if not base_url.strip():
        return None

    url = base_url.rstrip("/") + "/"
    try:
        # HEAD first - the headers are the entire payload of interest, and it avoids pulling the
        # Fireshare front page over the wire just to read them.
        response = requests.head(url, timeout=timeout, verify=verify, allow_redirects=True)
        if response.status_code in (405, 501):  # server refuses HEAD; fall back to a GET
            response = _lightweight_get(url, verify, timeout)
    except requests.RequestException as ex:
        log.debug("Cloudflare probe failed for %s: %s", url, ex)
        return None

    return has_cloudflare_markers(response.headers)


def _lightweight_get(url: str, verify: bool, timeout: float) -> requests.Response:
    """A GET whose body is never read - streamed and closed immediately, so a large landing page
    costs headers only."""
    response = requests.get(url, timeout=timeout, verify=verify, allow_redirects=True, stream=True)
    response.close()
    return response


def has_cloudflare_markers(headers) -> bool:
    if any(header in headers for header in _MARKER_HEADERS):
        return True
    return (headers.get("server") or "").strip().lower() == "cloudflare"


def chunk_size_exceeds_safe_limit(chunk_size_bytes: int) -> bool:
    return chunk_size_bytes > SAFE_CHUNK_MB * 1024 * 1024
