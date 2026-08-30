"""Small reusable building blocks so every settings tab shares the same look and spacing."""
from __future__ import annotations

import customtkinter as ctk

from fireshare_agent.config.secrets import get_secret

LABEL_WIDTH = 200
_SAVED_PLACEHOLDER = "(unchanged - already saved)"

_COLOR_SUCCESS = ("#1a8754", "#3ddc84")
_COLOR_ERROR = ("#d1453b", "#ff7b72")
_COLOR_MUTED = ("gray40", "gray65")

_STATUS_ICON = {"info": "⏳", "success": "✓", "error": "✕"}
_STATUS_COLOR = {"info": _COLOR_MUTED, "success": _COLOR_SUCCESS, "error": _COLOR_ERROR}


def heading_font(size: int = 15) -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight="bold")


def body_font(size: int = 13) -> ctk.CTkFont:
    return ctk.CTkFont(size=size)


def caption_font(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(size=size)


def scrollable_tab(tab: ctk.CTkFrame) -> ctk.CTkScrollableFrame:
    """Wraps a CTkTabview tab's content in a scrollable frame so a densely populated tab never
    gets clipped on a smaller screen or at higher DPI scaling."""
    container = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    container.pack(fill="both", expand=True)
    return container


def section_card(parent, title: str, subtitle: str | None = None) -> ctk.CTkFrame:
    """A titled, gently-bordered card. Returns the inner body frame - pack fields into that."""
    card = ctk.CTkFrame(parent, corner_radius=12, fg_color=("gray90", "gray17"))
    card.pack(fill="x", padx=2, pady=(0, 14))

    ctk.CTkLabel(card, text=title, font=heading_font(), anchor="w").pack(
        fill="x", padx=18, pady=(16, 2 if subtitle else 12)
    )
    if subtitle:
        ctk.CTkLabel(
            card, text=subtitle, font=caption_font(), text_color=_COLOR_MUTED,
            anchor="w", justify="left", wraplength=560,
        ).pack(fill="x", padx=18, pady=(0, 12))

    body = ctk.CTkFrame(card, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=18, pady=(0, 18))
    return body


def labeled_row(parent, label_text: str, label_width: int = LABEL_WIDTH) -> ctk.CTkFrame:
    """A row with a fixed-width label on the left; pack your field into the returned row."""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=4)
    ctk.CTkLabel(row, text=label_text, width=label_width, anchor="w", font=body_font()).pack(side="left")
    return row


def labeled_entry(parent, label_text: str, initial_value: str = "", label_width: int = LABEL_WIDTH) -> ctk.CTkEntry:
    row = labeled_row(parent, label_text, label_width)
    entry = ctk.CTkEntry(row, font=body_font())
    if initial_value:
        entry.insert(0, initial_value)
    entry.pack(side="left", fill="x", expand=True)
    return entry


def caption(parent, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(parent, text=text, font=caption_font(), text_color=_COLOR_MUTED, anchor="w", justify="left", wraplength=560)


class PasswordField(ctk.CTkFrame):
    """A password entry with a Show/Hide toggle, tagged with which Credential Manager key it
    belongs to so the settings window can persist it without every call site juggling that."""

    def __init__(self, parent, secret_key: str, placeholder: str | None = None) -> None:
        super().__init__(parent, fg_color="transparent")
        self.secret_key = secret_key
        self._visible = False

        self.entry = ctk.CTkEntry(self, show="*", placeholder_text=placeholder, font=body_font())
        self.entry.pack(side="left", fill="x", expand=True)

        self._toggle_button = ctk.CTkButton(
            self, text="Show", width=56, command=self._toggle,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), hover_color=("gray80", "gray30"),
        )
        self._toggle_button.pack(side="left", padx=(6, 0))

    def _toggle(self) -> None:
        self._visible = not self._visible
        self.entry.configure(show="" if self._visible else "*")
        self._toggle_button.configure(text="Hide" if self._visible else "Show")

    def get(self) -> str:
        return self.entry.get()


def labeled_password(parent, label_text: str, secret_key: str, label_width: int = LABEL_WIDTH) -> PasswordField:
    row = labeled_row(parent, label_text, label_width)
    placeholder = _SAVED_PLACEHOLDER if get_secret(secret_key) else None
    field = PasswordField(row, secret_key, placeholder)
    field.pack(side="left", fill="x", expand=True)
    return field


def set_status(label: ctk.CTkLabel, kind: str, message: str) -> None:
    """kind is one of 'info', 'success', 'error'."""
    icon = _STATUS_ICON.get(kind, "")
    label.configure(text=f"{icon}  {message}".strip(), text_color=_STATUS_COLOR.get(kind, _COLOR_MUTED))
