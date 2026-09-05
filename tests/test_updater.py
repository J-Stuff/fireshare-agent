"""
Coverage for the self-updater: version comparison, the GitHub API check (mocked - no real
network calls), and the parts of apply_update() that are safely testable without actually running
an installer (download/checksum verification, install-mode detection), with the actual process
handoff (subprocess.Popen + on_exit) mocked out.
"""
import hashlib
import sys
from unittest.mock import MagicMock, patch

import pytest

from fireshare_agent import updater
from fireshare_agent.updater import UpdateInfo


def test_parse_version_handles_common_formats():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("v1.2.3-rc.1") == (1, 2, 3)
    assert updater.parse_version("v1.0") == (1, 0, 0)
    assert updater.parse_version("v2") == (2, 0, 0)


def test_parse_version_orders_correctly():
    assert updater.parse_version("v1.2.0") > updater.parse_version("v1.1.9")
    assert updater.parse_version("v2.0.0") > updater.parse_version("v1.99.99")
    assert updater.parse_version("v1.0.0") == updater.parse_version("v1.0.0")


def test_check_for_update_is_a_noop_when_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    with patch("fireshare_agent.updater.requests.get") as mock_get:
        result = updater.check_for_update()

    assert result is None
    mock_get.assert_not_called()


def _mock_release_response(tag: str, assets: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"tag_name": tag, "assets": assets, "html_url": "https://example.com/releases/latest"}
    return resp


def test_check_for_update_finds_a_newer_version(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "__version__", "0.1.0")

    response = _mock_release_response(
        "v0.2.0",
        [
            {"name": "FireshareAgent-Setup-0.2.0.exe", "browser_download_url": "https://example.com/setup.exe"},
            {"name": "FireshareAgent-Setup-0.2.0.exe.sha256", "browser_download_url": "https://example.com/setup.exe.sha256"},
        ],
    )

    with patch("fireshare_agent.updater.requests.get", return_value=response):
        result = updater.check_for_update()

    assert result is not None
    assert result.version == "0.2.0"
    assert result.download_url == "https://example.com/setup.exe"
    assert result.checksum_url == "https://example.com/setup.exe.sha256"


def test_check_for_update_returns_none_when_already_current(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "__version__", "0.2.0")

    response = _mock_release_response("v0.2.0", [{"name": "x.exe", "browser_download_url": "u"}])

    with patch("fireshare_agent.updater.requests.get", return_value=response):
        result = updater.check_for_update()

    assert result is None


def test_check_for_update_returns_none_without_an_installer_asset(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "__version__", "0.1.0")

    response = _mock_release_response("v0.2.0", [{"name": "notes.txt", "browser_download_url": "u"}])

    with patch("fireshare_agent.updater.requests.get", return_value=response):
        result = updater.check_for_update()

    assert result is None


def test_check_for_update_never_raises_on_network_error(monkeypatch):
    import requests

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with patch("fireshare_agent.updater.requests.get", side_effect=requests.ConnectionError("down")):
        result = updater.check_for_update()

    assert result is None


def _mock_streaming_response() -> MagicMock:
    # _download_file() uses `with requests.get(...) as response:` - a bare MagicMock's
    # __enter__ returns a *different* auto-generated mock by default, not the object itself,
    # so this has to be wired explicitly or `response` inside the `with` block would be a mock
    # with none of the attributes configured below.
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.raise_for_status.return_value = None
    return resp


def _fake_installer_bytes() -> tuple[bytes, str]:
    content = b"fake installer exe bytes"
    return content, hashlib.sha256(content).hexdigest()


def _fake_get(installer_bytes: bytes, checksum: str):
    def fake_get(url, timeout=None, stream=False, headers=None):
        if url == "https://example.com/setup.exe":
            resp = _mock_streaming_response()
            resp.iter_content = lambda chunk_size: [installer_bytes]
            return resp
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.text = f"{checksum}  FireshareAgent-Setup-0.2.0.exe\n"
        return resp

    return fake_get


def _update_info() -> UpdateInfo:
    return UpdateInfo(
        version="0.2.0", tag="v0.2.0",
        download_url="https://example.com/setup.exe",
        checksum_url="https://example.com/setup.exe.sha256",
        notes_url="https://example.com/releases/latest",
    )


def test_apply_update_verifies_checksum_and_launches_installer_silently(tmp_path, monkeypatch):
    installer_bytes, correct_checksum = _fake_installer_bytes()

    install_dir = tmp_path / "AppData" / "Local" / "Programs" / "FireshareAgent"
    monkeypatch.setattr(sys, "executable", str(install_dir / "FireshareAgent.exe"), raising=False)
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)

    exited = []
    with patch("fireshare_agent.updater.requests.get", side_effect=_fake_get(installer_bytes, correct_checksum)):
        with patch("fireshare_agent.updater.subprocess.Popen") as mock_popen:
            updater.apply_update(_update_info(), on_exit=lambda: exited.append(True))

    assert exited == [True]  # handed off and told the app to quit
    mock_popen.assert_called_once()
    launched_args = mock_popen.call_args.args[0]
    assert launched_args[0].endswith("FireshareAgentSetup.exe")
    assert "/VERYSILENT" in launched_args
    assert "/CURRENTUSER" in launched_args  # not a Program Files install
    assert "/ALLUSERS" not in launched_args

    installer_path = tmp_path / "appdata" / "update" / "0.2.0" / "FireshareAgentSetup.exe"
    assert installer_path.exists()
    assert installer_path.read_bytes() == installer_bytes


def test_apply_update_uses_allusers_flag_for_a_program_files_install(tmp_path, monkeypatch):
    installer_bytes, correct_checksum = _fake_installer_bytes()

    program_files = tmp_path / "Program Files"
    install_dir = program_files / "Fireshare Agent"
    monkeypatch.setattr(sys, "executable", str(install_dir / "FireshareAgent.exe"), raising=False)
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)

    with patch("fireshare_agent.updater.requests.get", side_effect=_fake_get(installer_bytes, correct_checksum)):
        with patch("fireshare_agent.updater.subprocess.Popen") as mock_popen:
            updater.apply_update(_update_info(), on_exit=lambda: None)

    launched_args = mock_popen.call_args.args[0]
    assert "/ALLUSERS" in launched_args
    assert "/CURRENTUSER" not in launched_args


def test_apply_update_rejects_a_checksum_mismatch(tmp_path, monkeypatch):
    installer_bytes, _real_checksum = _fake_installer_bytes()

    monkeypatch.setattr(sys, "executable", str(tmp_path / "install" / "FireshareAgent.exe"), raising=False)
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path / "appdata")

    bad_checksum = "0" * 64
    with patch("fireshare_agent.updater.requests.get", side_effect=_fake_get(installer_bytes, bad_checksum)):
        with patch("fireshare_agent.updater.subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="checksum"):
                updater.apply_update(_update_info(), on_exit=lambda: None)

    mock_popen.assert_not_called()  # never launched the installer on a bad checksum
