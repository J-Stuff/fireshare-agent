"""Read-only view of recent upload activity, backed by the dedupe manifest."""
from __future__ import annotations

import customtkinter as ctk

from fireshare_agent import assets
from fireshare_agent.manifest.store import STATUS_ALREADY_EXISTED, STATUS_FAILED, STATUS_SUCCESS, ManifestStore

_STATUS_DISPLAY = {
    STATUS_SUCCESS: "UPLOADED",
    STATUS_ALREADY_EXISTED: "ALREADY ON SERVER",
    STATUS_FAILED: "FAILED",
}


class ActivityWindow(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk, manifest: ManifestStore) -> None:
        super().__init__(parent)
        self._manifest = manifest

        self.title("Fireshare Agent - Recent Activity")
        self.geometry("720x420")
        assets.apply_window_icon(self)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkButton(toolbar, text="Refresh", width=90, command=self.refresh).pack(side="right")

        self._textbox = ctk.CTkTextbox(self, wrap="none", font=("Consolas", 12))
        self._textbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.refresh()

    def refresh(self) -> None:
        entries = self._manifest.get_recent(200)
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")

        if not entries:
            self._textbox.insert("end", "No activity recorded yet.")
        else:
            for entry in entries:
                timestamp = entry.updated_at_utc.strftime("%Y-%m-%d %H:%M:%S")
                status_display = _STATUS_DISPLAY.get(entry.status, entry.status.upper())
                line = f"[{timestamp}] {status_display:<18} {entry.path}"
                if entry.error:
                    line += f"\n    error: {entry.error}"
                self._textbox.insert("end", line + "\n")

        self._textbox.configure(state="disabled")
