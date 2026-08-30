"""Loads/saves AppConfig as JSON under %AppData%\\FireshareAgent\\config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fireshare_agent.config.app_config import AppConfig


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "FireshareAgent"


def config_file_path() -> Path:
    return app_data_dir() / "config.json"


def load() -> AppConfig:
    path = config_file_path()
    if not path.exists():
        return AppConfig()

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return AppConfig.from_dict(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # A corrupt/unreadable config should not prevent the tray app from starting;
        # fall back to defaults and let the user reconfigure via Settings.
        return AppConfig()


def save(config: AppConfig) -> None:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)

    path = config_file_path()
    temp_path = path.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, indent=2)
    os.replace(temp_path, path)
