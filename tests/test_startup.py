"""
Coverage for the Windows "start at logon" Run-key helper and the checkbox that renders it.

`startup.is_enabled()` existed but was never called - the Settings window rendered the checkbox
from `config.start_with_windows` instead. So if the Run entry was removed behind the app's back (a
cleanup utility, another startup manager, a manual regedit), the checkbox went on claiming the app
starts with Windows when it no longer did.

These tests never touch the real HKCU Run key: the registry calls are stubbed, so running the suite
cannot register or unregister the user's actual startup entry.
"""
import winreg

import pytest

from fireshare_agent import startup
from fireshare_agent.config.app_config import AppConfig
from fireshare_agent.ui.settings_window import _start_with_windows_state


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_is_enabled_is_true_when_the_run_value_exists(monkeypatch):
    monkeypatch.setattr(startup.winreg, "OpenKey", lambda *a, **k: _FakeKey())
    monkeypatch.setattr(startup.winreg, "QueryValueEx", lambda key, name: ("cmd", winreg.REG_SZ))

    assert startup.is_enabled() is True


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "key missing"),
        PermissionError(5, "access is denied"),
        OSError(1, "something else went wrong"),
    ],
)
def test_is_enabled_reports_false_rather_than_raising(monkeypatch, error):
    """It only caught FileNotFoundError. A missing key and a missing value both raise that, but a
    permissions problem raises a plain OSError - and this is called to render a checkbox, where a
    traceback out of the settings window is much worse than answering "not enabled"."""

    def _raise(*args, **kwargs):
        raise error

    monkeypatch.setattr(startup.winreg, "OpenKey", _raise)

    assert startup.is_enabled() is False


def test_the_checkbox_reflects_the_registry_not_the_saved_config(monkeypatch):
    """The drift the fix is about: the config still says True, the Run entry is gone, and the
    checkbox must show what is actually true."""
    monkeypatch.setattr(startup, "is_enabled", lambda: False)

    assert _start_with_windows_state(AppConfig(start_with_windows=True)) is False


def test_the_checkbox_also_reflects_an_entry_added_behind_the_apps_back(monkeypatch):
    monkeypatch.setattr(startup, "is_enabled", lambda: True)

    assert _start_with_windows_state(AppConfig(start_with_windows=False)) is True


def test_the_saved_config_is_the_fallback_when_the_registry_is_unreadable(monkeypatch):
    """`startup` imports winreg at module scope, so on a non-Windows host the import itself fails.
    That is the one case where the saved config is the best answer available."""

    def _raise() -> bool:
        raise ImportError("no winreg here")

    monkeypatch.setattr(startup, "is_enabled", _raise)

    assert _start_with_windows_state(AppConfig(start_with_windows=True)) is True
