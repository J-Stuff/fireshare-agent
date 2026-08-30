"""Resolves paths to bundled files under img/ - works both running from source and from a
PyInstaller build (onedir or onefile), where bundled data lands under sys._MEIPASS instead of
next to the source tree."""
from __future__ import annotations

import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def asset_path(*parts: str) -> Path:
    return _base_dir().joinpath("img", *parts)


def apply_window_icon(window) -> None:
    """Sets the app icon on a Tk/CTk window (titlebar, Alt-Tab, taskbar button). Toplevels
    normally inherit their master's icon automatically in plain Tkinter, but CustomTkinter's
    dark-titlebar handling (DWM calls it makes shortly after a window is created) can reset an
    icon set immediately at construction time - and doesn't reliably pass it down to Toplevels
    either - so this is called on every window rather than relied on to propagate, and applied
    twice: once now, once again shortly after to win that race."""
    icon_path = str(asset_path("icon.ico"))

    def _apply() -> None:
        try:
            window.iconbitmap(icon_path)
        except Exception:
            pass  # a missing/bad icon file is cosmetic, never worth raising over

    _apply()
    window.after(250, _apply)
