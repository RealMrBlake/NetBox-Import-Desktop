"""SSH discovery and NetBox synchronization for HPE and Aruba switches."""

import re
import time

import paramiko


PLATFORMS = {
    "aruba_cx": "Aruba CX",
    "aruba_aos": "ArubaOS-Switch / ProCurve",
    "hpe_comware": "HPE Comware",
}

COMMANDS = {
    "aruba_cx": ("no page", "show interface brief", "show lldp neighbor-info"),
    "aruba_aos": ("no page", "show interfaces brief", "show lldp info remote-device"),
    "hpe_comware": ("screen-length disable", "display interface brief", "display lldp neighbor-information verbose"),
}

ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MAC_PATTERN = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b|\b[0-9a-f]{4}(?:-[0-9a-f]{4}){2}\b")


def _clean_output(value):
    value = ANSI_ESCAPE.sub("", value).replace("\r", "")
    value = re.sub(r"--\s*MORE\s*--|Press any key to continue.*", "", value, flags=re.I)
    return value


def _read_available(channel, quiet_seconds=0.35, timeout=12):
    chunks = []
    started = time.monotonic()
    last_data = started
    while time.monotonic() - started < timeout:
        if channel.recv_ready():
            chunks.append(channel.recv(65535).decode("utf-8", errors="replace"))
            last_data = time.monotonic()
        elif time.monotonic() - last_data >= quiet_seconds:
            break
        else:
            time.sleep(0.05)
    return _clean_output("".join(chunks))


def run_switch_commands(config):
    platform = config["platform"]
    if platform not in COMMANDS:
        raise ValueError(f"Nicht unterstützte Switch-Plattform: {platform}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=config["host"],
            port=int(config.get("port", 22)),
            username=config["username"],
            password=config["password"],
            look_for_keys=False,
            allow_agent=False,
            timeout=12,
            auth_timeout=12,
            banner_timeout=12,
        )
        channel = client.invoke_shell(width=240, height=1000)
        _read_available(channel, timeout=3)
        outputs = []
        for command in COMMANDS[platform]:
            channel.send(command + "\n")
            outputs.append(_read_available(channel))
        return {"interfaces": outputs[1], "neighbors": outputs[2]}
    finally:
        client.close()


def _normalize_mac(value):
    if not value:
        return None
    compact = re.sub(r"[^0-9a-fA-F]", "", value).upper()
    if len(compact) != 12:
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _speed_mbps(line):
    matches = re.findall(r"(?i)\b(\d+(?:\.\d+)?)\s*(T|G|M)(?:b(?:it)?|bps)?\b", line)
    if not matches:
        duplex = re.search(r"(?i)\b(\d+)(?:FDx|HDx)\b", line)
        if duplex:
            return int(duplex.group(1))
        trailing = re.search(r"\b(\d{2,6})\s*$", line)
        return int(trailing.group(1)) if trailing else None
    value, unit = matches[-1]
    factor = {"M": 1, "G": 1000, "T": 1_000_000}[unit.upper()]
    return int(float(value) * factor)


def _interface_name(token):
    token = token.strip(" |:")
    return bool(re.match(
        r"(?i)^(?:\d+(?:/\d+){0,3}|(?:GE|XGE|HGE|FGE|FortyGigE|Ten-GigabitEthernet|GigabitEthernet)\S+|"
        r"Bridge-Aggregation\d+|Trk\d+|lag\d+)$",
        token,
    ))


def parse_interfaces(output, platform):
    interfaces = []
    seen = set()
    for raw_line in _clean_output(output).splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"-", "=", " "}:
            continue
        tokens = re.split(r"\s+", line.replace("|", " | ").strip())
        name_index = next((index for index, token in enumerate(tokens[:3]) if _interface_name(token)), None)
        if name_index is None:
            continue
        name = tokens[name_index].strip(" |:")
        if name in seen:
            continue
        lower = line.lower()
        if not re.search(r"\b(up|down|administratively down|disabled|enabled)\b", lower):
            continue
        enabled = not bool(re.search(r"administratively down|admin.*down|disabled|\bno\s+(?:up|down)\b", lower))
        connected = bool(re.search(r"\bup\b", lower)) and not bool(re.search(r"\bdown\b", lower))
        mac_match = MAC_PATTERN.search(line)
        description = ""
        if platform == "aruba_aos" and "|" in raw_line:
            columns = [column.strip() for column in raw_line.split("|")]
            if len(columns) > 1:
                description = columns[1]
        interfaces.append({
            "name": name,
            "enabled": enabled,
            "connected": connected,
            "speed_mbps": _speed_mbps(line),
            "mac_address": _normalize_mac(mac_match.group(0)) if mac_match else None,
            "description": description,
            "neighbor": None,
        })
        seen.add(name)
    return interfaces


def _field(block, patterns):
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.I | re.M)
        if match:
            return match.group(1).strip()
    return ""


