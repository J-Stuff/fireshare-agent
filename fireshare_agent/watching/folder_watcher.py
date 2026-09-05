"""
Wraps one watchdog Observer watching all configured folders, debouncing the burst of
create/write events a single file produces while it's being written so a "candidate detected"
callback fires once per path a short quiet period after the last event for it.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from fireshare_agent.config.app_config import WatchFolderConfig

_DEBOUNCE_SECONDS = 2.0


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        allowed_extensions: set[str],
        is_paused: Callable[[], bool],
        on_candidate: Callable[[str], None],
        excluded_dir_name: str | None = None,
    ) -> None:
        self._allowed_extensions = allowed_extensions
        self._is_paused = is_paused
        self._on_candidate = on_candidate
        self._excluded_dir_name = excluded_dir_name.lower() if excluded_dir_name else None
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._debounce(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._debounce(event.dest_path)

    def _debounce(self, path: str) -> None:
        if self._is_paused():
            return
        if Path(path).suffix.lower() not in self._allowed_extensions:
            return
        # Ignore the post-upload destination subfolder (at any depth) - a file just moved there
        # after a successful upload would otherwise be picked straight back up as a "new" file.
        if self._excluded_dir_name is not None:
            if any(part.lower() == self._excluded_dir_name for part in Path(path).parent.parts):
                return

        with self._lock:
            existing = self._timers.get(path)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(_DEBOUNCE_SECONDS, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        if not self._is_paused():
            self._on_candidate(path)


class FolderWatcherService:
    def __init__(self) -> None:
        self._observer: Observer | None = None
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(
        self,
        folders: list[WatchFolderConfig],
        video_extensions: list[str],
        image_extensions: list[str],
        on_candidate: Callable[[str], None],
        excluded_dir_name: str | None = None,
    ) -> None:
        self.stop()

        video_exts = {e.lower() for e in video_extensions}
        image_exts = {e.lower() for e in image_extensions}

        observer = Observer()
        scheduled_any = False

        for folder in folders:
            if not folder.path or not Path(folder.path).is_dir():
                continue

            allowed: set[str] = set()
            if folder.watch_videos:
                allowed |= video_exts
            if folder.watch_images:
                allowed |= image_exts
            if not allowed:
                continue

            handler = _DebouncedHandler(allowed, lambda: self._paused, on_candidate, excluded_dir_name)
            observer.schedule(handler, folder.path, recursive=folder.recursive)
            scheduled_any = True

        if scheduled_any:
            observer.start()
            self._observer = observer
        self._paused = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
