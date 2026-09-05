"""
Checks GitHub Releases for a newer version and, if the user confirms, downloads it and hands off
to a small generated PowerShell script that waits for this process to exit, replaces the
installed files, and relaunches - a running Windows exe can't overwrite its own files directly.

Only meaningful for the packaged (frozen) build; check_for_update() is a no-op when running from
source, since there's no installed exe directory to update in place.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import zipfile
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
    zip_asset = next((a for a in assets if a.get("name", "").endswith(".zip")), None)
    if zip_asset is None:
        return None
    checksum_asset = next((a for a in assets if a.get("name") == zip_asset["name"] + ".sha256"), None)

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=zip_asset["browser_download_url"],
        checksum_url=checksum_asset["browser_download_url"] if checksum_asset else None,
        notes_url=data.get("html_url") or f"https://github.com/{_REPO}/releases/latest",
    )


def apply_update(info: UpdateInfo, on_exit: Callable[[], None]) -> None:
    """Downloads the release zip, verifies its checksum if one was published, extracts it,
    writes a relaunch script, launches that script detached, then calls on_exit() to quit this
    process and hand off control. Raises on failure (download error, checksum mismatch) so the
    caller can show that to the user - nothing has touched the installed files at that point."""
    install_dir = Path(sys.executable).resolve().parent
    staging_dir = app_data_dir() / "update" / info.version
    staging_dir.mkdir(parents=True, exist_ok=True)
    zip_path = staging_dir / "update.zip"

    _download_file(info.download_url, zip_path)

    if info.checksum_url:
        expected = _download_text(info.checksum_url).split()[0].strip().lower()
        actual = _sha256(zip_path).lower()
        if expected and expected != actual:
            raise RuntimeError("Downloaded update failed checksum verification - aborting.")

    extracted_dir = staging_dir / "extracted"
    if extracted_dir.exists():
        shutil.rmtree(extracted_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extracted_dir)

    script_path = staging_dir / "apply_update.ps1"
    script_path.write_text(
        _relaunch_script(pid=os.getpid(), source_dir=extracted_dir, install_dir=install_dir, staging_dir=staging_dir),
        encoding="utf-8",
    )

    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script_path)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    on_exit()


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


def _relaunch_script(pid: int, source_dir: Path, install_dir: Path, staging_dir: Path) -> str:
    exe_name = Path(sys.executable).name
    # PowerShell here-string values: paths are user/CI-controlled install locations, not
    # arbitrary input, but they're still quoted rather than interpolated unquoted.
    return f"""
$ErrorActionPreference = "Stop"
$targetPid = {pid}
$source = "{source_dir}"
$dest = "{install_dir}"
$exe = Join-Path "{install_dir}" "{exe_name}"

for ($i = 0; $i -lt 60; $i++) {{
    if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Seconds 1
}}

robocopy $source $dest /MIR /R:3 /W:2 | Out-Null
if ($LASTEXITCODE -ge 8) {{
    exit 1
}}

Start-Process -FilePath $exe
Start-Sleep -Seconds 2
Remove-Item -Path "{staging_dir}" -Recurse -Force -ErrorAction SilentlyContinue
"""
