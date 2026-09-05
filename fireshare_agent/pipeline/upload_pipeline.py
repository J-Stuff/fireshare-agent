"""
Ties together folder watching, readiness detection, dedupe, and upload dispatch. One candidate
is processed at a time by design - a background tray tool has no reason to saturate the user's
upload bandwidth or the OS with parallel large-file transfers.
"""
from __future__ import annotations

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
from fireshare_agent.uploaders.base import Uploader
from fireshare_agent.uploaders.web_api_uploader import WebApiUploader
from fireshare_agent.watching.folder_watcher import FolderWatcherService
from fireshare_agent.watching.readiness import is_ready

_NOT_READY_RETRY_DELAY_SECONDS = 15.0
_MAX_WAIT_FOR_READY_SECONDS = 24 * 60 * 60  # a file that never stabilizes after 24h is abandoned
_MAX_RETRY_BACKOFF_SECONDS = 30 * 60
_UPLOAD_METHOD_LABEL = "web_api"  # recorded in the manifest DB for historical/debugging purposes


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
        self._queue.put(None)  # unblock a pending queue.get()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

    def pause(self) -> None:
        self._watcher.pause()

    def resume(self) -> None:
        self._watcher.resume()

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
        """Walks all watch folders now and enqueues anything not already recorded as uploaded."""
        for folder in self._config.watch_folders:
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
                self._raise_activity(path, self._resolve_kind(path) or MediaKind.VIDEO, PipelineEventKind.FAILED, str(ex))
            if resolved:
                with self._in_flight_lock:
                    self._in_flight.pop(path, None)

    def _process_candidate(self, path: str) -> bool:
        """Returns True once this path is fully resolved (succeeded/failed/duplicate) and can
        leave the in-flight set. Returns False if it was requeued for a later readiness check
        (still being written) or a later upload retry - it stays marked in-flight so a watcher
        event or Sync Now doesn't pile another copy of it onto the queue in the meantime."""
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
                self._schedule_requeue(path, _NOT_READY_RETRY_DELAY_SECONDS)
                return False

            size_bytes = os.path.getsize(path)
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

        result = uploader.upload(pending_file)
        if result.success:
            self._manifest.record_success(fp, path, pending_file.size_bytes, _UPLOAD_METHOD_LABEL)
            self._apply_post_upload_action(path)
            self._raise_activity(path, kind, PipelineEventKind.SUCCEEDED)
            self._clear_retry_state(path)
            return True

        attempts_made += 1
        error = result.error_message or "Unknown error"
        if attempts_made >= self._config.max_retry_attempts:
            self._manifest.record_failure(fp, path, pending_file.size_bytes, _UPLOAD_METHOD_LABEL, error)
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
        self._schedule_requeue(path, backoff)
        return False

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
            self._pending_retry_timers.append(timer)
        timer.start()

    def _requeue(self, path: str) -> None:
        if not self._stop_event.is_set():
            self._queue.put(path)

    def _get_or_create_uploader(self) -> Uploader:
        with self._uploader_lock:
            if self._active_uploader is None:
                self._active_uploader = WebApiUploader(self._config.web_api, self._mfa_code_provider)
            return self._active_uploader

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

    def _raise_activity(self, path: str, kind: MediaKind, event_kind: PipelineEventKind, message: str | None = None) -> None:
        activity = PipelineActivity(path=path, kind=kind, event_kind=event_kind, message=message)
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
