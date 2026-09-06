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
from fireshare_agent.uploaders import web_api_uploader
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


def _saved_session(base_url: str, name: str, value: str, **attributes) -> str:
    """A persisted-session blob in the current envelope shape."""
    cookie = {"name": name, "value": value, "domain": "fireshare.example.com", "path": "/", "secure": True}
    cookie.update(attributes)
    return json.dumps({
        "version": web_api_uploader._SESSION_ENVELOPE_VERSION,
        "base_url": base_url,
        "cookies": [cookie],
    })


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
    assert envelope["version"] == web_api_uploader._SESSION_ENVELOPE_VERSION
    [cookie] = envelope["cookies"]
    assert cookie["name"] == "session"
    assert cookie["value"] == "abc123"
    # The attribute that confines the cookie to this server. Persisted as a flat {name: value} map
    # it came back domainless, and requests sends a domainless cookie to every host the session
    # contacts - including anywhere a redirect might land it.
    assert cookie["domain"] == "fireshare.example.com"


def test_fresh_uploader_resumes_persisted_session_without_logging_in_again(preserve_web_api_session_secret):
    # Simulates an app restart: a brand new uploader instance, but a session was saved earlier.
    set_secret(WEB_API_SESSION_COOKIES, _saved_session("https://fireshare.example.com", "session", "already-valid-token"))
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
    set_secret(WEB_API_SESSION_COOKIES, _saved_session("https://fireshare.example.com", "session", "expired-token"))
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
    set_secret(WEB_API_SESSION_COOKIES, _saved_session("https://other-server.example.com", "session", "not-for-this-server"))
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

    def capture(chunk, part, total_chunks, check_sum, file_name, size_bytes, folder):
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


