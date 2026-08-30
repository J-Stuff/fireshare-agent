"""
Regression coverage for the MFA login flow. The bug: WebApiUploader itself always handled
mfa_required correctly, but two call sites in the Settings window (Test Connection and Fetch
Folders) never passed an mfa_code_provider, and the old standalone fetch_upload_folders()
function skipped MFA handling entirely - so an MFA-enabled account could never get past
Settings, even though the real upload pipeline worked fine. These tests mock the HTTP layer
(no live Fireshare server available) to confirm the fixed code paths actually prompt for and
submit a TOTP code instead of failing outright.

Also covers session persistence: repeatedly logging in fresh every time TOTP is enabled was
"very annoying" per user feedback, so a valid session (including Fireshare's Flask-Login
"remember me" cookie) is now persisted and tried before ever hitting /api/login again.

These tests touch the real Windows Credential Manager via `keyring` for the
WEB_API_SESSION_COOKIES key (there's no sandboxed alternative here), so the fixture snapshots
and restores whatever was there before/after.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from fireshare_agent.config.app_config import WebApiSettings
from fireshare_agent.config.secrets import WEB_API_SESSION_COOKIES, delete_secret, get_secret, set_secret
from fireshare_agent.models import MediaKind, PendingFile
from fireshare_agent.uploaders.web_api_uploader import (
    MfaRequiredError,
    WebApiUploader,
    _normalize_filename,
    clear_persisted_web_api_session,
)


def _settings() -> WebApiSettings:
    return WebApiSettings(base_url="https://fireshare.example.com", username="admin")


def _mock_response(json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.content = b"{}"
    resp.json.return_value = json_data or {}
    return resp


def test_ensure_authenticated_without_mfa_succeeds():
    uploader = WebApiUploader(_settings())
    login_response = _mock_response({})

    with patch.object(uploader._session, "post", return_value=login_response) as mock_post:
        uploader._ensure_authenticated()

    assert uploader._authenticated is True
    mock_post.assert_called_once()


def test_ensure_authenticated_with_mfa_prompts_and_succeeds():
    provided_codes = []

    def provider():
        provided_codes.append("123456")
        return "123456"

    uploader = WebApiUploader(_settings(), mfa_code_provider=provider)
    login_response = _mock_response({"mfa_required": True})
    mfa_response = _mock_response({})

    with patch.object(uploader._session, "post", side_effect=[login_response, mfa_response]) as mock_post:
        uploader._ensure_authenticated()

    assert uploader._authenticated is True
    assert provided_codes == ["123456"]
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].args[0].endswith("/api/login/mfa")
    assert mock_post.call_args_list[1].kwargs["json"] == {"code": "123456"}


def test_ensure_authenticated_with_mfa_but_no_provider_raises_clear_error():
    uploader = WebApiUploader(_settings())  # no mfa_code_provider, as Settings used to construct it
    login_response = _mock_response({"mfa_required": True})

    with patch.object(uploader._session, "post", return_value=login_response):
        with pytest.raises(MfaRequiredError):
            uploader._ensure_authenticated()


def test_list_upload_folders_routes_through_mfa_flow():
    provided = []

    def provider():
        provided.append(True)
        return "654321"

    uploader = WebApiUploader(_settings(), mfa_code_provider=provider)
    login_response = _mock_response({"mfa_required": True})
    mfa_response = _mock_response({})
    folders_response = _mock_response({"folders": ["default", "clips"]})

    with patch.object(uploader._session, "post", side_effect=[login_response, mfa_response]):
        with patch.object(uploader._session, "get", return_value=folders_response) as mock_get:
            folders = uploader.list_upload_folders()

    assert folders == ["default", "clips"]
    assert provided == [True]  # the MFA prompt was actually invoked, not skipped
    mock_get.assert_called_once()


def test_test_connection_surfaces_mfa_required_message_when_cancelled():
    # Simulates the user cancelling the TOTP dialog (provider returns None).
    uploader = WebApiUploader(_settings(), mfa_code_provider=lambda: None)
    login_response = _mock_response({"mfa_required": True})

    with patch.object(uploader._session, "post", return_value=login_response):
        result = uploader.test_connection()

    assert result.success is False
    assert "TOTP" in result.message or "MFA" in result.message


@pytest.fixture
def preserve_web_api_session_secret():
    original = get_secret(WEB_API_SESSION_COOKIES)
    yield
    if original is None:
        delete_secret(WEB_API_SESSION_COOKIES)
    else:
        set_secret(WEB_API_SESSION_COOKIES, original)


def test_successful_login_persists_session_for_reuse(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    uploader = WebApiUploader(_settings())
    login_response = _mock_response({})

    def fake_login_post(*_args, **_kwargs):
        # Simulates what `requests` would normally capture from a real Set-Cookie header on
        # the login response - must happen as a side effect of the POST, not before it, or
        # _try_resume_session() sees a non-empty jar up front and probes for real.
        uploader._session.cookies.set("session", "abc123")
        return login_response

    with patch.object(uploader._session, "post", side_effect=fake_login_post):
        uploader._ensure_authenticated()

    saved = get_secret(WEB_API_SESSION_COOKIES)
    assert saved is not None
    envelope = json.loads(saved)
    assert envelope["base_url"] == "https://fireshare.example.com"
    assert envelope["cookies"]["session"] == "abc123"


def test_fresh_uploader_resumes_persisted_session_without_logging_in_again(preserve_web_api_session_secret):
    # Simulates an app restart: a brand new uploader instance, but a session was saved earlier.
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "base_url": "https://fireshare.example.com",
        "cookies": {"session": "already-valid-token"},
    }))
    uploader = WebApiUploader(_settings())
    probe_response = _mock_response({})
    probe_response.status_code = 200

    with patch.object(uploader._session, "post") as mock_post:
        with patch.object(uploader._session, "get", return_value=probe_response) as mock_get:
            uploader._ensure_authenticated()

    assert uploader._authenticated is True
    mock_post.assert_not_called()  # never touched /api/login - no MFA prompt possible
    mock_get.assert_called_once()
    assert uploader._session.cookies.get("session") == "already-valid-token"


def test_expired_persisted_session_falls_back_to_fresh_login(preserve_web_api_session_secret):
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "base_url": "https://fireshare.example.com",
        "cookies": {"session": "expired-token"},
    }))
    uploader = WebApiUploader(_settings())
    probe_response = _mock_response({})
    probe_response.status_code = 401
    login_response = _mock_response({})

    with patch.object(uploader._session, "post", return_value=login_response) as mock_post:
        with patch.object(uploader._session, "get", return_value=probe_response) as mock_get:
            uploader._ensure_authenticated()

    assert uploader._authenticated is True
    mock_get.assert_called_once()   # tried to resume first
    mock_post.assert_called_once()  # then fell back to a real login
    assert get_secret(WEB_API_SESSION_COOKIES) is None  # the stale cookie was discarded


def test_persisted_session_for_a_different_server_is_ignored(preserve_web_api_session_secret):
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "base_url": "https://other-server.example.com",
        "cookies": {"session": "not-for-this-server"},
    }))
    uploader = WebApiUploader(_settings())  # settings() base_url is https://fireshare.example.com
    login_response = _mock_response({})

    with patch.object(uploader._session, "post", return_value=login_response) as mock_post:
        with patch.object(uploader._session, "get") as mock_get:
            uploader._ensure_authenticated()

    mock_get.assert_not_called()  # never even tried the probe with a foreign cookie
    mock_post.assert_called_once()


def test_clear_persisted_web_api_session_removes_saved_secret(preserve_web_api_session_secret):
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({"base_url": "x", "cookies": {"session": "y"}}))

    clear_persisted_web_api_session()

    assert get_secret(WEB_API_SESSION_COOKIES) is None


def test_normalize_filename_ignores_case_and_punctuation():
    assert _normalize_filename("My Clip.mp4") == _normalize_filename("my_clip.MP4")
    assert _normalize_filename("Godfall 2026.01.15 - 20.30.00.mp4") == _normalize_filename("Godfall20260115-203000.mp4")
    assert _normalize_filename("clip-a.mp4") != _normalize_filename("clip-b.mp4")


def test_exists_at_destination_matches_by_filename_and_extension(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    uploader = WebApiUploader(_settings())
    login_response = _mock_response({})
    videos_response = _mock_response({"videos": [{"path": "Uploaded/my_clip.mp4", "extension": "mp4"}]})

    with patch.object(uploader._session, "post", return_value=login_response):
        with patch.object(uploader._session, "get", return_value=videos_response):
            found = uploader.exists_at_destination(PendingFile(path=r"C:\Clips\My Clip.mp4", kind=MediaKind.VIDEO, size_bytes=123))

    assert found is True


def test_exists_at_destination_returns_false_when_no_match(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    uploader = WebApiUploader(_settings())
    login_response = _mock_response({})
    videos_response = _mock_response({"videos": [{"path": "Uploaded/some_other_clip.mp4", "extension": "mp4"}]})

    with patch.object(uploader._session, "post", return_value=login_response):
        with patch.object(uploader._session, "get", return_value=videos_response):
            found = uploader.exists_at_destination(PendingFile(path=r"C:\Clips\My Clip.mp4", kind=MediaKind.VIDEO, size_bytes=123))

    assert found is False


def test_exists_at_destination_never_raises_on_network_error(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    uploader = WebApiUploader(_settings())

    with patch.object(uploader._session, "post", side_effect=requests.ConnectionError("down")):
        found = uploader.exists_at_destination(PendingFile(path=r"C:\Clips\My Clip.mp4", kind=MediaKind.VIDEO, size_bytes=123))

    assert found is False  # never blocks a genuinely new upload just because the check failed


def test_exists_at_destination_caches_the_video_list_briefly(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    uploader = WebApiUploader(_settings())
    login_response = _mock_response({})
    videos_response = _mock_response({"videos": []})

    with patch.object(uploader._session, "post", return_value=login_response):
        with patch.object(uploader._session, "get", return_value=videos_response) as mock_get:
            uploader.exists_at_destination(PendingFile(path=r"C:\Clips\A.mp4", kind=MediaKind.VIDEO, size_bytes=1))
            uploader.exists_at_destination(PendingFile(path=r"C:\Clips\B.mp4", kind=MediaKind.VIDEO, size_bytes=1))

    mock_get.assert_called_once()  # second call within the cache TTL reused the first fetch


def test_chunked_upload_uses_a_stable_checksum_across_retries(tmp_path):
    # Regression test: check_sum used to be a fresh uuid4() on every call, so retrying a failed
    # chunked upload abandoned whatever the previous attempt had already sent as orphaned junk
    # on the server - confirmed against the server source that partial chunk files are only
    # cleaned up on a successful reassembly (or a one-time sweep at server startup), never per
    # abandoned attempt. A stable checksum lets a retry resend into the same group instead.
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 100)

    uploader = WebApiUploader(_settings())
    pending_file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=100)

    seen_checksums = []
    with patch.object(uploader, "_post_chunk", side_effect=lambda *args: seen_checksums.append(args[3])):
        uploader._upload_video_chunked(pending_file)  # first attempt
        uploader._upload_video_chunked(pending_file)  # simulated retry of the same file

    assert len(seen_checksums) == 2
    assert seen_checksums[0] == seen_checksums[1]


def test_chunked_upload_checksum_differs_for_different_files(tmp_path):
    path_a = tmp_path / "a.mp4"
    path_b = tmp_path / "b.mp4"
    path_a.write_bytes(b"x" * 100)
    path_b.write_bytes(b"y" * 100)

    uploader = WebApiUploader(_settings())
    seen: dict[str, str] = {}

    def capture(chunk, part, total_chunks, check_sum, file_name, size_bytes):
        seen[file_name] = check_sum

    with patch.object(uploader, "_post_chunk", side_effect=capture):
        uploader._upload_video_chunked(PendingFile(path=str(path_a), kind=MediaKind.VIDEO, size_bytes=100))
        uploader._upload_video_chunked(PendingFile(path=str(path_b), kind=MediaKind.VIDEO, size_bytes=100))

    assert seen["a.mp4"] != seen["b.mp4"]


def test_upload_invalidates_the_existing_entries_cache(tmp_path, preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 10)

    uploader = WebApiUploader(_settings())
    uploader._existing_entries_cache[MediaKind.VIDEO] = (0.0, [{"path": "old.mp4", "extension": "mp4"}])

    post_response = _mock_response({})
    post_response.status_code = 201

    with patch.object(uploader._session, "post", return_value=post_response):
        result = uploader.upload(PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10))

    assert result.success is True
    assert MediaKind.VIDEO not in uploader._existing_entries_cache
