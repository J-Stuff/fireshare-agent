"""
Uploads via the Fireshare REST API. Auth is Flask session-cookie based (POST /api/login,
optional TOTP follow-up) - there is no API key/bearer token in Fireshare. Videos go through the
chunked endpoint so a single clip can be split into sub-100MB requests, which is what lets this
survive a Cloudflare-fronted Fireshare instance despite Cloudflare's 100MB request-body cap.
Screenshots are small enough to go through the plain single-request image endpoint.

Note: Fireshare's chunked endpoint does NOT validate the `checkSum` field against file content
(confirmed against the server source) - it only uses it as a grouping key for the chunk parts on
disk, so a random per-upload token is used rather than computing a real file hash.

Fireshare logs in via Flask-Login with remember=True, which sets a long-lived "remember me"
cookie alongside the session cookie. This uploader persists that cookie jar (via Credential
Manager, same as passwords) and tries it before ever hitting /api/login again, so a restart of
the agent - or clicking Test Connection more than once - doesn't force a fresh login (and thus a
fresh TOTP prompt) every time. It only falls back to a full login once the saved session
actually stops working (expired, revoked, or the server's secret key changed).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable

import requests
import urllib3

from fireshare_agent.config.app_config import WebApiSettings
from fireshare_agent.config.secrets import WEB_API_PASSWORD, WEB_API_SESSION_COOKIES, delete_secret, get_secret, set_secret
from fireshare_agent.models import ConnectionTestResult, MediaKind, PendingFile, UploadResult
from fireshare_agent.uploaders.base import Uploader

_EXISTING_ENTRIES_CACHE_TTL_SECONDS = 60


class MfaRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("Fireshare account requires a TOTP code (MFA), which was not provided.")


def clear_persisted_web_api_session() -> None:
    """Call this whenever the configured Fireshare URL/username/password changes, so a stale
    session for the old credentials never gets silently reused instead of testing the new ones."""
    delete_secret(WEB_API_SESSION_COOKIES)


class WebApiUploader(Uploader):
    def __init__(self, settings: WebApiSettings, mfa_code_provider: Callable[[], str | None] | None = None) -> None:
        self._settings = settings
        self._mfa_code_provider = mfa_code_provider
        self._session = requests.Session()
        self._session.verify = not settings.ignore_certificate_errors
        if settings.ignore_certificate_errors:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._authenticated = False
        self._resume_attempted = False
        self._existing_entries_cache: dict[MediaKind, tuple[float, list[dict]]] = {}

    def upload(self, file: PendingFile) -> UploadResult:
        try:
            self._ensure_authenticated()
            if file.kind == MediaKind.VIDEO:
                self._upload_video_chunked(file)
            else:
                self._upload_image(file)
            # Otherwise exists_at_destination() could serve a stale cached list (up to 60s old)
            # for the next file in the same batch and miss what was just uploaded.
            self._existing_entries_cache.pop(file.kind, None)
            return UploadResult.ok()
        except MfaRequiredError as ex:
            return UploadResult.fail(str(ex))
        except (requests.RequestException, OSError) as ex:
            return UploadResult.fail(str(ex))

    def test_connection(self) -> ConnectionTestResult:
        try:
            self._ensure_authenticated(force=True)
            return ConnectionTestResult.ok("Logged in to Fireshare successfully.")
        except MfaRequiredError as ex:
            return ConnectionTestResult.fail(str(ex))
        except requests.RequestException as ex:
            return ConnectionTestResult.fail(str(ex))

    def _ensure_authenticated(self, force: bool = False) -> None:
        if self._authenticated and not force:
            return

        # Only worth trying once per uploader instance: if the persisted session is invalid,
        # reloading the same bytes from disk again mid-run won't make it valid.
        if not self._resume_attempted:
            self._resume_attempted = True
            self._load_persisted_session()
            if self._try_resume_session():
                self._authenticated = True
                return
            self._clear_session_state()

        self._login_with_credentials()
        self._authenticated = True
        self._persist_session()

    def _try_resume_session(self) -> bool:
        """Probes a lightweight authenticated endpoint using whatever's already in the cookie
        jar. Only a 401 means "session invalid, log in fresh" - any other error (connectivity,
        server down, etc.) is a real problem and propagates instead of masquerading as an auth
        failure."""
        if len(self._session.cookies) == 0:
            return False
        response = self._session.get(self._url("/api/upload-folders"), timeout=15)
        if response.status_code == 401:
            return False
        response.raise_for_status()
        return True

    def _login_with_credentials(self) -> None:
        password = get_secret(WEB_API_PASSWORD) or ""
        response = self._session.post(
            self._url("/api/login"),
            json={"username": self._settings.username, "password": password},
            timeout=30,
        )
        response.raise_for_status()

        body = response.json() if response.content else {}
        if body.get("mfa_required"):
            if self._mfa_code_provider is None:
                raise MfaRequiredError()
            code = self._mfa_code_provider()
            if not code:
                raise MfaRequiredError()
            mfa_response = self._session.post(self._url("/api/login/mfa"), json={"code": code}, timeout=30)
            mfa_response.raise_for_status()

    def _load_persisted_session(self) -> None:
        raw = get_secret(WEB_API_SESSION_COOKIES)
        if not raw:
            return
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return
        if envelope.get("base_url") != self._settings.base_url:
            return  # saved session was for a different server; don't try to reuse it
        for name, value in envelope.get("cookies", {}).items():
            self._session.cookies.set(name, value)

    def _persist_session(self) -> None:
        cookie_dict = dict(self._session.cookies)
        if not cookie_dict:
            return
        envelope = {"base_url": self._settings.base_url, "cookies": cookie_dict}
        set_secret(WEB_API_SESSION_COOKIES, json.dumps(envelope))

    def _clear_session_state(self) -> None:
        self._session.cookies.clear()
        delete_secret(WEB_API_SESSION_COOKIES)

    def _upload_video_chunked(self, file: PendingFile) -> None:
        chunk_size = max(1, self._settings.chunk_size_bytes)
        total_chunks = max(1, -(-file.size_bytes // chunk_size))  # ceil division
        file_name = Path(file.path).name
        # Stable per (path, size) rather than a fresh random value per call: Fireshare names
        # chunk parts on disk as f"{checkSum}.part{chunkPart:04d}" and only cleans them up on a
        # successful reassembly (confirmed against the server source - abandoned chunks
        # otherwise sit on the server's disk indefinitely). A random checksum per attempt meant
        # every retry silently orphaned whatever the previous attempt had already uploaded. A
        # stable checksum lets a retry resend into the *same* group - already-received parts
        # just get overwritten with identical bytes - so the upload can still complete across
        # attempts instead of leaking a little more server disk space each time.
        check_sum = hashlib.sha256(f"{file.path}:{file.size_bytes}".encode("utf-8")).hexdigest()[:24]

        with open(file.path, "rb") as f:
            for part in range(1, total_chunks + 1):  # Fireshare's chunk loop is 1-indexed
                chunk = f.read(chunk_size)
                self._post_chunk(chunk, part, total_chunks, check_sum, file_name, file.size_bytes)

    def _post_chunk(self, chunk: bytes, part: int, total_chunks: int, check_sum: str, file_name: str, size_bytes: int) -> None:
        data = {
            "chunkPart": str(part),
            "totalChunks": str(total_chunks),
            "checkSum": check_sum,
            "fileName": file_name,
            "fileSize": str(size_bytes),
        }
        if self._settings.target_folder:
            data["folder"] = self._settings.target_folder

        response = self._session.post(
            self._url("/api/uploadChunked"),
            data=data,
            files={"blob": (file_name, chunk, "application/octet-stream")},
            timeout=120,
        )

        if response.status_code == 401:
            self._authenticated = False
            self._ensure_authenticated(force=True)
            response = self._session.post(
                self._url("/api/uploadChunked"),
                data=data,
                files={"blob": (file_name, chunk, "application/octet-stream")},
                timeout=120,
            )

        if response.status_code not in (200, 201, 202):
            response.raise_for_status()

    def _upload_image(self, file: PendingFile) -> None:
        file_name = Path(file.path).name
        data = {"folder": self._settings.target_folder} if self._settings.target_folder else {}

        response = self._post_image(file.path, file_name, data)
        if response.status_code == 401:
            self._authenticated = False
            self._ensure_authenticated(force=True)
            response = self._post_image(file.path, file_name, data)

        response.raise_for_status()

    def _post_image(self, path: str, file_name: str, data: dict[str, str]) -> requests.Response:
        with open(path, "rb") as f:
            return self._session.post(
                self._url("/api/upload/image"),
                data=data,
                files={"file": (file_name, f, "application/octet-stream")},
                timeout=120,
            )

    def exists_at_destination(self, file: PendingFile) -> bool:
        """Best-effort duplicate check: does a video/image with this filename already exist on
        the server? Fireshare stores uploads under `secure_filename(original_name)` and only
        appends a random suffix on an actual collision (confirmed against the server source), so
        matching on the normalized filename catches the common "this was already uploaded, by
        this agent or by hand" case. It can't be exact-hash-verified - Fireshare's API doesn't
        expose file size or a content hash for existing videos/images - so this is deliberately
        conservative: any failure to determine an answer returns False and lets the normal
        upload proceed rather than risk skipping a genuinely new file."""
        try:
            entries = self._fetch_existing_entries(file.kind)
        except (requests.RequestException, MfaRequiredError):
            return False

        target_stem = _normalize_filename(Path(file.path).name)
        target_ext = Path(file.path).suffix.lstrip(".").lower()

        for entry in entries:
            stored_path = entry.get("path") or ""
            if not stored_path:
                continue
            stored_ext = (entry.get("extension") or "").lower()
            if _normalize_filename(Path(stored_path).name) == target_stem and (not stored_ext or stored_ext == target_ext):
                return True
        return False

    def _fetch_existing_entries(self, kind: MediaKind) -> list[dict]:
        cached = self._existing_entries_cache.get(kind)
        if cached is not None and time.monotonic() - cached[0] < _EXISTING_ENTRIES_CACHE_TTL_SECONDS:
            return cached[1]

        self._ensure_authenticated()
        endpoint, key = ("/api/videos", "videos") if kind == MediaKind.VIDEO else ("/api/images", "images")
        response = self._session.get(self._url(endpoint), timeout=30)
        response.raise_for_status()
        entries = response.json().get(key, [])
        self._existing_entries_cache[kind] = (time.monotonic(), entries)
        return entries

    def list_upload_folders(self) -> list[str]:
        """Populates the folder picker in Settings via GET /api/upload-folders. Routed through
        the same _ensure_authenticated() as uploads, so an MFA-enabled account gets prompted
        here too instead of failing with an opaque 401."""
        self._ensure_authenticated()
        response = self._session.get(self._url("/api/upload-folders"), timeout=30)
        response.raise_for_status()
        return response.json().get("folders", [])

    def _url(self, path: str) -> str:
        return f"{self._settings.base_url.rstrip('/')}{path}"


def _normalize_filename(name: str) -> str:
    """Loose comparison key for filenames: lowercase, extension stripped, everything but
    letters/digits removed. Not trying to replicate Werkzeug's secure_filename exactly - just
    needs to treat "My Clip.mp4" and "my_clip.mp4" as the same file for dedupe purposes."""
    return re.sub(r"[^a-z0-9]+", "", Path(name).stem.lower())
