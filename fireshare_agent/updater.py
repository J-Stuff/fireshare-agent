"""
Checks GitHub Releases for a newer version and, if the user confirms, downloads the installer
built for that release and runs it silently, handing off control to it - a running Windows exe
can't overwrite its own files directly. The installer (packaging/installer.iss) closes this app,
replaces its files, and relaunches it, whether it's installed per-machine (Program Files, which
needs the installer to self-elevate via UAC) or per-user (AppData, no elevation needed).

Only meaningful for the packaged (frozen) build; check_for_update() is a no-op when running from
source, since there's no installed exe directory to update in place.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from fireshare_agent import __version__
from fireshare_agent.config.store import app_data_dir

log = logging.getLogger(__name__)

_REPO = "J-Stuff/fireshare-agent"
_API_LATEST_RELEASE = f"https://api.github.com/repos/{_REPO}/releases/latest"
_REQUEST_HEADERS = {"Accept": "application/vnd.github+json"}


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    download_url: str
    checksum_url: str | None
    notes_url: str


def parse_version(v: str) -> tuple[int, int, int]:
    """Loose (major, minor, patch) parse - tolerates a leading 'v' and a trailing
    prerelease/build suffix (e.g. "v1.2.3-rc.1" -> (1, 2, 3)) since we only compare stable
    release numbers here."""
    v = v.lstrip("vV").split("-")[0].split("+")[0]
    parts = v.split(".")[:3]
    numbers = []
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)  # type: ignore[return-value]


def check_for_update(timeout: float = 10.0) -> UpdateInfo | None:
    """Returns update info if a newer stable release is available, else None. Never raises - a
    failed check (offline, GitHub down/rate-limited, malformed response) is treated the same as
    "no update available" rather than surfacing an error for what's a background convenience."""
    if not getattr(sys, "frozen", False):
        return None  # nothing to self-update when running from source

    try:
        response = requests.get(_API_LATEST_RELEASE, timeout=timeout, headers=_REQUEST_HEADERS)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None

    tag = data.get("tag_name") or ""
    if not tag or parse_version(tag) <= parse_version(__version__):
        return None

    assets = data.get("assets") or []
    installer_asset = next((a for a in assets if a.get("name", "").lower().endswith(".exe")), None)
    if installer_asset is None:
        return None
    checksum_asset = next((a for a in assets if a.get("name") == installer_asset["name"] + ".sha256"), None)

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=installer_asset["browser_download_url"],
        checksum_url=checksum_asset["browser_download_url"] if checksum_asset else None,
        notes_url=data.get("html_url") or f"https://github.com/{_REPO}/releases/latest",
    )


def apply_update(info: UpdateInfo, on_exit: Callable[[], None]) -> None:
    """Downloads the release installer, verifies its checksum if one was published, then launches
    it silently and calls on_exit() to quit this process and hand off control. Raises on failure
    (download error, checksum mismatch) so the caller can show that to the user - nothing has
    touched the installed files at that point.

    Passes /CURRENTUSER or /ALLUSERS matching how this install was originally set up, so the
    installer repeats that choice instead of prompting for it again on what's meant to be an
    unattended update."""
    install_dir = Path(sys.executable).resolve().parent
    staging_dir = app_data_dir() / "update" / info.version
    staging_dir.mkdir(parents=True, exist_ok=True)
    installer_path = staging_dir / "FireshareAgentSetup.exe"

    _download_file(info.download_url, installer_path)

    if info.checksum_url:
        expected = _download_text(info.checksum_url).split()[0].strip().lower()
        actual = _sha256(installer_path).lower()
        if expected and expected != actual:
            raise RuntimeError("Downloaded update failed checksum verification - aborting.")

    mode_flag = "/ALLUSERS" if _is_all_users_install(install_dir) else "/CURRENTUSER"
    subprocess.Popen(
        [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/FORCECLOSEAPPLICATIONS", mode_flag],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    on_exit()


def _is_all_users_install(install_dir: Path) -> bool:
    """True if installed to a machine-wide location (Program Files) rather than a per-user one
    (AppData\\Local\\Programs) - determines which install mode to tell the installer to repeat."""
    candidates = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramW6432")]
    install_dir_str = str(install_dir).lower()
    return any(candidate and install_dir_str.startswith(candidate.lower()) for candidate in candidates)


def _download_file(url: str, destination: Path) -> None:
    with requests.get(url, timeout=120, stream=True, headers=_REQUEST_HEADERS) as response:
        response.raise_for_status()
        with open(destination, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)


def _download_text(url: str) -> str:
    response = requests.get(url, timeout=30, headers=_REQUEST_HEADERS)
    response.raise_for_status()
    return response.text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
