import io
import tarfile
import zlib

import pytest

from netbox_sync.core.backups import BackupError, parse_backup, parse_snmp_dump

BASE = "1.3.6.1.4.1.171.10.76.21.1"


def _octets(oid, value):
    value = value if isinstance(value, bytes) else value.encode()
    return f"{oid}\t 4\t{len(value):4d}\t".encode() + value + b"\n"


def _scalar(oid, kind, value):
    return f"{oid}\t{kind:>2}\t{value}\n".encode()


def dlink_backup():
    dump = b"".join([
        _octets("1.3.6.1.2.1.1.5.0", "SW-01"),
        _octets("1.3.6.1.2.1.1.6.0", "Keller"),
        _octets(f"{BASE}.1.16.1.2.1", "Uplink"),
        _octets(f"{BASE}.1.16.1.2.2", ""),
        _octets(f"{BASE}.1.16.1.2.3", ""),
        _octets(f"{BASE}.7.6.1.1.10", "LAN"),
        _octets(f"{BASE}.7.6.1.1.20", "VOICE"),
        # Bitmaps may contain a line break (0x0a) – the parser must honour the length.
        _octets(f"{BASE}.7.6.1.2.10", b"\xe0\x00\x00\x00"),
        _octets(f"{BASE}.7.6.1.4.10", b"\x60\x00\x00\x00"),
        _octets(f"{BASE}.7.6.1.2.20", b"\x80\x0a\x00\x00"),
        _octets(f"{BASE}.7.6.1.4.20", b"\x00\x00\x00\x00"),
        _scalar(f"{BASE}.7.7.1.1.1", 66, 10),
        _scalar(f"{BASE}.7.7.1.1.2", 66, 10),
        _scalar(f"{BASE}.7.7.1.1.3", 66, 10),
        _scalar("1.3.6.1.2.1.2.2.1.7.1", 2, 1),
        _scalar("1.3.6.1.2.1.2.2.1.7.2", 2, 1),
        _scalar("1.3.6.1.2.1.2.2.1.7.3", 2, 2),
        _scalar(f"{BASE}.2.5.2.0", 64, "10.0.0.5"),
        _scalar(f"{BASE}.2.5.3.0", 64, "255.255.255.0"),
        _octets(f"{BASE}.2.5.5.0", "System"),
    ])
    header = b"DGS-1210-28P".ljust(32, b"\x00") + b"\x00" * 16
    return header + zlib.compress(dump)


LINKSYS_CONF = """
system name "SW-02"
system location "Büro"
interface gigabitethernet 0/1
shutdown
description "AP"
!
interface gigabitethernet 0/2
shutdown
description ""
!
interface gigabitethernet 0/3
description ""
!
interface gigabitethernet 0/1
no shutdown
!
vlan 1
ports name default
!
vlan 10
ports add gigabitethernet 0/1 untagged gigabitethernet 0/1
ports add gigabitethernet 0/2
!
vlan 20
ports add gigabitethernet 0/2
!
interface vlan 1
ip address 10.0.0.6 255.255.255.0
!
snmp community name SECRET security noAuthUser
set switch-name LGS328MPC
"""


def linksys_backup():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = LINKSYS_CONF.encode()
        info = tarfile.TarInfo("current_config/SNCSR.conf")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return b"`A \x00 IMG-3.01.252 header".ljust(64, b"\x00") + buffer.getvalue()


def test_snmp_dump_keeps_binary_octets():
    values = parse_snmp_dump(_octets("1.2", b"a\nb") + _scalar("1.3", 2, 7))
    assert values == {"1.2": b"a\nb", "1.3": "7"}


def test_dlink_backup():
    switch, snapshot = parse_backup(dlink_backup(), "config.bin")
    assert switch["name"] == "SW-01" and switch["host"] == "10.0.0.5" and switch["model"] == "DGS-1210-28P"
    ports = {item["name"]: item for item in snapshot["interfaces"]}
    assert ports["1"]["description"] == "Uplink"
    assert ports["1"]["vlans"] == {"untagged": [], "tagged": [10, 20], "tagged_all": False}
    assert ports["2"]["vlans"]["untagged"] == [10]
    assert ports["3"]["enabled"] is False
    assert ports["System"]["ip_addresses"] == ["10.0.0.5/24"] and ports["System"]["virtual"]
    assert snapshot["vlan_names"] == {10: "LAN", 20: "VOICE"}


def test_linksys_backup():
    switch, snapshot = parse_backup(linksys_backup(), "x.cfg")
    assert switch == {"name": "SW-02", "host": "10.0.0.6", "model": "LGS328MPC", "platform": "linksys_backup",
                      "location": "Büro"}
    ports = {item["name"]: item for item in snapshot["interfaces"]}
    assert ports["GE1"]["enabled"] is True and ports["GE1"]["description"] == "AP"
    assert ports["GE1"]["vlans"]["untagged"] == [10]
    assert ports["GE2"]["enabled"] is False
    assert ports["GE2"]["vlans"] == {"untagged": [], "tagged": [10, 20], "tagged_all": False}
    assert ports["GE3"]["vlans"] is None
    assert ports["vlan1"]["ip_addresses"] == ["10.0.0.6/24"]
    assert "SECRET" not in snapshot["config_backup"]


def test_linksys_plain_text():
    switch, _snapshot = parse_backup(LINKSYS_CONF, "SNCSR.conf")
    assert switch["name"] == "SW-02"


def test_unknown_file():
    with pytest.raises(BackupError):
        parse_backup(b"hello world", "x.bin")
