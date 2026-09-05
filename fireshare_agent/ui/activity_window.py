"""
Recent upload activity, backed by the dedupe manifest, plus the review queue for files the
agent matched to a server-side file by name alone.

That match can't be verified exactly (Fireshare exposes neither size nor a content hash for
existing entries), so the pipeline refuses to move or delete the local copy on its own and
parks the file here instead - the user decides, per file, what happens to it.
"""
from __future__ import annotations

import tkinter.messagebox as messagebox

import customtkinter as ctk

from fireshare_agent import assets
from fireshare_agent.manifest.store import (
    STATUS_ALREADY_EXISTED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    ManifestEntry,
    ManifestStore,
)
from fireshare_agent.models import PostUploadAction
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline
from fireshare_agent.ui import widgets

_STATUS_DISPLAY = {
    STATUS_SUCCESS: "UPLOADED",
    STATUS_ALREADY_EXISTED: "ALREADY ON SERVER",
    STATUS_FAILED: "FAILED",
}


class ActivityWindow(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk, manifest: ManifestStore, pipeline: UploadPipeline | None = None) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self._pipeline = pipeline

        self.title("Fireshare Agent - Recent Activity")
        self.geometry("820x560")
        self.minsize(700, 420)
        self.configure(fg_color=widgets.WINDOW_BG)
        assets.apply_window_icon(self)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(toolbar, text="Recent Activity", font=widgets.heading_font(16)).pack(side="left")
        ctk.CTkButton(toolbar, text="Refresh", width=90, command=self.refresh).pack(side="right")

        # Sized to hold a few rows without crowding out the history below; it scrolls once the
        # queue is longer than that, and is hidden entirely when nothing needs reviewing.
        self._review_card = ctk.CTkFrame(self, corner_radius=14, fg_color=widgets.CARD_BG)
        self._review_heading = ctk.CTkLabel(
            self._review_card, text="", font=widgets.heading_font(), anchor="w", justify="left",
        )
        self._review_heading.pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            self._review_card,
            text="These were matched to a file already on Fireshare by name only, so nothing was "
                 "moved or deleted. Choose what to do with each local copy.",
            font=widgets.caption_font(), text_color=("gray40", "gray65"),
            anchor="w", justify="left", wraplength=740,
        ).pack(fill="x", padx=16, pady=(0, 8))
        self._review_list = ctk.CTkScrollableFrame(self._review_card, fg_color="transparent", height=150)
        self._review_list.pack(fill="both", expand=True, padx=10, pady=(0, 12))

        self._review_status = ctk.CTkLabel(self, text="", font=widgets.caption_font(), anchor="w")

        self._textbox = ctk.CTkTextbox(self, wrap="none", font=("Consolas", 12))
        self._textbox.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.refresh()

    # ------------------------------------------------------------------ review queue

    def _refresh_review_queue(self) -> None:
        for widget in self._review_list.winfo_children():
            widget.destroy()

        pending = self._manifest.get_pending_review() if self._pipeline is not None else []
        if not pending:
            self._review_card.pack_forget()
            self._review_status.pack_forget()
            return

        self._review_heading.configure(text=f"Needs review ({len(pending)})")
        # before=self._textbox so the queue always sits above the history, no matter which of
        # them was packed first across refreshes.
        self._review_card.pack(fill="x", padx=12, pady=(4, 0), before=self._textbox)

        for entry in pending:
            self._build_review_row(entry)

    def _build_review_row(self, entry: ManifestEntry) -> None:
        row = ctk.CTkFrame(self._review_list, corner_radius=8, fg_color=("gray93", "gray26"))
        row.pack(fill="x", pady=3, padx=2)
        row.grid_columnconfigure(0, weight=1)

        label = ctk.CTkLabel(
            row, text=entry.path, anchor="w", justify="left", font=widgets.body_font(),
        )
        label.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=10)

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
        if self._pipeline is None:
            return

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
        widgets.set_status(self._review_status, "success" if outcome.resolved else "error", outcome.message)
        self._review_status.pack(fill="x", padx=16, pady=(6, 0), before=self._textbox)
        self.refresh()

    # ---------------------------------------------------------------------- history

    def refresh(self) -> None:
        self._refresh_review_queue()

        entries = self._manifest.get_recent(200)
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")

        if not entries:
            self._textbox.insert("end", "No activity recorded yet.")
        else:
            for entry in entries:
                timestamp = entry.updated_at_utc.strftime("%Y-%m-%d %H:%M:%S")
                status_display = _STATUS_DISPLAY.get(entry.status, entry.status.upper())
                if entry.pending_review:
                    status_display = "NEEDS REVIEW"
                line = f"[{timestamp}] {status_display:<18} {entry.path}"
                if entry.error:
                    line += f"\n    error: {entry.error}"
                self._textbox.insert("end", line + "\n")

        self._textbox.configure(state="disabled")
