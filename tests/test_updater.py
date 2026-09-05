"""
Coverage for the self-updater: version comparison, the GitHub API check (mocked - no real
network calls), and the parts of apply_update() that are safely testable without actually
replacing files on disk (download/checksum verification, extraction, script generation), with
the actual process handoff (subprocess.Popen + on_exit) mocked out.
"""
import hashlib
import sys
import zipfile
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
            {"name": "FireshareAgent-v0.2.0-win64.zip", "browser_download_url": "https://example.com/app.zip"},
            {"name": "FireshareAgent-v0.2.0-win64.zip.sha256", "browser_download_url": "https://example.com/app.zip.sha256"},
        ],
    )

    with patch("fireshare_agent.updater.requests.get", return_value=response):
        result = updater.check_for_update()

    assert result is not None
    assert result.version == "0.2.0"
    assert result.download_url == "https://example.com/app.zip"
    assert result.checksum_url == "https://example.com/app.zip.sha256"


def test_check_for_update_returns_none_when_already_current(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "__version__", "0.2.0")

    response = _mock_release_response("v0.2.0", [{"name": "x.zip", "browser_download_url": "u"}])

    with patch("fireshare_agent.updater.requests.get", return_value=response):
        result = updater.check_for_update()

    assert result is None


def test_check_for_update_returns_none_without_a_zip_asset(monkeypatch):
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


def _make_fake_release_zip(tmp_path) -> tuple[bytes, str]:
    zip_path = tmp_path / "source.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("FireshareAgent.exe", b"fake exe bytes")
        zf.writestr("_internal/data.bin", b"fake data")
    content = zip_path.read_bytes()
    return content, hashlib.sha256(content).hexdigest()


def test_apply_update_verifies_checksum_and_hands_off_to_relaunch_script(tmp_path, monkeypatch):
    zip_bytes, correct_checksum = _make_fake_release_zip(tmp_path)

    monkeypatch.setattr(sys, "executable", str(tmp_path / "install" / "FireshareAgent.exe"), raising=False)
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path / "appdata")

    def fake_get(url, timeout=None, stream=False, headers=None):
        if url == "https://example.com/app.zip":
            resp = _mock_streaming_response()
            resp.iter_content = lambda chunk_size: [zip_bytes]
            return resp
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.text = f"{correct_checksum}  FireshareAgent-v0.2.0-win64.zip\n"
        return resp

    info = UpdateInfo(
        version="0.2.0", tag="v0.2.0",
        download_url="https://example.com/app.zip",
        checksum_url="https://example.com/app.zip.sha256",
        notes_url="https://example.com/releases/latest",
    )

    exited = []
    with patch("fireshare_agent.updater.requests.get", side_effect=fake_get):
        with patch("fireshare_agent.updater.subprocess.Popen") as mock_popen:
            updater.apply_update(info, on_exit=lambda: exited.append(True))

    assert exited == [True]  # handed off and told the app to quit
    mock_popen.assert_called_once()
    launched_args = mock_popen.call_args.args[0]
    assert "powershell.exe" in launched_args[0].lower()

    script_path = tmp_path / "appdata" / "update" / "0.2.0" / "apply_update.ps1"
    assert script_path.exists()
    script_text = script_path.read_text()
    assert "robocopy" in script_text.lower()
    assert "FireshareAgent.exe" in script_text

    extracted_dir = tmp_path / "appdata" / "update" / "0.2.0" / "extracted"
    assert (extracted_dir / "FireshareAgent.exe").exists()


def test_apply_update_rejects_a_checksum_mismatch(tmp_path, monkeypatch):
    zip_bytes, _real_checksum = _make_fake_release_zip(tmp_path)

    monkeypatch.setattr(sys, "executable", str(tmp_path / "install" / "FireshareAgent.exe"), raising=False)
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path / "appdata")

    def fake_get(url, timeout=None, stream=False, headers=None):
        if url.endswith(".zip"):
            resp = _mock_streaming_response()
            resp.iter_content = lambda chunk_size: [zip_bytes]
            return resp
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.text = "0000000000000000000000000000000000000000000000000000000000000000  bad.zip\n"
        return resp

    info = UpdateInfo(
        version="0.2.0", tag="v0.2.0",
        download_url="https://example.com/app.zip",
        checksum_url="https://example.com/app.zip.sha256",
        notes_url="https://example.com/releases/latest",
    )

    with patch("fireshare_agent.updater.requests.get", side_effect=fake_get):
        with patch("fireshare_agent.updater.subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="checksum"):
                updater.apply_update(info, on_exit=lambda: None)

    mock_popen.assert_not_called()  # never handed off to the relaunch script on a bad checksum
