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

from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.config.secrets import WEB_API_PASSWORD, delete_secret, get_secret, set_secret
from fireshare_agent.ui.settings_window import SettingsWindow


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
