"""Composition root: wires config, manifest, pipeline, tray, and settings UI together."""
from __future__ import annotations

import logging
import sys
import threading
import tkinter.messagebox as messagebox

import customtkinter as ctk

from fireshare_agent import __version__, assets, updater
from fireshare_agent.config import store as config_store
from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.pipeline.activity import PipelineActivity, PipelineEventKind
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline
from fireshare_agent.ui import mfa_dialog
from fireshare_agent.ui.activity_window import ActivityWindow
from fireshare_agent.ui.settings_window import SettingsWindow
from fireshare_agent.ui.tray import TrayIcon

log = logging.getLogger(__name__)


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
        self.activity_window: ActivityWindow | None = None
        self.tray: TrayIcon | None = None
        self._update_info: updater.UpdateInfo | None = None

    def run(self) -> None:
        self.pipeline.start()
        self.pipeline.sync_now()  # catches anything created while the app wasn't running

        self.tray = TrayIcon(
            on_open_settings=lambda: self.run_on_ui_thread(self.open_settings),
            on_open_activity=lambda: self.run_on_ui_thread(self.open_activity),
            on_sync_now=self.pipeline.sync_now,
            on_toggle_pause=self._toggle_pause,
            is_paused=lambda: self.pipeline.is_paused,
            has_failures=lambda: self.pipeline.failed_count > 0,
            on_exit=lambda: self.run_on_ui_thread(self.exit_app),
            has_update=lambda: self._update_info is not None,
            update_version=lambda: self._update_info.version if self._update_info else "",
            on_update_now=lambda: self.run_on_ui_thread(self.confirm_and_apply_update),
        )
        tray_thread = threading.Thread(target=self.tray.run, daemon=True, name="fireshare-agent-tray")
        tray_thread.start()

        if self.config.auto_check_for_updates:
            self.check_for_updates(notify_if_none=False)

        self.root.mainloop()

    def run_on_ui_thread(self, func) -> None:
        self.root.after(0, func)

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        self.settings_window = SettingsWindow(
            self.root, self.config, on_save=self._on_settings_saved,
            on_check_for_updates=lambda: self.check_for_updates(notify_if_none=True),
        )

    def open_activity(self) -> None:
        if self.activity_window is not None and self.activity_window.winfo_exists():
            self.activity_window.refresh()
            self.activity_window.lift()
            self.activity_window.focus_force()
            return
        self.activity_window = ActivityWindow(self.root, self.manifest)

    def _on_settings_saved(self, new_config: AppConfig) -> None:
        self.config = new_config
        config_store.save(new_config)
        self.pipeline.update_config(new_config)
        log.info("Settings saved; watcher and uploader reconfigured.")

    def _toggle_pause(self) -> None:
        if self.pipeline.is_paused:
            self.pipeline.resume()
        else:
            self.pipeline.pause()
        if self.tray:
            self.tray.refresh()

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

        if info is not None:
            if self.config.show_upload_notifications:
                self._notify(f"Fireshare Agent {info.version} is available - see the tray menu to update.")
        elif notify_if_none:
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
        # WAITING can repeat every ~15s for a long-running recording still being written; log
        # it at DEBUG so a multi-hour session doesn't flood the (size-capped) log file.
        level = logging.DEBUG if activity.event_kind == PipelineEventKind.WAITING else logging.INFO
        log.log(level, "%s: %s%s", activity.event_kind.value, activity.path, f" ({activity.message})" if activity.message else "")

        if self.tray and activity.event_kind in (PipelineEventKind.SUCCEEDED, PipelineEventKind.FAILED):
            self.run_on_ui_thread(self.tray.refresh)

        if self.config.show_upload_notifications and self.tray:
            if activity.event_kind == PipelineEventKind.SUCCEEDED:
                self._notify(f"Uploaded {_short_name(activity.path)}")
            elif activity.event_kind == PipelineEventKind.ALREADY_AT_DESTINATION:
                self._notify(f"Already on Fireshare, skipped: {_short_name(activity.path)}")
            elif activity.event_kind == PipelineEventKind.FAILED:
                self._notify(f"Failed to upload {_short_name(activity.path)}: {activity.message}")

    def _notify(self, message: str) -> None:
        try:
            if self.tray:
                self.tray.icon.notify(message, title="Fireshare Agent")
        except Exception:
            pass  # notifications are a nicety, never worth crashing the pipeline over

    def exit_app(self) -> None:
        log.info("Shutting down.")
        self.pipeline.stop()
        if self.tray:
            self.tray.stop()
        self.root.quit()
        self.root.destroy()
        sys.exit(0)


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
