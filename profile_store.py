import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path


APP_NAME = "NetBox VM Import"
SECRET_KEYS = ("password", "token_secret")


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
    """Encrypt a value for the current Windows user with DPAPI."""
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
        return base64.b64encode(ctypes.string_at(result.pbData, result.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def unprotect(value):
    """Decrypt a DPAPI value for the current Windows user."""
    if not value:
        return ""
    source, source_buffer = _blob(base64.b64decode(value))
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
        for record in data.get("profiles", []):
            name = record.get("name", "").strip()
            if not name:
                continue
            profile = {
                "name": name,
                "netbox_url": record.get("netbox_url", ""),
                "netbox_token": unprotect(record.get("netbox_token_encrypted", "")),
                "netbox_ignore_ssl": bool(record.get("netbox_ignore_ssl", record.get("ignore_ssl_errors", False))),
                "sources": [],
            }
            for source_record in record.get("sources", []):
                source = dict(source_record)
                for key in SECRET_KEYS:
                    encrypted_key = f"{key}_encrypted"
                    if encrypted_key in source:
                        source[key] = unprotect(source.pop(encrypted_key))
                profile["sources"].append(source)

            # Migration from v2: one VMware source embedded in each profile.
            if not profile["sources"] and record.get("esxi_host"):
                profile["sources"].append({
                    "id": "vmware-legacy",
                    "type": "vmware",
                    "name": "VMware",
                    "enabled": True,
                    "host": record.get("esxi_host", ""),
                    "username": record.get("esxi_user", ""),
                    "password": unprotect(record.get("esxi_password_encrypted", "")),
                    "ignore_ssl": bool(record.get("ignore_ssl_errors", False)),
                })
            profiles[name] = profile

        # Migration from the first plaintext single-profile format.
        if not profiles and any(key in data for key in ("esxi_host", "netbox_url")):
            profiles["Standard"] = {
                "name": "Standard",
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
            }
        return profiles, data.get("active_profile", "")

    def save(self, profiles, active_profile):
        records = []
        for name in sorted(profiles, key=str.casefold):
            profile = profiles[name]
            sources = []
            for source_data in profile.get("sources", []):
                source = {key: value for key, value in source_data.items() if key not in SECRET_KEYS}
                for key in SECRET_KEYS:
                    if key in source_data:
                        source[f"{key}_encrypted"] = protect(source_data.get(key, ""))
                sources.append(source)
            records.append({
                "name": name,
                "netbox_url": profile.get("netbox_url", ""),
                "netbox_token_encrypted": protect(profile.get("netbox_token", "")),
                "netbox_ignore_ssl": bool(profile.get("netbox_ignore_ssl", False)),
                "sources": sources,
            })

        payload = {"version": 3, "active_profile": active_profile, "profiles": records}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        temp_path.replace(self.path)
