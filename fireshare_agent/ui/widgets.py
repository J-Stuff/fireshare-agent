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

# A window is built from layered surfaces, each a shade apart so the layering itself reads as
# structure instead of a wash of near-identical gray: the window is the darkest/dimmest surface,
# the sidebar panel sits a step above it, and cards sit a step above that so they read as
# distinct, elevated surfaces rather than blending into the page behind them.
WINDOW_BG = ("gray95", "gray13")
SIDEBAR_BG = ("gray89", "gray17")
CARD_BG = ("white", "gray20")

_NAV_FG_DEFAULT = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
_NAV_HOVER_DEFAULT = ctk.ThemeManager.theme["CTkButton"]["hover_color"]
_NAV_TEXT_UNSELECTED = ("gray20", "gray85")
_NAV_HOVER_UNSELECTED = ("gray80", "gray26")


def heading_font(size: int = 15) -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight="bold")


def body_font(size: int = 13) -> ctk.CTkFont:
    return ctk.CTkFont(size=size)


def caption_font(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(size=size)


def scrollable_panel(parent) -> ctk.CTkScrollableFrame:
    """A full-size scrollable page for one sidebar section, so a densely populated section never
    gets clipped on a smaller screen or at higher DPI scaling. Caller is responsible for placing
    it (grid/pack) - this only builds it."""
    return ctk.CTkScrollableFrame(parent, fg_color="transparent")


class SidebarNav(ctk.CTkFrame):
    """A vertical, single-select navigation list, styled like the sidebar in a modern settings
    app: an inactive item is a plain label, the active one is a filled pill in the accent color.
    Selecting an item shows its associated page and grid_remove()s the others - all pages must
    already be gridded into the same cell of the shared content area. (CTkScrollableFrame keeps
    re-raising its own internal canvas, so plain tkraise() between two of them doesn't reliably
    change which is on top - grid_remove() sidesteps that by actually detaching the hidden ones.)
    """

    def __init__(self, parent, items: list[tuple[str, str, ctk.CTkBaseClass]], width: int = 184) -> None:
        # items: (icon, label, page)
        super().__init__(parent, width=width, corner_radius=14, fg_color=SIDEBAR_BG)
        self.grid_propagate(False)

        self._buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkBaseClass] = {}
        self._selected: str | None = None

        for i, (icon, label, page) in enumerate(items):
            button = ctk.CTkButton(
                self, text=f"{icon}   {label}", anchor="w", corner_radius=8, height=40,
                font=body_font(), command=lambda l=label: self.select(l),
            )
            button.pack(fill="x", padx=10, pady=(12 if i == 0 else 3, 3))
            self._buttons[label] = button
            self._pages[label] = page

        self.select(items[0][1])

    def select(self, label: str) -> None:
        self._selected = label
        for name, button in self._buttons.items():
            is_selected = name == label
            button.configure(
                fg_color=_NAV_FG_DEFAULT if is_selected else "transparent",
                hover_color=_NAV_HOVER_DEFAULT if is_selected else _NAV_HOVER_UNSELECTED,
                text_color="white" if is_selected else _NAV_TEXT_UNSELECTED,
            )
            page = self._pages[name]
            if is_selected:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()


def section_card(parent, title: str, subtitle: str | None = None) -> ctk.CTkFrame:
    """A titled card sitting one elevation above the page behind it. Returns the inner body
    frame - pack fields into that."""
    card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG)
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