def parse_neighbors(output):
    clean = _clean_output(output)
    starts = list(re.finditer(
        r"(?im)^(?:\s*(?:Local (?:Port|Interface|Intf)|LLDP neighbor-information of port)\s*[: ]\s*.+)$",
        clean,
    ))
    if not starts:
        return _parse_neighbor_table(clean)
    neighbors = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(clean)
        block = clean[start.start():end]
        local_port = _field(block, (
            r"^\s*Local (?:Port|Interface|Intf)\s*[: ]\s*(\S+)",
            r"^\s*LLDP neighbor-information of port\s+(?:\d+\[)?([^\]\s:]+)\]?",
        ))
        system_name = _field(block, (
            r"^\s*(?:System Name|SysName)\s*[: ]\s*(.+)$",
            r"^\s*Neighbor name\s*[: ]\s*(.+)$",
        ))
        remote_port = _field(block, (
            r"^\s*(?:Port ID|PortId|Port ID subtype.*\n\s*Port ID)\s*[: ]\s*(.+)$",
            r"^\s*(?:Port Description|PortDesc)\s*[: ]\s*(.+)$",
        ))
        management_ip = _field(block, (
            r"^\s*(?:Management Address|Management address|Management IP)\s*[: ]\s*([0-9a-fA-F:.]+)",
        ))
        if local_port and (system_name or remote_port):
            neighbors.append({
                "local_port": local_port,
                "system_name": system_name,
                "remote_port": remote_port,
                "management_ip": management_ip,
            })
    return neighbors


def _parse_neighbor_table(output):
    neighbors = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"-", "=", " "}:
            continue
        tokens = re.split(r"\s+", line.replace("|", " | "))
        if not tokens or not _interface_name(tokens[0]):
            continue
        if "|" in tokens:
            separator = tokens.index("|")
            remaining = tokens[separator + 1:]
            if len(remaining) < 3:
                continue
            remote_port = remaining[1]
            system_name = remaining[-1]
        else:
            if len(tokens) < 5:
                continue
            remote_port = tokens[2]
            system_name = tokens[-1]
        neighbors.append({
            "local_port": tokens[0],
            "system_name": system_name,
            "remote_port": remote_port,
            "management_ip": "",
        })
    return neighbors


def discover_switch(config):
    outputs = run_switch_commands(config)
    interfaces = parse_interfaces(outputs["interfaces"], config["platform"])
    neighbors = parse_neighbors(outputs["neighbors"])
    interface_map = {interface["name"].casefold(): interface for interface in interfaces}
    for neighbor in neighbors:
        local = interface_map.get(neighbor["local_port"].casefold())
        if local:
            local["neighbor"] = neighbor
    return {"interfaces": interfaces, "neighbors": neighbors, "raw": outputs}


def _netbox_interface_type(speed_mbps):
    if not speed_mbps:
        return "other"
    if speed_mbps >= 100_000:
        return "100gbase-x-qsfp28"
    if speed_mbps >= 40_000:
        return "40gbase-x-qsfpp"
    if speed_mbps >= 25_000:
        return "25gbase-x-sfp28"
    if speed_mbps >= 10_000:
        return "10gbase-x-sfpp"
    if speed_mbps >= 1000:
        return "1000base-t"
    return "100base-tx"


def sync_snapshot_to_netbox(nb, config, snapshot, create_cables=True):
    device_id = int(config["device_id"])
    interface_records = {}
    created = updated = cables = 0
    errors = []
    for interface in snapshot["interfaces"]:
        payload = {
            "device": device_id,
            "name": interface["name"],
            "type": _netbox_interface_type(interface.get("speed_mbps")),
            "enabled": interface.get("enabled", True),
        }
        if interface.get("description"):
            payload["description"] = interface["description"][:200]
        if interface.get("mac_address"):
            payload["mac_address"] = interface["mac_address"]
        try:
            record = nb.dcim.interfaces.get(device_id=device_id, name=interface["name"])
            if record:
                record.update(payload)
                updated += 1
            else:
                record = nb.dcim.interfaces.create(payload)
                created += 1
            interface_records[interface["name"].casefold()] = record
        except Exception as error:
            errors.append(f"Interface {interface['name']}: {error}")

    if create_cables:
        for neighbor in snapshot["neighbors"]:
            try:
                local = interface_records.get(neighbor["local_port"].casefold())
                if not local or getattr(local, "cable", None):
                    continue
                remote_device = nb.dcim.devices.get(name=neighbor["system_name"])
                if not remote_device:
                    continue
                remote = nb.dcim.interfaces.get(device_id=remote_device.id, name=neighbor["remote_port"])
                if not remote or getattr(remote, "cable", None):
                    continue
                nb.dcim.cables.create({
                    "a_terminations": [{"object_type": "dcim.interface", "object_id": local.id}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": remote.id}],
                    "status": "connected",
                })
                cables += 1
            except Exception as error:
                errors.append(
                    f"Nachbar {neighbor.get('local_port')} → {neighbor.get('system_name')} "
                    f"{neighbor.get('remote_port')}: {error}"
                )
    return {"created": created, "updated": updated, "cables": cables, "errors": errors}
