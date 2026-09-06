"""
Uploads via the Fireshare REST API. Auth is Flask session-cookie based (POST /api/login,
optional TOTP follow-up) - there is no API key/bearer token in Fireshare. Videos go through the
chunked endpoint so a single clip can be split into sub-100MB requests, which is what lets this
survive a Cloudflare-fronted Fireshare instance despite Cloudflare's 100MB request-body cap.
Screenshots are small enough to go through the plain single-request image endpoint.

Share links are not returned by the upload endpoints - both `/api/uploadChunked` and
`/api/upload/image` end in a bare `Response(status=201)` with no body (confirmed against the
server source). The row that carries the id is created afterwards by `fireshare scan-video`,
which the server launches as a *separate process*, so the id does not exist yet when the 201
arrives. Resolving a link therefore means looking the file back up by name - see
`resolve_share_url` below.

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
import logging
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
import urllib3

from fireshare_agent.config.app_config import WebApiSettings
from fireshare_agent.config.secrets import WEB_API_PASSWORD, WEB_API_SESSION_COOKIES, delete_secret, get_secret, set_secret
from fireshare_agent.models import ConnectionTestResult, MediaKind, PendingFile, UploadResult
from fireshare_agent.uploaders.base import ProgressCallback, Uploader
from fireshare_agent.uploaders.throttle import RateLimiter

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

_EXISTING_ENTRIES_CACHE_TTL_SECONDS = 60

# The upload speed limit is configured in KB/s; the limiter works in bytes per second.
_KB = 1024

# `/api/videos` reads its sort with `request.args.get('sort')` - no default - and 400s on anything
# outside its allowlist. Omitting it (as this uploader used to) meant every video duplicate check
# got a 400, which raise_for_status turned into an exception that exists_at_destination swallowed
# into "not a duplicate, upload anyway" - silently disabling one of the agent's two layers of
# duplicate protection for videos. `/api/images` defaults the same parameter, so images were
# unaffected. This value is on both endpoints' allowlists.
_LIST_SORT = "updated_at desc"

# Fireshare's own UI builds a share link as `{base}/w/{video_id}` for a video and
# `{base}/i/{image_id}` for an image (app/client/src/common/utils.js).
_SHARE_PATH = {MediaKind.VIDEO: "/w/", MediaKind.IMAGE: "/i/"}
_ID_FIELD = {MediaKind.VIDEO: "video_id", MediaKind.IMAGE: "image_id"}

# `ui_config.shareable_link_domain` from GET /api/config, when an admin has set one, replaces the
# server's own address in every link the web UI hands out. An agent that ignored it would produce
# links that work for the user but not for whoever they are sending them to - which is the entire
# point of copying one.
_CONFIG_CACHE_TTL_SECONDS = 300

# Bumped whenever the persisted-session envelope changes shape. v1 stored a flat {name: value}
# map, which lost every cookie attribute; anything that isn't the current version is discarded and
# re-earned by a fresh login rather than reinterpreted under new rules.
_SESSION_ENVELOPE_VERSION = 2


class MfaRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("Fireshare account requires a TOTP code (MFA), which was not provided.")


def clear_persisted_web_api_session() -> None:
    """Call this whenever the configured Fireshare URL/username/password changes, so a stale
    session for the old credentials never gets silently reused instead of testing the new ones."""
    delete_secret(WEB_API_SESSION_COOKIES)


class WebApiUploader(Uploader):
    def __init__(
        self,
        settings: WebApiSettings,
        mfa_code_provider: Callable[[], str | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._mfa_code_provider = mfa_code_provider
        # Paces the transfer to the configured speed limit; a limit of 0 makes every call a no-op,
        # so the chunk loop does not need to branch on whether one is set. `sleep` is injected
        # because the pipeline passes one that returns early on shutdown - a limiter pause at a low
        # limit can be minutes long, and stop() only waits 5s for this thread.
        self._throttle = RateLimiter(settings.upload_speed_limit_kbps * _KB, sleep=sleep)
        self._session = requests.Session()
        self._session.verify = not settings.ignore_certificate_errors
        if settings.ignore_certificate_errors:
            # Logged as well as suppressed, so running without TLS verification leaves a trace
            # instead of being invisible. disable_warnings() edits the process-wide warnings
            # filter and there is no per-session equivalent, so one uploader pointed at a
            # self-signed server silences the warning for every later uploader too. That is
            # tolerable rather than ideal: urllib3 only raises InsecureRequestWarning for a request
            # made *without* verification, so an uploader that verifies normally has no warning to
            # lose - the filter cannot hide a problem with a properly-certificated host.
            log.warning(
                "TLS certificate verification is DISABLED for %s (per this server's settings).",
                settings.base_url,
            )
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._authenticated = False
        self._resume_attempted = False
        self._existing_entries_cache: dict[MediaKind, tuple[float, list[dict]]] = {}
        self._share_base_cache: tuple[float, str] | None = None

    def upload(self, file: PendingFile, on_progress: ProgressCallback | None = None) -> UploadResult:
        report = _safe_progress(on_progress)
        # Reported before a single byte moves so a consumer that only learns about the file from
        # this callback still starts at a truthful 0% rather than at whatever the first chunk
        # happens to be - which for a small clip is most of the file at once.
        report(0, file.size_bytes)
        try:
            self._ensure_authenticated()
            if file.kind == MediaKind.VIDEO:
                self._upload_video_chunked(file, report)
            else:
                self._upload_image(file, report)
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
        if not isinstance(envelope, dict) or envelope.get("version") != _SESSION_ENVELOPE_VERSION:
            return  # absent or older shape - discard rather than guess at what it meant
        if _normalized_base_url(envelope.get("base_url") or "") != _normalized_base_url(self._settings.base_url):
            return  # saved session was for a different server; don't try to reuse it

        host = self._session_host()
        for cookie in envelope.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            # requests treats set(name, None) as a *deletion*, so a null value would quietly
            # remove a cookie rather than restore one.
            if not name or value is None:
                continue
            # The domain is what confines the cookie to this server. A cookie restored without one
            # is sent to every host the session ever contacts, and the token being restored here is
            # Flask-Login's long-lived "remember me" cookie - the durable one. Rather than restore
            # an unscoped cookie, fall back to the configured host, and skip it entirely if even
            # that is unknown.
            domain = cookie.get("domain") or host
            if not domain:
                continue
            self._session.cookies.set(
                name, value,
                domain=domain,
                path=cookie.get("path") or "/",
                secure=bool(cookie.get("secure")),
            )

    def _persist_session(self) -> None:
        # Iterated rather than dict()-flattened: dict(jar) keeps only name/value, dropping the
        # domain/path scoping above and silently discarding one of two same-named cookies scoped
        # to different paths.
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or self._session_host(),
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
            for cookie in self._session.cookies
            if cookie.value is not None
        ]
        if not cookies:
            return
        envelope = {
            "version": _SESSION_ENVELOPE_VERSION,
            "base_url": self._settings.base_url,
            "cookies": cookies,
        }
        set_secret(WEB_API_SESSION_COOKIES, json.dumps(envelope))

    def _session_host(self) -> str:
        return urlparse(self._settings.base_url).hostname or ""

    def _clear_session_state(self) -> None:
        self._session.cookies.clear()
        delete_secret(WEB_API_SESSION_COOKIES)

    def _resolve_folder(self, file: PendingFile) -> str:
        """The Fireshare folder to upload into: the file's local subfolder (mirroring
        ShadowPlay's per-game layout) when that's enabled and known, otherwise the configured
        fixed target folder (which may itself be empty, meaning Fireshare's own server default)."""
        if self._settings.mirror_local_folder_structure and file.remote_folder_hint:
            return file.remote_folder_hint
        return self._settings.target_folder

    def _upload_video_chunked(self, file: PendingFile, report: ProgressCallback | None = None) -> None:
        chunk_size = max(1, self._settings.chunk_size_bytes)
        total_chunks = max(1, -(-file.size_bytes // chunk_size))  # ceil division
        file_name = Path(file.path).name
        folder = self._resolve_folder(file)
        # Stable per (path, size) rather than a fresh random value per call: Fireshare names
        # chunk parts on disk as f"{checkSum}.part{chunkPart:04d}" and only cleans them up on a
        # successful reassembly (confirmed against the server source - abandoned chunks
        # otherwise sit on the server's disk indefinitely). A random checksum per attempt meant
        # every retry silently orphaned whatever the previous attempt had already uploaded. A
        # stable checksum lets a retry resend into the *same* group - already-received parts
        # just get overwritten with identical bytes - so the upload can still complete across
        # attempts instead of leaking a little more server disk space each time.
        check_sum = hashlib.sha256(f"{file.path}:{file.size_bytes}".encode("utf-8")).hexdigest()[:24]
        report = _safe_progress(report)

        sent = 0
        with open(file.path, "rb") as f:
            for part in range(1, total_chunks + 1):  # Fireshare's chunk loop is 1-indexed
                chunk = f.read(chunk_size)
                # Pays off what earlier chunks still owe the speed limit before sending this one,
                # rather than sleeping after each send: waiting first means the final chunk of a
                # transfer never delays the success event (and the post-upload move/delete) for a
                # file whose bytes are already on the server.
                self._throttle.wait()
                self._post_chunk(chunk, part, total_chunks, check_sum, file_name, file.size_bytes, folder)
                # Counted only after the POST returns, so the number reported is bytes the server
                # has actually accepted rather than bytes handed to requests. A retried chunk (see
                # _post_chunk's 401 path) resends the same bytes into the same group, so it must
                # not be counted twice either.
                sent += len(chunk)
                report(sent, file.size_bytes)
                self._throttle.charge(len(chunk))

    def _post_chunk(self, chunk: bytes, part: int, total_chunks: int, check_sum: str, file_name: str, size_bytes: int, folder: str) -> None:
        data = {
            "chunkPart": str(part),
            "totalChunks": str(total_chunks),
            "checkSum": check_sum,
            "fileName": file_name,
            "fileSize": str(size_bytes),
        }
        if folder:
            data["folder"] = folder

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

    def _upload_image(self, file: PendingFile, report: ProgressCallback | None = None) -> None:
        """Screenshots go up in one request, so there is no meaningful mid-transfer progress to
        report - this jumps 0 -> 100 on success. Wrapping the file object to report as requests
        reads it would report bytes buffered, not bytes accepted, and for a file this small the
        distinction is the whole of the transfer."""
        report = _safe_progress(report)
        file_name = Path(file.path).name
        folder = self._resolve_folder(file)
        data = {"folder": folder} if folder else {}

        self._throttle.wait()
        response = self._post_image(file.path, file_name, data)
        if response.status_code == 401:
            self._authenticated = False
            self._ensure_authenticated(force=True)
            response = self._post_image(file.path, file_name, data)

        response.raise_for_status()
        report(file.size_bytes, file.size_bytes)
        # A screenshot is one unsplittable request, so its own transfer cannot be paced - but it
        # is still charged, so the limit holds across a batch. Someone whose watch folder fills
        # with a few hundred screenshots at once is exactly who set a limit in the first place.
        self._throttle.charge(file.size_bytes)

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
            return self._find_existing_entry(file) is not None
        except (requests.RequestException, MfaRequiredError):
            return False

    def _find_existing_entry(self, file: PendingFile, force_refresh: bool = False) -> dict | None:
        """The server-side row for this file, or None if there isn't one.

        The matching half of exists_at_destination, split out so resolving a share link can reuse
        it rather than re-deriving the same normalized-name-and-folder comparison. Unlike
        exists_at_destination this propagates request errors - a caller looking up a link wants to
        know the lookup failed, whereas a duplicate check deliberately treats "cannot tell" as
        "not a duplicate"."""
        entries = self._fetch_existing_entries(file.kind, force_refresh=force_refresh)

        target_stem = _normalize_filename(Path(file.path).name)
        target_ext = _normalize_extension(Path(file.path).suffix)
        # Folder-aware now that folders carry real meaning (mirrored per-game subfolders): two
        # different games' clips can legitimately share a filename, so a folder mismatch means
        # "different file", not "already uploaded". Only enforced when we actually know which
        # folder this file would land in - an empty target_folder (Fireshare's own default) still
        # falls back to name-only matching.
        target_folder = _normalize_filename(self._resolve_folder(file))

        for entry in entries:
            stored_path = entry.get("path") or ""
            if not stored_path:
                continue
            stored_ext = _normalize_extension(entry.get("extension"))
            stored_path_obj = Path(stored_path)

            if _normalize_filename(stored_path_obj.name) != target_stem:
                continue
            if stored_ext and stored_ext != target_ext:
                continue

            stored_folder = _normalize_filename(str(stored_path_obj.parent)) if stored_path_obj.parent != Path(".") else ""
            if target_folder and stored_folder and stored_folder != target_folder:
                continue  # same filename, different folder - treat as a distinct file

            return entry
        return None

    def _fetch_existing_entries(self, kind: MediaKind, force_refresh: bool = False) -> list[dict]:
        """force_refresh skips the cache. Needed when looking up a file that was uploaded seconds
        ago: a cached list up to a minute old predates it by definition, and would report the
        upload as missing rather than as still being processed."""
        cached = self._existing_entries_cache.get(kind)
        if not force_refresh and cached is not None and time.monotonic() - cached[0] < _EXISTING_ENTRIES_CACHE_TTL_SECONDS:
            return cached[1]

        self._ensure_authenticated()
        endpoint, key = ("/api/videos", "videos") if kind == MediaKind.VIDEO else ("/api/images", "images")
        response = self._session.get(self._url(endpoint), params={"sort": _LIST_SORT}, timeout=30)
        response.raise_for_status()
        entries = response.json().get(key, [])
        self._existing_entries_cache[kind] = (time.monotonic(), entries)
        return entries

    def resolve_share_url(self, file: PendingFile, force_refresh: bool = True) -> str | None:
        """The public Fireshare link for a file this agent has uploaded, or None if the server
        does not know about it yet.

        None is a genuinely expected answer, not just an error case: the upload endpoint returns
        201 as soon as the bytes are reassembled, and the database row carrying the id is written
        afterwards by a `fireshare scan-video` process the server spawns separately. A link asked
        for immediately after an upload can legitimately not exist for a while.

        Raises on a request failure so the caller can tell "not there yet" (retry) apart from
        "could not ask" (report it)."""
        entry = self._find_existing_entry(file, force_refresh=force_refresh)
        if entry is None:
            return None

        identifier = entry.get(_ID_FIELD[file.kind])
        if not identifier:
            return None  # a row without an id is not something a link can be built from

        return f"{self._share_base_url()}{_SHARE_PATH[file.kind]}{identifier}"

    def _share_base_url(self) -> str:
        """The origin share links are built on: the admin-configured `shareable_link_domain` when
        there is one, otherwise the server this agent uploads to.

        Cached for a few minutes - it is one more request per link resolution otherwise, and this
        is a setting an admin changes approximately never. Any failure to read it falls back to
        the configured base_url, which is the same thing Fireshare's UI does when the setting is
        absent."""
        if self._share_base_cache is not None and time.monotonic() - self._share_base_cache[0] < _CONFIG_CACHE_TTL_SECONDS:
            return self._share_base_cache[1]

        base = _normalized_base_url(self._settings.base_url)
        try:
            response = self._session.get(self._url("/api/config"), timeout=15)
            response.raise_for_status()
            configured = (response.json() or {}).get("shareable_link_domain") or ""
        except (requests.RequestException, ValueError):
            configured = ""  # unreachable or not JSON - fall back to the upload target

        configured = _normalized_base_url(configured)
        if configured:
            # The web UI concatenates this verbatim, so a value saved without a scheme would
            # produce "example.com/w/abc" - not a link anything can open. Borrow the scheme the
            # agent is already talking to the server with rather than assuming https.
            if "://" not in configured:
                scheme = urlparse(self._settings.base_url).scheme or "https"
                configured = f"{scheme}://{configured}"
            base = configured

        self._share_base_cache = (time.monotonic(), base)
        return base

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


def _normalized_base_url(base_url: str) -> str:
    """Trailing slashes are cosmetic - _url() strips them before building every request - so a
    saved session must not be thrown away (forcing a fresh login, and a fresh TOTP prompt) just
    because the user typed one this time and not last time."""
    return base_url.strip().rstrip("/")


def _normalize_filename(name: str) -> str:
    """Loose comparison key for filenames: lowercase, extension stripped, everything but
    letters/digits removed. Not trying to replicate Werkzeug's secure_filename exactly - just
    needs to treat "My Clip.mp4" and "my_clip.mp4" as the same file for dedupe purposes."""
    return re.sub(r"[^a-z0-9]+", "", Path(name).stem.lower())


def _normalize_extension(value: str | None) -> str:
    """Comparison key for a file extension, tolerant of the leading dot being there or not.

    Both /api/videos and /api/images return `extension` *with* the dot (".mp4"), while the local
    side derived its value from Path.suffix and stripped the dot - so ".mp4" != "mp4" made every
    candidate row fail the extension check and _find_existing_entry returned None for files that
    were plainly sitting on the server. That silently broke two separate features at once: Copy
    Link / Open in Fireshare answered "Fireshare hasn't finished processing ... yet" forever for
    a clip that had uploaded fine, and exists_at_destination reported False for everything,
    disabling server-side dedupe exactly the way the missing `sort` parameter once did. Every fixture in the test suite
    happened to spell the extension without a dot, so nothing caught it.

    Normalizing both sides rather than adding a dot to the local one is deliberate: it costs
    nothing and neither spelling can reintroduce the bug if the server's shape ever changes.
    """
    return (value or "").strip().lstrip(".").lower()


def _safe_progress(on_progress: ProgressCallback | None) -> ProgressCallback:
    """Normalizes an optional, caller-supplied callback into one that is always callable and can
    never break an upload.

    The consumer on the other end of this is UI state - a progress bar, a tray tooltip - and a
    multi-gigabyte transfer that is going fine has no business failing because a window was
    destroyed mid-callback."""
    if on_progress is None:
        return lambda sent, total: None

    def report(sent: int, total: int) -> None:
        try:
            on_progress(sent, total)
        except Exception:
            log.debug("An upload progress callback raised; ignoring it.", exc_info=True)

    return report
