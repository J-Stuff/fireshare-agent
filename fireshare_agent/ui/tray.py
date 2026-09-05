from __future__ import annotations

from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageEnhance

from fireshare_agent import assets

_ICON_SIZE = 256
_base_icon_cache: Image.Image | None = None


def _base_icon() -> Image.Image:
    global _base_icon_cache
    if _base_icon_cache is None:
        path = assets.asset_path("Fireshare-Agent-1024.png")
        _base_icon_cache = Image.open(path).convert("RGBA").resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
    return _base_icon_cache


def _desaturated(image: Image.Image) -> Image.Image:
    # Signals "not actively watching" while keeping the artwork's silhouette recognizable -
    # ImageEnhance.Color on RGBA can be inconsistent about preserving alpha, so it's done
    # explicitly here rather than trusted to the enhancer.
    alpha = image.getchannel("A")
    grayscale_rgb = ImageEnhance.Color(image.convert("RGB")).enhance(0.0)
    result = grayscale_rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def _build_icon_image(paused: bool, has_failures: bool) -> Image.Image:
    image = _desaturated(_base_icon()) if paused else _base_icon().copy()

    if has_failures:
        draw = ImageDraw.Draw(image)
        badge_diameter = _ICON_SIZE * 0.3
        x2, y2 = _ICON_SIZE - 4, _ICON_SIZE - 4
        x1, y1 = x2 - badge_diameter, y2 - badge_diameter
        draw.ellipse((x1, y1, x2, y2), fill=(200, 0, 0, 255), outline=(255, 255, 255, 255), width=6)

    return image


class TrayIcon:
    def __init__(
        self,
        on_open_settings: Callable[[], None],
        on_open_activity: Callable[[], None],
        on_sync_now: Callable[[], None],
        on_toggle_pause: Callable[[], None],
        is_paused: Callable[[], bool],
        has_failures: Callable[[], bool],
        on_exit: Callable[[], None],
        has_update: Callable[[], bool] = lambda: False,
        update_version: Callable[[], str] = lambda: "",
        on_update_now: Callable[[], None] = lambda: None,
        pending_review_count: Callable[[], int] = lambda: 0,
    ) -> None:
        self._is_paused = is_paused
        self._has_failures = has_failures
        self._has_update = has_update
        self._update_version = update_version
        self._pending_review_count = pending_review_count
        self._on_exit = on_exit

        self.icon = pystray.Icon(
            "FireshareAgent",
            icon=_build_icon_image(False, False),
            title="Fireshare Agent",
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Update to {self._update_version()} Now",
                    lambda: on_update_now(),
                    visible=lambda item: self._has_update(),
                ),
                pystray.MenuItem(
                    lambda item: f"Review {self._pending_review_count()} File(s)...",
                    lambda: on_open_activity(),
                    visible=lambda item: self._pending_review_count() > 0,
                ),
                pystray.MenuItem("Open Settings", lambda: on_open_settings()),
                pystray.MenuItem("View Activity / Log", lambda: on_open_activity()),
                pystray.MenuItem("Sync Now", lambda: on_sync_now()),
                pystray.MenuItem(
                    lambda item: "Resume Watching" if self._is_paused() else "Pause Watching",
                    lambda: on_toggle_pause(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda: on_exit()),
            ),
        )

    def refresh(self) -> None:
        self.icon.icon = _build_icon_image(self._is_paused(), self._has_failures())
        self.icon.title = "Fireshare Agent (paused)" if self._is_paused() else "Fireshare Agent"
        self.icon.update_menu()

    def run(self) -> None:
        self.icon.run()

    def stop(self) -> None:
        self.icon.stop()
