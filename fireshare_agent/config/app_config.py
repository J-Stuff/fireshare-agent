"""AppConfig: the full user-editable configuration, persisted as JSON (secrets excluded)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from fireshare_agent.models import PostUploadAction

# Bounds for the numeric settings, kept here rather than in the settings window so the UI and the
# config loader clamp against the same numbers. Both matter: config.json is a documented,
# hand-editable file (the README points users at it), so the UI is not the only way a value gets in.
#
# Chunk size: below ~1MB the per-request overhead dominates and a large clip becomes an absurd
# number of POSTs; above ~256MB the peak memory matters, since _upload_video_chunked reads a whole
# chunk into memory before sending it. The Cloudflare-related advice (stay under 100MB) is guidance
# in the UI rather than a hard cap - a LAN instance with no proxy in front of it is free to use more.
CHUNK_SIZE_MB_MIN = 1
CHUNK_SIZE_MB_MAX = 256

# Backoff: the pipeline doubles this after each failed attempt and caps the effective delay at 30
# minutes, so anything past an hour here is already meaningless. The floor exists to stop a retry
# storm against a server that is briefly down.
RETRY_BACKOFF_MIN_SECONDS = 5
RETRY_BACKOFF_MAX_SECONDS = 3600

# Retries: 0 would mean "never retry", which reads as a way to disable retrying but actually just
# fails every transient blip permanently. The upper bound catches a typo'd extra digit.
MAX_RETRY_ATTEMPTS_MIN = 1
MAX_RETRY_ATTEMPTS_MAX = 50

# Upload speed limit, in KB/s (1024-based, matching ui.formatting.format_bytes and therefore the
# rate the main window shows during a transfer).
#
# Unlike the settings above, 0 is meaningful here rather than a mistake - it is the "no limit"
# state, and it has to be, because that is the default and there is no other natural way to spell
# it in a numeric field. So the allowed values are 0, or MIN..MAX; anything between 1 and MIN-1 is
# a typo or a misunderstanding of the unit rather than an intent. The floor matters because the
# limiter paces *between* chunk requests: at a few hundred bytes a second, a 50MB chunk implies a
# sleep of over a day, and an agent that looks permanently wedged is a worse outcome than a
# rejected value. The ceiling is far past any consumer uplink and only exists to catch a stray
# digit.
UPLOAD_SPEED_LIMIT_UNLIMITED = 0
UPLOAD_SPEED_LIMIT_KBPS_MIN = 32
UPLOAD_SPEED_LIMIT_KBPS_MAX = 1024 * 1024  # 1 GB/s


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def clamp_speed_limit(value: int) -> int:
    """The upload speed limit's own clamp, because its valid set has a hole in it: 0 (unlimited),
    or MIN..MAX. Anything at or below 0 - including a negative from a hand-edited config - reads
    as "no limit", which is the safe interpretation: it leaves uploads working normally rather
    than silently throttling them to a crawl."""
    if value <= 0:
        return UPLOAD_SPEED_LIMIT_UNLIMITED
    return clamp(value, UPLOAD_SPEED_LIMIT_KBPS_MIN, UPLOAD_SPEED_LIMIT_KBPS_MAX)


def _as_int(raw: Any, default: int) -> int:
    """One value from a persisted config as an int. A missing key, or a value that isn't a number
    at all (config.json is hand-editable, so `"50"` or `null` are both realistic), falls back to
    the default rather than propagating a bad type into the pipeline."""
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _bounded(raw: Any, default: int, minimum: int, maximum: int) -> int:
    """Coerces one value from a persisted config into the allowed range."""
    return clamp(_as_int(raw, default), minimum, maximum)


@dataclass
class WatchFolderConfig:
    path: str = ""
    recursive: bool = True
    watch_videos: bool = True
    watch_images: bool = True


@dataclass
class WebApiSettings:
    base_url: str = ""
    username: str = ""
    ignore_certificate_errors: bool = False
    target_folder: str = ""
    chunk_size_bytes: int = 50 * 1024 * 1024
    # Ceiling on how fast this agent uploads, in KB/s; 0 means no limit. Lives here rather than on
    # AppConfig because it is a property of the transport, which means update_config()'s existing
    # "drop the cached uploader" step already applies a change to it without further plumbing.
    upload_speed_limit_kbps: int = UPLOAD_SPEED_LIMIT_UNLIMITED
    # When true, a file's Fireshare folder is taken from its local subfolder name relative to
    # whichever watch folder contains it (e.g. ".../captures/HELLDIVERS 2/clip.mp4" -> folder
    # "HELLDIVERS 2"), so ShadowPlay's per-game folder layout carries over instead of everything
    # landing in one flat target_folder. target_folder is still used as the fallback for files
    # that sit directly in a watch folder's root with no subfolder to mirror.
    mirror_local_folder_structure: bool = True
    # password lives in Windows Credential Manager


@dataclass
class AppConfig:
    watch_folders: list[WatchFolderConfig] = field(default_factory=list)
    video_extensions: list[str] = field(default_factory=lambda: [".mp4"])
    image_extensions: list[str] = field(default_factory=lambda: [".png", ".jpg", ".jpeg"])

    post_upload_action: str = PostUploadAction.LEAVE.value
    move_to_subfolder_name: str = "Uploaded"

    web_api: WebApiSettings = field(default_factory=WebApiSettings)

    max_retry_attempts: int = 5
    retry_backoff_seconds: int = 30

    start_with_windows: bool = False
    show_upload_notifications: bool = True
    auto_check_for_updates: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AppConfig":
        config = AppConfig()
        config.watch_folders = [WatchFolderConfig(**wf) for wf in data.get("watch_folders", [])]
        config.video_extensions = data.get("video_extensions", config.video_extensions)
        config.image_extensions = data.get("image_extensions", config.image_extensions)
        config.post_upload_action = data.get("post_upload_action", config.post_upload_action)
        config.move_to_subfolder_name = data.get("move_to_subfolder_name", config.move_to_subfolder_name)
        if "web_api" in data:
            config.web_api = WebApiSettings(**data["web_api"])
        config.max_retry_attempts = _bounded(
            data.get("max_retry_attempts"), config.max_retry_attempts,
            MAX_RETRY_ATTEMPTS_MIN, MAX_RETRY_ATTEMPTS_MAX,
        )
        config.retry_backoff_seconds = _bounded(
            data.get("retry_backoff_seconds"), config.retry_backoff_seconds,
            RETRY_BACKOFF_MIN_SECONDS, RETRY_BACKOFF_MAX_SECONDS,
        )
        config.web_api.chunk_size_bytes = _bounded(
            config.web_api.chunk_size_bytes, WebApiSettings.chunk_size_bytes,
            CHUNK_SIZE_MB_MIN * 1024 * 1024, CHUNK_SIZE_MB_MAX * 1024 * 1024,
        )
        config.web_api.upload_speed_limit_kbps = clamp_speed_limit(
            _as_int(config.web_api.upload_speed_limit_kbps, WebApiSettings.upload_speed_limit_kbps)
        )
        config.start_with_windows = data.get("start_with_windows", config.start_with_windows)
        config.show_upload_notifications = data.get("show_upload_notifications", config.show_upload_notifications)
        config.auto_check_for_updates = data.get("auto_check_for_updates", config.auto_check_for_updates)
        return config
