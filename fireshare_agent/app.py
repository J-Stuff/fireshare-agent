"""Composition root: wires config, manifest, pipeline, tray, and settings UI together."""
from __future__ import annotations

import logging
import sys
import threading
import tkinter.messagebox as messagebox
from enum import Enum

import customtkinter as ctk

from fireshare_agent import __version__, assets, updater
from fireshare_agent.config import store as config_store
from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.pipeline.activity import FILELESS_EVENT_KINDS, PipelineActivity, PipelineEventKind
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline
from fireshare_agent.ui import formatting, mfa_dialog
from fireshare_agent.ui.main_window import MainWindow
from fireshare_agent.ui.settings_window import SettingsWindow
from fireshare_agent.ui.tray import TrayIcon

log = logging.getLogger(__name__)


class UpdateCheckResponse(Enum):
    """What the user should be shown after an update check. Split out from the code that shows it
    so the decision can be tested without a UI - `_on_update_check_result` is otherwise pure
    dispatch into modal dialogs and tray balloons."""

    OFFER_UPDATE = "offer_update"      # jump straight to the confirm-and-install dialog
    ANNOUNCE_UPDATE = "announce_update"  # unobtrusive tray balloon
    ALREADY_CURRENT = "already_current"  # modal "you are up to date"
    NOTHING = "nothing"


def decide_update_check_response(
    update_available: bool, user_initiated: bool, announce_automatic_updates: bool,
) -> UpdateCheckResponse:
    """A check the user explicitly asked for is owed a definite answer in *both* directions.

    The old shape got this backwards. "No update" - the less interesting outcome - got a modal
    window, while "update available" got only a transient tray balloon, and that balloon was gated
    on `show_upload_notifications`, the per-upload notification toggle. Turning that off is entirely
    reasonable for a background uploader, and it meant a manual check with an update waiting
    produced no feedback of any kind: click the button, nothing happens.

    So a user-initiated check with an update available now goes straight to the confirm dialog. The
    user asked about updates, there is one, and that dialog already names the version and explains
    what will happen - which collapses "read balloon, find the tray icon, open the menu, click
    update" into a single click. It also sidesteps the balloon's other problem: it said "see the
    tray menu", but a manual check is launched from Settings > Advanced, so the window the user is
    looking at is very likely covering the tray icon it points them at.

    The automatic startup check keeps the balloon - a modal on launch would be an ambush - but gated
    on a flag that actually means "tell me about updates"."""
    if update_available:
        if user_initiated:
            return UpdateCheckResponse.OFFER_UPDATE
        return UpdateCheckResponse.ANNOUNCE_UPDATE if announce_automatic_updates else UpdateCheckResponse.NOTHING
    return UpdateCheckResponse.ALREADY_CURRENT if user_initiated else UpdateCheckResponse.NOTHING


