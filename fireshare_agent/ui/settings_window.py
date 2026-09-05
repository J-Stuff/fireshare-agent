"""
The Settings window: everything the user can configure, across three tabs (General, Fireshare
Account, Advanced).

Password fields never show the stored secret - they use a placeholder indicating one is saved,
and are written to Windows Credential Manager as soon as you Test Connection / Fetch Folders (not
only on Save), so testing a freshly typed credential works without saving and reopening first.
"""
from __future__ import annotations

import copy
import os
import threading
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from fireshare_agent import __version__, assets
from fireshare_agent.config.app_config import (
    CHUNK_SIZE_MB_MAX,
    CHUNK_SIZE_MB_MIN,
    MAX_RETRY_ATTEMPTS_MAX,
    MAX_RETRY_ATTEMPTS_MIN,
    RETRY_BACKOFF_MAX_SECONDS,
    RETRY_BACKOFF_MIN_SECONDS,
    AppConfig,
    WatchFolderConfig,
    clamp,
)
from fireshare_agent.config.secrets import WEB_API_PASSWORD, delete_secret, get_secret, set_secret
from fireshare_agent.models import PostUploadAction
from fireshare_agent.ui import mfa_dialog, widgets
from fireshare_agent.uploaders import cloudflare
from fireshare_agent.uploaders.web_api_uploader import WebApiUploader, clear_persisted_web_api_session

POST_UPLOAD_LABELS = {
    PostUploadAction.LEAVE.value: "Leave in place",
    PostUploadAction.MOVE_TO_SUBFOLDER.value: "Move to subfolder",
    PostUploadAction.DELETE.value: "Delete",
}
_POST_UPLOAD_BY_LABEL = {v: k for k, v in POST_UPLOAD_LABELS.items()}


