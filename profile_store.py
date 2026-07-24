import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path


APP_NAME = "NetBox VM Import"


def config_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME / "config.json"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect(value):
    """Encrypt a string for the current Windows user using DPAPI."""
    if not value:
        return ""
    if sys.platform != "win32":
        raise RuntimeError("Verschlüsselte Profile werden derzeit nur unter Windows unterstützt.")
    source, source_buffer = _blob(value.encode("utf-8"))
    result = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), APP_NAME, None, None, None, 0, ctypes.byref(result)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(result.pbData, result.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def unprotect(value):
    """Decrypt a DPAPI string for the current Windows user."""
    if not value:
        return ""
    encrypted = base64.b64decode(value)
    source, source_buffer = _blob(encrypted)
    result = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_path()

    def load(self):
        if not self.path.exists():
            return {}, ""
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)

        profiles = {}
        for item in data.get("profiles", []):
            name = item.get("name", "").strip()
            if not name:
                continue
            profile = dict(item)
            profile["esxi_password"] = unprotect(item.get("esxi_password_encrypted", ""))
            profile["netbox_token"] = unprotect(item.get("netbox_token_encrypted", ""))
            profiles[name] = profile

        # Migration from the original single-profile, plaintext format.
        if not profiles and any(key in data for key in ("esxi_host", "netbox_url")):
            profiles["Standard"] = {
                "name": "Standard",
                "esxi_host": data.get("esxi_host", ""),
                "esxi_user": data.get("esxi_user", ""),
                "esxi_password": data.get("esxi_password", ""),
                "netbox_url": data.get("netbox_url", ""),
                "netbox_token": data.get("netbox_token", ""),
                "ignore_ssl_errors": bool(data.get("ignore_ssl_errors", False)),
            }
        return profiles, data.get("active_profile", "")

    def save(self, profiles, active_profile):
        records = []
        for name in sorted(profiles, key=str.casefold):
            profile = profiles[name]
            records.append({
                "name": name,
                "esxi_host": profile.get("esxi_host", ""),
                "esxi_user": profile.get("esxi_user", ""),
                "esxi_password_encrypted": protect(profile.get("esxi_password", "")),
                "netbox_url": profile.get("netbox_url", ""),
                "netbox_token_encrypted": protect(profile.get("netbox_token", "")),
                "ignore_ssl_errors": bool(profile.get("ignore_ssl_errors", False)),
            })
        payload = {"version": 2, "active_profile": active_profile, "profiles": records}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        temp_path.replace(self.path)
