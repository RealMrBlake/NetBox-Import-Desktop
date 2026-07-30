"""SSH discovery and NetBox synchronization for HPE and Aruba switches."""

import ipaddress
import re
import time

import paramiko


PLATFORMS = {
    "aruba_cx": "Aruba CX",
    "aruba_aos": "ArubaOS-Switch / ProCurve",
    "hpe_comware": "HPE Comware",
}

COMMANDS = {
    "aruba_cx": ("no page", "show interface brief", "show lldp neighbor-info detail"),
    "aruba_aos": ("no page", "show interfaces brief", "show lldp info remote-device detail"),
    "hpe_comware": ("screen-length disable", "display interface brief", "display lldp neighbor-information verbose"),
}

IP_COMMANDS = {
    "aruba_cx": ("show ip interface brief", "show ipv6 interface brief"),
    "aruba_aos": ("show ip", "show ipv6"),
    "hpe_comware": ("display ip interface brief", "display ipv6 interface brief"),
}

LLDP_CONTROL = {
    "aruba_cx": {
        "status": "show lldp configuration",
        "disabled": r"LLDP Enabled\s*:\s*No",
        "enable": ("configure terminal", "lldp", "end"),
    },
    "aruba_aos": {
        "status": "show lldp config",
        "disabled": r"LLDP (?:Enabled|Run)\s*:\s*(?:No|False)|LLDP operation.*disabled",
        "enable": ("configure terminal", "lldp run", "exit"),
    },
    "hpe_comware": {
        "status": "display lldp status",
        "disabled": r"Global status of LLDP\s*:\s*Disable",
        "enable": ("system-view", "lldp global enable", "return"),
    },
}

ANSI_ESCAPE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_]|[=>])"
)
TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAC_PATTERN = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b|\b[0-9a-f]{4}(?:-[0-9a-f]{4}){2}\b")
CLI_PROMPT = re.compile(r"(?:^|\n)[^\r\n]*[>#][ \t]*(?:\n)?\Z")


def _clean_output(value):
    value = ANSI_ESCAPE.sub("", value).replace("\r", "")
    value = TERMINAL_CONTROL.sub("", value)
    value = re.sub(r"--\s*MORE\s*--|Press any key to continue.*", "", value, flags=re.I)
    return value


def _read_available(channel, timeout=20):
    chunks = []
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if channel.recv_ready():
            chunks.append(channel.recv(65535).decode("utf-8", errors="replace"))
            # Interactive AOS-CX/AOS-S/Comware commands are complete when the
            # device prompt returns. This avoids truncating output at a short
            # pause while also avoiding a fixed delay after every command.
            current = _clean_output("".join(chunks))
            if CLI_PROMPT.search(current):
                break
        else:
            time.sleep(0.05)
    output = _clean_output("".join(chunks))
    if not CLI_PROMPT.search(output):
        raise TimeoutError("Der Switch hat die CLI-Ausgabe nicht mit einem vollständigen Prompt abgeschlossen.")
    return output


