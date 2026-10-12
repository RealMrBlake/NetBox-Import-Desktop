import json
import sys

import pytest

from netbox_sync.core.config import DEFAULT_SETTINGS, ProfileStore

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


def test_roundtrip_encrypts_secrets(tmp_path):
    store = ProfileStore(tmp_path / "config.json")
    profiles = {
        "Kunde A": {
            "name": "Kunde A",
            "netbox_url": "https://netbox.example",
            "netbox_token": "token-123",
            "netbox_ignore_ssl": True,
            "sources": [{"id": "s1", "type": "proxmox", "name": "PVE", "url": "https://pve", "token_id": "a@pve!b",
                         "token_secret": "geheim", "enabled": True}],
            "switches": [{"id": "w1", "name": "sw1", "device_id": 5, "platform": "auto", "host": "10.0.0.1",
                          "port": 22, "username": "admin", "password": "pw", "enabled": True, "_runtime": "x"}],
        }
    }
    settings = dict(DEFAULT_SETTINGS, theme="dark")
    store.save(profiles, "Kunde A", settings)

    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "token-123" not in raw and "geheim" not in raw and '"pw"' not in raw
    assert "_runtime" not in raw

    loaded, active, loaded_settings = store.load()
    assert active == "Kunde A"
    assert loaded_settings["theme"] == "dark"
    assert loaded["Kunde A"]["netbox_token"] == "token-123"
    assert loaded["Kunde A"]["sources"][0]["token_secret"] == "geheim"
    assert loaded["Kunde A"]["switches"][0]["password"] == "pw"


def test_unreadable_secret_does_not_block_loading(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "version": 4,
        "active_profile": "X",
        "profiles": [{"name": "X", "netbox_url": "u", "netbox_token_encrypted": "AAAA", "sources": [], "switches": []}],
    }), encoding="utf-8")
    store = ProfileStore(path)
    profiles, _active, settings = store.load()
    assert profiles["X"]["netbox_token"] == ""
    assert store.decrypt_failures
    assert settings == DEFAULT_SETTINGS


def test_legacy_v1_migration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"esxi_host": "vc", "esxi_user": "u", "esxi_password": "p", "netbox_url": "nb"}),
                    encoding="utf-8")
    profiles, _active, _settings = ProfileStore(path).load()
    assert profiles["Standard"]["sources"][0]["host"] == "vc"