class SettingsWindow(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        config: AppConfig,
        on_save: Callable[[AppConfig], None],
        on_check_for_updates: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_save = on_save
        self._on_check_for_updates = on_check_for_updates
        # Work on a deep copy so Cancel (closing without saving) never mutates live config.
        self._config = copy.deepcopy(config)
        self._watch_folders: list[WatchFolderConfig] = list(self._config.watch_folders)
        # base_url -> is-behind-Cloudflare, for definite answers only. Avoids re-probing the
        # same server on every focus change.
        self._cloudflare_by_url: dict[str, bool] = {}

        self.title("Fireshare Agent - Settings")
        self.geometry("940x680")
        self.minsize(820, 540)
        self.configure(fg_color=widgets.WINDOW_BG)
        assets.apply_window_icon(self)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 12))
        ctk.CTkLabel(header, text="Settings", font=widgets.heading_font(20)).pack(anchor="w")
        ctk.CTkLabel(
            header, text="Choose what to watch and how captures get uploaded to Fireshare.",
            font=widgets.caption_font(), text_color=("gray40", "gray65"),
        ).pack(anchor="w", pady=(2, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 4))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        content = ctk.CTkFrame(body, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        general_page = widgets.scrollable_panel(content)
        account_page = widgets.scrollable_panel(content)
        advanced_page = widgets.scrollable_panel(content)

        self._build_general_tab(general_page)
        self._build_account_tab(account_page)
        self._build_advanced_tab(advanced_page)

        nav = widgets.SidebarNav(body, [
            ("🗂", "General", general_page),
            ("👤", "Fireshare Account", account_page),
            ("⚙", "Advanced", advanced_page),
        ])
        nav.grid(row=0, column=0, sticky="ns", padx=(0, 16))

        divider = ctk.CTkFrame(self, height=1, fg_color=("gray85", "gray22"))
        divider.pack(fill="x", padx=20, pady=(4, 0))

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.pack(fill="x", padx=20, pady=14)
        ctk.CTkButton(button_bar, text="Save", width=110, command=self._save).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            button_bar, text="Cancel", width=110, command=self.destroy,
            fg_color="transparent", border_width=1, text_color=("gray10", "gray90"), hover_color=("gray85", "gray28"),
        ).pack(side="right")
        self._save_error_label = ctk.CTkLabel(button_bar, text="", font=widgets.caption_font(), text_color=("#d1453b", "#ff7b72"))
        self._save_error_label.pack(side="left")

    # ---------------------------------------------------------------- General

    def _build_general_tab(self, tab) -> None:
        folders_body = widgets.section_card(tab, "Watch Folders", "Folders the agent watches for new ShadowPlay clips and screenshots.")

        # A plain frame, not another CTkScrollableFrame: this whole page already scrolls, and
        # nesting scrollable regions fights the mouse wheel over whose area is being scrolled.
        # It sizes to however many folders there are instead of reserving dead space.
        self._folders_frame = ctk.CTkFrame(folders_body, fg_color="transparent")
        self._folders_frame.pack(fill="x")
        self._refresh_folder_rows()

        ctk.CTkButton(folders_body, text="+ Add Folder...", width=140, command=self._add_folder).pack(anchor="w", pady=(10, 0))

        extensions_body = widgets.section_card(tab, "File Types")
        self._video_ext_entry = widgets.labeled_entry(extensions_body, "Video extensions:", ", ".join(self._config.video_extensions))
        self._image_ext_entry = widgets.labeled_entry(extensions_body, "Image extensions:", ", ".join(self._config.image_extensions))
        widgets.caption(extensions_body, "Comma-separated, e.g. .mp4, .mkv").pack(anchor="w", pady=(2, 0))

        upload_body = widgets.section_card(tab, "After a Successful Upload")
        self._post_upload_var = ctk.StringVar(value=POST_UPLOAD_LABELS[self._config.post_upload_action])
        ctk.CTkSegmentedButton(
            upload_body, values=list(POST_UPLOAD_LABELS.values()),
            variable=self._post_upload_var, command=self._on_post_upload_changed,
        ).pack(fill="x", pady=(0, 8))

        self._subfolder_row = widgets.labeled_row(upload_body, "Subfolder name:")
        self._subfolder_entry = ctk.CTkEntry(self._subfolder_row, font=widgets.body_font())
        self._subfolder_entry.insert(0, self._config.move_to_subfolder_name)
        self._subfolder_entry.pack(side="left", fill="x", expand=True)
        self._on_post_upload_changed(self._post_upload_var.get())

    def _on_post_upload_changed(self, _label: str) -> None:
        if self._post_upload_var.get() == POST_UPLOAD_LABELS[PostUploadAction.MOVE_TO_SUBFOLDER.value]:
            self._subfolder_row.pack(fill="x", pady=4)
        else:
            self._subfolder_row.pack_forget()

    def _refresh_folder_rows(self) -> None:
        for widget in self._folders_frame.winfo_children():
            widget.destroy()

        if not self._watch_folders:
            widgets.caption(self._folders_frame, "No folders configured yet - add one below.").pack(anchor="w", pady=8)

        for index, wf in enumerate(self._watch_folders):
            row = ctk.CTkFrame(self._folders_frame, corner_radius=8, fg_color=("gray93", "gray26"))
            row.pack(fill="x", pady=3, padx=2)
            row.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(row, text=wf.path, anchor="w", font=widgets.body_font()).grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=10)

            checks = ctk.CTkFrame(row, fg_color="transparent")
            checks.grid(row=0, column=1, padx=4)
            self._folder_checkbox(checks, "Videos", wf, "watch_videos")
            self._folder_checkbox(checks, "Images", wf, "watch_images")
            self._folder_checkbox(checks, "Recursive", wf, "recursive")

            ctk.CTkButton(
                row, text="✕", width=28, height=28, fg_color="transparent", border_width=1,
                text_color=("gray10", "gray90"), hover_color=("#f4c7c7", "#4a1f22"),
                command=lambda i=index: self._remove_folder(i),
            ).grid(row=0, column=2, padx=(4, 12))

    def _folder_checkbox(self, parent, text: str, wf: WatchFolderConfig, attr: str) -> None:
        var = ctk.BooleanVar(value=getattr(wf, attr))

        def _on_toggle() -> None:
            setattr(wf, attr, var.get())

        ctk.CTkCheckBox(parent, text=text, variable=var, width=76, font=widgets.caption_font(), command=_on_toggle).pack(side="left", padx=4)

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="Select folder to watch", parent=self)
        if path:
            self._watch_folders.append(WatchFolderConfig(path=path))
            self._refresh_folder_rows()

    def _remove_folder(self, index: int) -> None:
        del self._watch_folders[index]
        self._refresh_folder_rows()

    # ------------------------------------------------------------- Account

    def _build_account_tab(self, tab) -> None:
        widgets.caption(tab, "Uploads go through Fireshare's own website login. Large clips are automatically split into pieces under 100MB, so this still works behind Cloudflare.").pack(anchor="w", pady=(0, 4))
        widgets.caption(tab, "Passwords are saved to Windows Credential Manager as soon as you Test Connection or Save - not only on Save.").pack(anchor="w", pady=(0, 12))

        s = self._config.web_api
        account_body = widgets.section_card(tab, "Fireshare Account")
        self._webapi_url_entry = widgets.labeled_entry(account_body, "Server URL:", s.base_url)
        self._webapi_username_entry = widgets.labeled_entry(account_body, "Username:", s.username)
        self._webapi_password_entry = widgets.labeled_password(account_body, "Password:", WEB_API_PASSWORD)

        self._webapi_ignore_cert_var = ctk.BooleanVar(value=s.ignore_certificate_errors)
        ctk.CTkCheckBox(
            account_body, text="Ignore TLS certificate errors (self-signed servers only - reduces security)",
            variable=self._webapi_ignore_cert_var, font=widgets.body_font(),
        ).pack(anchor="w", pady=(6, 0))

        upload_body = widgets.section_card(tab, "Upload Options")

        self._mirror_folders_var = ctk.BooleanVar(value=s.mirror_local_folder_structure)
        ctk.CTkCheckBox(
            upload_body, text="Mirror local subfolders as Fireshare folders (e.g. a per-game capture folder)",
            variable=self._mirror_folders_var, font=widgets.body_font(),
        ).pack(anchor="w", pady=(0, 8))

        folder_row = widgets.labeled_row(upload_body, "Fallback folder:")
        self._webapi_folder_combo = ctk.CTkComboBox(folder_row, values=[s.target_folder] if s.target_folder else [""], font=widgets.body_font())
        self._webapi_folder_combo.set(s.target_folder)
        self._webapi_folder_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(folder_row, text="Fetch Folders", width=110, command=self._fetch_web_api_folders).pack(side="left")
        widgets.caption(upload_body, "Used for files that sit directly in a watch folder with no subfolder, or for everything if mirroring is off. Leave blank to use Fireshare's own default folder.").pack(anchor="w", pady=(2, 0))

        chunk_mb = max(1, s.chunk_size_bytes // (1024 * 1024))
        self._webapi_chunk_entry = widgets.labeled_entry(upload_body, "Chunk size (MB):", str(chunk_mb))
        # Re-checked when the user leaves the field rather than on every keystroke: the probe is a
        # network round trip, and a half-typed "1" on the way to "150" is not worth warning about.
        self._webapi_chunk_entry.bind("<FocusOut>", lambda _e: self._check_cloudflare_chunk_limit())
        self._webapi_url_entry.bind("<FocusOut>", lambda _e: self._check_cloudflare_chunk_limit())

        self._cloudflare_warning = widgets.LinkLabel(
            upload_body,
            text="",
            url=cloudflare.UPLOAD_LIMITS_DOC_URL,
            link_text="Read Cloudflare's upload limits",
        )
        widgets.caption(
            upload_body,
            f"{CHUNK_SIZE_MB_MIN}-{CHUNK_SIZE_MB_MAX} MB. Keep this well under 100MB if your "
            "Fireshare instance is behind Cloudflare.",
        ).pack(anchor="w", pady=(2, 0))

        test_row = ctk.CTkFrame(tab, fg_color="transparent")
        test_row.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(test_row, text="Test Connection", width=140, command=self._test_web_api_connection).pack(side="left")
        self._connection_status_label = ctk.CTkLabel(test_row, text="", anchor="w", font=widgets.caption_font())
        self._connection_status_label.pack(side="left", padx=10, fill="x", expand=True)

        self._check_cloudflare_chunk_limit()

    def _test_web_api_connection(self) -> None:
        widgets.set_status(self._connection_status_label, "info", "Testing...")
        # persist_secrets=True: a freshly typed password must be usable immediately, not only
        # after Save + reopening Settings - and the MFA follow-up inside test_connection() reads
        # the password back out of Credential Manager, so it has to be there before the call.
        previous_password = get_secret(WEB_API_PASSWORD)
        working_config = self._build_config_from_fields(persist_secrets=True)

        def worker() -> None:
            try:
                uploader = WebApiUploader(working_config.web_api, mfa_code_provider=self._prompt_for_mfa_code)
                result = uploader.test_connection()
            except Exception as ex:  # a bad field value shouldn't crash the settings window
                _keep_password_only_if_confirmed(previous_password, confirmed=False)
                self.after(0, lambda: widgets.set_status(self._connection_status_label, "error", str(ex)))
                return
            _keep_password_only_if_confirmed(previous_password, confirmed=result.success)
            self.after(0, lambda: widgets.set_status(self._connection_status_label, "success" if result.success else "error", result.message))
            self.after(0, self._check_cloudflare_chunk_limit)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_web_api_folders(self) -> None:
        # persist_secrets=True for the same reason as _test_web_api_connection above.
        previous_password = get_secret(WEB_API_PASSWORD)
        working_config = self._build_config_from_fields(persist_secrets=True)
        widgets.set_status(self._connection_status_label, "info", "Fetching folders...")

        def worker() -> None:
            try:
                uploader = WebApiUploader(working_config.web_api, mfa_code_provider=self._prompt_for_mfa_code)
                folders = uploader.list_upload_folders()
            except Exception as ex:
                _keep_password_only_if_confirmed(previous_password, confirmed=False)
                self.after(0, lambda: widgets.set_status(self._connection_status_label, "error", f"Could not fetch folders: {ex}"))
                return
            _keep_password_only_if_confirmed(previous_password, confirmed=True)

            if folders:
                self.after(0, lambda: self._webapi_folder_combo.configure(values=folders))
                self.after(0, lambda: widgets.set_status(self._connection_status_label, "success", f"Loaded {len(folders)} folder(s)."))
            else:
                self.after(0, lambda: widgets.set_status(self._connection_status_label, "info", "Logged in, but the server returned no folders."))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------- Cloudflare chunk check

    def _check_cloudflare_chunk_limit(self) -> None:
        """Warns when the configured chunk size would exceed what Cloudflare will proxy, but only
        once we actually know Cloudflare is in front of this server. Cheap paths first: if the
        chunk size is already safe, or the answer for this URL is cached, no network call happens
        at all - this runs on every focus change out of the URL and chunk fields."""
        chunk_mb = _safe_int(self._webapi_chunk_entry.get(), 50)
        base_url = self._webapi_url_entry.get().strip()

        if chunk_mb <= cloudflare.SAFE_CHUNK_MB or not base_url:
            self._hide_cloudflare_warning()
            return

        cached = self._cloudflare_by_url.get(base_url)
        if cached is not None:
            self._render_cloudflare_warning(cached, chunk_mb)
            return

        verify = not self._webapi_ignore_cert_var.get()

        def worker() -> None:
            detected = cloudflare.is_behind_cloudflare(base_url, verify=verify)
            self.after(0, lambda: self._on_cloudflare_probe_result(base_url, detected))

        threading.Thread(target=worker, daemon=True, name="fireshare-agent-cloudflare-probe").start()

    def _on_cloudflare_probe_result(self, base_url: str, detected: bool | None) -> None:
        if not self.winfo_exists():
            return  # window closed while the probe was in flight
        if detected is not None:
            # Only a definite answer is cached. "Could not determine" (offline, server down) must
            # be re-probed later rather than remembered as "not behind Cloudflare".
            self._cloudflare_by_url[base_url] = detected
        if base_url != self._webapi_url_entry.get().strip():
            return  # the user has moved on to a different server since this probe started
        self._render_cloudflare_warning(bool(detected), _safe_int(self._webapi_chunk_entry.get(), 50))

    def _render_cloudflare_warning(self, behind_cloudflare: bool, chunk_mb: int) -> None:
        if not behind_cloudflare or chunk_mb <= cloudflare.SAFE_CHUNK_MB:
            self._hide_cloudflare_warning()
            return

        self._cloudflare_warning.set_text(
            f"⚠  This server appears to be behind Cloudflare, which rejects a single upload larger "
            f"than {cloudflare.MAX_UPLOAD_MB}MB. A chunk size of {chunk_mb}MB will likely fail with "
            f"a 413 error - {cloudflare.SAFE_CHUNK_MB}MB or lower is safe."
        )
        self._cloudflare_warning.pack(anchor="w", pady=(4, 0))

    def _hide_cloudflare_warning(self) -> None:
        self._cloudflare_warning.pack_forget()

    def _prompt_for_mfa_code(self) -> str | None:
        """Called from a background worker thread (Test Connection / Fetch Folders); shows the
        modal TOTP prompt on the UI thread and blocks the worker until it's answered."""
        result: dict[str, str | None] = {"code": None}
        done = threading.Event()

        def ask() -> None:
            try:
                result["code"] = mfa_dialog.ask_for_code(self)
            finally:
                done.set()

        self.after(0, ask)
        done.wait(timeout=180)
        return result["code"]

    # -------------------------------------------------------------- Advanced

    def _build_advanced_tab(self, tab) -> None:
        reliability_body = widgets.section_card(tab, "Reliability")
        self._max_retries_entry = widgets.labeled_entry(reliability_body, "Max retry attempts per file:", str(self._config.max_retry_attempts))
        self._retry_backoff_entry = widgets.labeled_entry(reliability_body, "Retry backoff (seconds):", str(self._config.retry_backoff_seconds))
        widgets.caption(
            reliability_body,
            f"Attempts {MAX_RETRY_ATTEMPTS_MIN}-{MAX_RETRY_ATTEMPTS_MAX}, backoff "
            f"{RETRY_BACKOFF_MIN_SECONDS}-{RETRY_BACKOFF_MAX_SECONDS} seconds. The delay doubles "
            "after each failed attempt, up to 30 minutes.",
        ).pack(anchor="w", pady=(2, 0))

        startup_body = widgets.section_card(tab, "Startup & Notifications")
        # Read from the registry rather than from the saved config. The Run entry can be removed
        # behind the app's back (a cleanup utility, another startup manager, a manual regedit), and
        # rendering the config would leave the checkbox ticked while the app no longer starts with
        # Windows. Whatever is shown here is what Save writes back, so opening Settings and saving
        # also resolves the drift.
        self._start_with_windows_var = ctk.BooleanVar(value=_start_with_windows_state(self._config))
        ctk.CTkCheckBox(startup_body, text="Start Fireshare Agent automatically when you sign in to Windows", variable=self._start_with_windows_var, font=widgets.body_font()).pack(anchor="w", pady=4)
        self._notifications_var = ctk.BooleanVar(value=self._config.show_upload_notifications)
        ctk.CTkCheckBox(startup_body, text="Show a tray notification for each upload", variable=self._notifications_var, font=widgets.body_font()).pack(anchor="w", pady=4)

        data_body = widgets.section_card(tab, "Data", "Passwords are stored in Windows Credential Manager, never in the config file itself.")
        ctk.CTkButton(data_body, text="Open Config Folder", width=170, command=self._open_config_folder).pack(anchor="w")

        updates_body = widgets.section_card(tab, "Updates", f"Currently running version {__version__}.")
        self._auto_update_var = ctk.BooleanVar(value=self._config.auto_check_for_updates)
        ctk.CTkCheckBox(updates_body, text="Automatically check for updates on startup", variable=self._auto_update_var, font=widgets.body_font()).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(updates_body, text="Check for Updates Now", width=170, command=self._check_for_updates_now).pack(anchor="w")

    def _check_for_updates_now(self) -> None:
        if self._on_check_for_updates:
            self._on_check_for_updates()

    def _open_config_folder(self) -> None:
        from fireshare_agent.config.store import app_data_dir

        directory = app_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)  # noqa: S606 - opening our own known-safe local folder in Explorer

    # ------------------------------------------------------------------ Save

    def _read_bounded(
        self, entry, default: int, minimum: int, maximum: int, label: str, unit: str, adjustments: list[str],
    ) -> int:
        """Reads one numeric field, clamps it, and - if the typed value was out of range - rewrites
        the field with what was actually stored and records a note for the user. Silently accepting
        0 here is what let a chunk size of 0 become 1-byte chunks."""
        typed = _safe_int(entry.get(), default)
        value = clamp(typed, minimum, maximum)
        if value != typed:
            adjustments.append(f"{label} must be {minimum}-{maximum} {unit} (you entered {typed}, using {value})")
            entry.delete(0, "end")
            entry.insert(0, str(value))
        return value

    def _build_config_from_fields(self, persist_secrets: bool, adjustments: list[str] | None = None) -> AppConfig:
        adjustments = adjustments if adjustments is not None else []
        config = copy.deepcopy(self._config)

        config.watch_folders = list(self._watch_folders)
        config.video_extensions = _split_extensions(self._video_ext_entry.get())
        config.image_extensions = _split_extensions(self._image_ext_entry.get())
        config.post_upload_action = _POST_UPLOAD_BY_LABEL[self._post_upload_var.get()]
        config.move_to_subfolder_name = self._subfolder_entry.get().strip() or "Uploaded"

        config.web_api.base_url = self._webapi_url_entry.get().strip()
        config.web_api.username = self._webapi_username_entry.get().strip()
        config.web_api.ignore_certificate_errors = self._webapi_ignore_cert_var.get()
        config.web_api.mirror_local_folder_structure = self._mirror_folders_var.get()
        config.web_api.target_folder = self._webapi_folder_combo.get().strip()
        config.web_api.chunk_size_bytes = self._read_bounded(
            self._webapi_chunk_entry, 50, CHUNK_SIZE_MB_MIN, CHUNK_SIZE_MB_MAX,
            "Chunk size", "MB", adjustments,
        ) * 1024 * 1024

        webapi_credentials_changed = (
            bool(self._webapi_password_entry.get())
            or config.web_api.username != self._config.web_api.username
            or config.web_api.base_url != self._config.web_api.base_url
        )
        self._maybe_persist_secret(self._webapi_password_entry, persist_secrets)
        if persist_secrets and webapi_credentials_changed:
            # The saved session belongs to whatever URL/account it was logged into - if any of
            # that just changed, reusing it would silently test the OLD credentials instead of
            # what's now in these fields.
            clear_persisted_web_api_session()

        config.max_retry_attempts = self._read_bounded(
            self._max_retries_entry, 5, MAX_RETRY_ATTEMPTS_MIN, MAX_RETRY_ATTEMPTS_MAX,
            "Max retry attempts", "", adjustments,
        )
        config.retry_backoff_seconds = self._read_bounded(
            self._retry_backoff_entry, 30, RETRY_BACKOFF_MIN_SECONDS, RETRY_BACKOFF_MAX_SECONDS,
            "Retry backoff", "seconds", adjustments,
        )
        config.start_with_windows = self._start_with_windows_var.get()
        config.show_upload_notifications = self._notifications_var.get()
        config.auto_check_for_updates = self._auto_update_var.get()

        return config

    @staticmethod
    def _maybe_persist_secret(field: widgets.PasswordField, persist_secrets: bool) -> None:
        if not persist_secrets:
            return
        value = field.get()
        if value:
            set_secret(field.secret_key, value)

    def _save(self) -> None:
        adjustments: list[str] = []
        try:
            new_config = self._build_config_from_fields(persist_secrets=True, adjustments=adjustments)
        except Exception as ex:
            self._save_error_label.configure(text=f"Could not save: {ex}")
            return

        if adjustments:
            # Deliberately does not close. These values change how the agent behaves - a chunk size
            # of 0 would have meant 1-byte chunks - so the corrected number is written back into the
            # field and the user gets to see it before committing to it. Clicking Save again with
            # the values now in range goes straight through.
            self._save_error_label.configure(text=" - ".join(adjustments) + ". Check and Save again.")
            return
        self._save_error_label.configure(text="")

        from fireshare_agent import startup

        try:
            startup.set_enabled(new_config.start_with_windows)
        except OSError:
            pass  # non-fatal: registry write can fail under odd permission setups

        self._on_save(new_config)
        self.destroy()