def _run_cli_command(channel, command, timeout=20):
    channel.send(command + "\n")
    try:
        return _read_available(channel, timeout=timeout)
    except TimeoutError as error:
        raise TimeoutError(f"CLI-Befehl nicht vollständig abgeschlossen: {command}") from error


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
        try:
            _read_available(channel, timeout=30)
        except TimeoutError as error:
            raise TimeoutError("SSH-Anmeldung nicht mit einem vollständigen CLI-Prompt abgeschlossen") from error
        pager_command, interface_command, neighbor_command = COMMANDS[platform]
        _run_cli_command(channel, pager_command)

        control = LLDP_CONTROL[platform]
        lldp_status = _run_cli_command(channel, control["status"])
        lldp_enabled_automatically = bool(re.search(control["disabled"], lldp_status, flags=re.I))
        if lldp_enabled_automatically:
            enable_output = []
            for command in control["enable"]:
                enable_output.append(_run_cli_command(channel, command))
            combined = "\n".join(enable_output)
            if platform == "hpe_comware" and re.search(r"(?i)unrecognized|invalid|wrong parameter", combined):
                _run_cli_command(channel, "system-view")
                combined = _run_cli_command(channel, "lldp enable")
                combined += "\n" + _run_cli_command(channel, "return")
            if re.search(r"(?i)permission denied|authorization failed|access denied", combined):
                raise PermissionError("LLDP konnte mangels Konfigurationsrechten nicht aktiviert werden.")
            if re.search(r"(?i)unrecognized|invalid input|unknown command|wrong parameter", combined):
                raise RuntimeError("Der LLDP-Aktivierungsbefehl wird von diesem Switch nicht unterstützt.")
            time.sleep(2)

        interfaces = _run_cli_command(channel, interface_command, timeout=45)
        ip_outputs = []
        for command in IP_COMMANDS[platform]:
            ip_outputs.append(_run_cli_command(channel, command, timeout=35))
        neighbors = _run_cli_command(channel, neighbor_command, timeout=60)
        if platform == "aruba_cx" and re.search(r"(?i)invalid input|unknown command|unrecognized", neighbors):
            neighbors = _run_cli_command(channel, "show lldp neighbor-info", timeout=60)
        cdp_neighbors = ""
        if platform == "aruba_cx":
            cdp_summary = _run_cli_command(channel, "show cdp neighbor-info", timeout=35)
            cdp_ports = []
            for line in cdp_summary.splitlines():
                match = re.match(r"^\s*(\d+/\d+/\d+)\s+\S+", line)
                if match and match.group(1) not in cdp_ports:
                    cdp_ports.append(match.group(1))
            details = []
            for port in cdp_ports:
                details.append(_run_cli_command(channel, f"show cdp neighbor-info {port}", timeout=30))
            cdp_neighbors = "\n".join([cdp_summary, *details])
        return {
            "interfaces": interfaces,
            "ipv4_interfaces": ip_outputs[0],
            "ipv6_interfaces": ip_outputs[1],
            "neighbors": neighbors,
            "cdp_neighbors": cdp_neighbors,
            "lldp_enabled_automatically": lldp_enabled_automatically,
        }
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
    matches = re.findall(
        r"(?i)\b(\d+(?:\.\d+)?)\s*(T|G|M)(?:b(?:it)?|bps)?(?:T|X|SR|LR|CR)?\b",
        line,
    )
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
        r"Bridge-Aggregation\d+|Trk\d+|lag\d+|Vlan-interface\d+|vlan\s*\d+|loopback\s*\d*|lo\d+|mgmt)$",
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
            "ip_addresses": [],
            "virtual": False,
        })
        seen.add(name)
    return interfaces


def _address_with_mask(address, mask=None):
    try:
        if "/" in address:
            return str(ipaddress.ip_interface(address))
        if mask:
            return str(ipaddress.ip_interface(f"{address}/{mask}"))
    except ValueError:
        return None
    return None


def parse_interface_ips(ipv4_output, ipv6_output, platform):
    entries = []
    if platform == "aruba_aos":
        for line in _clean_output(ipv4_output).splitlines():
            match = re.match(
                r"^\s*([^|]+?)\s*\|\s*(?:Manual|DHCP/Bootp|Disabled)\s+(\d+(?:\.\d+){3})\s+(\d+(?:\.\d+){3})",
                line,
                flags=re.I,
            )
            if match:
                address = _address_with_mask(match.group(2), match.group(3))
                if address and not address.startswith("0.0.0.0/"):
                    entries.append({"interface": match.group(1).strip(), "address": address})
        current_interface = ""
        for line in _clean_output(ipv6_output).splitlines():
            vlan_match = re.match(r"^\s*Vlan Name\s*:\s*(.+)$", line, flags=re.I)
            if vlan_match:
                current_interface = vlan_match.group(1).strip()
            address_match = re.search(r"([0-9a-fA-F:]+/\d{1,3})", line)
            if address_match and current_interface:
                address = _address_with_mask(address_match.group(1))
                if address and not address.lower().startswith("fe80:"):
                    entries.append({"interface": current_interface, "address": address})
    else:
        for output, version in ((ipv4_output, 4), (ipv6_output, 6)):
            for line in _clean_output(output).splitlines():
                tokens = re.split(r"\s+", line.strip())
                if len(tokens) < 2 or not _interface_name(tokens[0]):
                    continue
                patterns = (
                    r"\b(\d+(?:\.\d+){3}/\d{1,2})\b"
                    if version == 4 else
                    r"\b([0-9a-fA-F]*:[0-9a-fA-F:]+/\d{1,3})\b"
                )
                match = re.search(patterns, line)
                if not match:
                    continue
                address = _address_with_mask(match.group(1))
                if not address or address.startswith("0.0.0.0/") or address.lower().startswith("fe80:"):
                    continue
                entries.append({"interface": tokens[0], "address": address})
    unique = {}
    for entry in entries:
        unique[(entry["interface"].casefold(), entry["address"])] = entry
    return list(unique.values())


