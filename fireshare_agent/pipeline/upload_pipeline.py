"""
Ties together folder watching, readiness detection, dedupe, and upload dispatch. One candidate
is processed at a time by design - a background tray tool has no reason to saturate the user's
upload bandwidth or the OS with parallel large-file transfers.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest import fingerprint
from fireshare_agent.manifest.store import ManifestEntry, ManifestStore
from fireshare_agent.models import MediaKind, PendingFile, PostUploadAction
from fireshare_agent.pipeline.activity import PipelineActivity, PipelineEventKind
from fireshare_agent.pipeline.status import ActiveUpload, PipelineStatus
from fireshare_agent.uploaders.base import Uploader
from fireshare_agent.uploaders.web_api_uploader import WebApiUploader
from fireshare_agent.watching.folder_watcher import FolderWatcherService
from fireshare_agent.watching.readiness import is_ready

log = logging.getLogger(__name__)

_NOT_READY_RETRY_DELAY_SECONDS = 15.0
_MAX_WAIT_FOR_READY_SECONDS = 24 * 60 * 60  # a file that never stabilizes after 24h is abandoned
_MAX_RETRY_BACKOFF_SECONDS = 30 * 60
_UPLOAD_METHOD_LABEL = "web_api"  # recorded in the manifest DB for historical/debugging purposes

# A 4 GB clip at a 50 MB chunk size fires ~80 progress callbacks; a 200 MB one fires 4. Rather
# than let the event rate follow the chunk size, PROGRESS events are emitted on a wall-clock
# cadence. Every listener downstream is either a UI redraw or a log line, and neither benefits
# from more than about one update a second - the note in feature-ideas.md flagged flooding the
# size-capped agent.log as the specific trap here, the same one WAITING already fell into.
#
# The in-memory snapshot behind get_status() is NOT throttled: it is a single assignment, and
# the window polls it rather than being pushed to.
_PROGRESS_EVENT_INTERVAL_SECONDS = 1.0


_REVIEW_ACTION_VERB = {
    PostUploadAction.LEAVE: "keep",
    PostUploadAction.MOVE_TO_SUBFOLDER: "move",
    PostUploadAction.DELETE: "delete",
}


@dataclass(frozen=True)
class ShareLinkOutcome:
    """The result of asking for a file's public Fireshare link.

    Three outcomes, not two, because "not ready yet" is a normal state rather than a failure:
    Fireshare returns 201 for an upload as soon as the bytes land, and creates the row carrying
    the id afterwards in a separate `fireshare scan-video` process. Telling a user their upload
    failed because the link is not ready would be wrong."""

    url: str | None
    message: str

    @staticmethod
    def found(url: str) -> "ShareLinkOutcome":
        return ShareLinkOutcome(url, url)

    @staticmethod
    def not_ready(name: str) -> "ShareLinkOutcome":
        return ShareLinkOutcome(
            None,
            f"Fireshare hasn't finished processing {name} yet - try again in a moment.",
        )

    @staticmethod
    def failed(message: str) -> "ShareLinkOutcome":
        return ShareLinkOutcome(None, message)


@dataclass(frozen=True)
class ReviewOutcome:
    """What actually happened when the user's decision about a reviewed file was carried out.
    `resolved` means the row should stop appearing in the review list; a failure that the user
    could retry (a locked file, say) should leave it unresolved so the entry stays put."""
    resolved: bool
    message: str

    @staticmethod
    def done(message: str) -> "ReviewOutcome":
        return ReviewOutcome(True, message)

    @staticmethod
    def failed(message: str) -> "ReviewOutcome":
        return ReviewOutcome(False, message)


_REVIEW_ACTION_VERB = {
    PostUploadAction.LEAVE: "keep",
    PostUploadAction.MOVE_TO_SUBFOLDER: "move",
    PostUploadAction.DELETE: "delete",
}


@dataclass(frozen=True)
class ReviewOutcome:
    """What actually happened when the user's decision about a reviewed file was carried out.
    `resolved` means the row should stop appearing in the review list; a failure that the user
    could retry (a locked file, say) should leave it unresolved so the entry stays put."""
    resolved: bool
    message: str

    @staticmethod
    def done(message: str) -> "ReviewOutcome":
        return ReviewOutcome(True, message)

    @staticmethod
    def failed(message: str) -> "ReviewOutcome":
        return ReviewOutcome(False, message)


class UploadPipeline:
    def __init__(
        self,
        manifest: ManifestStore,
        config: AppConfig,
        mfa_code_provider: Callable[[], Optional[str]] | None = None,
    ) -> None:
        self._manifest = manifest
        self._config = config
        self._mfa_code_provider = mfa_code_provider

        self._watcher = FolderWatcherService()
        # Pause is sticky across restarts: an agent the user paused stays paused until they
        # resume it, rather than quietly coming back up watching after the next reboot.
        if manifest.is_watching_paused():
            self._watcher.pause()
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._in_flight: dict[str, float] = {}  # path -> first-seen monotonic timestamp
        self._in_flight_lock = threading.Lock()
        # path -> (pending_file, fingerprint, attempts already made) for a candidate that has
        # passed its duplicate/exists-at-destination checks and is partway through its upload
        # retry budget. Presence of an entry means a retry should skip straight to re-attempting
        # the upload rather than redoing those checks.
        self._retry_state: dict[str, tuple[PendingFile, str, int]] = {}
        self._retry_state_lock = threading.Lock()
        self._pending_retry_timers: list[threading.Timer] = []
        self._pending_retry_timers_lock = threading.Lock()

        self._activity_listeners: list[Callable[[PipelineActivity], None]] = []

        # Everything the main window and the tray tooltip poll, behind one lock. Deliberately
        # separate from the queue/retry structures above: those are the worker's own bookkeeping
        # and are held across slow operations, whereas this is read once a second by the UI
        # thread and must never be blocked behind an upload.
        self._status_lock = threading.Lock()
        self._active_path: str | None = None
        self._active_kind: MediaKind = MediaKind.VIDEO
        self._active_size_bytes = 0
        self._active_bytes_sent = 0
        self._active_started_at = 0.0
        self._last_progress_event_at = 0.0
        # path -> why it is parked (still being written, or sitting out a retry backoff). These
        # stay in _in_flight, so without tracking them separately they would be indistinguishable
        # from files genuinely queued and about to be worked on.
        self._waiting: dict[str, str] = {}
        self._scanning = False
        self._uploaded_this_session = 0
        self._failed_this_session = 0
        # Guards the "nothing left to do" announcement so a run of files produces one line at the
        # end rather than one per file that happened to briefly empty the queue.
        self._announced_idle = True

        self._active_uploader: Uploader | None = None
        self._uploader_lock = threading.Lock()

        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def add_activity_listener(self, listener: Callable[[PipelineActivity], None]) -> None:
        self._activity_listeners.append(listener)

    @property
    def is_paused(self) -> bool:
        return self._watcher.is_paused

    @property
    def failed_count(self) -> int:
        return self._manifest.get_failed_count()

    @property
    def pending_review_count(self) -> int:
        return self._manifest.get_pending_review_count()

    def get_pending_review(self) -> list[ManifestEntry]:
        return self._manifest.get_pending_review()

    def resolve_pending_review(self, entry: ManifestEntry, action: PostUploadAction) -> ReviewOutcome:
        """Applies the user's decision about the local copy of a file that was matched to a
        server-side file by name alone. Called from the UI thread, not the upload worker - it
        touches only this one file and the manifest row for it, both of which the worker has
        already finished with by the time a row becomes reviewable."""
        name = os.path.basename(entry.path)

        # The row can outlive the file: minutes or days may pass between the match being recorded
        # and the user getting to it, and in the meantime they may have moved or deleted the file
        # in Explorer themselves. There is nothing left to act on, and leaving the entry queued
        # would strand it there permanently, so treat it as answered rather than as a failure.
        if not os.path.exists(entry.path):
            self._manifest.clear_pending_review(entry.fingerprint)
            return ReviewOutcome.done(f"{name} is no longer on disk - removed from the review list.")

        try:
            self.perform_local_action(entry.path, action)
        except OSError as ex:
            # Deliberately leaves the row pending. A locked file is precisely the case the user
            # can fix (close whatever holds it) and try again, and clearing the flag here would
            # leave them believing the file had been dealt with when it hadn't.
            return ReviewOutcome.failed(f"Could not {_REVIEW_ACTION_VERB[action]} {name}: {ex}")

        self._manifest.clear_pending_review(entry.fingerprint)
        return ReviewOutcome.done(self._review_success_message(name, action))

    def _review_success_message(self, name: str, action: PostUploadAction) -> str:
        if action == PostUploadAction.MOVE_TO_SUBFOLDER:
            return f"{name} moved to {self._config.move_to_subfolder_name}."
        if action == PostUploadAction.DELETE:
            return f"{name} deleted."
        return f"{name} kept in place."

    def get_status(self) -> PipelineStatus:
        """A consistent snapshot of what the pipeline is doing, cheap enough to poll at 1 Hz.

        Assembled under one lock so the numbers agree with each other - reading them individually
        could show a file as both active and queued, or report an empty queue mid-handoff."""
        with self._in_flight_lock:
            in_flight = len(self._in_flight)

        with self._status_lock:
            active = None
            if self._active_path is not None:
                active = ActiveUpload(
                    path=self._active_path,
                    kind=self._active_kind,
                    size_bytes=self._active_size_bytes,
                    bytes_sent=self._active_bytes_sent,
                    elapsed_seconds=max(0.0, time.monotonic() - self._active_started_at),
                )
            waiting = dict(self._waiting)
            return PipelineStatus(
                paused=self._watcher.is_paused,
                scanning=self._scanning,
                active=active,
                # Clamped at zero rather than trusted to be consistent: in_flight is read under a
                # different lock a moment earlier, so a file resolving in between can briefly make
                # the arithmetic negative. A transiently low count is a cosmetic inaccuracy; a
                # negative one renders as nonsense.
                queued_count=max(0, in_flight - len(waiting) - (1 if active else 0)),
                waiting_count=len(waiting),
                waiting_note=next(iter(waiting.values()), None),
                uploaded_this_session=self._uploaded_this_session,
                failed_this_session=self._failed_this_session,
            )

    def _begin_active(self, path: str, kind: MediaKind, size_bytes: int) -> None:
        with self._status_lock:
            self._active_path = path
            self._active_kind = kind
            self._active_size_bytes = size_bytes
            self._active_bytes_sent = 0
            self._active_started_at = time.monotonic()
            # Reset rather than left alone, so the first chunk of a new file always produces an
            # event even if the previous file's last one was moments ago.
            self._last_progress_event_at = 0.0
            self._announced_idle = False

    def _end_active(self) -> None:
        with self._status_lock:
            self._active_path = None
            self._active_bytes_sent = 0
            self._active_size_bytes = 0

    def _on_upload_progress(self, path: str, kind: MediaKind, bytes_sent: int, total_bytes: int) -> None:
        """Called from inside the uploader on the worker thread, once per accepted chunk.

        Records the byte count unconditionally (one assignment - the polled snapshot should be as
        fresh as possible) but rate-limits the broadcast to listeners."""
        with self._status_lock:
            if self._active_path != path:
                return  # a late callback from a superseded upload; the snapshot has moved on
            self._active_bytes_sent = bytes_sent
            now = time.monotonic()
            is_final = total_bytes > 0 and bytes_sent >= total_bytes
            if not is_final and now - self._last_progress_event_at < _PROGRESS_EVENT_INTERVAL_SECONDS:
                return
            self._last_progress_event_at = now

        self._raise_activity(
            path, kind, PipelineEventKind.PROGRESS,
            bytes_sent=bytes_sent, total_bytes=total_bytes,
        )

    def _mark_waiting(self, path: str, reason: str) -> None:
        with self._status_lock:
            self._waiting[path] = reason

    def _clear_waiting(self, path: str) -> None:
        with self._status_lock:
            self._waiting.pop(path, None)

    def _announce_idle_if_drained(self) -> None:
        """Reports "nothing left to upload" once the queue actually empties, which is what issue
        #7 asked the log to say. Latched on _announced_idle so a batch of twenty clips produces
        one line at the end rather than a line every time the queue momentarily empties between
        two of them."""
        with self._in_flight_lock:
            drained = not self._in_flight
        with self._status_lock:
            if not drained or self._scanning or self._announced_idle:
                return
            self._announced_idle = True

        log.info("Nothing left to upload - all watched files are up to date.")
        self._raise_activity("", MediaKind.VIDEO, PipelineEventKind.IDLE, "No files left to upload")

    def resolve_share_url(self, entry: ManifestEntry) -> ShareLinkOutcome:
        """Looks up (and caches) the public Fireshare link for an already-uploaded file.

        Called from a background thread owned by the UI, never from the upload worker: it makes a
        request that lists every video on the server, and a queue of clips must not stall behind
        someone clicking "Copy Link". A cached link short-circuits it entirely.

        Only meaningful for a file that actually reached the server - a FAILED row has nothing to
        link to, and callers are expected to have checked that, but it is re-checked here because
        this is a public method."""
        if entry.share_url:
            return ShareLinkOutcome.found(entry.share_url)

        name = os.path.basename(entry.path)
        kind = self._resolve_kind(entry.path)
        if kind is None:
            return ShareLinkOutcome.failed(f"{name} isn't a type this agent uploads.")

        pending_file = PendingFile(
            path=entry.path, kind=kind, size_bytes=entry.size_bytes,
            remote_folder_hint=self._compute_remote_folder_hint(entry.path),
        )

        try:
            url = self._get_or_create_uploader().resolve_share_url(pending_file)
        except Exception as ex:
            # Broad on purpose: this runs on a UI-owned thread to answer a button press, and every
            # failure mode here (offline, auth expired, TLS, a server that changed its API) has
            # the same correct outcome - say so in the window rather than take the app down.
            log.warning("Could not resolve a Fireshare link for %s: %s", entry.path, ex)
            return ShareLinkOutcome.failed(f"Couldn't reach Fireshare to get the link: {ex}")

        if not url:
            return ShareLinkOutcome.not_ready(name)

        try:
            self._manifest.set_share_url(entry.fingerprint, url)
        except Exception:
            # The link is still perfectly usable; only the cache write failed, which costs one
            # extra lookup next time and nothing else.
            log.debug("Could not cache the resolved share link.", exc_info=True)
        return ShareLinkOutcome.found(url)

    def start(self) -> None:
        self._stop_event.clear()
        self._watcher.start(
            self._config.watch_folders, self._config.video_extensions, self._config.image_extensions,
            self._enqueue, self._config.move_to_subfolder_name,
        )
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True, name="fireshare-agent-upload-worker")
        self._worker_thread.start()

    def stop(self) -> None:
        self._watcher.stop()
        self._stop_event.set()
        with self._pending_retry_timers_lock:
            for timer in self._pending_retry_timers:
                timer.cancel()
            self._pending_retry_timers.clear()
        with self._retry_state_lock:
            self._retry_state.clear()
        with self._status_lock:
            self._waiting.clear()
            self._active_path = None
        self._queue.put(None)  # unblock a pending queue.get()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

    def pause(self) -> None:
        self._watcher.pause()
        self._persist_paused(True)

    def resume(self) -> None:
        self._watcher.resume()
        self._persist_paused(False)

    def _persist_paused(self, paused: bool) -> None:
        """Runtime state is updated first and persisted second, so a database problem leaves the
        agent actually doing what the tray says it is doing - the only thing lost is the memory
        of it across the next restart. Called on the tray's callback thread, so it must not
        raise: an unhandled error here would take out the pause toggle itself."""
        try:
            self._manifest.set_watching_paused(paused)
        except Exception:
            log.exception("Could not persist the pause state; it will not survive a restart.")

    def update_config(self, config: AppConfig) -> None:
        """Applies newly saved settings: restarts folder watches and forces the uploader to be rebuilt."""
        self._config = config
        with self._uploader_lock:
            self._active_uploader = None
        self._watcher.start(
            self._config.watch_folders, self._config.video_extensions, self._config.image_extensions,
            self._enqueue, self._config.move_to_subfolder_name,
        )

    def sync_now(self) -> None:
        """Walks all watch folders now and enqueues anything not already recorded as uploaded.

        Runs on a caller-supplied background thread (see FireshareAgentApp._start_sync), so it can
        still be part-way through a large library when the user hits Exit. It bails out on the stop
        event rather than churning the disk enqueueing work that the now-stopped worker will never
        pick up."""
        with self._status_lock:
            self._scanning = True
            # A scan that turns up nothing still owes the user the "nothing left to upload"
            # answer at the end of it, so re-arm the announcement here rather than leaving it
            # latched from the previous run.
            self._announced_idle = False
        try:
            self._walk_watch_folders()
        finally:
            with self._status_lock:
                self._scanning = False
        self._announce_idle_if_drained()

    def _walk_watch_folders(self) -> None:
        for folder in self._config.watch_folders:
            if self._stop_event.is_set():
                return
            if not folder.path or not os.path.isdir(folder.path):
                continue

            if folder.recursive:
                walker = os.walk(folder.path)
            else:
                try:
                    walker = [(folder.path, [], os.listdir(folder.path))]
                except OSError:
                    walker = []

            for root, dirs, files in walker:
                if self._stop_event.is_set():
                    return
                if folder.recursive:
                    # Prune the post-upload destination subfolder (at any depth, e.g. also under
                    # a mirrored per-game subfolder) so already-uploaded files that were moved
                    # there don't get walked, re-hashed, and re-checked against the manifest on
                    # every rescan.
                    dirs[:] = [d for d in dirs if d.lower() != self._config.move_to_subfolder_name.lower()]

                for name in files:
                    full_path = os.path.join(root, name)
                    kind = self._resolve_kind(full_path)
                    if kind is None:
                        continue
                    if (kind == MediaKind.VIDEO and folder.watch_videos) or (kind == MediaKind.IMAGE and folder.watch_images):
                        self._enqueue(full_path)

    def _enqueue(self, path: str) -> None:
        with self._in_flight_lock:
            if path in self._in_flight:
                return
            self._in_flight[path] = time.monotonic()
        self._raise_activity(path, self._resolve_kind(path) or MediaKind.VIDEO, PipelineEventKind.QUEUED)
        self._queue.put(path)

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            path = self._queue.get()
            if path is None:
                continue
            try:
                resolved = self._process_candidate(path)
            except Exception as ex:  # a single bad candidate must not kill the worker thread
                resolved = True
                self._end_active()
                self._raise_activity(path, self._resolve_kind(path) or MediaKind.VIDEO, PipelineEventKind.FAILED, str(ex))
            if resolved:
                with self._in_flight_lock:
                    self._in_flight.pop(path, None)
                self._clear_waiting(path)
                self._announce_idle_if_drained()

    def _process_candidate(self, path: str) -> bool:
        """Returns True once this path is fully resolved (succeeded/failed/duplicate) and can
        leave the in-flight set. Returns False if it was requeued for a later readiness check
        (still being written) or a later upload retry - it stays marked in-flight so a watcher
        event or Sync Now doesn't pile another copy of it onto the queue in the meantime."""
        # Cleared on entry rather than only on resolution: a path being processed is by
        # definition no longer parked, and if it ends up parked again the branch that does so
        # re-marks it with an up-to-date reason.
        self._clear_waiting(path)

        kind = self._resolve_kind(path)
        if kind is None:
            self._clear_retry_state(path)
            return True

        if not os.path.exists(path):
            self._clear_retry_state(path)
            return True  # moved or deleted before we got to it

        with self._retry_state_lock:
            retrying = self._retry_state.get(path)

        if retrying is None:
            # The manifest check runs *before* the readiness probe, not after it. is_ready()
            # sleeps 3s unconditionally as its stable-size window, and with the default
            # post_upload_action = LEAVE every previously-uploaded file stays in the watch folder
            # and is re-walked by sync_now() on every launch - a few hundred clips is that many
            # multiples of 3s spent sleeping before the first new file is even looked at.
            #
            # Safe to do first because the size is part of the fingerprint string: a file still
            # being written is a different size from the finished file it will become, so it
            # cannot match a stored fingerprint and simply falls through to the readiness path
            # below, exactly as before.
            precomputed = self._try_compute_fingerprint(path)
            if precomputed is not None and self._manifest.is_already_handled(precomputed[1]):
                self._raise_activity(path, kind, PipelineEventKind.SKIPPED_DUPLICATE)
                return True

            if not is_ready(path):
                with self._in_flight_lock:
                    first_seen = self._in_flight.get(path, time.monotonic())
                if time.monotonic() - first_seen > _MAX_WAIT_FOR_READY_SECONDS:
                    self._raise_activity(path, kind, PipelineEventKind.FAILED, "File never finished being written after 24 hours - giving up")
                    return True

                # A single ShadowPlay "Record" session (as opposed to a quick Instant Replay clip)
                # can stay open and growing for hours, so this requeues indefinitely rather than
                # giving up after a fixed number of attempts - and does it via a timer rather than
                # blocking this worker thread, so other ready files aren't stuck queued behind it.
                self._raise_activity(path, kind, PipelineEventKind.WAITING, "Still being written; will check again shortly")
                self._mark_waiting(path, "Still being written")
                self._schedule_requeue(path, _NOT_READY_RETRY_DELAY_SECONDS)
                return False

            size_bytes = os.path.getsize(path)
            if precomputed is not None and precomputed[0] == size_bytes:
                # Same size as the pre-check moments ago, and is_ready() has just confirmed the
                # size held steady across its own window too - re-reading a megabyte from each
                # end of the file would only reproduce the hash we already have.
                fp = precomputed[1]
            else:
                fp = fingerprint.compute(path, size_bytes)

            if self._manifest.is_already_handled(fp):
                self._raise_activity(path, kind, PipelineEventKind.SKIPPED_DUPLICATE)
                return True

            uploader = self._get_or_create_uploader()
            pending_file = PendingFile(
                path=path, kind=kind, size_bytes=size_bytes,
                remote_folder_hint=self._compute_remote_folder_hint(path),
            )

            # Best-effort check against the actual destination (not just our local history) -
            # covers a lost/fresh-install manifest, or a file that was already uploaded some
            # other way.
            #
            # Deliberately does NOT run the post-upload action: this match is inferred from the
            # filename alone (Fireshare exposes no size or content hash to verify against), so
            # acting on it would mean moving - or, with the delete action configured, permanently
            # destroying - a local file that may never have been uploaded at all. Instead the row
            # is flagged for review and the user decides per file; see
            # WebApiUploader.exists_at_destination and resolve_pending_review below.
            if uploader.exists_at_destination(pending_file):
                self._manifest.record_already_existed(
                    fp, path, size_bytes, _UPLOAD_METHOD_LABEL, pending_review=True
                )
                self._raise_activity(
                    path, kind, PipelineEventKind.ALREADY_AT_DESTINATION,
                    "Matched an existing file on the server by name - waiting for your review",
                )
                return True

            self._raise_activity(path, kind, PipelineEventKind.UPLOADING)
            attempts_made = 0
        else:
            pending_file, fp, attempts_made = retrying
            uploader = self._get_or_create_uploader()
            # A retry is a fresh transfer of the whole file as far as the user can see, so the
            # UPLOADING event is raised again - otherwise the window would show the file's second
            # attempt starting from whatever percentage the first one died at.
            self._raise_activity(path, kind, PipelineEventKind.UPLOADING)

        self._begin_active(path, kind, pending_file.size_bytes)
        try:
            result = uploader.upload(
                pending_file,
                on_progress=lambda sent, total: self._on_upload_progress(path, kind, sent, total),
            )
        finally:
            # In a finally, so the snapshot is released even if the uploader raises past its own
            # error handling. A window still showing a progress bar for a file nothing is working
            # on is worse than showing no bar at all.
            self._end_active()

        if result.success:
            self._manifest.record_success(fp, path, pending_file.size_bytes, _UPLOAD_METHOD_LABEL)
            self._apply_post_upload_action(path)
            with self._status_lock:
                self._uploaded_this_session += 1
            self._raise_activity(path, kind, PipelineEventKind.SUCCEEDED)
            self._clear_retry_state(path)
            return True

        attempts_made += 1
        error = result.error_message or "Unknown error"
        if attempts_made >= self._config.max_retry_attempts:
            self._manifest.record_failure(fp, path, pending_file.size_bytes, _UPLOAD_METHOD_LABEL, error)
            with self._status_lock:
                self._failed_this_session += 1
            self._raise_activity(path, kind, PipelineEventKind.FAILED, error)
            self._clear_retry_state(path)
            return True

        # Requeue via a timer instead of blocking this worker thread on time.sleep() for the
        # backoff - otherwise one failing upload (e.g. the server briefly unreachable) would
        # stall every other already-queued file for the full retry budget, which can be tens of
        # minutes with the default settings and far longer with a raised retry count/backoff.
        with self._retry_state_lock:
            self._retry_state[path] = (pending_file, fp, attempts_made)
        backoff = min(self._config.retry_backoff_seconds * (2 ** (attempts_made - 1)), _MAX_RETRY_BACKOFF_SECONDS)
        self._raise_activity(path, kind, PipelineEventKind.WAITING, f"Upload attempt {attempts_made} failed ({error}); retrying in {int(backoff)}s")
        self._mark_waiting(path, f"Retrying in {int(backoff)}s after attempt {attempts_made} failed")
        self._schedule_requeue(path, backoff)
        return False

    def _try_compute_fingerprint(self, path: str) -> tuple[int, str] | None:
        """(size, fingerprint) for a file that can be read right now, or None if it can't be.

        None means "unknown", and callers fall through to the normal readiness path - it is not a
        failure. The file may have been moved between the walk and here, or still be held
        exclusively by the recorder that is writing it; letting either OSError escape to the
        worker's generic catch would mark a perfectly good recording as FAILED."""
        try:
            size_bytes = os.path.getsize(path)
            if size_bytes == 0:
                return None  # a placeholder the recorder has only just created; nothing to hash
            return size_bytes, fingerprint.compute(path, size_bytes)
        except OSError:
            return None

    def _clear_retry_state(self, path: str) -> None:
        with self._retry_state_lock:
            self._retry_state.pop(path, None)

    def _schedule_requeue(self, path: str, delay_seconds: float) -> None:
        if self._stop_event.is_set():
            return
        # threading.Timer treats a negative delay as "fire immediately", which turns a retry
        # schedule into a hot loop. Config is clamped now, but this is the place where a bad
        # number would actually do damage, so it refuses one here too.
        timer = threading.Timer(max(0.0, delay_seconds), self._requeue, args=(path,))
        timer.daemon = True
        with self._pending_retry_timers_lock:
            # The list exists only so stop() can cancel what is still pending, but nothing used to
            # take entries back out of it: a multi-hour "Record" session rechecked every 15s left
            # ~240 dead Timer objects (each wrapping a Thread) behind per hour. Dropping the
            # finished ones here keeps it at roughly the number of genuinely pending retries, which
            # is small enough that the O(n) scan never matters.
            self._pending_retry_timers = [t for t in self._pending_retry_timers if t.is_alive()]
            self._pending_retry_timers.append(timer)
            # Started while the lock is held, deliberately. An unstarted Timer reports
            # is_alive() == False, so a timer appended before it was started could be pruned by a
            # concurrent scheduler between the two steps - quietly losing the only reference
            # stop() has to cancel it by. Starting here means no other holder of this lock can
            # ever observe an entry in that not-yet-alive state.
            timer.start()

    def _requeue(self, path: str) -> None:
        if not self._stop_event.is_set():
            self._queue.put(path)

    def _get_or_create_uploader(self) -> Uploader:
        with self._uploader_lock:
            if self._active_uploader is None:
                self._active_uploader = WebApiUploader(
                    self._config.web_api, self._mfa_code_provider, sleep=self._interruptible_sleep,
                )
            return self._active_uploader

    def _interruptible_sleep(self, seconds: float) -> None:
        """time.sleep, except that shutting the agent down cuts it short.

        The uploader uses this to pace transfers against the configured speed limit, and at a low
        limit one pause can run to minutes. stop() only gives this worker 5 seconds to finish
        before giving up on it, so a plain sleep would turn every Exit during a throttled upload
        into a five-second hang. Event.wait() is a sleep that the stop event can end early."""
        self._stop_event.wait(seconds)

    def _apply_post_upload_action(self, path: str) -> None:
        try:
            self.perform_local_action(path, PostUploadAction(self._config.post_upload_action))
        except (OSError, ValueError):
            # The upload already succeeded and is recorded; a local housekeeping failure
            # (e.g. destination locked) shouldn't be treated as an upload failure.
            pass

    def perform_local_action(self, path: str, action: PostUploadAction) -> None:
        """Carries out one local file action, raising OSError if it fails. Shared by the
        automatic post-upload path (which swallows failures) and the review flow (which reports
        them back to the user), so both apply identical move/delete semantics."""
        if action == PostUploadAction.LEAVE:
            return
        if action == PostUploadAction.MOVE_TO_SUBFOLDER:
            directory = os.path.dirname(path)
            subfolder = os.path.join(directory, self._config.move_to_subfolder_name)
            os.makedirs(subfolder, exist_ok=True)
            destination = _non_conflicting_path(os.path.join(subfolder, os.path.basename(path)))
            os.replace(path, destination)
            return
        if action == PostUploadAction.DELETE:
            os.remove(path)

    def _resolve_kind(self, path: str) -> MediaKind | None:
        ext = Path(path).suffix.lower()
        if ext in {e.lower() for e in self._config.video_extensions}:
            return MediaKind.VIDEO
        if ext in {e.lower() for e in self._config.image_extensions}:
            return MediaKind.IMAGE
        return None

    def _compute_remote_folder_hint(self, path: str) -> str | None:
        """The file's subfolder relative to whichever configured watch folder contains it (e.g.
        "HELLDIVERS 2" for .../captures/HELLDIVERS 2/clip.mp4) - ShadowPlay's default layout is
        one subfolder per game under the capture root, and this lets that carry over to Fireshare
        instead of every file landing in one flat folder. None if the file sits directly in a
        watch folder's root."""
        file_dir = os.path.dirname(path)
        for folder in self._config.watch_folders:
            if not folder.path:
                continue
            try:
                watch_root = os.path.realpath(folder.path)
                candidate_dir = os.path.realpath(file_dir)
            except OSError:
                continue

            if candidate_dir == watch_root:
                return None  # directly in the watch root, nothing to mirror

            try:
                rel = os.path.relpath(candidate_dir, watch_root)
            except ValueError:
                continue  # e.g. different drives on Windows - not actually under this root

            if rel == os.curdir or rel.startswith(os.pardir):
                continue  # not actually inside this watch folder

            return rel.replace(os.sep, "/")
        return None

    def _raise_activity(
        self, path: str, kind: MediaKind, event_kind: PipelineEventKind, message: str | None = None,
        bytes_sent: int | None = None, total_bytes: int | None = None,
    ) -> None:
        activity = PipelineActivity(
            path=path, kind=kind, event_kind=event_kind, message=message,
            bytes_sent=bytes_sent, total_bytes=total_bytes,
        )
        for listener in list(self._activity_listeners):
            try:
                listener(activity)
            except Exception:
                pass


def _non_conflicting_path(desired_path: str) -> str:
    if not os.path.exists(desired_path):
        return desired_path

    directory = os.path.dirname(desired_path)
    stem = Path(desired_path).stem
    suffix = Path(desired_path).suffix

    i = 2
    while True:
        candidate = os.path.join(directory, f"{stem} ({i}){suffix}")
        if not os.path.exists(candidate):
            return candidate
        i += 1
