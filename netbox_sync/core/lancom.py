"""LANCOM switches with the LCOS SX 3.x mode-based CLI (e.g. GS-2326P+, GS-2310P+).

There is no ``show running-config``: every area is a mode (``port``, ``vlan``,
``lldp``, ``system``, ``ip``) with its own ``show`` command, left with ``exit``.
The outputs are converted into the snapshot of :mod:`netbox_sync.core.switches`.
"""

import ipaddress
import re
import time

from netbox_sync.core.switches import (
    CLI_ERROR,
    _interface,
    _compress_vlans,
    _membership,
    _run_cli_command,
    clean_output,
    parse_vlan_list,
)

PLATFORM = "lancom_sx3"

# (mode, command, output key)
COMMANDS = (
    ("system", "show", "system"),
    ("port", "show status", "status"),
    ("port", "show configuration", "port_config"),
    ("vlan", "show vlan", "vlans"),
    ("vlan", "show port-config", "vlan_ports"),
    ("ip", "show", "ip"),
    ("lldp", "show config", "lldp_config"),
    # "{ports}" becomes the port list from "show status", e.g. "1-26".
    ("lldp", "show info {ports}", "neighbors"),
)


def is_lancom(text):
    return bool(re.search(r"(?i)Model Name\s*:\s*LANCOM", text or ""))


# Neighbors usually send LLDP every 30 seconds; after enabling reception we wait for one round.
LLDP_WAIT_SECONDS = 35


def run_commands(channel, log=lambda *_args: None, name="", enable_lldp=False, wait=LLDP_WAIT_SECONDS):
    """Run :data:`COMMANDS` in their modes; a missing command only leaves its output empty.

    With ``enable_lldp`` ports that do not receive LLDP are switched to ``enable``
    (running configuration only, nothing is saved).
    """
    outputs, mode = {"lldp_enabled_automatically": False}, None
    for wanted, command, key in COMMANDS:
        if mode != wanted:
            if mode:
                _run_cli_command(channel, "exit")
            _run_cli_command(channel, wanted)
            mode = wanted
        if "{ports}" in command:
            numbers = sorted(parse_status(outputs.get("status", "")))
            if not numbers:
                outputs[key] = ""
                continue
            command = command.format(ports=_compress_vlans(numbers))
        output = _run_cli_command(channel, command, timeout=60)
        if CLI_ERROR.search(output):
            log(f"{name}: „{wanted} → {command}“ wird nicht unterstützt")
            output = ""
        outputs[key] = output
        if key == "lldp_config" and enable_lldp:
            ports = not_receiving(output)
            if ports:
                result = _run_cli_command(channel, f"mode {_compress_vlans(ports)} enable")
                if CLI_ERROR.search(result) or re.search(r"(?i)permission|denied|privilege", result):
                    raise RuntimeError(f"LLDP konnte nicht aktiviert werden: {clean_output(result).strip()[:200]}")
                outputs["lldp_enabled_automatically"] = True
                log(f"{name}: LLDP auf Port(s) {_compress_vlans(ports)} aktiviert – warte {wait} s auf Nachbarn …")
                time.sleep(wait)
                outputs[key] = _run_cli_command(channel, "show config", timeout=60)
    if mode:
        _run_cli_command(channel, "exit")
    return outputs


def _field(text, label):
    match = re.search(rf"(?im)^{re.escape(label)}\s*:\s*(.*?)\s*$", text or "")
    return match.group(1) if match else ""


def _speed(text):
    match = re.match(r"(?i)^(\d+(?:\.\d+)?)(G|M)?$", text or "")
    if not match:
        return None
    return int(float(match.group(1)) * (1000 if (match.group(2) or "").upper() == "G" else 1))


def parse_status(output):
    """``show status``: ``2/UTP  Enabled  Up  Enabled  1G Full ...``."""
    ports = {}
    for line in clean_output(output).splitlines():
        match = re.match(r"^\s*(\d+)/(\w+)\s+(Enabled|Disabled)\s+(Up|Down)\s+\S+\s+(\S+)", line, re.I)
        if match:
            up = match.group(4).lower() == "up"
            ports[int(match.group(1))] = {
                "enabled": match.group(3).lower() == "enabled", "connected": up,
                "speed_mbps": _speed(match.group(5)) if up else None,
                # Copper ports of these switches are Gigabit; the combo ports report SFP while a module is active.
                "media": "SFP" if match.group(2).upper() == "SFP" else "1000T",
            }
    return ports


def parse_port_config(output):
    """``show configuration``: a port line followed by its description line."""
    ports, current = {}, None
    for line in clean_output(output).splitlines():
        match = re.match(r"^\s*(\d+)\s+(Enabled|Disabled)\s", line, re.I)
        if match:
            current = int(match.group(1))
            ports[current] = {"enabled": match.group(2).lower() == "enabled", "description": ""}
        elif current is not None and line.strip() and not set(line.strip()) <= {"-"}:
            ports[current]["description"] = line.strip()
            current = None
        elif current is not None and set(line.strip()) <= {"-"} and line.strip():
            current = None
    return ports


def parse_vlans(output):
    """``show vlan``: ``({vid: name}, {vid: {ports}})``."""
    names, members, current = {}, {}, None
    for line in clean_output(output).splitlines():
        match = re.match(r"^\s*(\d+)\s+(.*?)\s+(Static|Combined|GVRP|MSTP|MVR|NAS|VCL|Voice)\b\s*(.*)$", line, re.I)
        if match:
            current = int(match.group(1))
            if match.group(2).strip():
                names[current] = match.group(2).strip()
            members[current] = parse_vlan_list(match.group(4))
        elif current is not None and re.match(r"^\s+[\d,\-\s]+$", line):
            members[current] |= parse_vlan_list(line)  # wrapped port list
        else:
            current = None
    return names, members