def test_resolve_folder_prefers_the_local_subfolder_hint_when_mirroring_is_on():
    settings = _settings()
    settings.mirror_local_folder_structure = True
    settings.target_folder = "Fallback"
    uploader = WebApiUploader(settings)

    file = PendingFile(path=r"C:\Clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=1, remote_folder_hint="SomeGame")

    assert uploader._resolve_folder(file) == "SomeGame"


def test_resolve_folder_falls_back_to_target_folder_without_a_hint():
    settings = _settings()
    settings.mirror_local_folder_structure = True
    settings.target_folder = "Fallback"
    uploader = WebApiUploader(settings)

    file = PendingFile(path=r"C:\Clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=1, remote_folder_hint=None)

    assert uploader._resolve_folder(file) == "Fallback"


def test_resolve_folder_ignores_the_hint_when_mirroring_is_off():
    settings = _settings()
    settings.mirror_local_folder_structure = False
    settings.target_folder = "Fallback"
    uploader = WebApiUploader(settings)

    file = PendingFile(path=r"C:\Clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=1, remote_folder_hint="SomeGame")

    assert uploader._resolve_folder(file) == "Fallback"


def test_chunked_upload_sends_the_resolved_folder_in_the_request(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 10)

    settings = _settings()
    settings.mirror_local_folder_structure = True
    uploader = WebApiUploader(settings)
    file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10, remote_folder_hint="SomeGame")

    seen_folders = []
    with patch.object(uploader, "_post_chunk", side_effect=lambda *args: seen_folders.append(args[6])):
        uploader._upload_video_chunked(file)

    assert seen_folders == ["SomeGame"]


def test_image_upload_sends_the_resolved_folder_in_the_request(tmp_path, preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    path = tmp_path / "shot.png"
    path.write_bytes(b"x" * 10)

    settings = _settings()
    settings.mirror_local_folder_structure = True
    uploader = WebApiUploader(settings)
    file = PendingFile(path=str(path), kind=MediaKind.IMAGE, size_bytes=10, remote_folder_hint="SomeGame")

    response = _mock_response({})
    response.status_code = 201
    with patch.object(uploader._session, "post", return_value=response) as mock_post:
        uploader._upload_image(file)

    assert mock_post.call_args.kwargs["data"]["folder"] == "SomeGame"


def test_exists_at_destination_treats_same_name_in_a_different_folder_as_distinct(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    settings = _settings()
    settings.mirror_local_folder_structure = True
    uploader = WebApiUploader(settings)

    login_response = _mock_response({})
    # Same filename already exists, but under a different game's folder.
    videos_response = _mock_response({"videos": [{"path": "OtherGame/clip.mp4", "extension": "mp4"}]})

    file = PendingFile(path=r"C:\Clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=1, remote_folder_hint="SomeGame")

    with patch.object(uploader._session, "post", return_value=login_response):
        with patch.object(uploader._session, "get", return_value=videos_response):
            found = uploader.exists_at_destination(file)

    assert found is False


def test_exists_at_destination_matches_same_name_in_the_same_folder(preserve_web_api_session_secret):
    delete_secret(WEB_API_SESSION_COOKIES)
    settings = _settings()
    settings.mirror_local_folder_structure = True
    uploader = WebApiUploader(settings)

    login_response = _mock_response({})
    videos_response = _mock_response({"videos": [{"path": "SomeGame/clip.mp4", "extension": "mp4"}]})

    file = PendingFile(path=r"C:\Clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=1, remote_folder_hint="SomeGame")

    with patch.object(uploader._session, "post", return_value=login_response):
        with patch.object(uploader._session, "get", return_value=videos_response):
            found = uploader.exists_at_destination(file)

    assert found is True


# --- Persisted cookies must keep their scoping ------------------------------------------------
#
# The jar used to be flattened with dict(self._session.cookies), which keeps only name/value, and
# restored with cookies.set(name, value) and no domain. A domainless cookie in a requests session
# is sent to *every* host that session contacts. Nothing in today's code paths talks to a second
# host, so nothing leaked in practice - but the token handled this way is Flask-Login's long-lived
# "remember me" cookie, the one deliberately persisted because it is durable, and any redirect onto
# another host would have handed it over.


def _jar_cookie(uploader, name: str):
    return next(c for c in uploader._session.cookies if c.name == name)


def test_a_restored_cookie_is_scoped_to_its_domain(preserve_web_api_session_secret):
    set_secret(WEB_API_SESSION_COOKIES, _saved_session(
        "https://fireshare.example.com", "remember_token", "durable-value",
    ))
    uploader = WebApiUploader(_settings())

    uploader._load_persisted_session()

    cookie = _jar_cookie(uploader, "remember_token")
    assert cookie.domain == "fireshare.example.com"
    assert cookie.value == "durable-value"


def test_a_restored_cookie_is_not_sent_to_another_host(preserve_web_api_session_secret):
    """The property that actually matters, asserted through requests' own cookie matching rather
    than by reading back the attribute we just set. prepare_request() is the same call the session
    makes for every real request, so this is the header the server would genuinely receive."""
    set_secret(WEB_API_SESSION_COOKIES, _saved_session(
        "https://fireshare.example.com", "remember_token", "durable-value",
    ))
    uploader = WebApiUploader(_settings())
    uploader._load_persisted_session()

    def cookie_header(url: str) -> str:
        prepared = uploader._session.prepare_request(requests.Request("GET", url))
        return prepared.headers.get("Cookie", "")

    assert "durable-value" in cookie_header("https://fireshare.example.com/api/x")
    assert cookie_header("https://attacker.example.net/api/x") == ""


def test_a_domainless_cookie_would_have_leaked(preserve_web_api_session_secret):
    """Characterises the old behaviour, so the test above is demonstrably testing something. This
    is what cookies.set(name, value) with no domain does: matches every host."""
    uploader = WebApiUploader(_settings())
    uploader._session.cookies.set("remember_token", "durable-value")  # exactly the old restore call

    prepared = uploader._session.prepare_request(
        requests.Request("GET", "https://attacker.example.net/api/x")
    )

    assert "durable-value" in prepared.headers.get("Cookie", "")


def test_a_cookie_saved_without_a_domain_falls_back_to_the_configured_host(preserve_web_api_session_secret):
    """Never restore an unscoped cookie: if the stored blob has no domain, the configured host is
    used instead of leaving it matching everything."""
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "version": web_api_uploader._SESSION_ENVELOPE_VERSION,
        "base_url": "https://fireshare.example.com",
        "cookies": [{"name": "session", "value": "v"}],
    }))
    uploader = WebApiUploader(_settings())

    uploader._load_persisted_session()

    assert _jar_cookie(uploader, "session").domain == "fireshare.example.com"


def test_two_same_named_cookies_on_different_paths_both_survive(preserve_web_api_session_secret):
    """dict(jar) silently kept only one of them."""
    uploader = WebApiUploader(_settings())
    uploader._session.cookies.set("token", "root-value", domain="fireshare.example.com", path="/")
    uploader._session.cookies.set("token", "api-value", domain="fireshare.example.com", path="/api")

    uploader._persist_session()

    envelope = json.loads(get_secret(WEB_API_SESSION_COOKIES))
    assert sorted(c["path"] for c in envelope["cookies"]) == ["/", "/api"]


def test_a_null_valued_cookie_is_skipped_rather_than_deleting_one(preserve_web_api_session_secret):
    """requests treats cookies.set(name, None) as a *deletion*, so a null value in the stored blob
    would quietly remove a cookie instead of restoring one."""
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "version": web_api_uploader._SESSION_ENVELOPE_VERSION,
        "base_url": "https://fireshare.example.com",
        "cookies": [
            {"name": "session", "value": None, "domain": "fireshare.example.com"},
            {"name": "remember_token", "value": "kept", "domain": "fireshare.example.com"},
        ],
    }))
    uploader = WebApiUploader(_settings())

    uploader._load_persisted_session()

    assert [c.name for c in uploader._session.cookies] == ["remember_token"]


def test_an_old_format_session_blob_is_discarded_not_misread(preserve_web_api_session_secret):
    """A v1 blob is a {name: value} map where v2 expects a list of dicts. Reading it under the new
    rules would iterate the *keys*; the version field makes it a clean discard and one fresh
    login."""
    set_secret(WEB_API_SESSION_COOKIES, json.dumps({
        "base_url": "https://fireshare.example.com",
        "cookies": {"session": "from-the-old-format"},
    }))
    uploader = WebApiUploader(_settings())

    uploader._load_persisted_session()

    assert len(uploader._session.cookies) == 0


