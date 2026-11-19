"""Secrets (the Plex token, subtitle-site credentials): never in config.toml, never in logs.

A secret is read from the environment first (``CINEMIND_<NAME>``, handy for Docker and CI),
then from the operating system's keyring: Windows Credential Manager, the macOS Keychain or the
Secret Service on Linux. ``cinemind secrets set NAME`` stores one.
"""

from __future__ import annotations

import os

SERVICE = "cinemind"
KNOWN = {
    "plex": "Plex token (X-Plex-Token), for library refreshes",
    "opensubtitles_api_key": "OpenSubtitles API key",
    "opensubtitles_password": "OpenSubtitles password (the username goes in config.toml)",
}


class SecretsUnavailable(RuntimeError):
    pass


def env_name(name: str) -> str:
    return f"CINEMIND_{name.upper()}"


def get_secret(name: str) -> str | None:
    if value := os.environ.get(env_name(name)):
        return value
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def set_secret(name: str, value: str) -> None:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        raise SecretsUnavailable(
            f"No keyring is available; set {env_name(name)} in the environment instead."
        ) from exc
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        hint = f"set {env_name(name)} in the environment instead"
        raise SecretsUnavailable(f"{exc}; {hint}.") from exc


def delete_secret(name: str) -> bool:
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
    except ImportError:
        return False
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except PasswordDeleteError:
        return False
