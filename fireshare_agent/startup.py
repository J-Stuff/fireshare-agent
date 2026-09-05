"""Registers/unregisters the app to launch at Windows logon via the HKCU Run key."""
from __future__ import annotations

import sys
import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "FireshareAgent"


def _get_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Running from source: launch via pythonw so no console window pops up.
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    script = sys.argv[0]
    return f'"{pythonw}" "{script}"'


def set_enabled(enabled: bool) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _get_command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass


def is_enabled() -> bool:
    """Whether the Run entry is actually present right now.

    The saved config is not evidence: the entry can be removed behind the app's back by a cleanup
    utility, another startup manager, or a manual regedit, and the config would go on claiming the
    app starts with Windows. Catches OSError rather than FileNotFoundError - a missing key and a
    missing value both raise the latter, but a permissions problem reading HKCU raises a plain
    OSError, and this is called to render a checkbox, where "cannot tell" is far better expressed
    as "not enabled" than as a traceback out of the settings window."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
    except OSError:
        return False
