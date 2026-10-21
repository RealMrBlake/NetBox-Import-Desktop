import json

import pytest

from netbox_sync.core import exchange


def _vm(**values):
    vm = {"id": "src:0:web01", "source_id": "src", "name": "web01", "source": "vcenter", "source_type": "VMware",
          "kind": "VM", "status": "active", "vcpus": 2, "memory": 4096, "disk": 51200, "guest_os": "Linux",
          "cluster": "CL1", "interfaces": [{"name": "eth0", "mac": "00:50:56:00:00:01", "ips": ["10.0.0.5/24"]}]}
    vm.update(values)
    return vm


def _switch(**values):
    switch = {"id": "s1", "name": "sw01", "device_id": 7, "platform": "aruba_cx", "host": "10.0.0.2", "port": 22,
              "username": "admin", "password": "geheim", "enabled": True}
    switch.update(values)
    return switch


SNAPSHOT = {"platform": "aruba_cx", "interfaces": [{"name": "1/1/1"}], "discovered_at": 1.0}


def test_round_trip_strips_credentials(tmp_path):
    path = tmp_path / "export.nbsync.json"
    exchange.write_export(path, exchange.build_export([_vm()], [(_switch(), SNAPSHOT)], "Kunde"))
    text = path.read_text(encoding="utf-8")
    assert "geheim" not in text and "admin" not in text
    data = exchange.read_export(path)
    assert data["profile"] == "Kunde"
    assert "id" not in data["vms"][0] and data["vms"][0]["cluster"] == "CL1"
    assert data["switches"][0]["switch"] == {"id": "s1", "name": "sw01", "device_id": 7, "platform": "aruba_cx",
                                             "host": "10.0.0.2", "port": 22}


def test_prepare_vms_assigns_runtime_ids():
    vms = exchange.prepare_vms([{"name": "a", "source": "vc"}, {"name": "a", "source": "vc"}])
    assert vms[0]["source_id"] == "file:vc"
    assert vms[0]["id"] != vms[1]["id"]


@pytest.mark.parametrize("payload", [
    {"format": "other"},
    {"format": exchange.FORMAT, "version": exchange.FORMAT_VERSION + 1},
    {"format": exchange.FORMAT, "version": 1, "vms": [{"name": "x"}]},
    {"format": exchange.FORMAT, "version": 1, "switches": [{"switch": {"name": "x"}}]},
])
def test_invalid_files_are_rejected(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(exchange.ExchangeError):
        exchange.read_export(path)


def test_merge_switches_matches_by_device_then_name():
    profile = [_switch(id="a", device_id=7, name="old-name"), _switch(id="b", device_id=None, name="SW02")]
    items = [
        {"switch": {"name": "sw01", "device_id": 7}, "snapshot": {"n": 1}},
        {"switch": {"name": "sw02"}, "snapshot": {"n": 2}},
        {"switch": {"name": "sw03", "host": "10.0.0.3"}, "snapshot": {"n": 3}},
    ]
    snapshots, added = exchange.merge_switches(profile, items, lambda: "new")
    assert snapshots == {"a": {"n": 1}, "b": {"n": 2}, "new": {"n": 3}}
    assert added == 1
    assert profile[-1]["name"] == "sw03" and profile[-1]["enabled"] is False and profile[-1]["password"] == ""
