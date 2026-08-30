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
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