def _interface_key(value):
    value = value.casefold().replace("_", "").replace("-", "").replace(" ", "")
    value = value.replace("vlaninterface", "vlan")
    return value


def _field(block, patterns):
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.I | re.M)
        if match:
            return match.group(1).strip()
    return ""


def parse_neighbors(output):
    clean = _clean_output(output)
    starts = list(re.finditer(
        r"(?im)^(?:\s*(?:Port|Local (?:Port|Interface|Intf)|LLDP neighbor-information of port)\s*[: ]\s*.+)$",
        clean,
    ))
    if not starts:
        return _parse_neighbor_table(clean)
    neighbors = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(clean)
        block = clean[start.start():end]
        local_port = _field(block, (
            r"^\s*Port\s*:\s*(\S+)",
            r"^\s*Local (?:Port|Interface|Intf)\s*[: ]\s*(\S+)",
            r"^\s*LLDP neighbor-information of port\s+(?:\d+\[)?([^\]\s:]+)\]?",
        ))
        system_name = _field(block, (
            r"^\s*Neighbor Chassis-Name\s*:\s*(.+)$",
            r"^[ \t]*Neighbor System-Name[ \t]*:[ \t]*(.*)$",
            r"^[ \t]*(?:System Name|SysName)[ \t]*:[ \t]*(.*)$",
            r"^\s*Neighbor name\s*[: ]\s*(.+)$",
        ))
        if not system_name:
            system_name = _field(block, (
                r"^[ \t]*Neighbor Chassis-ID[ \t]*:[ \t]*(.+)$",
                r"^[ \t]*ChassisId[ \t]*:[ \t]*(.+)$",
            ))
        remote_port = _field(block, (
            r"^\s*Neighbor Port-ID\s*:\s*(.+)$",
            r"^\s*Neighbor Port-Desc\s*:\s*(.+)$",
            r"^[ \t]*(?:Port ID|PortId)[ \t]*:[ \t]*(.*)$",
            r"^\s*Port ID subtype.*\n\s*Port ID\s*[: ]\s*(.+)$",
            r"^\s*(?:Port Description|PortDesc)\s*[: ]\s*(.+)$",
        ))
        management_ip = _field(block, (
            r"^\s*Neighbor Management-Address\s*:\s*([0-9a-fA-F:.]+)",
            r"^\s*(?:Management Address|Management address|Management IP)\s*[: ]\s*([0-9a-fA-F:.]+)",
            r"^[ \t]*Address[ \t]*:[ \t]*([0-9a-fA-F:.]+)[ \t]*$",
        ))
        if local_port and (system_name or remote_port):
            neighbors.append({
                "local_port": local_port,
                "system_name": system_name,
                "remote_port": remote_port,
                "management_ip": management_ip,
            })
    return neighbors


