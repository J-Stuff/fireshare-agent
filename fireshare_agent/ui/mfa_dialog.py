"""
Modal TOTP-entry prompt used when a Fireshare account has MFA enabled. This is a best-effort,
interactive-only flow (per the plan): an unattended background service can't complete MFA on its
own, so when the server responds mfa_required, the pipeline pauses that upload and asks here.
"""
from __future__ import annotations

import customtkinter as ctk

from fireshare_agent import assets


def ask_for_code(parent: ctk.CTk) -> str | None:
    result: dict[str, str | None] = {"code": None}

    dialog = ctk.CTkToplevel(parent)
    dialog.title("Fireshare - MFA Code Required")
    dialog.geometry("340x170")
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)
    assets.apply_window_icon(dialog)

    ctk.CTkLabel(dialog, text="Your Fireshare account requires a TOTP code.\nEnter the current code from your authenticator app:", justify="left").pack(padx=20, pady=(20, 8))
    entry = ctk.CTkEntry(dialog, width=200, justify="center")
    entry.pack(pady=4)
    entry.focus_set()

    def submit() -> None:
        result["code"] = entry.get().strip() or None
        dialog.destroy()

    def cancel() -> None:
        result["code"] = None
        dialog.destroy()

    button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
    button_frame.pack(pady=14)
    ctk.CTkButton(button_frame, text="Submit", command=submit, width=90).pack(side="left", padx=6)
    ctk.CTkButton(button_frame, text="Cancel", command=cancel, width=90, fg_color="gray40").pack(side="left", padx=6)

    dialog.bind("<Return>", lambda _e: submit())
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    dialog.grab_set()
    dialog.wait_window()
    return result["code"]