class FireshareAgentApp:
    def __init__(self) -> None:
        _claim_taskbar_identity()

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.withdraw()
        # Closing the (normally hidden) root window must not exit the app - only the tray's
        # Exit item should.
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        assets.apply_window_icon(self.root)

        self.config: AppConfig = config_store.load()
        self.manifest = ManifestStore()
        self.pipeline = UploadPipeline(self.manifest, self.config, mfa_code_provider=self._prompt_for_mfa_code)
        self.pipeline.add_activity_listener(self._on_activity)

        self.settings_window: SettingsWindow | None = None
        self.main_window: MainWindow | None = None
        self.tray: TrayIcon | None = None
        self._update_info: updater.UpdateInfo | None = None
        self._sync_thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()

    def run(self) -> None:
        # Every update we ever applied left its ~60MB installer sitting under %AppData%, one
        # directory per version, and nothing removed them. Done here rather than after applying an
        # update, because the installer we would be deleting is the one that just relaunched us -
        # and done before the tray exists so it cannot overlap a staging download.
        updater.cleanup_staged_installers()

        self.pipeline.start()
        if self.pipeline.is_paused:
            # Pause now survives a restart, which makes this rescan newly dangerous: a paused
            # agent would come back up and immediately upload everything it found sitting in the
            # watch folders - exactly what the user paused it to stop. Pause suppresses automatic
            # work; the manual "Sync Now" menu item is deliberately still allowed to run, since
            # that is a button the user just pressed rather than something happening on its own.
            log.info("Agent is paused; skipping the startup rescan.")
        else:
            self._start_sync()  # catches anything created while the app wasn't running

        self.tray = TrayIcon(
            on_open_settings=lambda: self.run_on_ui_thread(self.open_settings),
            on_open_main_window=lambda: self.run_on_ui_thread(self.open_main_window),
            on_sync_now=self._start_sync,
            on_toggle_pause=self._toggle_pause,
            is_paused=lambda: self.pipeline.is_paused,
            has_failures=lambda: self.pipeline.failed_count > 0,
            on_exit=lambda: self.run_on_ui_thread(self.exit_app),
            has_update=lambda: self._update_info is not None,
            update_version=lambda: self._update_info.version if self._update_info else "",
            on_update_now=lambda: self.run_on_ui_thread(self.confirm_and_apply_update),
            pending_review_count=lambda: self.pipeline.pending_review_count,
            # Read on every hover and on every progress tick, so it has to be a live callback
            # rather than a string computed once at construction.
            tooltip=lambda: formatting.tray_tooltip(self.pipeline.get_status()),
        )
        tray_thread = threading.Thread(target=self.tray.run, daemon=True, name="fireshare-agent-tray")
        tray_thread.start()

        if self.config.auto_check_for_updates:
            self.check_for_updates(notify_if_none=False)

        self.root.mainloop()

    def run_on_ui_thread(self, func) -> None:
        self.root.after(0, func)

    def _start_sync(self) -> None:
        """Kicks off a full rescan on a short-lived daemon thread, ignoring the request if one is
        already running.

        Never inline. A rescan is a recursive os.walk of every watch folder, and both callers are
        on threads that have to stay responsive: pystray dispatches menu items on a single
        callback thread, so blocking there freezes the entire menu (Exit included), and at startup
        this runs on the main thread before the tray icon even exists. Marshalling to the UI thread
        instead would be no better - run_on_ui_thread() runs the work inside Tk's event loop, which
        would just move the freeze onto the settings and activity windows."""
        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                # Sync Now is a menu item the user can click repeatedly while a slow scan of a
                # large library is still going. Each extra scan would be pure duplicated work -
                # anything the running one has already queued is held in the pipeline's in-flight
                # set, so a second walk would find the same files and discard them again.
                log.info("A rescan is already in progress; ignoring this request.")
                return
            self._sync_thread = threading.Thread(
                target=self._run_sync, daemon=True, name="fireshare-agent-sync",
            )
            self._sync_thread.start()

    def _run_sync(self) -> None:
        try:
            self.pipeline.sync_now()
        except Exception:
            # Nothing above this frame would report it: this is the top of a bare worker thread,
            # so an escaping error would only reach threading's default hook and be lost.
            log.exception("Rescan failed.")

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        self.settings_window = SettingsWindow(
            self.root, self.config, on_save=self._on_settings_saved,
            on_check_for_updates=lambda: self.check_for_updates(notify_if_none=True),
        )

    def open_main_window(self) -> None:
        """Shows the agent's main window, building it the first time it is asked for.

        Kept alive and hidden thereafter rather than destroyed on close: it is now the window the
        app is driven from, and rebuilding a few hundred widgets - and losing the user's filter,
        search, and scroll position - every time they glance at the tray would be the wrong
        trade. `winfo_exists()` is still checked because Tk can tear a Toplevel down underneath
        us during shutdown."""
        if self.main_window is not None and self.main_window.winfo_exists():
            self.main_window.show()
            return

        self.main_window = MainWindow(
            self.root, self.manifest, self.pipeline,
            config_provider=lambda: self.config,
            on_open_settings=self.open_settings,
            on_sync_now=self._start_sync,
            on_toggle_pause=self._toggle_pause,
            on_exit=self.exit_app,
            version=__version__,
            has_update=lambda: self._update_info is not None,
            update_version=lambda: self._update_info.version if self._update_info else "",
            on_update_now=self.confirm_and_apply_update,
        )

    def _on_settings_saved(self, new_config: AppConfig) -> None:
        self.config = new_config
        config_store.save(new_config)
        self.pipeline.update_config(new_config)
        # The icon and menu labels are rendered from live callbacks (pause state, failure count,
        # pending-review count), and nothing else on this path pokes them. Refresh unconditionally
        # so the tray can never keep asserting a state the pipeline has moved on from.
        if self.tray:
            self.tray.refresh()
        self._refresh_main_window()
        log.info("Settings saved; watcher and uploader reconfigured.")

    def _toggle_pause(self) -> None:
        if self.pipeline.is_paused:
            self.pipeline.resume()
        else:
            self.pipeline.pause()
        if self.tray:
            self.tray.refresh()
        self._refresh_main_window()

    def _refresh_main_window(self) -> None:
        """Redraws the main window if it exists and is on screen. A hidden window refreshes
        itself when it is shown again, so nudging one here would be wasted work."""
        window = self.main_window
        if window is None or not window.winfo_exists():
            return
        try:
            if window.winfo_viewable():
                window.refresh()
        except Exception:
            log.debug("Could not refresh the main window.", exc_info=True)

    def check_for_updates(self, notify_if_none: bool = True) -> None:
        """Runs the (network) version check on a background thread so it never blocks the UI
        thread or delays startup, then hands the result back via run_on_ui_thread."""
        def worker() -> None:
            info = updater.check_for_update()
            self.run_on_ui_thread(lambda: self._on_update_check_result(info, notify_if_none))

        threading.Thread(target=worker, daemon=True, name="fireshare-agent-update-check").start()

    def _on_update_check_result(self, info: updater.UpdateInfo | None, notify_if_none: bool) -> None:
        self._update_info = info
        if self.tray:
            self.tray.refresh()

        # notify_if_none is really "the user pressed the button", which now governs both branches
        # rather than only the no-update one.
        response = decide_update_check_response(
            update_available=info is not None,
            user_initiated=notify_if_none,
            # Someone who turned automatic checks on has already said they want to hear about
            # updates; the per-upload notification toggle says nothing about that.
            announce_automatic_updates=self.config.auto_check_for_updates,
        )

        if response == UpdateCheckResponse.OFFER_UPDATE:
            self.confirm_and_apply_update()
        elif response == UpdateCheckResponse.ANNOUNCE_UPDATE and info is not None:
            self._notify(f"Fireshare Agent {info.version} is available - see the tray menu to update.")
        elif response == UpdateCheckResponse.ALREADY_CURRENT:
            messagebox.showinfo("Fireshare Agent", f"You're already on the latest version ({__version__}).")

    def confirm_and_apply_update(self) -> None:
        info = self._update_info
        if info is None:
            return

        if not messagebox.askyesno(
            "Update Available",
            f"Fireshare Agent {info.version} is available (you're on {__version__}).\n\n"
            "The app will close and restart automatically to apply it. Update now?",
        ):
            return

        try:
            updater.apply_update(info, on_exit=self.exit_app)
        except Exception as ex:
            log.exception("Failed to apply update")
            messagebox.showerror("Update Failed", f"Could not apply the update:\n\n{ex}\n\nYou can also download it manually from {info.notes_url}")

    def _prompt_for_mfa_code(self) -> str | None:
        """Called from the pipeline's worker thread; blocks it while a modal prompt runs on the UI thread."""
        result: dict[str, str | None] = {"code": None}
        done = threading.Event()

        def ask() -> None:
            try:
                result["code"] = mfa_dialog.ask_for_code(self.root)
            finally:
                done.set()

        self.run_on_ui_thread(ask)
        done.wait(timeout=180)
        return result["code"]

    def _on_activity(self, activity: PipelineActivity) -> None:
        log.log(*_log_line_for(activity))

        if activity.event_kind == PipelineEventKind.PROGRESS:
            # Only the hover text moves during a transfer. Going through the full tray refresh
            # here would rebuild the icon bitmap and the native menu roughly once a second for
            # the length of the upload.
            if self.tray:
                self.run_on_ui_thread(self.tray.refresh_tooltip)
            return

        if self.tray and activity.event_kind == PipelineEventKind.UPLOADING:
            # A transfer starting changes the hover text but neither the icon nor the menu.
            self.run_on_ui_thread(self.tray.refresh_tooltip)
        elif self.tray and activity.event_kind in (
            PipelineEventKind.SUCCEEDED, PipelineEventKind.FAILED,
            PipelineEventKind.ALREADY_AT_DESTINATION, PipelineEventKind.IDLE,
        ):
            # These move the failure badge and the "Review N File(s)" menu entry, so the icon and
            # the menu both have to be rebuilt.
            self.run_on_ui_thread(self.tray.refresh)

        if activity.event_kind == PipelineEventKind.IDLE:
            return  # nothing to notify about; the log line and the window's status card say it

        if self.config.show_upload_notifications and self.tray:
            if activity.event_kind == PipelineEventKind.SUCCEEDED:
                self._notify(f"Uploaded {_short_name(activity.path)}")
            elif activity.event_kind == PipelineEventKind.ALREADY_AT_DESTINATION:
                self._notify(
                    f"Already on Fireshare: {_short_name(activity.path)} - open Fireshare Agent "
                    "to choose what happens to the local copy."
                )
            elif activity.event_kind == PipelineEventKind.FAILED:
                self._notify(f"Failed to upload {_short_name(activity.path)}: {activity.message}")

    def _notify(self, message: str) -> None:
        try:
            if self.tray:
                self.tray.icon.notify(message, title="Fireshare Agent")
        except Exception:
            # Still swallowed - a balloon is never worth crashing the pipeline over - but recorded,
            # so "the notification never appeared" is diagnosable rather than indistinguishable from
            # "the notification was never attempted".
            log.debug("Could not show a tray notification: %s", message, exc_info=True)

    def exit_app(self) -> None:
        log.info("Shutting down.")
        self.pipeline.stop()
        if self.tray:
            self.tray.stop()
        self.root.quit()
        self.root.destroy()
        sys.exit(0)


