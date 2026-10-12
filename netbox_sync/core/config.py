"""Customer profiles, application settings and DPAPI-protected secrets."""

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

from netbox_sync import CONFIG_DIR_NAME

SECRET_KEYS = ("password", "token_secret")
CONFIG_VERSION = 5

DEFAULT_SETTINGS = {
    "theme": "system",            # system | light | dark
    "parallel_ssh": 4,            # concurrent switch discoveries
    "enable_lldp": False,         # allow configuration changes on switches
    "create_cables": True,
}

log = logging.getLogger(__name__)


def config_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / CONFIG_DIR_NAME


def config_path():
    return config_dir() / "config.json"


def log_dir():
    return config_dir() / "logs"


def known_hosts_path():
    return config_dir() / "known_hosts"


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(data):
        buffer = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class SecretError(RuntimeError):
    """A stored secret cannot be decrypted by the current Windows user."""


def protect(value):
    """Encrypt a value for the current Windows user with DPAPI."""
    if not value:
        return ""
    if sys.platform != "win32":
        raise RuntimeError("Verschlüsselte Profile werden derzeit nur unter Windows unterstützt.")
    source, _buffer = _blob(value.encode("utf-8"))
    result = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "NetBox VM Import", None, None, None, 0, ctypes.byref(result)
    ):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(result.pbData, result.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def unprotect(value):
    """Decrypt a DPAPI value for the current Windows user."""
    if not value:
        return ""
    if sys.platform != "win32":
        raise SecretError("DPAPI ist nur unter Windows verfügbar.")
    source, _buffer = _blob(base64.b64decode(value))
    result = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
    ):
        raise SecretError("Gespeichertes Geheimnis kann mit diesem Windows-Konto nicht entschlüsselt werden.")
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


# --------------------------------------------------------------------------
# Profile store
# --------------------------------------------------------------------------

def empty_profile(name):
    return {
        "name": name,
        "netbox_url": "",
        "netbox_token": "",
        "netbox_ignore_ssl": False,
        "sources": [],
        "switches": [],
    }


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_path()
        self.decrypt_failures = []

    def _decrypt(self, value, context):
        try:
            return unprotect(value)
        except Exception as error:  # noqa: BLE001 - a broken secret must not block the app
            self.decrypt_failures.append(context)
            log.warning("Secret für %s nicht lesbar: %s", context, error)
            return ""

    def _decrypt_record(self, record, context):
        item = dict(record)
        for key in SECRET_KEYS:
            encrypted_key = f"{key}_encrypted"
            if encrypted_key in item:
                item[key] = self._decrypt(item.pop(encrypted_key), context)
        return item

    def load(self):
        """Return ``(profiles, active_profile, settings)``."""
        self.decrypt_failures = []
        settings = dict(DEFAULT_SETTINGS)
        if not self.path.exists():
            return {}, "", settings
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        settings.update({key: value for key, value in data.get("settings", {}).items() if key in DEFAULT_SETTINGS})

        profiles = {}
        for record in data.get("profiles", []):
            name = record.get("name", "").strip()
            if not name:
                continue
            profile = empty_profile(name)
            profile.update({
                "netbox_url": record.get("netbox_url", ""),
                "netbox_token": self._decrypt(record.get("netbox_token_encrypted", ""), f"{name}: NetBox-Token"),
                "netbox_ignore_ssl": bool(record.get("netbox_ignore_ssl", record.get("ignore_ssl_errors", False))),
            })
            for source in record.get("sources", []):
                profile["sources"].append(self._decrypt_record(source, f"{name}: {source.get('name', 'Quelle')}"))
            for switch in record.get("switches", []):
                profile["switches"].append(self._decrypt_record(switch, f"{name}: {switch.get('name', 'Switch')}"))

            # Migration from v2: one VMware source embedded in each profile.
            if not profile["sources"] and record.get("esxi_host"):
                profile["sources"].append({
                    "id": "vmware-legacy",
                    "type": "vmware",
                    "name": "VMware",
                    "enabled": True,
                    "host": record.get("esxi_host", ""),
                    "username": record.get("esxi_user", ""),
                    "password": self._decrypt(record.get("esxi_password_encrypted", ""), f"{name}: VMware"),
                    "ignore_ssl": bool(record.get("ignore_ssl_errors", False)),
                })
            profiles[name] = profile

        # Migration from the first plaintext single-profile format.
        if not profiles and any(key in data for key in ("esxi_host", "netbox_url")):
            profile = empty_profile("Standard")
            profile.update({
                "netbox_url": data.get("netbox_url", ""),
                "netbox_token": data.get("netbox_token", ""),
                "netbox_ignore_ssl": bool(data.get("ignore_ssl_errors", False)),
                "sources": [{
                    "id": "vmware-legacy",
                    "type": "vmware",
                    "name": "VMware",
                    "enabled": True,
                    "host": data.get("esxi_host", ""),
                    "username": data.get("esxi_user", ""),
                    "password": data.get("esxi_password", ""),
                    "ignore_ssl": bool(data.get("ignore_ssl_errors", False)),
                }],
            })
            profiles["Standard"] = profile
        return profiles, data.get("active_profile", ""), settings

    @staticmethod
    def _encrypt_record(record):
        item = {key: value for key, value in record.items() if key not in SECRET_KEYS and not key.startswith("_")}
        for key in SECRET_KEYS:
            if key in record:
                item[f"{key}_encrypted"] = protect(record.get(key, ""))
        return item

    def save(self, profiles, active_profile, settings=None):
        records = []
        for name in sorted(profiles, key=str.casefold):
            profile = profiles[name]
            records.append({
                "name": name,
                "netbox_url": profile.get("netbox_url", ""),
                "netbox_token_encrypted": protect(profile.get("netbox_token", "")),
                "netbox_ignore_ssl": bool(profile.get("netbox_ignore_ssl", False)),
                "sources": [self._encrypt_record(source) for source in profile.get("sources", [])],
                "switches": [self._encrypt_record(switch) for switch in profile.get("switches", [])],
            })

        payload = {
            "version": CONFIG_VERSION,
            "active_profile": active_profile,
            "settings": dict(settings or DEFAULT_SETTINGS),
            "profiles": records,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        temp_path.replace(self.path)
