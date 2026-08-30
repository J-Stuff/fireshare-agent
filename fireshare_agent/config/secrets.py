"""
Secret storage for passwords/passphrases, backed by Windows Credential Manager via `keyring`.

Secrets are never written to config.json - only a fixed keyring key per settings slot is used
(there is only ever one active configuration, so no per-profile key namespacing is needed).
This matters because this app is meant to be distributed to other Fireshare users, not just
run from one person's trusted machine, so plaintext-in-JSON was never an acceptable default.
"""
from __future__ import annotations

import keyring

_SERVICE_NAME = "FireshareAgent"

WEB_API_PASSWORD = "web_api_password"

# Fireshare's login sets a Flask-Login "remember me" cookie alongside the session cookie -
# persisting it here lets the agent skip the full login (and any TOTP prompt) on every restart,
# only falling back to a fresh login once that cookie actually expires or is invalidated.
WEB_API_SESSION_COOKIES = "web_api_session_cookies"


def set_secret(key: str, value: str | None) -> None:
    if not value:
        delete_secret(key)
        return
    keyring.set_password(_SERVICE_NAME, key, value)


def get_secret(key: str) -> str | None:
    try:
        return keyring.get_password(_SERVICE_NAME, key)
    except keyring.errors.KeyringError:
        return None


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(_SERVICE_NAME, key)
    except keyring.errors.PasswordDeleteError:
        pass
