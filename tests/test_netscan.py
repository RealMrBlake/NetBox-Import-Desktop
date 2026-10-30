import ipaddress
import struct
from types import SimpleNamespace

import pytest
from fake_netbox import FakeNetBox, Record

from netbox_sync.core import netscan


def test_ports_and_ranges_are_parsed():
    assert netscan.parse_ports("21-23, 80;443 80") == [21, 22, 23, 80, 443]
    with pytest.raises(netscan.ScanError):
        netscan.parse_ports("70000")
    assert netscan.expand_ips("192.0.2.1-3, 192.0.2.2") == ["192.0.2.1", "192.0.2.2", "192.0.2.3"]
    assert len(netscan.expand_ips("192.0.2.0/24")) == 254
    with pytest.raises(netscan.ScanError):
        netscan.expand_ips("10.0.0.0/16")


def _nbstat_answer(names, mac):
    header = b"\x13\x37\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    question = b"\x20" + b"CK" + b"A" * 30 + b"\x00" + b"\x00\x21\x00\x01"
    entries = b"".join(name.ljust(15).encode() + bytes([suffix]) + struct.pack(">H", flags)
                       for name, suffix, flags in names)
    body = bytes([len(names)]) + entries + mac
    return header + question + b"\x00\x00\x00\x00" + struct.pack(">H", len(body)) + body


def test_netbios_answer_gives_name_workgroup_and_mac():
    data = _nbstat_answer([("PC-042", 0x00, 0x0400), ("KDS", 0x00, 0x8400), ("PC-042", 0x20, 0x0400)],
                          bytes.fromhex("001122aabbcc"))
    assert netscan.parse_nbstat(data) == ("PC-042", "KDS", "00:11:22:AA:BB:CC")
    assert netscan.parse_nbstat(b"\x00" * 10) == ("", "", None)


def test_netbox_entries_are_found_per_block_and_compared():
    nb = FakeNetBox()
    nb.ipam.ip_addresses.records += [
        Record(nb.ipam.ip_addresses, id=1, address="192.0.2.10/24", dns_name="srv01.example.local",
               vrf=None, tenant=SimpleNamespace(id=7, name="Muster"), status=SimpleNamespace(label="Aktiv"),
               description="", assigned_object=SimpleNamespace(virtual_machine=SimpleNamespace(name="SRV01"))),
        Record(nb.ipam.ip_addresses, id=2, address="192.0.2.20/24", dns_name="", vrf=None, tenant=None,
               status=SimpleNamespace(label="Aktiv"), description="",
               assigned_object=SimpleNamespace(device=SimpleNamespace(name="drucker-3og"))),
        Record(nb.ipam.ip_addresses, id=3, address="198.51.100.1/24", dns_name="", vrf=None, tenant=None,
               status=None, description="", assigned_object=None),
    ]
    queried = []

    def by_parent(parent):
        queried.append(parent)
        network = ipaddress.ip_network(parent)
        return [record for record in nb.ipam.ip_addresses.records
                if ipaddress.ip_interface(record.address).ip in network]
    nb.ipam.ip_addresses.filter = by_parent

    found = netscan.netbox_lookup(nb, ["192.0.2.10", "192.0.2.20", "192.0.2.30"])
    assert queried == ["192.0.2.0/24"], "one request per /24"
    assert found["192.0.2.10"][0]["object"] == "SRV01" and found["192.0.2.10"][0]["object_kind"] == "VM"
    assert found["192.0.2.20"][0]["object"] == "drucker-3og"
    assert "192.0.2.30" not in found

    alive = {"alive": True, "dns_name": "SRV01.example.local", "netbios_name": "SRV01"}
    assert netscan.compare(alive, found["192.0.2.10"]) == "match"
    assert netscan.compare(dict(alive, dns_name="pc99", netbios_name=""), found["192.0.2.20"]) == "differs"
    assert netscan.compare(alive, []) == "missing"
    assert netscan.compare({"alive": False}, found["192.0.2.20"]) == "offline"
    assert netscan.compare({"alive": False}, []) == "unknown"


def test_unreachable_host_is_reported_without_details(monkeypatch):
    monkeypatch.setattr(netscan, "ping", lambda *_args: None)
    monkeypatch.setattr(netscan, "arp_mac", lambda _ip: None)
    monkeypatch.setattr(netscan, "tcp_open", lambda *_args: False)
    assert netscan.scan_host("192.0.2.1", [80]) == {"ip": "192.0.2.1", "alive": False}


def test_host_behind_firewall_is_found_by_port(monkeypatch):
    monkeypatch.setattr(netscan, "ping", lambda *_args: None)
    monkeypatch.setattr(netscan, "arp_mac", lambda _ip: None)
    monkeypatch.setattr(netscan, "tcp_open", lambda _ip, port, _timeout=0: port in (445, 3389))
    monkeypatch.setattr(netscan, "netbios", lambda _ip: ("PC-1", "KDS", "00:11:22:33:44:55"))
    monkeypatch.setattr(netscan, "reverse_dns", lambda _ip: "pc-1.kds.local")
    host = netscan.scan_host("192.0.2.5", [80, 445, 3389])
    assert host["alive"] and [item["port"] for item in host["ports"]] == [445, 3389]
    assert host["mac"] == "00:11:22:33:44:55" and host["netbios_name"] == "PC-1"
