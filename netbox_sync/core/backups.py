"""One-off import of switch configuration backups (no SSH).

Some small switches cannot be read over SSH in a useful way, but their web UI
exports a configuration backup. These parsers turn such a backup into the
same snapshot that :func:`netbox_sync.core.switches.build_snapshot` produces,
so the normal review and synchronization run unchanged.

Supported formats:

* D-Link DGS-1210 series (``config.bin``): a short header with the model name
  followed by a zlib-compressed dump of SNMP objects (``OID<TAB>type<TAB>value``).
* Linksys LGS series (``*.cfg``): a firmware header followed by a tar archive
  whose ``current_config/SNCSR.conf`` holds an ISS-style CLI configuration.
  A plain text ``SNCSR.conf`` is accepted as well.
"""

import io
import ipaddress
import re
import tarfile
import time
import zlib

from netbox_sync.core.switches import _interface, _membership, mask_config

BACKUP_PLATFORMS = {
    "dlink_backup": "D-Link (Backup-Datei)",
    "linksys_backup": "Linksys LGS (Backup-Datei)",
}

FILE_FILTER = "Switch-Backups (*.bin *.cfg *.conf *.txt);;Alle Dateien (*)"


class BackupError(ValueError):
    """The file is no supported switch backup."""


def parse_backup(data, filename=""):
    """Snapshot of a backup file plus ``name``, ``host`` and ``model`` from its configuration.

    Returns ``(switch, snapshot)`` where ``switch`` is a profile entry without credentials.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    dump = _dlink_dump(data)
    if dump is not None:
        return _dlink_snapshot(dump, data)
    text = _linksys_text(data)
    if text is not None:
        return _linksys_snapshot(text)
    raise BackupError(f"„{filename or 'Datei'}“ ist kein unterstütztes Backup (D-Link DGS-1210 oder Linksys LGS).")


def _snapshot(platform, interfaces, vlan_names, config_backup=""):
    ips = [{"interface": item["name"], "address": address}
           for item in interfaces for address in item["ip_addresses"]]
    return {
        "platform": platform,
        "interfaces": interfaces,
        "ip_addresses": ips,
        "neighbors": [],
        "lldp_neighbors": [],
        "cdp_neighbors": [],
        "raw": {},
        "vlans_read": True,
        "config_backup": config_backup,
        "vlan_names": vlan_names,
        "stack": [],
        "lldp_disabled": False,
        "lldp_enabled_automatically": False,
        "discovered_at": time.time(),
        "source": "backup",
    }


def _address(address, mask):
    try:
        value = ipaddress.ip_interface(f"{address}/{mask}")
    except ValueError:
        return None
    return None if value.ip.is_unspecified else str(value)


# --------------------------------------------------------------------------
# D-Link DGS-1210
# --------------------------------------------------------------------------

_DLINK_OID = re.compile(rb"^1\.3\.6\.1\.")


def _dlink_dump(data):
    """Decompressed SNMP dump of a D-Link backup, or ``None``."""
    if _DLINK_OID.match(data):
        return data
    if not data[:4] in (b"DGS-", b"DES-", b"DXS-"):
        return None
    for match in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", data[:512]):
        try:
            dump = zlib.decompressobj().decompress(data[match.start():])
        except zlib.error:
            continue
        if _DLINK_OID.match(dump):
            return dump
    raise BackupError("D-Link-Backup erkannt, aber die Konfiguration lässt sich nicht entpacken.")


def parse_snmp_dump(dump):
    """``{OID: value}``; octet strings are ``bytes`` (they may contain line breaks), the rest ``str``."""
    values, position, size = {}, 0, len(dump)
    while position < size:
        if dump[position] in b"\x00\r\n":
            position += 1
            continue
        tab = dump.find(b"\t", position)
        if tab < 0:
            break
        oid = dump[position:tab].decode("ascii", "replace").strip()
        type_end = dump.find(b"\t", tab + 1)
        line_end = dump.find(b"\n", tab + 1)
        if line_end < 0:
            line_end = size
        if type_end < 0 or type_end > line_end:
            position = line_end + 1  # unknown line format
            continue
        kind = dump[tab + 1:type_end].strip()
        if kind == b"4":
            length_end = dump.find(b"\t", type_end + 1)
            try:
                length = int(dump[type_end + 1:length_end])
            except ValueError:
                position = line_end + 1
                continue
            start = length_end + 1
            values[oid] = dump[start:start + length]
            position = start + length + 1
        else:
            values[oid] = dump[type_end + 1:line_end].decode("ascii", "replace").strip()
            position = line_end + 1
    return values


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00").strip()
    return (value or "").strip()


def _bitmap_ports(value):
    """Ports set in an SNMP PortList (byte 0, bit 7 = port 1)."""
    ports = set()
    for index, byte in enumerate(value or b""):
        for bit in range(8):
            if byte & (0x80 >> bit):
                ports.add(index * 8 + bit + 1)
    return ports


def _table(values, prefix):
    """``{last index: value}`` of all OIDs ``<prefix>.<index>``."""
    result = {}
    for oid, value in values.items():
        if oid.startswith(prefix + ".") and oid[len(prefix) + 1:].isdigit():
            result[int(oid[len(prefix) + 1:])] = value
    return result


def _dlink_snapshot(dump, data):
    values = parse_snmp_dump(dump)
    # The enterprise subtree of the model, e.g. 1.3.6.1.4.1.171.10.76.21.1 for DGS-1210-28P.
    base = next((match.group(1) for match in
                 (re.match(r"^(1\.3\.6\.1\.4\.1\.171\.10(?:\.\d+)+?)\.7\.6\.1\.1\.\d+$", oid) for oid in values)
                 if match), None)
    names, egress, untagged, pvids = {}, {}, {}, {}
    if base:
        names = _table(values, f"{base}.7.6.1.1")
        egress = _table(values, f"{base}.7.6.1.2")
        untagged = _table(values, f"{base}.7.6.1.4")
        pvids = _table(values, f"{base}.7.7.1.1")
    if not egress:  # standard Q-BRIDGE-MIB
        names = _table(values, "1.3.6.1.2.1.17.7.1.4.3.1.1")
        egress = _table(values, "1.3.6.1.2.1.17.7.1.4.3.1.2")
        untagged = _table(values, "1.3.6.1.2.1.17.7.1.4.3.1.4")
        pvids = _table(values, "1.3.6.1.2.1.17.7.1.4.5.1.1")
    admin = _table(values, "1.3.6.1.2.1.2.2.1.7")
    descriptions = _table(values, f"{base}.1.16.1.2") if base else {}
    if not descriptions:
        descriptions = _table(values, "1.3.6.1.2.1.31.1.1.1.18")
    port_numbers = sorted(set(pvids) | set(admin) | set(descriptions))
    if not port_numbers:
        raise BackupError("Im D-Link-Backup wurden keine Ports gefunden.")

    members = {port: {"untagged": set(), "tagged": set()} for port in port_numbers}
    for vid, bitmap in egress.items():
        untagged_ports = _bitmap_ports(untagged.get(vid))
        for port in _bitmap_ports(bitmap):
            if port in members:
                members[port]["untagged" if port in untagged_ports else "tagged"].add(vid)
    interfaces = []
    for port in port_numbers:
        entry = members[port]
        interfaces.append(_interface(
            str(port),
            enabled=str(admin.get(port, "1")) != "2",
            description=_text(descriptions.get(port)),
            vlans=_membership(entry["untagged"], entry["tagged"]) if entry["untagged"] or entry["tagged"] else None,
            lag=None,
        ))
    if base:
        address = _address(_text(values.get(f"{base}.2.5.2.0")), _text(values.get(f"{base}.2.5.3.0")))
        if address:
            interfaces.append(_interface(_text(values.get(f"{base}.2.5.5.0")) or "System", connected=True,
                                         description="Management-Interface", virtual=True,
                                         ip_addresses=[address], vlans=None, lag=None))
    vlan_names = {vid: _text(name) for vid, name in names.items() if _text(name)}
    model = data[:16].split(b"\x00", 1)[0].decode("ascii", "replace") if not _DLINK_OID.match(data) else ""
    name = _text(values.get("1.3.6.1.2.1.1.5.0")) or (_text(values.get(f"{base}.2.7.0")) if base else "")
    host = next((item["ip_addresses"][0].split("/")[0] for item in interfaces if item["ip_addresses"]), "")
    switch = {"name": name, "host": host, "model": model, "platform": "dlink_backup",
              "location": _text(values.get("1.3.6.1.2.1.1.6.0"))}
    return switch, _snapshot("dlink_backup", interfaces, vlan_names)


# --------------------------------------------------------------------------
# Linksys LGS (ISS CLI configuration)
# --------------------------------------------------------------------------

_LINKSYS_MEMBER = "current_config/SNCSR.conf"


def _linksys_text(data):
    """``SNCSR.conf`` of a Linksys backup, or ``None``."""
    offset = data.find(_LINKSYS_MEMBER.encode())
    if offset >= 0:
        try:
            with tarfile.open(fileobj=io.BytesIO(data[offset:]), mode="r:") as archive:
                stream = archive.extractfile(_LINKSYS_MEMBER)
                if stream is not None:
                    return stream.read().decode("utf-8", "replace")
        except (tarfile.TarError, KeyError) as error:
            raise BackupError(f"Linksys-Backup erkannt, aber nicht lesbar: {error}") from error
    head = data[:200_000].decode("utf-8", "replace")
    if re.search(r"(?m)^interface gigabitethernet \d+/\d+\s*$", head) and re.search(r"(?m)^vlan \d+\s*$", head):
        return data.decode("utf-8", "replace")
    return None


_GI_PORT = re.compile(r"(?:gigabitethernet|gi)\s*(\d+)/(\d+)(?:-(\d+))?", re.I)


def _linksys_ports(text):
    """Port numbers in ``gigabitethernet 0/1``, ``gi 0/1-4`` or comma lists."""
    ports = []
    for match in _GI_PORT.finditer(text):
        start, end = int(match.group(2)), int(match.group(3) or match.group(2))
        ports += range(start, end + 1)
    return ports


def _linksys_blocks(text):
    """``(header, lines)`` for every top-level block; ISS repeats blocks per feature."""
    header, body = None, []
    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        if line == "!":
            if header:
                yield header, body
            header, body = None, []
        elif header is None and re.match(r"^(?:interface|vlan)\s+", line, re.I):
            header, body = line, []
        elif header is not None:
            body.append(line)
    if header:
        yield header, body


def _linksys_snapshot(text):
    ports, vlan_names, vlan_members, lags = {}, {}, {}, {}
    ips = {}
    for header, body in _linksys_blocks(text):
        port = re.match(r"(?i)^interface gigabitethernet (\d+)/(\d+)$", header)
        vlan_interface = re.match(r"(?i)^interface vlan (\d+)$", header)
        vlan = re.match(r"(?i)^vlan (\d+)$", header)
        if port:
            entry = ports.setdefault(int(port.group(2)), {"enabled": True, "description": ""})
            for line in body:
                if line == "shutdown":
                    entry["enabled"] = False
                elif line == "no shutdown":
                    entry["enabled"] = True
                description = re.match(r'^description\s+"?(.*?)"?$', line)
                if description:
                    entry["description"] = description.group(1)
                group = re.match(r"^channel-group (\d+)\b", line)
                if group:
                    lags[int(port.group(2))] = f"Po{group.group(1)}"
        elif vlan_interface:
            for line in body:
                address = re.match(r"^ip address (\S+) (\S+)$", line)
                if address:
                    value = _address(address.group(1), address.group(2))
                    if value:
                        ips.setdefault(int(vlan_interface.group(1)), []).append(value)
        elif vlan:
            vid = int(vlan.group(1))
            members = vlan_members.setdefault(vid, {"untagged": set(), "tagged": set()})
            for line in body:
                name = re.match(r'^(?:ports\s+)?name\s+"?(.+?)"?$', line)
                if name:
                    vlan_names[vid] = name.group(1)
                added = re.match(r"(?i)^ports add (.+?)(?:\s+untagged\s+(.+))?$", line)
                if added:
                    untagged = set(_linksys_ports(added.group(2) or ""))
                    for number in _linksys_ports(added.group(1)):
                        members["untagged" if number in untagged else "tagged"].add(number)
    if not ports:
        raise BackupError("In der Linksys-Konfiguration wurden keine Ports gefunden.")

    interfaces = []
    for number in sorted(ports):
        untagged = {vid for vid, item in vlan_members.items() if number in item["untagged"]}
        tagged = {vid for vid, item in vlan_members.items() if number in item["tagged"]}
        interfaces.append(_interface(
            f"GE{number}",
            enabled=ports[number]["enabled"],
            description=ports[number]["description"],
            vlans=_membership(untagged, tagged) if untagged or tagged else None,
            lag=lags.get(number),
        ))
    for vid in sorted(ips):
        interfaces.append(_interface(f"vlan{vid}", connected=True, description="Management-Interface",
                                     virtual=True, ip_addresses=ips[vid], vlans=None, lag=None))
    system = {key: (re.search(rf'(?m)^system {key} "?(.*?)"?\s*$', text) or [None, ""])[1]
              for key in ("name", "location")}
    model = (re.search(r'(?m)^set switch-name "?(.*?)"?\s*$', text) or [None, ""])[1]
    host = next((item["ip_addresses"][0].split("/")[0] for item in interfaces if item["ip_addresses"]), "")
    switch = {"name": system["name"], "host": host, "model": model, "platform": "linksys_backup",
              "location": system["location"]}
    return switch, _snapshot("linksys_backup", interfaces, vlan_names, mask_config(text))