def test_a_trailing_slash_difference_does_not_throw_the_session_away(preserve_web_api_session_secret):
    """base_url was compared as an exact string, so a trailing slash typed this time but not last
    time silently discarded a valid session - and with TOTP enabled, that means a fresh prompt."""
    set_secret(WEB_API_SESSION_COOKIES, _saved_session(
        "https://fireshare.example.com/", "session", "still-good",
    ))
    uploader = WebApiUploader(_settings())  # configured without the trailing slash

    uploader._load_persisted_session()

    assert _jar_cookie(uploader, "session").value == "still-good"


def test_a_session_saved_for_another_server_is_still_rejected(preserve_web_api_session_secret):
    """Normalising trailing slashes must not soften the check itself."""
    set_secret(WEB_API_SESSION_COOKIES, _saved_session(
        "https://other-server.example.com", "session", "not-for-this-server",
    ))
    uploader = WebApiUploader(_settings())

    uploader._load_persisted_session()

    assert len(uploader._session.cookies) == 0


def test_a_persisted_session_round_trips(preserve_web_api_session_secret):
    """Save then load must reproduce the same jar, so persistence is transparent rather than a
    lossy re-derivation."""
    saver = WebApiUploader(_settings())
    saver._session.cookies.set("session", "s-value", domain="fireshare.example.com", path="/")
    saver._session.cookies.set("remember_token", "r-value", domain="fireshare.example.com", path="/")
    saver._persist_session()

    loader = WebApiUploader(_settings())
    loader._load_persisted_session()

    assert {(c.name, c.value, c.domain, c.path) for c in loader._session.cookies} == {
        (c.name, c.value, c.domain, c.path) for c in saver._session.cookies
    }
<<<<<<< HEAD


# --------------------------------------------------------------------- upload progress reporting
#
# feature-ideas.md #1: a 4 GB clip used to show UPLOADING and then nothing for twenty minutes,
# with no way to tell a slow upload from a wedged one. The chunk loop already knows exactly how
# far along it is; these assert that it says so, and that saying so can never break an upload.


def test_chunked_upload_reports_progress_after_each_chunk(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")

    settings = _settings()
    settings.chunk_size_bytes = 4
    uploader = WebApiUploader(settings)
    file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10)

    reported = []
    with patch.object(uploader, "_post_chunk"):
        uploader._upload_video_chunked(file, lambda sent, total: reported.append((sent, total)))

    # Counted only after the POST returns, so these are bytes the server accepted rather than
    # bytes handed to requests - and the last chunk is short, not padded to the chunk size.
    assert reported == [(4, 10), (8, 10), (10, 10)]


def test_progress_is_reported_at_zero_before_any_bytes_move(tmp_path):
    """A consumer that only learns about the file from this callback must start at a truthful 0%,
    not at whatever the first chunk happens to be - which for a small clip is most of the file."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")

    uploader = WebApiUploader(_settings())
    file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10)

    reported = []
    with patch.object(uploader, "_ensure_authenticated"), patch.object(uploader, "_upload_video_chunked"):
        uploader.upload(file, on_progress=lambda sent, total: reported.append((sent, total)))

    assert reported[0] == (0, 10)


def test_an_image_upload_reports_completion(tmp_path):
    """One request, so there is no meaningful mid-transfer progress - but the caller still needs
    to be told it finished, or a bar sits at 0% until the SUCCEEDED event arrives."""
    path = tmp_path / "shot.png"
    path.write_bytes(b"x" * 64)

    uploader = WebApiUploader(_settings())
    file = PendingFile(path=str(path), kind=MediaKind.IMAGE, size_bytes=64)

    response = _mock_response({})
    response.status_code = 201
    reported = []
    with patch.object(uploader, "_post_image", return_value=response):
        uploader._upload_image(file, lambda sent, total: reported.append((sent, total)))

    assert reported == [(64, 64)]


def test_a_progress_callback_that_raises_never_fails_the_upload(tmp_path):
    """The consumer is UI state. A multi-gigabyte transfer that is going fine has no business
    failing because a window was destroyed mid-callback."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")

    settings = _settings()
    settings.chunk_size_bytes = 4
    uploader = WebApiUploader(settings)
    file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10)

    def explode(sent, total):
        raise RuntimeError("the window went away")

    with patch.object(uploader, "_post_chunk"):
        uploader._upload_video_chunked(file, explode)  # must not raise


def test_progress_is_optional(tmp_path):
    """The Uploader contract makes the callback optional, so the helpers have to cope without
    one rather than requiring every caller to supply a no-op."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")

    uploader = WebApiUploader(_settings())
    file = PendingFile(path=str(path), kind=MediaKind.VIDEO, size_bytes=10)

    with patch.object(uploader, "_post_chunk"):
        uploader._upload_video_chunked(file)  # must not raise
=======
>>>>>>> origin/main