def parse_cdp_neighbors(output):
    """Parse detailed AOS-CX CDP records into the common neighbor shape."""
    clean = _clean_output(output)
    starts = list(re.finditer(r"(?im)^\s*Local Port\s*:\s*\S+", clean))
    neighbors = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(clean)
        block = clean[start.start():end]
        local_port = _field(block, (r"^\s*Local Port\s*:\s*(\S+)",))
        system_name = _field(block, (
            r"^\s*Device ID\s*:\s*(.+)$",
            r"^\s*System Name\s*:\s*(.+)$",
        ))
        remote_port = _field(block, (
            r"^\s*Neighbor Port-ID\s*:\s*(.+)$",
            r"^\s*Port ID\s*:\s*(.+)$",
        ))
        management_ip = _field(block, (
            r"^\s*Address\s*:\s*([0-9a-fA-F:.]+)",
            r"^\s*Management Address\s*:\s*([0-9a-fA-F:.]+)",
        ))
        if local_port and (system_name or remote_port or management_ip):
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
    interface_ips = parse_interface_ips(
        outputs.get("ipv4_interfaces", ""),
        outputs.get("ipv6_interfaces", ""),
        config["platform"],
    )
    lldp_neighbors = parse_neighbors(outputs["neighbors"])
    cdp_neighbors = parse_cdp_neighbors(outputs.get("cdp_neighbors", ""))
    # LLDP normally contains richer data. CDP fills ports for which CX did not
    # return an LLDP record, without duplicating a physical local interface.
    neighbors = list(lldp_neighbors)
    occupied_ports = {_interface_key(item["local_port"]) for item in neighbors}
    for neighbor in cdp_neighbors:
        key = _interface_key(neighbor["local_port"])
        if key not in occupied_ports:
            neighbors.append(neighbor)
            occupied_ports.add(key)
    interface_map = {_interface_key(interface["name"]): interface for interface in interfaces}
    for item in interface_ips:
        key = _interface_key(item["interface"])
        interface = interface_map.get(key)
        if not interface:
            interface = {
                "name": item["interface"],
                "enabled": True,
                "connected": True,
                "speed_mbps": None,
                "mac_address": None,
                "description": "Logisches Layer-3-Interface",
                "neighbor": None,
                "ip_addresses": [],
                "virtual": True,
            }
            interfaces.append(interface)
            interface_map[key] = interface
        interface["ip_addresses"].append(item["address"])
    for neighbor in neighbors:
        local = interface_map.get(_interface_key(neighbor["local_port"]))
        if local:
            local["neighbor"] = neighbor
    return {
        "interfaces": interfaces,
        "ip_addresses": interface_ips,
        "neighbors": neighbors,
        "lldp_neighbors": lldp_neighbors,
        "cdp_neighbors": cdp_neighbors,
        "raw": outputs,
        "lldp_enabled_automatically": outputs.get("lldp_enabled_automatically", False),
    }


def _netbox_interface_type(speed_mbps, virtual=False):
    if virtual:
        return "virtual"
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
    created = updated = cables = ip_addresses = 0
    errors = []
    device = nb.dcim.devices.get(id=device_id)
    tenant_id = getattr(getattr(device, "tenant", None), "id", None) if device else None
    for interface in snapshot["interfaces"]:
        payload = {
            "device": device_id,
            "name": interface["name"],
            "type": _netbox_interface_type(interface.get("speed_mbps"), interface.get("virtual", False)),
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

    for interface in snapshot["interfaces"]:
        record = interface_records.get(interface["name"].casefold())
        if not record:
            continue
        for address in interface.get("ip_addresses", []):
            try:
                host = str(ipaddress.ip_interface(address).ip)
                candidates = [
                    item for item in nb.ipam.ip_addresses.filter(address=host)
                    if getattr(getattr(item, "vrf", None), "id", None) is None
                ]
                if len(candidates) > 1:
                    raise ValueError("mehrere globale IP-Adressen mit diesem Host gefunden")
                payload = {
                    "address": address,
                    "status": "active",
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": record.id,
                }
                if tenant_id:
                    payload["tenant"] = tenant_id
                if candidates:
                    candidates[0].update(payload)
                else:
                    nb.ipam.ip_addresses.create(payload)
                ip_addresses += 1
            except Exception as error:
                errors.append(f"IP-Adresse {address} auf {interface['name']}: {error}")

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
    return {
        "created": created,
        "updated": updated,
        "ip_addresses": ip_addresses,
        "cables": cables,
        "errors": errors,
    }
