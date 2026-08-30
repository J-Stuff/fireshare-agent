"""AppConfig: the full user-editable configuration, persisted as JSON (secrets excluded)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from fireshare_agent.models import PostUploadAction


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
        config.max_retry_attempts = data.get("max_retry_attempts", config.max_retry_attempts)
        config.retry_backoff_seconds = data.get("retry_backoff_seconds", config.retry_backoff_seconds)
        config.start_with_windows = data.get("start_with_windows", config.start_with_windows)
        config.show_upload_notifications = data.get("show_upload_notifications", config.show_upload_notifications)
        return config
