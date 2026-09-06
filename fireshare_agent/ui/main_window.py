"""
The agent's main window: what it is doing right now, what it has done, what needs a decision
from you, and the four controls (Sync Now / Pause / Settings / Exit) that used to live only in
the tray menu.

This replaces the old read-only "View Activity / Log" dialog. That window answered exactly one
question - what happened in the past - which made a tray menu the only place to see or change
anything about the present. Left-clicking the tray icon now opens this instead, and it is the
window the app is driven from; the tray menu is kept as a shortcut, not as the only route.

Three refresh cadences, deliberately different:

  * The status card polls `pipeline.get_status()` once a second. That snapshot is pure in-memory
    state, so a poll costs a lock acquisition and nothing else - and polling (rather than being
    pushed events from the upload thread) means a window opened halfway through a 4 GB upload
    shows the truth immediately instead of waiting for the next chunk to land.
  * The history and the review queue refresh every five seconds, which is what issue #7 asked
    for, but only actually rebuild when `manifest.revision` has moved. Rebuilding unconditionally
    would throw away the user's scroll position and text selection every five seconds - an
    "auto-refreshing" log you cannot read is worse than a stale one.
  * Nothing refreshes at all while the window is hidden. Closing it withdraws rather than
    destroys (so reopening from the tray is instant and keeps your filter and scroll position),
    and the timer is cancelled on the way out so a hidden window costs nothing.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
import tkinter.messagebox as messagebox
import webbrowser
from typing import Callable

import customtkinter as ctk

from fireshare_agent import assets
from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.manifest.store import (
    STATUS_ALREADY_EXISTED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    ManifestEntry,
    ManifestStore,
)
from fireshare_agent.models import PostUploadAction
from fireshare_agent.pipeline.upload_pipeline import ShareLinkOutcome, UploadPipeline
from fireshare_agent.ui import formatting, widgets

_STATUS_POLL_MS = 1000
# Issue #7 asked for "every 5-10 seconds"; the lower end costs nothing given the revision check
# below usually short-circuits it, and 5s is close enough to feel live during a batch upload.
_HISTORY_POLL_MS = 5000
_HISTORY_LIMIT = 200

# One review row is a 28px button plus its padding; the list shows at most three before it starts
# scrolling, which keeps the card from crowding out the activity list underneath it.
_REVIEW_ROW_HEIGHT = 54
_REVIEW_MAX_VISIBLE_ROWS = 3

# Column widths for one history line, in monospace characters: timestamp, status, size. Their
# total is where an error's continuation line is indented to, so it lines up under the path
# rather than floating in the middle of the size column.
_COL_TIMESTAMP = 21   # "YYYY-MM-DD HH:MM:SS" plus two trailing spaces
_COL_STATUS = 13
_COL_SIZE = 11        # right-aligned size plus two trailing spaces
_ERROR_INDENT = _COL_TIMESTAMP + _COL_STATUS + _COL_SIZE

# Only a file that actually reached the server has something to link to. A FAILED row does not,
# and offering a Copy Link button for one would promise a link that cannot exist.
_LINKABLE_STATUSES = frozenset({STATUS_SUCCESS, STATUS_ALREADY_EXISTED})

_STATUS_DISPLAY = {
    STATUS_SUCCESS: "UPLOADED",
    STATUS_ALREADY_EXISTED: "ON SERVER",
    STATUS_FAILED: "FAILED",
}
_REVIEW_DISPLAY = "NEEDS REVIEW"

# Filter options for the history. The keys are what the segmented button shows; the values are
# the manifest statuses (or the review flag) a row must match to survive.
FILTER_ALL = "All"
FILTER_UPLOADED = "Uploaded"
FILTER_REVIEW = "Needs review"
FILTER_FAILED = "Failed"
_FILTERS = (FILTER_ALL, FILTER_UPLOADED, FILTER_REVIEW, FILTER_FAILED)

_TONE_COLORS = {
    formatting.TONE_IDLE: widgets.COLOR_SUCCESS,
    formatting.TONE_BUSY: widgets.COLOR_LINK,
    formatting.TONE_PAUSED: widgets.COLOR_MUTED,
    formatting.TONE_WARNING: widgets.COLOR_WARNING,
}


def entry_matches(entry: ManifestEntry, selected_filter: str, search: str) -> bool:
    """Whether one manifest row survives the history's filter and search box.

    A free function rather than a method so the filtering rules can be tested directly - this is
    the part users will notice being wrong, and it needs no window to exercise. Search matches
    against the whole path, not just the filename: "HELLDIVERS" should find a game's whole
    folder, which is exactly how the mirrored-subfolder layout makes people think about their
    captures."""
    if selected_filter == FILTER_UPLOADED and entry.status != STATUS_SUCCESS:
        return False
    if selected_filter == FILTER_FAILED and entry.status != STATUS_FAILED:
        return False
    if selected_filter == FILTER_REVIEW and not entry.pending_review:
        return False

    needle = search.strip().lower()
    return not needle or needle in entry.path.lower()


def display_status(entry: ManifestEntry) -> str:
    """Pending review outranks the underlying status: the row is stored as "already existed", but
    what the user needs to know is that it is waiting on them."""
    if entry.pending_review:
        return _REVIEW_DISPLAY
    return _STATUS_DISPLAY.get(entry.status, entry.status.upper())


class MainWindow(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        manifest: ManifestStore,
        pipeline: UploadPipeline,
        config_provider: Callable[[], AppConfig],
        on_open_settings: Callable[[], None],
        on_sync_now: Callable[[], None],
        on_toggle_pause: Callable[[], None],
        on_exit: Callable[[], None],
        version: str = "",
        has_update: Callable[[], bool] = lambda: False,
        update_version: Callable[[], str] = lambda: "",
        on_update_now: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self._pipeline = pipeline
        self._config_provider = config_provider
        self._on_open_settings = on_open_settings
        self._on_sync_now = on_sync_now
        self._on_toggle_pause = on_toggle_pause
        self._on_exit = on_exit
        self._has_update = has_update
        self._update_version = update_version
        self._on_update_now = on_update_now

        # The history row the link buttons act on, held by fingerprint rather than by line
        # number so a selection survives the list being re-rendered underneath it (an
        # auto-refresh, a filter change, a new upload arriving at the top).
        self._selected_fingerprint: str | None = None
        self._rows: list[tuple[int, int, ManifestEntry]] = []   # (first line, last line, entry)
        self._link_lookup_running = False

        self._tick_job: str | None = None
        self._ticks_since_history = 0
        # None (not 0) so the first render always happens - revision 0 is a real value, meaning
        # "an untouched manifest", and a window opening onto one still has to draw the empty state.
        self._rendered_revision: int | None = None

        self.title("Fireshare Agent")
        self.geometry("940x720")
        self.minsize(760, 560)
        self.configure(fg_color=widgets.WINDOW_BG)
        assets.apply_window_icon(self)
        # Closing hides; only the Exit button (or the tray's Exit) stops the agent. A background
        # uploader that quits because you closed its window would be a trap.
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_header(version)
        self._build_update_banner()
        self._build_status_card()
        self._build_review_card()
        self._build_footer()   # packed to the bottom before the history claims the middle
        self._build_history()

        self.refresh()
        self._schedule_tick()

    # -------------------------------------------------------------------- layout

    def _build_header(self, version: str) -> None:
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=20, pady=(18, 10))
        header = self._header

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(left, text="Fireshare Agent", font=widgets.heading_font(20), anchor="w").pack(anchor="w")
        self._subtitle = ctk.CTkLabel(
            left, text="", font=widgets.caption_font(), text_color=widgets.COLOR_MUTED, anchor="w",
        )
        self._subtitle.pack(anchor="w", pady=(2, 0))

        if version:
            ctk.CTkLabel(
                header, text=f"v{version}", font=widgets.caption_font(),
                text_color=widgets.COLOR_MUTED,
            ).pack(side="right", anchor="n")

    def _build_update_banner(self) -> None:
        """Shown only when an update is waiting. Packed and unpacked rather than built lazily so
        it always lands in the same place in the stacking order."""
        self._update_banner = ctk.CTkFrame(self, corner_radius=12, fg_color=("#dbeafe", "#1e3a5f"))
        self._update_label = ctk.CTkLabel(
            self._update_banner, text="", font=widgets.body_font(), anchor="w",
        )
        self._update_label.pack(side="left", padx=(16, 8), pady=12)
        ctk.CTkButton(
            self._update_banner, text="Update Now", width=110, command=self._on_update_now,
        ).pack(side="right", padx=(8, 12), pady=8)

    def _build_status_card(self) -> None:
        card = self._status_card = ctk.CTkFrame(self, corner_radius=14, fg_color=widgets.CARD_BG)
        card.pack(fill="x", padx=20, pady=(0, 12))

        self._status_headline = ctk.CTkLabel(
            card, text="", font=widgets.heading_font(16), anchor="w", justify="left",
        )
        self._status_headline.pack(fill="x", padx=18, pady=(16, 2))

        self._status_detail = ctk.CTkLabel(
            card, text="", font=widgets.caption_font(), text_color=widgets.COLOR_MUTED,
            anchor="w", justify="left",
        )
        self._status_detail.pack(fill="x", padx=18, pady=(0, 10))

        # Created once and packed/unpacked as transfers come and go. Rebuilding it per upload
        # would make the card visibly jump as widgets are destroyed and recreated.
        self._progress = ctk.CTkProgressBar(card, height=10, corner_radius=5)
        self._progress.set(0)

        self._stats_row = ctk.CTkFrame(card, fg_color="transparent")
        self._stats_row.pack(fill="x", padx=18, pady=(0, 16))
        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        for key, caption in (
            ("uploaded", "Uploaded"),
            ("already", "Already on server"),
            ("failed", "Failed"),
            ("review", "Needs review"),
            ("bytes", "Data uploaded"),
        ):
            cell = ctk.CTkFrame(self._stats_row, fg_color="transparent")
            cell.pack(side="left", padx=(0, 26))
            value = ctk.CTkLabel(cell, text="-", font=widgets.heading_font(15), anchor="w")
            value.pack(anchor="w")
            ctk.CTkLabel(
                cell, text=caption, font=widgets.caption_font(11),
                text_color=widgets.COLOR_MUTED, anchor="w",
            ).pack(anchor="w")
            self._stat_labels[key] = value

    def _build_review_card(self) -> None:
        """The manual-sync conflicts: files matched to something already on Fireshare by filename
        alone. The agent refuses to move or delete a local copy on that evidence, so each one
        waits here for a per-file decision."""
        self._review_card = ctk.CTkFrame(self, corner_radius=14, fg_color=widgets.CARD_BG)

        self._review_heading = ctk.CTkLabel(
            self._review_card, text="", font=widgets.heading_font(), anchor="w", justify="left",
        )
        self._review_heading.pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            self._review_card,
            text="These were matched to a file already on Fireshare by name only, so nothing was "
                 "moved or deleted. Choose what to do with each local copy.",
            font=widgets.caption_font(), text_color=widgets.COLOR_MUTED,
            anchor="w", justify="left", wraplength=820,
        ).pack(fill="x", padx=16, pady=(0, 8))

        # A fixed-height holder with propagation switched off, rather than a height on the
        # scrollable frame itself. A CTkScrollableFrame reports whatever height its content wants
        # and configuring its height does not override that, so left to itself the card grew with
        # the queue and pushed the activity list off the bottom of the window. pack_propagate(False)
        # makes the holder's own height authoritative; _render_review_queue sets it from the row
        # count, and the scroll region inside takes over past that.
        self._review_holder = ctk.CTkFrame(
            self._review_card, fg_color="transparent", height=_REVIEW_ROW_HEIGHT,
        )
        self._review_holder.pack(fill="x", padx=10, pady=(0, 8))
        self._review_holder.pack_propagate(False)

        self._review_list = ctk.CTkScrollableFrame(self._review_holder, fg_color="transparent")
        self._review_list.pack(fill="both", expand=True)

        self._review_status = ctk.CTkLabel(
            self._review_card, text="", font=widgets.caption_font(), anchor="w", justify="left",
        )

    def _build_history(self) -> None:
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=20, pady=(4, 6))

        ctk.CTkLabel(toolbar, text="Activity", font=widgets.heading_font(16)).pack(side="left")

        self._filter_var = ctk.StringVar(value=FILTER_ALL)
        ctk.CTkSegmentedButton(
            toolbar, values=list(_FILTERS), variable=self._filter_var,
            command=lambda _value: self._render_history(force=True),
            font=widgets.caption_font(),
        ).pack(side="left", padx=(16, 0))

        self._search = ctk.CTkEntry(
            toolbar, placeholder_text="Filter by name or folder...", width=240, font=widgets.body_font(),
        )
        self._search.pack(side="right")
        # KeyRelease rather than a variable trace: a trace fires on programmatic changes too, and
        # 200 rows re-render fast enough that debouncing would add latency for no benefit.
        self._search.bind("<KeyRelease>", lambda _e: self._render_history(force=True))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 6))

        self._copy_link_button = ctk.CTkButton(
            actions, text="Copy Link", width=110, height=28, font=widgets.caption_font(),
            command=self._copy_link, state="disabled",
        )
        self._copy_link_button.pack(side="left")
        self._open_link_button = ctk.CTkButton(
            actions, text="Open in Fireshare", width=150, height=28, font=widgets.caption_font(),
            command=self._open_link, state="disabled",
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), hover_color=("gray85", "gray28"),
        )
        self._open_link_button.pack(side="left", padx=8)

        # Always visible rather than appearing on selection: it doubles as the instruction for an
        # interaction (click a row) that has no other affordance, and a row that appears and
        # disappears would make the list below it jump.
        self._link_status = ctk.CTkLabel(
            actions, text="", font=widgets.caption_font(), text_color=widgets.COLOR_MUTED,
            anchor="w", justify="left",
        )
        self._link_status.pack(side="left", padx=(10, 0), fill="x", expand=True)

        self._history = ctk.CTkTextbox(self, wrap="none", font=("Consolas", 12), activate_scrollbars=True)
        self._history.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self._history.configure(state="disabled")

        self._history.bind("<Button-1>", self._on_history_click)
        self._history.bind("<Double-Button-1>", self._on_history_double_click)
        self._history.bind("<Button-3>", self._on_history_right_click)

        # A plain tk.Menu: CustomTkinter has no popup-menu widget, and this is the one place a
        # native context menu is what people expect from a log view.
        self._context_menu = tk.Menu(self, tearoff=0)
        self._context_menu.add_command(label="Copy Fireshare Link", command=self._copy_link)
        self._context_menu.add_command(label="Open in Fireshare", command=self._open_link)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Copy File Path", command=self._copy_file_path)

    def _build_footer(self) -> None:
        divider = ctk.CTkFrame(self, height=1, fg_color=("gray85", "gray22"))
        divider.pack(side="bottom", fill="x", padx=20)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=14)

        ctk.CTkButton(footer, text="Sync Now", width=120, command=self._sync_now).pack(side="left")
        self._pause_button = ctk.CTkButton(
            footer, text="Pause", width=120, command=self._toggle_pause,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), hover_color=("gray85", "gray28"),
        )
        self._pause_button.pack(side="left", padx=8)
        ctk.CTkButton(
            footer, text="Settings", width=120, command=self._open_settings,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), hover_color=("gray85", "gray28"),
        ).pack(side="left")

        # Far right, visually separated from the three routine controls, and styled as the one
        # destructive action on the window - the same reasoning that puts Delete last in a review
        # row. It also confirms, which the tray's Exit does not: there, Exit is the only thing the
        # menu is for, whereas here it sits a few pixels from buttons you press all the time.
        ctk.CTkButton(
            footer, text="Exit Agent", width=120, command=self._exit,
            fg_color="transparent", border_width=1,
            text_color=widgets.COLOR_ERROR, hover_color=("#f4c7c7", "#4a1f22"),
        ).pack(side="right")

    # -------------------------------------------------------------------- visibility

    def show(self) -> None:
        """Bring the window back from hidden, refresh it, and restart polling."""
        self.deiconify()
        self.lift()
        self.focus_force()
        self.refresh()
        self._schedule_tick()

    def hide(self) -> None:
        self._cancel_tick()
        self.withdraw()

    def destroy(self) -> None:
        self._cancel_tick()
        super().destroy()

    def _schedule_tick(self) -> None:
        if self._tick_job is None:
            self._tick_job = self.after(_STATUS_POLL_MS, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass  # already fired or the interpreter is going away; nothing to cancel
            self._tick_job = None

    def _tick(self) -> None:
        self._tick_job = None
        if not self.winfo_exists():
            return  # the window is gone; there is nothing left to reschedule onto

        # Minimizing also makes a window non-viewable, and unlike closing it never calls hide().
        # So the timer keeps running and only the drawing is skipped - returning without
        # rescheduling here would leave a restored window frozen on whatever it last rendered,
        # with no way to start it ticking again. A covered-but-open window is still viewable and
        # keeps updating, which is what someone watching an upload behind a game expects.
        if self.winfo_viewable():
            self._render_status()
            self._ticks_since_history += 1
            if self._ticks_since_history * _STATUS_POLL_MS >= _HISTORY_POLL_MS:
                self._ticks_since_history = 0
                self._render_manifest_views()

        self._schedule_tick()

    def _render_manifest_views(self, force_history: bool = False) -> None:
        """Everything backed by a database read, refreshed together on the slow cadence."""
        self._render_stats()
        self._render_review_queue()
        self._render_history(force=force_history)

    # -------------------------------------------------------------------- rendering

    def refresh(self) -> None:
        """A full, unconditional redraw. Called on open and after anything the user did that
        changed state, where waiting up to five seconds for the timer would feel broken."""
        self._render_status()
        self._render_manifest_views(force_history=True)

    def _render_status(self) -> None:
        status = self._pipeline.get_status()
        summary = formatting.summarize(status)

        self._status_headline.configure(
            text=summary.headline, text_color=_TONE_COLORS.get(summary.tone, widgets.COLOR_MUTED),
        )
        self._status_detail.configure(text=summary.detail)

        if summary.fraction is None:
            self._progress.pack_forget()
        else:
            self._progress.set(summary.fraction)
            # before=self._stats_row so the bar always sits between the detail line and the
            # totals, regardless of what was packed when.
            self._progress.pack(fill="x", padx=18, pady=(0, 12), before=self._stats_row)

        self._pause_button.configure(text="Resume" if status.paused else "Pause")
        self._subtitle.configure(text=self._describe_configuration())
        self._render_update_banner()

    def _render_update_banner(self) -> None:
        if self._has_update():
            self._update_label.configure(
                text=f"Fireshare Agent {self._update_version()} is available.",
            )
            if not self._update_banner.winfo_ismapped():
                self._update_banner.pack(fill="x", padx=20, pady=(0, 12), after=self._header)
        elif self._update_banner.winfo_ismapped():
            self._update_banner.pack_forget()

    def _describe_configuration(self) -> str:
        config = self._config_provider()
        server = config.web_api.base_url.strip()
        folders = [f for f in config.watch_folders if f.path]
        if not server or not folders:
            return "Not set up yet - open Settings to choose a watch folder and your Fireshare server."
        folder_text = "1 watch folder" if len(folders) == 1 else f"{len(folders)} watch folders"
        return f"{folder_text} -> {server}"

    def _render_history(self, force: bool = False) -> None:
        """Redraws the activity list, skipping the work entirely when nothing has changed.

        The revision check is what makes a five-second auto-refresh usable: without it every tick
        would clear and refill the widget, dropping the user's text selection and yanking their
        scroll position back. `force` is for the cases where the view must change even though the
        data did not - a different filter, or a new search string."""
        revision = self._manifest.revision
        if not force and revision == self._rendered_revision:
            return
        self._rendered_revision = revision

        selected_filter = self._filter_var.get()
        search = self._search.get()
        entries = [
            entry for entry in self._manifest.get_recent(_HISTORY_LIMIT)
            if entry_matches(entry, selected_filter, search)
        ]

        top_fraction = self._history.yview()[0]
        self._history.configure(state="normal")
        self._history.delete("1.0", "end")
        self._configure_history_tags()
        self._rows = []

        if not entries:
            self._history.insert("end", self._empty_history_message(selected_filter, search), "muted")
        else:
            for entry in entries:
                self._insert_history_row(entry)

        self._history.configure(state="disabled")
        # The selected row may have been filtered out or scrolled off the retained window; drop
        # the selection in that case rather than leaving the buttons pointed at nothing.
        if self._selected_fingerprint and not any(e.fingerprint == self._selected_fingerprint for _, _, e in self._rows):
            self._selected_fingerprint = None
        self._apply_selection_highlight()
        # Restored rather than reset to the top: an auto-refresh that scrolls you back to the
        # newest row every five seconds makes reading anything older impossible.
        self._history.yview_moveto(top_fraction)

    def _empty_history_message(self, selected_filter: str, search: str) -> str:
        if search.strip():
            return f"Nothing matching “{search.strip()}”."
        if selected_filter == FILTER_FAILED:
            return "No failed uploads."
        if selected_filter == FILTER_REVIEW:
            return "Nothing waiting for review."
        if selected_filter == FILTER_UPLOADED:
            return "Nothing uploaded yet."
        return "No activity recorded yet. Drop a clip into a watch folder, or press Sync Now."

    def _configure_history_tags(self) -> None:
        """Re-applied on every render because the colours depend on the current appearance mode,
        which the user can change (or which can follow the OS) while the window is open."""
        self._history.tag_config("muted", foreground=widgets.resolve_color(widgets.COLOR_MUTED))
        self._history.tag_config("success", foreground=widgets.resolve_color(widgets.COLOR_SUCCESS))
        self._history.tag_config("error", foreground=widgets.resolve_color(widgets.COLOR_ERROR))
        self._history.tag_config("warning", foreground=widgets.resolve_color(widgets.COLOR_WARNING))
        self._history.tag_config("link", foreground=widgets.resolve_color(widgets.COLOR_LINK))
        self._history.tag_config("selected", background=widgets.resolve_color(("#d6e4ff", "#2b3d59")))

    def _insert_history_row(self, entry: ManifestEntry) -> None:
        first_line = int(self._history.index("end-1c").split(".")[0])
        status_text = display_status(entry)
        tag = {
            _REVIEW_DISPLAY: "warning",
            "UPLOADED": "success",
            "FAILED": "error",
            "ON SERVER": "link",
        }.get(status_text, "muted")

        self._history.insert("end", f"{formatting.format_timestamp(entry.updated_at_utc)}  ", "muted")
        self._history.insert("end", f"{status_text:<{_COL_STATUS}}", tag)
        self._history.insert("end", f"{formatting.format_bytes(entry.size_bytes):>{_COL_SIZE - 2}}  ", "muted")
        self._history.insert("end", f"{entry.path}\n")
        if entry.error:
            self._history.insert("end", f"{'':<{_ERROR_INDENT}}{entry.error}\n", "error")

        # end-1c is now the start of the *next* row, so the last line of this one is one back.
        last_line = int(self._history.index("end-1c").split(".")[0]) - 1
        self._rows.append((first_line, last_line, entry))

    # -------------------------------------------------------------------- selection & links

    def _on_history_click(self, event) -> None:
        """Selects the row under the pointer. Returns nothing so the Text widget still handles
        the click normally - the history stays selectable and copyable as text."""
        self._select_row_at(event)

    def _on_history_double_click(self, event) -> None:
        self._select_row_at(event)
        if self._selected_entry_is_linkable():
            self._copy_link()

    def _on_history_right_click(self, event) -> None:
        self._select_row_at(event)
        entry = self._selected_entry()
        linkable = "normal" if self._selected_entry_is_linkable() else "disabled"
        self._context_menu.entryconfigure("Copy Fireshare Link", state=linkable)
        self._context_menu.entryconfigure("Open in Fireshare", state=linkable)
        self._context_menu.entryconfigure("Copy File Path", state="normal" if entry else "disabled")
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            # Without this the menu can keep the pointer grabbed if it is dismissed by clicking
            # elsewhere, leaving the rest of the window unresponsive to the mouse.
            self._context_menu.grab_release()

    def _select_row_at(self, event) -> None:
        line = int(self._history.index(f"@{event.x},{event.y}").split(".")[0])
        match = next((entry for first, last, entry in self._rows if first <= line <= last), None)
        self._selected_fingerprint = match.fingerprint if match else None
        self._apply_selection_highlight()

    def _selected_entry(self) -> ManifestEntry | None:
        if self._selected_fingerprint is None:
            return None
        return next(
            (entry for _, _, entry in self._rows if entry.fingerprint == self._selected_fingerprint),
            None,
        )

    def _selected_entry_is_linkable(self) -> bool:
        entry = self._selected_entry()
        return entry is not None and entry.status in _LINKABLE_STATUSES

    def _apply_selection_highlight(self) -> None:
        self._history.tag_remove("selected", "1.0", "end")
        entry = self._selected_entry()

        if entry is not None:
            span = next(((f, l) for f, l, e in self._rows if e.fingerprint == entry.fingerprint), None)
            if span is not None:
                self._history.tag_add("selected", f"{span[0]}.0", f"{span[1]}.end")

        linkable = self._selected_entry_is_linkable()
        state = "normal" if linkable and not self._link_lookup_running else "disabled"
        self._copy_link_button.configure(state=state)
        self._open_link_button.configure(state=state)

        if entry is None:
            self._set_link_status("Select a row to copy its Fireshare link.", "muted")
        elif not linkable:
            self._set_link_status(f"{os.path.basename(entry.path)} isn't on Fireshare, so it has no link.", "muted")
        elif entry.share_url:
            self._set_link_status(entry.share_url, "muted")
        else:
            self._set_link_status(f"{os.path.basename(entry.path)} selected.", "muted")

    def _set_link_status(self, message: str, tone: str) -> None:
        colors = {
            "muted": widgets.COLOR_MUTED,
            "success": widgets.COLOR_SUCCESS,
            "error": widgets.COLOR_ERROR,
            "warning": widgets.COLOR_WARNING,
        }
        self._link_status.configure(text=message, text_color=colors.get(tone, widgets.COLOR_MUTED))

    def _copy_link(self) -> None:
        self._with_share_url(self._copy_to_clipboard)

    def _open_link(self) -> None:
        self._with_share_url(self._open_in_browser)

    def _with_share_url(self, then) -> None:
        """Resolves the selected row's Fireshare link on a background thread, then hands it to
        `then` back on the UI thread.

        Off-thread because resolving can mean a request that lists every video on the server, and
        this is a button on the window's own event loop - doing it inline would freeze the window
        (progress bar included) for the length of that request. The pipeline's upload worker is
        deliberately not involved either: a queue of clips must not stall behind a button press."""
        entry = self._selected_entry()
        if entry is None or self._link_lookup_running:
            return

        if entry.share_url:
            then(entry, entry.share_url)   # already known - no request, no thread, no delay
            return

        self._link_lookup_running = True
        self._copy_link_button.configure(state="disabled")
        self._open_link_button.configure(state="disabled")
        self._set_link_status(f"Looking up the link for {os.path.basename(entry.path)}...", "muted")

        def worker() -> None:
            outcome = self._pipeline.resolve_share_url(entry)
            # after() is the only thread-safe way back into Tk; the window may also have been
            # destroyed while the request was in flight.
            try:
                if self.winfo_exists():
                    self.after(0, lambda: self._on_share_url_resolved(entry, outcome, then))
            except Exception:
                pass  # the interpreter is going away; there is nothing left to update

        threading.Thread(target=worker, daemon=True, name="fireshare-agent-share-link").start()

    def _on_share_url_resolved(self, entry: ManifestEntry, outcome: ShareLinkOutcome, then) -> None:
        self._link_lookup_running = False
        if outcome.url:
            # Re-read the row so the cached share_url the pipeline just wrote is reflected in the
            # list, rather than leaving a stale entry that would trigger another lookup.
            self._render_history(force=True)
            then(entry, outcome.url)
        else:
            self._apply_selection_highlight()
            # "Not ready yet" is a normal state, not a failure - Fireshare creates the id in a
            # separate process after the upload returns - so it is a warning, not an error.
            self._set_link_status(outcome.message, "warning" if entry.status in _LINKABLE_STATUSES else "error")

    def _copy_to_clipboard(self, entry: ManifestEntry, url: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(url)
        # Tk hands the clipboard over lazily; update() forces it out now, so the link is really
        # on the clipboard even if the window is closed a moment later.
        self.update()
        self._set_link_status(f"Copied  {url}", "success")

    def _open_in_browser(self, entry: ManifestEntry, url: str) -> None:
        webbrowser.open(url)
        self._set_link_status(f"Opened  {url}", "success")

    def _copy_file_path(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self.clipboard_clear()
        self.clipboard_append(entry.path)
        self.update()
        self._set_link_status(f"Copied  {entry.path}", "success")

    # -------------------------------------------------------------------- review queue

    def _render_review_queue(self) -> None:
        for widget in self._review_list.winfo_children():
            widget.destroy()
        self._review_status.pack_forget()

        pending = self._manifest.get_pending_review()
        if not pending:
            self._review_card.pack_forget()
            return

        self._review_heading.configure(text=f"Needs review ({len(pending)})")
        # Grows with the queue up to a few rows, then scrolls. A single file waiting on a decision
        # should not claim a third of the window, and forty of them should not claim all of it.
        self._review_holder.configure(
            height=min(len(pending), _REVIEW_MAX_VISIBLE_ROWS) * _REVIEW_ROW_HEIGHT,
        )
        self._review_card.pack(fill="x", padx=20, pady=(0, 12), after=self._status_card)

        for entry in pending:
            self._build_review_row(entry)

    def _build_review_row(self, entry: ManifestEntry) -> None:
        row = ctk.CTkFrame(self._review_list, corner_radius=8, fg_color=("gray93", "gray26"))
        row.pack(fill="x", pady=3, padx=2)
        row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            row, text=entry.path, anchor="w", justify="left", font=widgets.body_font(),
        ).grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=10)

        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.grid(row=0, column=1, padx=(4, 10))

        # Keep first and Delete last, with Delete visually separated - the destructive option
        # should never be the one your muscle memory lands on.
        self._review_button(buttons, "Keep", entry, PostUploadAction.LEAVE)
        self._review_button(buttons, "Move", entry, PostUploadAction.MOVE_TO_SUBFOLDER)
        self._review_button(buttons, "Delete", entry, PostUploadAction.DELETE, destructive=True)

    def _review_button(
        self, parent, text: str, entry: ManifestEntry, action: PostUploadAction, destructive: bool = False,
    ) -> None:
        ctk.CTkButton(
            parent, text=text, width=76, height=28, font=widgets.caption_font(),
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"),
            hover_color=("#f4c7c7", "#4a1f22") if destructive else ("gray80", "gray30"),
            command=lambda: self._resolve(entry, action),
        ).pack(side="left", padx=3)

    def _resolve(self, entry: ManifestEntry, action: PostUploadAction) -> None:
        # This whole queue exists because the agent refuses to delete a file on an unverified
        # match. Letting a single misclick do it here would hand back exactly the risk the queue
        # was built to remove - and unlike the agent, the user can confirm they know which file
        # this is.
        if action == PostUploadAction.DELETE and not messagebox.askyesno(
            "Delete Local File",
            f"Permanently delete this local file?\n\n{entry.path}\n\n"
            "Fireshare was matched by filename only, so this copy may not actually be on the "
            "server. This cannot be undone.",
            parent=self,
        ):
            return

        outcome = self._pipeline.resolve_pending_review(entry, action)
        self.refresh()
        # Packed after the refresh, which rebuilds the card's children - and only if the card is
        # still on screen, since resolving the last entry hides it and the message would
        # otherwise reappear attached to nothing.
        if self._review_card.winfo_ismapped():
            widgets.set_status(self._review_status, "success" if outcome.resolved else "error", outcome.message)
            self._review_status.pack(fill="x", padx=16, pady=(0, 12))

    # -------------------------------------------------------------------- actions

    def _sync_now(self) -> None:
        self._on_sync_now()
        # The scan runs on its own thread; the next status tick picks up `scanning`. Refreshing
        # here just means the card does not sit on a stale "no files left to upload" for a second
        # after the user pressed a button.
        self.after(150, self._render_status)

    def _toggle_pause(self) -> None:
        self._on_toggle_pause()
        self._render_status()

    def _open_settings(self) -> None:
        self._on_open_settings()

    def _exit(self) -> None:
        status = self._pipeline.get_status()
        warning = ""
        if status.active is not None:
            warning = f"\n\n{os.path.basename(status.active.path)} is still uploading and will be interrupted."
        elif status.pending_count:
            warning = f"\n\n{status.pending_count} file(s) are still queued."

        if messagebox.askyesno(
            "Exit Fireshare Agent",
            "Stop watching for new captures and close the agent completely?" + warning,
            parent=self,
        ):
            self._on_exit()

    # -------------------------------------------------------------------- theming

    def _set_appearance_mode(self, mode_string) -> None:
        """CustomTkinter's hook for a light/dark switch. The history's text tags are raw Tk
        colours that CTk knows nothing about, so they have to be re-resolved by hand - without
        this, switching to dark mode leaves dark-on-dark timestamps."""
        super()._set_appearance_mode(mode_string)
        try:
            self._render_history(force=True)
        except Exception:
            pass  # a redraw during teardown is never worth raising over

    # -------------------------------------------------------------------- stats

    def _render_stats(self) -> None:
        stats = self._manifest.get_stats()
        self._stat_labels["uploaded"].configure(text=str(stats.uploaded))
        self._stat_labels["already"].configure(text=str(stats.already_on_server))
        self._stat_labels["failed"].configure(text=str(stats.failed))
        self._stat_labels["review"].configure(text=str(stats.pending_review))
        self._stat_labels["bytes"].configure(text=formatting.format_bytes(stats.bytes_uploaded))