def _log_line_for(activity: PipelineActivity) -> tuple:
    """(level, format, *args) for one activity event.

    Split out from _on_activity so the level rules are stated in one place and can be checked in
    a test. Both WAITING and PROGRESS are high-frequency - WAITING repeats every ~15s for a
    recording still being written, and PROGRESS fires about once a second for the whole length of
    a transfer - so both are DEBUG, which under the app's INFO root logger means they are never
    written to the size-capped agent.log at all."""
    noisy = activity.event_kind in (PipelineEventKind.WAITING, PipelineEventKind.PROGRESS)
    level = logging.DEBUG if noisy else logging.INFO

    percent = activity.percent
    if percent is not None:
        return (level, "%s: %s (%.0f%%)", activity.event_kind.value, activity.path, percent)

    detail = f" ({activity.message})" if activity.message else ""
    if activity.event_kind in FILELESS_EVENT_KINDS:
        # IDLE describes the pipeline, not a file. Rendering it through the normal format would
        # print a stray empty path where every other line has a filename.
        return (level, "%s%s", activity.event_kind.value, detail)
    return (level, "%s: %s%s", activity.event_kind.value, activity.path, detail)


def _short_name(path: str) -> str:
    from pathlib import Path

    return Path(path).name


def _claim_taskbar_identity() -> None:
    """Running from source, the process is python.exe/pythonw.exe, and Windows normally groups
    the taskbar button under that host's own identity/icon rather than this app's - regardless
    of what iconbitmap() sets on the window. Giving the process its own AppUserModelID (before
    any window is created) tells Windows to treat it as its own taskbar entry, so the window's
    own icon is what shows. Irrelevant for the packaged exe (which already has its own identity
    and embedded icon), but harmless there too. Must run before the first window is shown."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FireshareAgent.TrayApp")
    except Exception:
        pass  # non-Windows or a locked-down environment - cosmetic, never worth failing over
