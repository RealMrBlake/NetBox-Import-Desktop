"""Customer profiles, application settings and DPAPI-protected secrets."""

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

from netbox_sync import CONFIG_DIR_NAME
from netbox_sync.core.clients import DEFAULT_CLIENT_OPTIONS

SECRET_KEYS = ("password", "token_secret")
CONFIG_VERSION = 5

DEFAULT_SETTINGS = {
    "theme": "system",            # system | light | dark
    "parallel_ssh": 4,            # concurrent switch discoveries
    "enable_lldp": False,         # allow configuration changes on switches
    "create_cables": True,
    "offline_mode": False,        # collect and export to JSON without NetBox
    "parallel_wmi": 8,            # concurrent WinRM sessions for client scans
    "auto_update_check": True,    # look for a newer Gitea release on start
    "skipped_version": "",        # release the user chose to skip
    "scanner_targets": "",        # last IP ranges of the network scanner
    "scanner_ports": "",          # empty = default port list
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
        # Remembered assignment "<source id>:<hypervisor cluster>" -> NetBox cluster name.
        "cluster_map": {},
        "host_map": {},
        # Switch VLAN decisions for all switches: "<vid>" -> {"action": "map"|"new"|"skip"|"ignore", ...}.
        "vlan_map": {},
        # Switch network decisions: "<network>" -> {"vrf_id", "vrf_name" (new VRF), "create": prefix}.
        "network_map": {},
        # Windows client scan: targets and options, never a password.
        "clients": dict(DEFAULT_CLIENT_OPTIONS),
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
                "cluster_map": dict(record.get("cluster_map", {})),
                "host_map": dict(record.get("host_map", {})),
                "vlan_map": dict(record.get("vlan_map", {})),
                "network_map": dict(record.get("network_map", {})),
                "clients": {**DEFAULT_CLIENT_OPTIONS, **{key: value for key, value in record.get("clients", {}).items()
                                                         if key in DEFAULT_CLIENT_OPTIONS}},
            })
            for source in record.get("sources", []):
                profile["sources"].append(self._decrypt_record(source, f"{name}: {source.get('name', 'Quelle')}"))
            for switch in record.get("switches", []):
                profile["switches"].append(self._decrypt_record(switch, f"{name}: {switch.get('name', 'Switch')}"))
            # Migration from 4.5/4.6: VLAN and network decisions were stored per switch.
            for switch in profile["switches"]:
                for vid, choice in (switch.pop("vlan_map", None) or {}).items():
                    profile["vlan_map"].setdefault(vid, choice)
                switch.pop("create_vlans", None)
                approved = switch.pop("create_prefixes", None) or []
                for network, vrf_name in (switch.pop("network_vrfs", None) or {}).items():
                    profile["network_map"].setdefault(network, {"vrf_id": None, "vrf_name": vrf_name or "",
                                                                "create": network in approved})

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
                "cluster_map": dict(profile.get("cluster_map", {})),
                "host_map": dict(profile.get("host_map", {})),
                "vlan_map": dict(profile.get("vlan_map", {})),
                "network_map": dict(profile.get("network_map", {})),
                "clients": dict(profile.get("clients", DEFAULT_CLIENT_OPTIONS)),
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
