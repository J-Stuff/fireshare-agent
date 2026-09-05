"""
Regression coverage for the "Test Connection doesn't see what you just typed" bug: the window
used to build its working config with persist_secrets=False for Test Connection / Fetch
Folders, so a freshly typed password never reached Windows Credential Manager and the uploader
fell back to whatever (if anything) was already saved there - requiring Save, then reopening
Settings, then testing again. Fixed by persisting on test too.

These tests touch the real Windows Credential Manager via `keyring` (there's no sandboxed
alternative here), so the fixture snapshots and restores whatever was there before/after.
"""
import customtkinter as ctk
import pytest

from fireshare_agent.config.app_config import (
    CHUNK_SIZE_MB_MIN,
    MAX_RETRY_ATTEMPTS_MIN,
    RETRY_BACKOFF_MIN_SECONDS,
    AppConfig,
)
from fireshare_agent.config.secrets import WEB_API_PASSWORD, delete_secret, get_secret, set_secret
from fireshare_agent.ui.settings_window import SettingsWindow
from fireshare_agent.uploaders import cloudflare


@pytest.fixture
def preserve_web_api_secret():
    original = get_secret(WEB_API_PASSWORD)
    yield
    if original is None:
        delete_secret(WEB_API_PASSWORD)
    else:
        set_secret(WEB_API_PASSWORD, original)


@pytest.fixture(scope="module")
def tk_root():
    # tkinter only reliably supports one Tcl interpreter per process - recreating a Tk() root
    # after a previous one was destroyed is unsupported on some builds, so this is shared across
    # every test in this module and only torn down once at the end.
    root = ctk.CTk()
    root.withdraw()
    yield root
    root.destroy()


def test_testing_a_freshly_typed_password_persists_it_before_use(tk_root, preserve_web_api_secret):
    delete_secret(WEB_API_PASSWORD)  # start from "nothing saved yet"

    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        window._webapi_password_entry.entry.insert(0, "freshly-typed-password")

        # This is exactly what _run_connection_test/_fetch_web_api_folders call before testing.
        window._build_config_from_fields(persist_secrets=True)

        assert get_secret(WEB_API_PASSWORD) == "freshly-typed-password"
    finally:
        window.destroy()


def test_building_config_for_display_only_does_not_touch_credential_manager(tk_root, preserve_web_api_secret):
    delete_secret(WEB_API_PASSWORD)

    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        window._webapi_password_entry.entry.insert(0, "should-not-be-saved")

        window._build_config_from_fields(persist_secrets=False)

        assert get_secret(WEB_API_PASSWORD) is None
    finally:
        window.destroy()


def _set(entry, value: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, value)


def test_out_of_range_numbers_are_clamped_and_written_back_into_the_field(tk_root):
    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        _set(window._webapi_chunk_entry, "0")
        _set(window._retry_backoff_entry, "-30")
        _set(window._max_retries_entry, "0")

        adjustments: list[str] = []
        config = window._build_config_from_fields(persist_secrets=False, adjustments=adjustments)

        assert config.web_api.chunk_size_bytes == CHUNK_SIZE_MB_MIN * 1024 * 1024
        assert config.retry_backoff_seconds == RETRY_BACKOFF_MIN_SECONDS
        assert config.max_retry_attempts == MAX_RETRY_ATTEMPTS_MIN
        assert len(adjustments) == 3
        # The user must be able to see what was actually stored, not just be told it changed.
        assert window._webapi_chunk_entry.get() == str(CHUNK_SIZE_MB_MIN)
        assert window._retry_backoff_entry.get() == str(RETRY_BACKOFF_MIN_SECONDS)
    finally:
        window.destroy()


def test_saving_an_out_of_range_value_reports_it_instead_of_closing(tk_root):
    saved: list[AppConfig] = []
    window = SettingsWindow(tk_root, AppConfig(), on_save=saved.append)
    tk_root.update()
    try:
        _set(window._webapi_chunk_entry, "0")

        window._save()

        assert saved == []  # must not have committed the config
        assert window.winfo_exists()  # nor closed over the correction
        assert "Chunk size" in window._save_error_label.cget("text")

        # Saving again, with the field now holding the corrected value, goes straight through.
        window._save()
        assert len(saved) == 1
        assert saved[0].web_api.chunk_size_bytes == CHUNK_SIZE_MB_MIN * 1024 * 1024
    finally:
        if window.winfo_exists():
            window.destroy()


def test_oversized_chunk_warns_when_the_server_is_behind_cloudflare(tk_root):
    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        _set(window._webapi_url_entry, "https://fireshare.example.com")
        _set(window._webapi_chunk_entry, str(cloudflare.SAFE_CHUNK_MB + 20))
        # Pre-seed the cache so the check resolves without a network probe.
        window._cloudflare_by_url["https://fireshare.example.com"] = True

        window._check_cloudflare_chunk_limit()
        tk_root.update()

        assert window._cloudflare_warning.winfo_ismapped()
    finally:
        window.destroy()


def test_oversized_chunk_is_not_flagged_when_cloudflare_is_absent(tk_root):
    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        _set(window._webapi_url_entry, "https://fireshare.example.com")
        _set(window._webapi_chunk_entry, str(cloudflare.SAFE_CHUNK_MB + 20))
        window._cloudflare_by_url["https://fireshare.example.com"] = False

        window._check_cloudflare_chunk_limit()
        tk_root.update()

        assert not window._cloudflare_warning.winfo_ismapped()
    finally:
        window.destroy()


def test_a_safe_chunk_size_never_probes_the_network(tk_root, monkeypatch):
    # The check runs on every focus change out of the URL and chunk fields, so the common case
    # (a sane chunk size) must not cost a request each time.
    def _fail(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no Cloudflare probe should run for an in-range chunk size")

    monkeypatch.setattr(cloudflare, "is_behind_cloudflare", _fail)

    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        _set(window._webapi_url_entry, "https://fireshare.example.com")
        _set(window._webapi_chunk_entry, "50")

        window._check_cloudflare_chunk_limit()
        tk_root.update()

        assert not window._cloudflare_warning.winfo_ismapped()
    finally:
        window.destroy()


def test_an_undetermined_probe_is_not_cached_as_not_cloudflare(tk_root):
    window = SettingsWindow(tk_root, AppConfig(), on_save=lambda _c: None)
    tk_root.update()
    try:
        _set(window._webapi_url_entry, "https://fireshare.example.com")
        _set(window._webapi_chunk_entry, str(cloudflare.SAFE_CHUNK_MB + 20))

        window._on_cloudflare_probe_result("https://fireshare.example.com", None)

        # An unreachable server must stay unknown, so a later probe can still find Cloudflare.
        assert "https://fireshare.example.com" not in window._cloudflare_by_url
    finally:
        window.destroy()


def test_in_range_values_save_without_complaint(tk_root):
    saved: list[AppConfig] = []
    window = SettingsWindow(tk_root, AppConfig(), on_save=saved.append)
    tk_root.update()
    try:
        _set(window._webapi_chunk_entry, "50")
        _set(window._retry_backoff_entry, "30")
        _set(window._max_retries_entry, "5")

        window._save()

        assert len(saved) == 1
        assert saved[0].web_api.chunk_size_bytes == 50 * 1024 * 1024
    finally:
        if window.winfo_exists():
            window.destroy()