def _keep_password_only_if_confirmed(previous_password: str | None, confirmed: bool) -> None:
    """Rolls the stored password back to what it was, unless the server just confirmed the new one.

    Test Connection has to write the password to Credential Manager *before* the test, because the
    MFA follow-up inside the login flow reads it back from there. The cost used to be that a typo
    was persisted permanently: the user tests, sees "login failed", closes Settings without saving,
    and the background pipeline goes on retrying with the wrong password - which is exactly how an
    account ends up locked out by its own uploader. Narrowing the window to the duration of the
    test keeps the MFA flow working while removing that trap.

    A cancelled MFA prompt counts as unconfirmed and reverts, even though reaching the MFA step
    proves the password was right. Reverting to the previous known state is the conservative
    choice, and Save still persists unconditionally."""
    if confirmed:
        return
    if previous_password is None:
        delete_secret(WEB_API_PASSWORD)
    else:
        set_secret(WEB_API_PASSWORD, previous_password)


def _start_with_windows_state(config: AppConfig) -> bool:
    """What the "Start with Windows" checkbox should show: the registry's answer, falling back to
    the saved config only if the registry cannot be read at all (a non-Windows host, where the
    module's `winreg` import fails outright). is_enabled() already absorbs its own OSErrors."""
    try:
        from fireshare_agent import startup

        return startup.is_enabled()
    except Exception:
        return config.start_with_windows


def _split_extensions(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [p if p.startswith(".") else f".{p}" for p in parts]


def _safe_int(raw: str, default: int) -> int:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return default
