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
import re
import shutil
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

# A release tag reaches us as attacker-influenceable text (a hostile or compromised release can name
# a tag anything) and is then used as a directory name under %AppData%. Anything outside this set -
# a separator, a drive letter, a parent reference - must never reach the filesystem.
_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


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

    if not _is_safe_path_segment(tag.lstrip("vV")):
        # Rejected here as well as in apply_update() so a malformed release never even surfaces as
        # an offer the user can click.
        log.warning("Ignoring release %r: the tag is not usable as a directory name.", tag)
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
    """Downloads the release installer, verifies its checksum, then launches it silently and calls
    on_exit() to quit this process and hand off control. Raises on failure (download error, missing
    or unusable checksum, checksum mismatch) so the caller can show that to the user - nothing has
    touched the installed files at that point.

    Passes /CURRENTUSER or /ALLUSERS matching how this install was originally set up, so the
    installer repeats that choice instead of prompting for it again on what's meant to be an
    unattended update."""
    if not _is_safe_path_segment(info.version):
        raise RuntimeError(f"Refusing to stage an update for an unsafe release name: {info.version!r}")

    install_dir = Path(sys.executable).resolve().parent
    update_root = app_data_dir() / "update"
    staging_dir = update_root / info.version
    # Checked against the resolved paths, not the strings, so a symlinked component is caught too -
    # and checked before mkdir, so a rejected name never gets a directory created for it.
    _reject_escape(staging_dir, update_root)
    staging_dir.mkdir(parents=True, exist_ok=True)
    installer_path = staging_dir / "FireshareAgentSetup.exe"

    # Fetched before the download so a release that cannot be verified fails fast, rather than
    # after pulling down an installer that was never going to be run.
    expected = _expected_sha256(info)
    _download_file(info.download_url, installer_path)

    actual = _sha256(installer_path).lower()
    if expected != actual:
        raise RuntimeError("Downloaded update failed checksum verification - aborting.")

    mode_flag = "/ALLUSERS" if _is_all_users_install(install_dir) else "/CURRENTUSER"
    subprocess.Popen(
        [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/FORCECLOSEAPPLICATIONS", mode_flag],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    on_exit()


def cleanup_staged_installers() -> int:
    r"""Deletes installers left behind in %AppData%\FireshareAgent\update by past updates, and
    returns how many entries were removed.

    apply_update() stages each download in its own per-version directory and nothing ever removed
    them, so a user who took five updates was left carrying five ~60MB installers forever - every
    one of them already run and useless. Called at startup, which is the only moment this is
    race-free: apply_update() is the only writer, it runs from a dialog the user just clicked, and
    it exits the process as soon as the installer is launched, so nothing can be mid-download here.

    Never raises. A cleanup is not worth failing a boot over, and one failure is entirely expected:
    immediately after an update the installer that relaunched us is often still running, and Windows
    locks a running exe's image file. That entry simply stays until the next boot removes it.
    """
    update_root = app_data_dir() / "update"
    try:
        entries = list(update_root.iterdir())
    except OSError:
        return 0  # no update directory yet, or unreadable - either way there is nothing to do

    removed = 0
    for entry in entries:
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as ex:
            log.debug("Leaving staged update %s in place for now: %s", entry, ex)
            continue
        removed += 1

    if removed:
        log.info("Removed %d stale installer(s) from %s", removed, update_root)
    return removed


def _is_safe_path_segment(value: str) -> bool:
    """True if `value` is safe to use as a single directory name. `fullmatch` rules out separators
    and drive letters; the explicit `.`/`..` check is the case the character class alone misses -
    ".." is made entirely of permitted characters but still walks up a level."""
    return bool(value) and value not in (".", "..") and _SAFE_PATH_SEGMENT.fullmatch(value) is not None


def _reject_escape(candidate: Path, expected_parent: Path) -> None:
    if expected_parent.resolve() not in candidate.resolve().parents:
        raise RuntimeError("Refusing to stage an update outside the update directory.")


def _expected_sha256(info: UpdateInfo) -> str:
    """The digest this release's installer must hash to, or a raised error explaining why the
    update cannot proceed.

    Fails closed, deliberately. What follows verification is "run this downloaded executable
    silently, elevated via UAC on an all-users install" - the one code path that must not have an
    implicit skip in it. Previously a release that published no .sha256 asset was installed with no
    integrity check at all, and an empty or malformed checksum file did the same, because the guard
    was `if expected and expected != actual` and an empty string is falsy. Transport is HTTPS to
    GitHub either way, so this is defence in depth rather than a live exploit - but the failure mode
    it removes is silent."""
    manual = f"Download and install it manually from {info.notes_url}"
    if not info.checksum_url:
        raise RuntimeError(
            "This release did not publish a checksum, so the download cannot be verified. "
            f"{manual}"
        )

    tokens = _download_text(info.checksum_url).split()
    # An empty checksum file used to reach .split()[0] and raise IndexError, which the caller's
    # broad except turned into a confusingly generic "Update Failed".
    candidate = tokens[0].strip().lower() if tokens else ""
    if _SHA256_HEX.fullmatch(candidate) is None:
        raise RuntimeError(
            "This release's checksum file is not a valid SHA-256 digest, so the download cannot "
            f"be verified. {manual}"
        )
    return candidate


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