def parse_vlan_ports(output):
    """``show port-config``: ``{port: (pvid, egress rule)}``."""
    ports = {}
    for line in clean_output(output).splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+\S+\s+\S+\s+(Access|Hybrid|Trunk)\b", line, re.I)
        if match:
            ports[int(match.group(1))] = (int(match.group(2)), match.group(3).lower())
    return ports


def port_membership(vids, pvid, rule):
    """Access: untagged; Trunk: all tagged; Hybrid: the PVID untagged, the rest tagged."""
    if not vids:
        return None
    if rule == "access":
        return _membership(vids)
    if rule == "trunk":
        return _membership((), vids)
    return _membership({pvid} & vids, vids - {pvid})


def parse_ip(output):
    """``ip → show``: management address with its VLAN, using the "Current" column."""
    def current(label):
        values = _field(output, label).split()
        return values[-1] if values else ""
    try:
        address = ipaddress.ip_interface(f"{current('IP Address')}/{current('IP Mask')}")
    except ValueError:
        return None, None
    vlan = current("VLAN ID")
    return (None if address.ip.is_unspecified else str(address)), (int(vlan) if vlan.isdigit() else None)


def _lldp_modes(output):
    """``lldp → show config``: ``{port: mode}``."""
    return {int(port): mode.lower() for port, mode in re.findall(
        r"(?im)^\s*(\d+)\s+(Enabled|Disabled|Rx[ _-]?only|Tx[ _-]?only)\b", clean_output(output or ""))}


def not_receiving(output):
    """Ports that drop received LLDP (mode Disabled or Tx only)."""
    return sorted(port for port, mode in _lldp_modes(output).items() if mode == "disabled" or mode.startswith("tx"))


def lldp_receiving(output):
    """``False`` when no port receives LLDP; ``True`` when unknown (no warning)."""
    modes = _lldp_modes(output)
    return not modes or len(not_receiving(output)) < len(modes)


def parse_neighbors(output):
    """``lldp → show info``: one block per neighbor, ``Label : value`` lines."""
    neighbors = []
    blocks = re.split(r"(?im)^(?=\s*Local Port\s*:)", clean_output(output or ""))
    for block in blocks:
        local = _field(block, "Local Port")
        if not local:
            continue
        port = re.match(r"(\d+)", local)
        neighbors.append({
            "local_port": port.group(1) if port else local,
            "system_name": _field(block, "System Name"),
            "remote_port": _field(block, "Port ID") or _field(block, "Port Description"),
            "port_description": _field(block, "Port Description"),
            "management_ip": (re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", _field(block, "Management Address"))
                              or [""])[0],
            "chassis_id": _field(block, "Chassis ID"),
            "protocol": "LLDP",
        })
    return neighbors


def build_snapshot(outputs):
    status = parse_status(outputs.get("status", ""))
    config = parse_port_config(outputs.get("port_config", ""))
    names, members = parse_vlans(outputs.get("vlans", ""))
    vlan_ports = parse_vlan_ports(outputs.get("vlan_ports", ""))
    vlans_read = bool(members and vlan_ports)
    interfaces = []
    for number in sorted(set(status) | set(config)):
        vids = {vid for vid, ports in members.items() if number in ports}
        pvid, rule = vlan_ports.get(number, (1, "hybrid"))
        values = dict(status.get(number, {}))
        values["enabled"] = config.get(number, {}).get("enabled", values.get("enabled", True))
        interfaces.append(_interface(str(number), description=config.get(number, {}).get("description", ""),
                                     vlans=port_membership(vids, pvid, rule) if vlans_read else None,
                                     lag=None, **values))
    address, vlan = parse_ip(outputs.get("ip", ""))
    ip_addresses = []
    if address:
        name = f"vlan{vlan}" if vlan else "System"
        interfaces.append(_interface(name, connected=True, description="Management-Interface", virtual=True,
                                     ip_addresses=[address], vlans=None, lag=None))
        ip_addresses.append({"interface": name, "address": address})
    neighbors = parse_neighbors(outputs.get("neighbors", ""))
    by_name = {item["name"]: item for item in interfaces}
    for neighbor in neighbors:
        if neighbor["local_port"] in by_name:
            by_name[neighbor["local_port"]]["neighbor"] = neighbor
    system = outputs.get("system", "")
    mac = _field(system, "Host MAC Address")
    raw = {key: value for key, value in outputs.items() if key != "platform" and isinstance(value, str)}
    return {
        "platform": PLATFORM,
        "interfaces": interfaces,
        "ip_addresses": ip_addresses,
        "neighbors": neighbors,
        "lldp_neighbors": neighbors,
        "cdp_neighbors": [],
        "raw": raw,
        "vlans_read": vlans_read,
        # No configuration file over the CLI; the readable state serves as backup.
        "config_backup": "\n\n".join(f"### {mode} → {command}\n{clean_output(outputs.get(key, '')).strip()}"
                                     for mode, command, key in COMMANDS if outputs.get(key)),
        "vlan_names": names,
        "stack": [],
        "lldp_disabled": not lldp_receiving(outputs.get("lldp_config", "")),
        "lldp_enabled_automatically": bool(outputs.get("lldp_enabled_automatically")),
        "discovered_at": time.time(),
        "system": {"name": _field(system, "Device Name"), "model": _field(system, "Model Name"),
                   "location": _field(system, "Location"), "mac": mac,
                   "serial": _field(system, "Serial Number"), "firmware": _field(system, "Firmware Version")},
    }
