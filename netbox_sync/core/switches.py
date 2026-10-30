"""SSH discovery and NetBox synchronization for HPE and Aruba switches."""

import ipaddress
import re
import threading
import time
from pathlib import Path

import paramiko

from netbox_sync.core.netbox import assign_mac, related_id

PLATFORMS = {
    "auto": "Automatisch erkennen",
    "aruba_cx": "Aruba CX (AOS-CX)",
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

# The running configuration is the only output that shows untagged and tagged
# VLAN membership per port on all three platforms.
CONFIG_COMMANDS = {
    "aruba_cx": "show running-config",
    "aruba_aos": "show running-config",
    "hpe_comware": "display current-configuration",
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

ANSI_ESCAPE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_]|[=>])")
TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAC_PATTERN = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b|\b[0-9a-f]{4}(?:-[0-9a-f]{4}){2}\b")
# "host#", "host>", Comware "<host>" and Comware system-view "[host]".
CLI_PROMPT = re.compile(r"(?:^|\n)(?:[^\r\n]*[>#]|\s*\[[^\[\]\r\n]+\])[ \t]*(?:\n)?\Z")
COMWARE_PROMPT = re.compile(r"(?:^|\n)\s*[<\[][^<>\[\]\r\n]+[>\]][ \t]*\Z")
CLI_ERROR = re.compile(r"(?i)invalid input|unknown command|unrecognized command|% ?wrong parameter|incomplete command")

_HOSTKEY_LOCK = threading.Lock()


def _noop(*_args, **_kwargs):
    return None


class HostKeyChangedError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Interface names
# --------------------------------------------------------------------------

# Every spelling maps to one short token so that e.g. Comware "GE1/0/1",
# NetBox "GigabitEthernet1/0/1" and a Cisco neighbor "Gi1/0/1" compare equal.
_NAME_ALIASES = {
    "ge": "ge", "gi": "ge", "gigabitethernet": "ge", "gigethernet": "ge",
    "xge": "xge", "te": "xge", "tengigabitethernet": "xge", "tengige": "xge",
    "wge": "wge", "twe": "wge", "twentyfivegige": "wge", "twentyfivegigabitethernet": "wge",
    "fge": "fge", "fo": "fge", "fortygige": "fge", "fortygigabitethernet": "fge",
    "hge": "hge", "hu": "hge", "hundredgige": "hge", "hundredgigabitethernet": "hge",
    "fa": "fe", "fastethernet": "fe",
    "mge": "mge", "mgigabitethernet": "mge",
    "bagg": "bagg", "bridgeaggregation": "bagg",
    "ragg": "ragg", "routeaggregation": "ragg",
    "po": "po", "portchannel": "po",
    "vlan": "vlan", "vl": "vlan", "vlaninterface": "vlan", "vlanif": "vlan",
    "loop": "lo", "lo": "lo", "loopback": "lo",
    "eth": "eth", "et": "eth", "ethernet": "eth",
}

_COMWARE_EXPANSION = {
    "GE": "GigabitEthernet", "XGE": "Ten-GigabitEthernet", "FGE": "FortyGigE", "HGE": "HundredGigE",
    "WGE": "Twenty-FiveGigE", "MGE": "M-GigabitEthernet", "BAGG": "Bridge-Aggregation",
    "RAGG": "Route-Aggregation", "VLAN": "Vlan-interface", "LOOP": "LoopBack",
}

_NAME_SPLIT = re.compile(r"^([A-Za-z][A-Za-z\- ]*?)\s*(\d[\d/:.]*)$")


def interface_key(value):
    """Normalized comparison key for interface names across vendors."""
    value = (value or "").strip()
    match = _NAME_SPLIT.match(value)
    if match:
        prefix = re.sub(r"[\s\-_]", "", match.group(1)).casefold()
        return _NAME_ALIASES.get(prefix, prefix) + match.group(2)
    return re.sub(r"[\s\-_]", "", value).casefold()


def expand_comware_name(name):
    match = _NAME_SPLIT.match(name)
    if not match:
        return name
    full = _COMWARE_EXPANSION.get(match.group(1).upper())
    return f"{full}{match.group(2)}" if full else name


def _interface_name(token):
    token = token.strip(" |:")
    return bool(re.match(
        r"(?i)^(?:\d+(?:/\d+){0,3}|[A-Z]{1,2}\d{1,2}|"
        r"(?:GE|XGE|HGE|FGE|WGE|MGE|FortyGigE|HundredGigE|Ten-GigabitEthernet|GigabitEthernet|M-GigabitEthernet)\S+|"
        r"Bridge-Aggregation\d+|BAGG\d+|RAGG\d+|Trk\d+|lag\d+|Vlan-interface\d+|vlan\s*\d+|Loop(?:Back)?\d+|"
        r"loopback\s*\d*|lo\d+|mgmt)$",
        token,
    ))


# --------------------------------------------------------------------------
# SSH session handling
# --------------------------------------------------------------------------

def clean_output(value):
    value = ANSI_ESCAPE.sub("", value).replace("\r", "")
    value = TERMINAL_CONTROL.sub("", value)
    return re.sub(r"--\s*MORE\s*--|Press any key to continue.*", "", value, flags=re.I)


def _read_available(channel, timeout=20, initialize_session=False):
    chunks = []
    started = time.monotonic()
    next_nudge = started + 0.5
    nudges = 0
    while time.monotonic() - started < timeout:
        if channel.recv_ready():
            chunks.append(channel.recv(65535).decode("utf-8", errors="replace"))
            # The command is complete when the device prompt returns. This
            # avoids truncating output at a short pause and fixed delays.
            if CLI_PROMPT.search(clean_output("".join(chunks))):
                break
        now = time.monotonic()
        if initialize_session and nudges < 3 and now >= next_nudge:
            # ArubaOS-S may stop at the login banner until a key is pressed.
            channel.send("\r")
            nudges += 1
            next_nudge = now + 1.5
        time.sleep(0.05)
    output = clean_output("".join(chunks))
    if not CLI_PROMPT.search(output):
        tail = " | ".join(line.strip() for line in output.splitlines()[-3:] if line.strip())
        detail = f" Letzte Ausgabe: {tail[-300:]}" if tail else " Keine Ausgabe vom Switch empfangen."
        raise TimeoutError("Der Switch hat die CLI-Ausgabe nicht mit einem vollständigen Prompt abgeschlossen." + detail)
    return output


def _run_cli_command(channel, command, timeout=20):
    channel.send(command + "\n")
    try:
        return _read_available(channel, timeout=timeout)
    except TimeoutError as error:
        raise TimeoutError(f"CLI-Befehl nicht vollständig abgeschlossen: {command}. {error}") from error


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Remember unknown host keys; paramiko rejects changed keys afterwards."""

    def __init__(self, path, log):
        self.path = Path(path)
        self.log = log

    def missing_host_key(self, client, hostname, key):
        with _HOSTKEY_LOCK:
            keys = paramiko.HostKeys()
            if self.path.exists():
                keys.load(str(self.path))
            keys.add(hostname, key.get_name(), key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            keys.save(str(self.path))
        fingerprint = key.fingerprint if hasattr(key, "fingerprint") else key.get_base64()[:24]
        self.log(f"{hostname}: neuer SSH-Hostschlüssel gespeichert ({key.get_name()} {fingerprint})")


def _host_entry(host, port):
    return host if int(port) == 22 else f"[{host}]:{port}"


def forget_host_key(known_hosts, host, port=22):
    path = Path(known_hosts)
    if not path.exists():
        return False
    with _HOSTKEY_LOCK:
        keys = paramiko.HostKeys(str(path))
        entry = _host_entry(host, port)
        if entry not in keys:
            return False
        del keys[entry]
        keys.save(str(path))
    return True


def _open_client(config, known_hosts, log):
    client = paramiko.SSHClient()
    if known_hosts:
        if Path(known_hosts).exists():
            with _HOSTKEY_LOCK:
                client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(_TrustOnFirstUse(known_hosts, log))
    else:
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
    except paramiko.BadHostKeyException as error:
        client.close()
        raise HostKeyChangedError(
            f"Der SSH-Hostschlüssel von {config['host']} hat sich geändert! Mögliche Ursache: Austausch des "
            "Geräts – oder ein Angriff. Falls der Wechsel erwartet ist, im Switch-Dialog "
            "„Gespeicherten Hostschlüssel vergessen“ wählen."
        ) from error
    except paramiko.AuthenticationException as error:
        client.close()
        raise PermissionError("SSH-Anmeldung abgelehnt – Benutzername oder Passwort prüfen.") from error
    except Exception:
        client.close()
        raise
    return client


def detect_platform(channel, banner):
    """Identify the switch OS from prompt and ``show version`` output."""
    if COMWARE_PROMPT.search(banner):
        return "hpe_comware"
    _run_cli_command(channel, "no page")
    version = _run_cli_command(channel, "show version", timeout=30)
    if re.search(r"(?i)ArubaOS-CX|AOS-CX|\b[A-Z]{2}\.10\.\d+", version):
        return "aruba_cx"
    if re.search(r"(?i)Image stamp|ProCurve|\b[A-Z]{1,2}\.1[456]\.\d+", version):
        return "aruba_aos"
    display = _run_cli_command(channel, "display version", timeout=30)
    if re.search(r"(?i)Comware", display):
        return "hpe_comware"
    raise RuntimeError("Plattform konnte nicht automatisch erkannt werden – bitte im Switch-Dialog manuell wählen.")


def run_switch_commands(config, enable_lldp=False, known_hosts=None, log=_noop):
    platform = config.get("platform", "auto")
    if platform not in PLATFORMS:
        raise ValueError(f"Nicht unterstützte Switch-Plattform: {platform}")
    client = _open_client(config, known_hosts, log)
    try:
        channel = client.invoke_shell(width=240, height=1000)
        try:
            banner = _read_available(channel, timeout=45, initialize_session=True)
        except TimeoutError as error:
            raise TimeoutError(f"SSH-Anmeldung nicht mit einem vollständigen CLI-Prompt abgeschlossen. {error}") from error
        if platform == "auto":
            platform = detect_platform(channel, banner)
            log(f"{config.get('name', config['host'])}: Plattform erkannt – {PLATFORMS[platform]}")
        pager_command, interface_command, neighbor_command = COMMANDS[platform]
        _run_cli_command(channel, pager_command)

        control = LLDP_CONTROL[platform]
        lldp_status = _run_cli_command(channel, control["status"])
        lldp_disabled = bool(re.search(control["disabled"], lldp_status, flags=re.I))
        lldp_enabled_automatically = False
        if lldp_disabled and enable_lldp:
            enable_output = [_run_cli_command(channel, command) for command in control["enable"]]
            combined = "\n".join(enable_output)
            if platform == "hpe_comware" and CLI_ERROR.search(combined):
                _run_cli_command(channel, "system-view")
                combined = _run_cli_command(channel, "lldp enable")
                combined += "\n" + _run_cli_command(channel, "return")
            if re.search(r"(?i)permission denied|authorization failed|access denied", combined):
                raise PermissionError("LLDP konnte mangels Konfigurationsrechten nicht aktiviert werden.")
            if CLI_ERROR.search(combined):
                raise RuntimeError("Der LLDP-Aktivierungsbefehl wird von diesem Switch nicht unterstützt.")
            lldp_enabled_automatically = True
            time.sleep(2)

        interfaces = _run_cli_command(channel, interface_command, timeout=45)
        ip_outputs = [_run_cli_command(channel, command, timeout=35) for command in IP_COMMANDS[platform]]
        neighbors = _run_cli_command(channel, neighbor_command, timeout=60)
        if platform == "aruba_cx" and CLI_ERROR.search(neighbors):
            neighbors = _run_cli_command(channel, "show lldp neighbor-info", timeout=60)
        cdp_neighbors = ""
        if platform == "aruba_cx":
            cdp_summary = _run_cli_command(channel, "show cdp neighbor-info", timeout=35)
            cdp_ports = []
            for line in cdp_summary.splitlines():
                match = re.match(r"^\s*(\d+/\d+/\d+)\s+\S+", line)
                if match and match.group(1) not in cdp_ports:
                    cdp_ports.append(match.group(1))
            details = [_run_cli_command(channel, f"show cdp neighbor-info {port}", timeout=30) for port in cdp_ports]
            cdp_neighbors = "\n".join([cdp_summary, *details])
        config_output = _run_cli_command(channel, CONFIG_COMMANDS[platform], timeout=90)
        if CLI_ERROR.search(config_output[:500]):
            log(f"{config.get('name', config['host'])}: Konfiguration nicht lesbar – VLANs werden nicht übernommen")
            config_output = ""
        return {
            "platform": platform,
            "interfaces": interfaces,
            "ipv4_interfaces": ip_outputs[0],
            "ipv6_interfaces": ip_outputs[1],
            "neighbors": neighbors,
            "cdp_neighbors": cdp_neighbors,
            "config": config_output,
            "lldp_status": lldp_status,
            "lldp_disabled": lldp_disabled and not lldp_enabled_automatically,
            "lldp_enabled_automatically": lldp_enabled_automatically,
        }
    finally:
        client.close()


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

def speed_mbps(text):
    gig = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*Gig", text)
    if gig:
        return int(float(gig.group(1)) * 1000)
    matches = re.findall(r"(?i)\b(\d+(?:\.\d+)?)\s*(T|G|M)(?:b(?:it)?|bps)?(?:T|X|SR|LR|CR)?\b", text)
    if not matches:
        duplex = re.search(r"(?i)\b(\d+)(?:FDx|HDx)\b", text)
        if duplex:
            return int(duplex.group(1))
        trailing = re.search(r"\b(\d{2,6})\s*$", text)
        return int(trailing.group(1)) if trailing else None
    value, unit = matches[-1]
    return int(float(value) * {"M": 1, "G": 1000, "T": 1_000_000}[unit.upper()])


def _interface(name, **values):
    record = {
        "name": name, "enabled": True, "connected": False, "speed_mbps": None, "media": "",
        "mac_address": None, "description": "", "neighbor": None, "ip_addresses": [], "virtual": False,
    }
    record.update(values)
    return record


def _parse_aos_interfaces(output):
    """ArubaOS-S: ``Port Type | Alert Enabled Status Mode ...``."""
    interfaces = []
    seen = set()
    for raw_line in clean_output(output).splitlines():
        if "|" not in raw_line:
            continue
        left, right = raw_line.split("|", 1)
        left_tokens = left.split()
        right_tokens = right.split()
        if not left_tokens or not _interface_name(left_tokens[0]) or len(right_tokens) < 3:
            continue
        name = left_tokens[0]
        if name in seen:
            continue
        enabled_value, status_value = right_tokens[1].lower(), right_tokens[2].lower()
        if enabled_value not in ("yes", "no") or status_value not in ("up", "down"):
            continue
        mode = right_tokens[3] if len(right_tokens) > 3 else ""
        interfaces.append(_interface(
            name,
            enabled=enabled_value == "yes",
            connected=status_value == "up",
            speed_mbps=speed_mbps(mode) if status_value == "up" else None,
            media=left_tokens[1] if len(left_tokens) > 1 else "",
        ))
        seen.add(name)
    return interfaces


def parse_interfaces(output, platform):
    if platform == "aruba_aos":
        return _parse_aos_interfaces(output)
    interfaces = []
    seen = set()
    for raw_line in clean_output(output).splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"-", "=", " "}:
            continue
        tokens = re.split(r"\s+", line)
        name_index = next((index for index, token in enumerate(tokens[:3]) if _interface_name(token)), None)
        if name_index is None:
            continue
        name = tokens[name_index].strip(" |:")
        if platform == "hpe_comware":
            name = expand_comware_name(name)
        if name in seen:
            continue
        lower = line.lower()
        if not re.search(r"\b(up|down|adm|administratively down|disabled|enabled)\b", lower):
            continue
        enabled = not re.search(r"administratively down|admin.*down|\badm\b|disabled|\bno\s+(?:up|down)\b", lower)
        connected = bool(re.search(r"\bup\b", lower)) and not re.search(r"\bdown\b", lower)
        mac_match = MAC_PATTERN.search(line)
        media = tokens[name_index + 3] if platform == "aruba_cx" and len(tokens) > name_index + 3 else ""
        interfaces.append(_interface(
            name,
            enabled=bool(enabled),
            connected=connected,
            speed_mbps=speed_mbps(line),
            media=media,
            mac_address=mac_match.group(0) if mac_match else None,
        ))
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
        current_interface = ""
        for line in clean_output(ipv4_output).splitlines():
            if "|" not in line:
                continue
            interface_column, values = line.split("|", 1)
            candidate = interface_column.strip()
            if candidate and not re.search(r"(?i)^VLAN$", candidate) and not set(candidate) <= {"-", "+", " "}:
                current_interface = candidate
            addresses = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", values)
            if current_interface and len(addresses) >= 2:
                address = _address_with_mask(addresses[0], addresses[1])
                if address and not address.startswith("0.0.0.0/"):
                    entries.append({"interface": current_interface, "address": address})
        current_interface = ""
        for line in clean_output(ipv6_output).splitlines():
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
            pattern = r"\b(\d+(?:\.\d+){3}/\d{1,2})\b" if version == 4 else r"\b([0-9a-fA-F]*:[0-9a-fA-F:]+/\d{1,3})\b"
            for line in clean_output(output).splitlines():
                tokens = re.split(r"\s+", line.strip())
                if len(tokens) < 2 or not _interface_name(tokens[0]):
                    continue
                match = re.search(pattern, line)
                if not match:
                    continue
                address = _address_with_mask(match.group(1))
                if not address or address.startswith("0.0.0.0/") or address.lower().startswith("fe80:"):
                    continue
                name = expand_comware_name(tokens[0]) if platform == "hpe_comware" else tokens[0]
                entries.append({"interface": name, "address": address})
    unique = {}
    for entry in entries:
        unique[(interface_key(entry["interface"]), entry["address"])] = entry
    return list(unique.values())


def _field(block, patterns):
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.I | re.M)
        if match:
            return match.group(1).strip()
    return ""


def parse_neighbors(output):
    clean = clean_output(output)
    # "Port : 1/1/1" (CX) starts a record, but "Port ID : …" or
    # "Port description : …" inside a Comware record must not.
    starts = list(re.finditer(
        r"(?im)^\s*(?:Port\s*:|Local (?:Port|Interface|Intf)\s*[: ]|LLDP neighbor-information of port\s).*$",
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
            system_name = _field(block, (r"^[ \t]*Neighbor Chassis-ID[ \t]*:[ \t]*(.+)$", r"^[ \t]*ChassisId[ \t]*:[ \t]*(.+)$"))
        remote_port = _field(block, (
            r"^\s*Neighbor Port-ID\s*:\s*(.+)$",
            r"^[ \t]*(?:Port ID|PortId)[ \t]*:[ \t]*(.*)$",
            r"^\s*Port ID subtype.*\n\s*Port ID\s*[: ]\s*(.+)$",
            r"^\s*Neighbor Port-Desc\s*:\s*(.+)$",
            r"^\s*(?:Port Description|PortDesc|PortDescr)\s*[: ]\s*(.+)$",
        ))
        port_description = _field(block, (
            r"^\s*Neighbor Port-Desc\s*:\s*(.+)$",
            r"^\s*(?:Port Description|PortDesc|PortDescr)\s*[: ]\s*(.+)$",
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
                "port_description": port_description,
                "management_ip": management_ip,
                "protocol": "LLDP",
            })
    return neighbors


def parse_cdp_neighbors(output):
    """Parse detailed AOS-CX CDP records into the common neighbor shape."""
    clean = clean_output(output)
    starts = list(re.finditer(r"(?im)^\s*Local Port\s*:\s*\S+", clean))
    neighbors = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(clean)
        block = clean[start.start():end]
        local_port = _field(block, (r"^\s*Local Port\s*:\s*(\S+)",))
        system_name = _field(block, (r"^\s*Device ID\s*:\s*(.+)$", r"^\s*System Name\s*:\s*(.+)$"))
        remote_port = _field(block, (r"^\s*Neighbor Port-ID\s*:\s*(.+)$", r"^\s*Port ID\s*:\s*(.+)$"))
        management_ip = _field(block, (r"^\s*Address\s*:\s*([0-9a-fA-F:.]+)", r"^\s*Management Address\s*:\s*([0-9a-fA-F:.]+)"))
        if local_port and (system_name or remote_port or management_ip):
            neighbors.append({
                "local_port": local_port,
                "system_name": system_name,
                "remote_port": remote_port,
                "port_description": "",
                "management_ip": management_ip,
                "protocol": "CDP",
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
            remaining = tokens[tokens.index("|") + 1:]
            if len(remaining) < 3:
                continue
            remote_port, system_name = remaining[1], remaining[-1]
        else:
            if len(tokens) < 5:
                continue
            remote_port, system_name = tokens[2], tokens[-1]
        neighbors.append({
            "local_port": tokens[0], "system_name": system_name, "remote_port": remote_port,
            "port_description": "", "management_ip": "", "protocol": "LLDP",
        })
    return neighbors


# --------------------------------------------------------------------------
# VLAN membership from the running configuration
# --------------------------------------------------------------------------

_PORT_RANGE = re.compile(r"^(\D*(?:\d+/)*)(\d+)-(?:\1)?(\d+)$")
_VLAN_CONFIG_LINE = re.compile(r"(?i)^\s*(?:interface\s|vlan\s|#|exit|\s*(?:no\s+)?routing\b)|vlan|tagged|link-type|link-mode")


def parse_vlan_list(text):
    """``10,20-22`` (Aruba) or ``10 20 to 22`` (Comware) → set of VLAN IDs."""
    text = re.sub(r"\s+to\s+", "-", text.strip(), flags=re.I)
    vlans = set()
    for part in re.split(r"[,\s]+", text):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if 1 <= start <= end <= 4094:
            vlans.update(range(start, end + 1))
    return vlans


def _expand_ports(text):
    """ArubaOS-S port lists such as ``1-4,A1-A2,1/5-1/8,Trk1``."""
    ports = []
    for part in (item.strip() for item in text.split(",")):
        match = _PORT_RANGE.match(part)
        if match and int(match.group(2)) <= int(match.group(3)):
            ports += [f"{match.group(1)}{number}" for number in range(int(match.group(2)), int(match.group(3)) + 1)]
        elif part:
            ports.append(part)
    return ports


def _membership(untagged=(), tagged=(), tagged_all=False):
    untagged = set(untagged)
    return {"untagged": sorted(untagged), "tagged": sorted(set(tagged) - untagged), "tagged_all": bool(tagged_all)}


def _config_blocks(config, header):
    """Yield ``(header match, body lines)`` for indented configuration blocks."""
    current, body = None, []
    for line in clean_output(config).splitlines():
        match = header.match(line)
        if match:
            if current:
                yield current, body
            current, body = match, []
        elif current and line[:1] in (" ", "\t"):
            body.append(line.strip())
        elif current:
            yield current, body
            current, body = None, []
    if current:
        yield current, body


def _parse_aos_vlans(config):
    ports = {}
    for match, body in _config_blocks(config, re.compile(r"^vlan\s+(\d+)\s*$", re.I)):
        vlan = int(match.group(1))
        for line in body:
            member = re.match(r"^(untagged|tagged)\s+(\S+)", line, flags=re.I)
            if member:
                for port in _expand_ports(member.group(2)):
                    entry = ports.setdefault(interface_key(port), {"untagged": set(), "tagged": set()})
                    entry[member.group(1).lower()].add(vlan)
    return {key: _membership(entry["untagged"], entry["tagged"]) for key, entry in ports.items()}


def _parse_cx_vlans(config):
    result = {}
    for match, body in _config_blocks(config, re.compile(r"^interface\s+(.+?)\s*$", re.I)):
        text = "\n".join(body)
        access = re.search(r"(?im)^vlan access (\d+)", text)
        native = re.search(r"(?im)^vlan trunk native (\d+)(\s+tag)?", text)
        allowed = re.findall(r"(?im)^vlan trunk allowed (.+)$", text)
        if access:
            result[interface_key(match.group(1))] = _membership([int(access.group(1))])
        elif native or allowed:
            native_id = int(native.group(1)) if native else 1
            tagged_native = bool(native and native.group(2))
            tagged = set().union(*(parse_vlan_list(item) for item in allowed))
            everything = not allowed or any(item.strip().lower() == "all" for item in allowed)
            untagged = [] if tagged_native else [native_id]
            result[interface_key(match.group(1))] = _membership(untagged, tagged | ({native_id} if tagged_native else set()),
                                                                everything)
        elif re.search(r"(?im)^no routing\b", text):
            result[interface_key(match.group(1))] = _membership([1])
    return result


def _parse_comware_vlans(config):
    result = {}
    for match, body in _config_blocks(config, re.compile(r"^interface\s+(\S+)\s*$", re.I)):
        name = match.group(1)
        prefix = re.match(r"^[a-z]+", interface_key(name))
        if not prefix or prefix.group(0) not in ("ge", "xge", "wge", "fge", "hge", "fe", "bagg"):
            continue
        text = "\n".join(body)
        if re.search(r"(?im)^port link-mode route", text):
            continue
        link_type = re.search(r"(?im)^port link-type (\w+)", text)
        link_type = link_type.group(1).lower() if link_type else "access"
        if link_type == "trunk":
            pvid = re.search(r"(?im)^port trunk pvid vlan (\d+)", text)
            pvid = int(pvid.group(1)) if pvid else 1
            permitted, everything = {1}, False
            for undo, vlans in re.findall(r"(?im)^(undo )?port trunk permit vlan (.+)$", text):
                if vlans.strip().lower() == "all":
                    everything = not undo
                elif undo:
                    permitted -= parse_vlan_list(vlans)
                else:
                    permitted |= parse_vlan_list(vlans)
            untagged = [pvid] if everything or pvid in permitted else []
            result[interface_key(name)] = _membership(untagged, permitted, everything)
        elif link_type == "hybrid":
            untagged, tagged = set(), set()
            for vlans, mode in re.findall(r"(?im)^port hybrid vlan (.+?) (untagged|tagged)\s*$", text):
                (untagged if mode.lower() == "untagged" else tagged).update(parse_vlan_list(vlans))
            result[interface_key(name)] = _membership(untagged or {1}, tagged)
        else:
            access = re.search(r"(?im)^port access vlan (\d+)", text)
            result[interface_key(name)] = _membership([int(access.group(1)) if access else 1])
    return result


def parse_vlan_names(config):
    """``{VLAN ID: name}`` from ``vlan <id>`` blocks (same syntax on all platforms)."""
    names = {}
    for match, body in _config_blocks(config or "", re.compile(r"^vlan\s+(\d+)\s*$", re.I)):
        for line in body:
            name = re.match(r'^name\s+"?(.*?)"?\s*$', line, flags=re.I)
            if name and name.group(1):
                names[int(match.group(1))] = name.group(1)
    return names


def parse_vlans(config, platform):
    """VLAN membership per port, keyed by :func:`interface_key`."""
    if not config:
        return {}
    parser = {"aruba_aos": _parse_aos_vlans, "aruba_cx": _parse_cx_vlans, "hpe_comware": _parse_comware_vlans}
    return parser[platform](config) if platform in parser else {}


def _compress_vlans(vlans):
    parts, values = [], sorted(vlans)
    start = previous = None
    for value in values + [None]:
        if start is not None and value == previous + 1:
            previous = value
            continue
        if start is not None:
            parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    return ",".join(parts)


def vlan_label(vlans, limit=64):
    """All VLANs of a port (untagged and tagged) as short NetBox label."""
    if not vlans:
        return ""
    if vlans.get("tagged_all"):
        return "all"
    text = _compress_vlans(set(vlans.get("untagged", [])) | set(vlans.get("tagged", [])))
    if len(text) > limit:
        text = text[:limit - 1].rsplit(",", 1)[0] + "…"
    return text


def _vlan_config_excerpt(config):
    """Keep only VLAN-related lines – the full configuration may contain secrets."""
    return "\n".join(line for line in clean_output(config).splitlines() if _VLAN_CONFIG_LINE.search(line))


def build_snapshot(outputs, platform):
    interfaces = parse_interfaces(outputs["interfaces"], platform)
    vlans = parse_vlans(outputs.get("config", ""), platform)
    for interface in interfaces:
        interface["vlans"] = vlans.get(interface_key(interface["name"]))
    interface_ips = parse_interface_ips(outputs.get("ipv4_interfaces", ""), outputs.get("ipv6_interfaces", ""), platform)
    lldp_neighbors = parse_neighbors(outputs["neighbors"])
    cdp_neighbors = parse_cdp_neighbors(outputs.get("cdp_neighbors", ""))
    # LLDP normally contains richer data. CDP fills ports for which CX did not
    # return an LLDP record, without duplicating a physical local interface.
    neighbors = list(lldp_neighbors)
    occupied = {interface_key(item["local_port"]) for item in neighbors}
    for neighbor in cdp_neighbors:
        key = interface_key(neighbor["local_port"])
        if key not in occupied:
            neighbors.append(neighbor)
            occupied.add(key)
    interface_map = {interface_key(interface["name"]): interface for interface in interfaces}
    for item in interface_ips:
        key = interface_key(item["interface"])
        interface = interface_map.get(key)
        if not interface:
            interface = _interface(item["interface"], connected=True, description="Logisches Layer-3-Interface", virtual=True)
            interfaces.append(interface)
            interface_map[key] = interface
        interface["ip_addresses"].append(item["address"])
    for neighbor in neighbors:
        local = interface_map.get(interface_key(neighbor["local_port"]))
        if local:
            local["neighbor"] = neighbor
    return {
        "platform": platform,
        "interfaces": interfaces,
        "ip_addresses": interface_ips,
        "neighbors": neighbors,
        "lldp_neighbors": lldp_neighbors,
        "cdp_neighbors": cdp_neighbors,
        "raw": {**{key: value for key, value in outputs.items() if key != "config"},
                **({"vlan_config": _vlan_config_excerpt(outputs["config"])} if outputs.get("config") else {})},
        "vlans_read": bool(outputs.get("config")),
        "vlan_names": parse_vlan_names(outputs.get("config", "")),
        "lldp_disabled": outputs.get("lldp_disabled", False),
        "lldp_enabled_automatically": outputs.get("lldp_enabled_automatically", False),
        "discovered_at": time.time(),
    }


def discover_switch(config, enable_lldp=False, known_hosts=None, log=_noop):
    outputs = run_switch_commands(config, enable_lldp=enable_lldp, known_hosts=known_hosts, log=log)
    return build_snapshot(outputs, outputs["platform"])


# --------------------------------------------------------------------------
# Platform suggestion from NetBox data
# --------------------------------------------------------------------------

def suggest_platform(*texts):
    """Guess the switch OS from NetBox platform, manufacturer and model names."""
    text = " ".join(str(value or "") for value in texts).casefold()
    if not text.strip():
        return "auto"
    if re.search(r"comware|h3c|\b(?:19[0-9]0|51[234]0|55[01]0|57[01]0|59[0-9]0|75\d\d|10500|12500|12900)\b", text):
        return "hpe_comware"
    if re.search(r"aos-?cx|arubaos-?cx|\bcx\b|\b(?:4100i|6000|6100|6200[fm]?|6300[fm]?|6400|8100|8320|8325|8360|8400|9300|10000)\b", text):
        return "aruba_cx"
    if re.search(r"procurve|aos-?s\b|arubaos-?switch|provision|\b(?:2530|2540|2620|2920|2930[fm]?|3800|3810m?|5400r?l?|5406|5412)\b", text):
        return "aruba_aos"
    return "auto"


# --------------------------------------------------------------------------
# NetBox synchronization
# --------------------------------------------------------------------------

def netbox_interface_type(interface):
    if interface.get("virtual"):
        return "virtual"
    key = interface_key(interface["name"])
    if re.match(r"^(?:lag|trk|bagg|ragg|po)\d", key):
        return "lag"
    if re.match(r"^(?:vlan|lo)\d", key):
        return "virtual"
    media = (interface.get("media") or "").casefold()
    if media and media != "--":
        if "qsfp28" in media or "100g" in media:
            return "100gbase-x-qsfp28"
        if "qsfp" in media or "40g" in media:
            return "40gbase-x-qsfpp"
        if "sfp28" in media or "25g" in media:
            return "25gbase-x-sfp28"
        if "smartrate" in media or "2.5g" in media or "mgig" in media:
            return "2.5gbase-t"
        if re.search(r"10gbe?-?t\b|10gbaset", media):
            return "10gbase-t"
        if "sfp+" in media or "10g" in media:
            return "10gbase-x-sfpp"
        if "sfp" in media or "1000sx" in media or "1000lx" in media or "gbic" in media:
            return "1000base-x-sfp"
        if re.search(r"1000t|1gbt|1gbaset", media):
            return "1000base-t"
        if "100tx" in media or "100/" in media:
            return "100base-tx"
    prefix = re.match(r"^[a-z]+", key)
    by_prefix = {"xge": "10gbase-x-sfpp", "wge": "25gbase-x-sfp28", "fge": "40gbase-x-qsfpp",
                 "hge": "100gbase-x-qsfp28", "ge": "1000base-t", "fe": "100base-tx", "mge": "1000base-t"}
    if prefix and prefix.group(0) in by_prefix:
        return by_prefix[prefix.group(0)]
    speed = interface.get("speed_mbps")
    if not speed:
        return "other"
    for threshold, value in ((100_000, "100gbase-x-qsfp28"), (40_000, "40gbase-x-qsfpp"),
                             (25_000, "25gbase-x-sfp28"), (10_000, "10gbase-x-sfpp"), (1000, "1000base-t")):
        if speed >= threshold:
            return value
    return "100base-tx"


def _device_interfaces(nb, device_id):
    records = {}
    for record in nb.dcim.interfaces.filter(device_id=device_id):
        key = interface_key(record.name)
        # Prefer a template-created full name over a stray short duplicate.
        if key not in records or len(record.name) > len(records[key].name):
            records[key] = record
    return records


MATCH_METHODS = {
    "name": "gleicher Name",
    "number": "gleiche Portnummer",
    "order": "Reihenfolge",
    "manual": "manuell",
    "new": "neu anlegen",
}

_PORT_NAME = re.compile(r"^[A-Za-z\- ]*\d+(?:/\d+)*$")


def _natural_key(name):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def _port_number(name):
    """Last number of a port-like name: ``1/1/5`` → 5, ``GE1/0/5`` → 5, ``5`` → 5."""
    name = (name or "").strip()
    return int(re.search(r"(\d+)$", name).group(1)) if _PORT_NAME.match(name) else None


def _record_type(record):
    value = getattr(record, "type", None)
    return getattr(value, "value", value if isinstance(value, str) else None) or ""


def _physical_record(record):
    return (_record_type(record) not in ("virtual", "lag", "bridge")
            and not re.match(r"^(?:vlan|lo|mgmt|oobm)", interface_key(record.name)))


def _physical_interface(interface):
    return netbox_interface_type(interface) not in ("virtual", "lag")


def match_interfaces(interfaces, records, manual=None):
    """Assign NetBox interfaces to discovered switch interfaces.

    Order of precedence: stored manual choices (``{switch name: NetBox name}``,
    an empty NetBox name means "create new"), equal names across vendor
    spellings, an equal last port number (``1/1/5`` ↔ ``5``) and finally the
    natural order of the remaining physical ports when both sides have the
    same number left. Returns ``{switch name: (record or None, method)}``.
    """
    manual = manual or {}
    by_name = {record.name: record for record in records}
    by_key = {}
    for record in records:
        key = interface_key(record.name)
        if key not in by_key or len(record.name) > len(by_key[key].name):
            by_key[key] = record
    result, used = {}, set()

    def assign(interface, record, method):
        result[interface["name"]] = (record, method)
        if record is not None:
            used.add(record.id)

    for interface in interfaces:
        if interface["name"] not in manual:
            continue
        target = manual[interface["name"]]
        record = by_name.get(target) if target else None
        if target and (record is None or record.id in used):
            continue  # stale choice – fall back to the automatic match
        assign(interface, record, "manual" if record else "new")
    for interface in interfaces:
        if interface["name"] not in result:
            record = by_key.get(interface_key(interface["name"]))
            if record and record.id not in used:
                assign(interface, record, "name")

    open_ports = [item for item in interfaces if item["name"] not in result and _physical_interface(item)]
    open_records = [record for record in records if record.id not in used and _physical_record(record)]
    ports_by_number, records_by_number = {}, {}
    for item in open_ports:
        ports_by_number.setdefault(_port_number(item["name"]), []).append(item)
    for record in open_records:
        records_by_number.setdefault(_port_number(record.name), []).append(record)
    for number, ports in ports_by_number.items():
        candidates = records_by_number.get(number, [])
        if number is not None and len(ports) == 1 and len(candidates) == 1:
            assign(ports[0], candidates[0], "number")

    open_ports = [item for item in open_ports if item["name"] not in result]
    open_records = [record for record in open_records if record.id not in used]
    if open_ports and len(open_ports) == len(open_records):
        open_ports.sort(key=lambda item: _natural_key(item["name"]))
        open_records.sort(key=lambda record: _natural_key(record.name))
        for interface, record in zip(open_ports, open_records):
            assign(interface, record, "order")
    for interface in interfaces:
        result.setdefault(interface["name"], (None, "new"))
    return result


def _resolve_device(nb, config):
    """The NetBox device of a switch config, or an error text."""
    if config.get("device_id"):
        device = nb.dcim.devices.get(int(config["device_id"]))
        return (device, None) if device else (None, f"NetBox-Gerät #{config['device_id']} existiert nicht mehr")
    # Switches captured in offline mode are only known by name.
    matches = list(nb.dcim.devices.filter(name=config["name"]))
    if len(matches) != 1:
        return None, f"NetBox-Gerät „{config['name']}“ " + ("nicht gefunden" if not matches else "ist nicht eindeutig")
    return matches[0], None


def candidate_devices(nb, device):
    """Devices a neighbor may be assigned to: the tenant group of the switch.

    Falls back to the switch's tenant and, without a tenant, to all devices.
    Returns ``(scope description, [{"id", "name"}])``.
    """
    tenant_id = related_id(device, "tenant")
    scope, devices = "alle Geräte", None
    if tenant_id:
        tenant = nb.tenancy.tenants.get(tenant_id)
        group_id = related_id(tenant, "group") if tenant else None
        if group_id:
            group = getattr(tenant, "group", None)
            scope = f"Mandantengruppe {getattr(group, 'name', None) or group_id}"
            devices = nb.dcim.devices.filter(tenant_group_id=group_id)
        else:
            scope = f"Mandant {getattr(tenant, 'name', None) or tenant_id}"
            devices = nb.dcim.devices.filter(tenant_id=tenant_id)
    if devices is None:
        devices = nb.dcim.devices.all()
    rows = [{"id": item.id, "name": item.name} for item in devices if getattr(item, "name", None) and item.id != device.id]
    return scope, sorted(rows, key=lambda row: row["name"].casefold())


def _require_tenant(device):
    tenant_id = related_id(device, "tenant")
    if tenant_id:
        return tenant_id, None
    return None, (f"NetBox-Gerät „{device.name}“ hat keinen Mandanten – bitte zuerst in NetBox einen Mandanten "
                  "zuweisen. Ohne Mandant werden keine Objekte angelegt.")


def snapshot_networks(snapshot):
    """Networks of all interface IP addresses (host routes are left out)."""
    networks = set()
    for interface in snapshot["interfaces"]:
        for address in interface.get("ip_addresses", []):
            network = ipaddress.ip_interface(address).network
            if network.prefixlen < network.max_prefixlen:
                networks.add(network)
    return [str(network) for network in sorted(networks, key=lambda item: (item.version, item))]


def snapshot_vlan_ids(snapshot):
    vids = set()
    for interface in snapshot["interfaces"]:
        vlans = interface.get("vlans") or {}
        vids.update(vlans.get("untagged", []))
        vids.update(vlans.get("tagged", []))
    return sorted(vids)


VLAN_METHODS = {
    "vid": "gleiche VID",
    "name": "VID + Name",
    "ambiguous": "mehrdeutig",
    "manual": "manuell",
    "new": "fehlt",
    "skip": "nicht zuordnen",
    "ignore": "ignoriert",
}


def ignored_vlans(vlan_map):
    """VLAN IDs the user chose to ignore on all switches of the profile."""
    return {int(vid) for vid, choice in (vlan_map or {}).items() if (choice or {}).get("action") == "ignore"}


def without_vlans(vlans, ignored):
    """Port VLAN membership without ignored VLANs (``None`` stays ``None``)."""
    if not vlans or not ignored:
        return vlans
    return dict(vlans, untagged=[vid for vid in vlans.get("untagged", []) if vid not in ignored],
                tagged=[vid for vid in vlans.get("tagged", []) if vid not in ignored])


def candidate_vlans(nb, tenant_id):
    """VLANs of the switch's tenant group (or tenant) – a VLAN may belong to a
    sibling tenant, e.g. a school network documented under its own tenant."""
    tenant = nb.tenancy.tenants.get(tenant_id)
    group_id = related_id(tenant, "group") if tenant else None
    found = {}
    sources = [nb.ipam.vlans.filter(tenant_id=tenant_id)]
    if group_id:
        sources.append(nb.ipam.vlans.filter(tenant_group_id=group_id))
    for source in sources:
        for vlan in source:
            found[vlan.id] = vlan
    return list(found.values())


def candidate_tenants(nb, tenant_id):
    """Tenants a new VLAN may get: the tenant group of the switch's tenant."""
    tenant = nb.tenancy.tenants.get(tenant_id)
    group_id = related_id(tenant, "group") if tenant else None
    tenants = {tenant_id: getattr(tenant, "name", None) or str(tenant_id)}
    if group_id:
        tenants.update({item.id: item.name for item in nb.tenancy.tenants.filter(group_id=group_id)})
    return sorted(({"id": key, "name": value} for key, value in tenants.items()), key=lambda row: row["name"].casefold())


def _name_tokens(text):
    return set(re.findall(r"[a-z]+|\d+", (text or "").casefold()))


def vlan_choice_label(vlan):
    owner = getattr(getattr(vlan, "tenant", None), "name", None) or getattr(getattr(vlan, "group", None), "name", None)
    return f"{vlan.vid} – {vlan.name}" + (f" ({owner})" if owner else "")


def match_vlans(vids, names, vlans, tenant_id, site_id, manual=None):
    """Assign NetBox VLANs to switch VLAN IDs.

    Candidates need the same VID. With several, the own tenant, the switch's
    site and similar names (``Server_LEO`` ↔ ``Leo Server``) decide.
    ``manual`` is ``{str(vid): {"action": "map"|"new"|"skip", "id", "name"}}``.
    Returns ``{vid: (record or None, method, name for a new VLAN)}``.
    """
    manual = manual or {}
    by_vid, by_id = {}, {vlan.id: vlan for vlan in vlans}
    for vlan in vlans:
        by_vid.setdefault(vlan.vid, []).append(vlan)
    result = {}
    for vid in vids:
        switch_name = names.get(vid) or f"VLAN{vid}"
        choice = manual.get(str(vid)) or {}
        if choice.get("action") == "new":
            name = (choice.get("name") or switch_name)[:64]
            # Created by an earlier run – use it instead of creating it again.
            created = next((vlan for vlan in by_vid.get(vid, []) if vlan.name.casefold() == name.casefold()), None)
            result[vid] = (created, "manual", name) if created else (None, "new", name)
            continue
        if choice.get("action") in ("skip", "ignore"):
            result[vid] = (None, choice["action"], switch_name)
            continue
        if choice.get("action") == "map" and choice.get("id") in by_id:
            result[vid] = (by_id[choice["id"]], "manual", switch_name)
            continue
        candidates = by_vid.get(vid, [])
        if not candidates:
            result[vid] = (None, "new", switch_name)
            continue
        tokens = _name_tokens(switch_name)

        def score(vlan):
            other = _name_tokens(vlan.name)
            similarity = len(tokens & other) / len(tokens | other) if tokens | other else 0
            return ((related_id(vlan, "tenant") == tenant_id) * 4 + (related_id(vlan, "site") == site_id) * 2
                    + similarity * 3)

        ranked = sorted(candidates, key=score, reverse=True)
        if len(ranked) == 1:
            method = "vid"
        else:
            method = "name" if score(ranked[0]) > score(ranked[1]) else "ambiguous"
        result[vid] = (ranked[0], method, switch_name)
    return result


def _switch_vlan_matches(nb, config, snapshot, tenant_id, site_id):
    names = {int(key): value for key, value in (snapshot.get("vlan_names") or {}).items()}
    vlans = candidate_vlans(nb, tenant_id)
    return vlans, match_vlans(snapshot_vlan_ids(snapshot), names, vlans, tenant_id, site_id, config.get("vlan_map"))


def plan_switch_sync(nb, config, snapshot):
    """Proposals for the review dialog: interface mapping, neighbor devices,
    networks (VRF and missing prefixes) and missing VLANs."""
    device, error = _resolve_device(nb, config)
    if not error:
        tenant_id, error = _require_tenant(device)
    if error:
        return {"error": error}
    records = list(nb.dcim.interfaces.filter(device_id=device.id))
    matches = match_interfaces(snapshot["interfaces"], records, config.get("interface_map"))
    scope, devices = candidate_devices(nb, device)
    known = {row["id"] for row in devices}
    lookup = _RemoteLookup(nb)
    manual = config.get("neighbor_devices") or {}
    neighbors = []
    for neighbor in snapshot["neighbors"]:
        local = neighbor.get("local_port")
        if local in manual:
            device_id, method = int(manual[local]) or None, "manual"
            remote = nb.dcim.devices.get(device_id) if device_id else None
        else:
            remote = lookup.device(neighbor)
            device_id, method = (remote.id if remote else None), "auto"
        if remote and remote.id not in known and remote.id != device.id:
            devices.append({"id": remote.id, "name": remote.name})
            known.add(remote.id)
        neighbors.append(dict(neighbor, device_id=device_id, method=method))
    devices.sort(key=lambda row: row["name"].casefold())

    vrf_records = candidate_vrfs(nb, tenant_id)
    vrfs = [{"id": vrf.id, "label": vrf_choice_label(vrf)}
            for vrf in sorted(vrf_records, key=lambda item: (item.name or "").casefold())]
    networks = []
    for network in snapshot_networks(snapshot):
        # Where the prefix already exists: 0 = global, otherwise a VRF of the tenant group.
        existing = sorted({related_id(prefix, "vrf") or 0 for prefix in nb.ipam.prefixes.filter(prefix=network)}
                          & ({0} | {vrf.id for vrf in vrf_records}), key=lambda value: (value == 0, value))
        choice = _network_choice(config, network)
        if choice is not None:
            vrf_id = _resolve_vrf_choice(choice, vrf_records, tenant_id)
            vrf_id = vrf_id if isinstance(vrf_id, int) else 0
            networks.append({"network": network, "vrf_id": vrf_id,
                             "vrf_name": choice.get("vrf_name") if not vrf_id else "",
                             "existing": existing, "create": bool(choice.get("create")), "stored": True})
        else:
            networks.append({"network": network, "vrf_id": existing[0] if existing else 0, "vrf_name": "",
                             "existing": existing, "create": True, "stored": False})

    names = {int(key): value for key, value in (snapshot.get("vlan_names") or {}).items()}
    candidates, vlan_matches = _switch_vlan_matches(nb, config, snapshot, tenant_id, related_id(device, "site"))
    tenants = candidate_tenants(nb, tenant_id)
    allowed = {row["id"] for row in tenants}
    vlan_map = config.get("vlan_map") or {}
    vlans = []
    for vid, (record, method, new_name) in vlan_matches.items():
        new_tenant = (vlan_map.get(str(vid)) or {}).get("tenant")
        vlans.append({
            "vid": vid, "name": names.get(vid) or f"VLAN{vid}", "method": method, "new_name": new_name,
            "stored": str(vid) in vlan_map,
            "new_tenant": new_tenant if new_tenant in allowed else tenant_id,
            "netbox_id": record.id if record else None,
            "candidates": [{"id": vlan.id, "label": vlan_choice_label(vlan)}
                           for vlan in sorted(candidates, key=lambda item: (item.name or "").casefold())
                           if vlan.vid == vid],
        })
    return {
        "error": None,
        "device": {"id": device.id, "name": device.name},
        "interfaces": [
            {"name": item["name"], "netbox_name": record.name if record else "", "method": method,
             "physical": _physical_interface(item),
             "vlan_label": vlan_label(without_vlans(item.get("vlans"), ignored_vlans(vlan_map)))}
            for item in snapshot["interfaces"]
            for record, method in [matches[item["name"]]]
        ],
        "netbox_interfaces": sorted((record.name for record in records), key=_natural_key),
        "neighbors": neighbors,
        "device_scope": scope,
        "devices": devices,
        "tenant": getattr(getattr(device, "tenant", None), "name", None) or str(tenant_id),
        "vrfs": vrfs,
        "networks": networks,
        "vlans": vlans,
        "tenants": tenants,
    }


class _RemoteLookup:
    """Find neighbor devices and interfaces in NetBox, cached per sync run."""

    def __init__(self, nb):
        self.nb = nb
        self.devices = {}
        self.interfaces = {}

    def device(self, neighbor):
        name = (neighbor.get("system_name") or "").strip()
        cache_key = (name.casefold(), neighbor.get("management_ip"))
        if cache_key in self.devices:
            return self.devices[cache_key]
        found = None
        for candidate in dict.fromkeys(filter(None, (name, name.split(".")[0]))):
            matches = list(self.nb.dcim.devices.filter(name__ie=candidate))
            if len(matches) == 1:
                found = matches[0]
                break
        if not found and neighbor.get("management_ip"):
            for ip in self.nb.ipam.ip_addresses.filter(address=neighbor["management_ip"]):
                if getattr(ip, "assigned_object_type", "") == "dcim.interface":
                    device_id = related_id(ip.assigned_object, "device")
                    if device_id:
                        found = self.nb.dcim.devices.get(device_id)
                        break
        self.devices[cache_key] = found
        return found

    def interface(self, device, neighbor):
        if device.id not in self.interfaces:
            self.interfaces[device.id] = _device_interfaces(self.nb, device.id)
        records = self.interfaces[device.id]
        candidates = [neighbor.get("remote_port"), neighbor.get("port_description")]
        if neighbor.get("port_description"):
            candidates.append(neighbor["port_description"].split()[0])
        for candidate in filter(None, candidates):
            record = records.get(interface_key(candidate))
            if record:
                return record
        # Device types often name ports "1", "2", … while LLDP reports "1/1/1".
        number = _port_number(neighbor.get("remote_port"))
        if number is not None:
            found = [record for record in records.values()
                     if _physical_record(record) and _port_number(record.name) == number]
            if len(found) == 1:
                return found[0]
        return None


_NEW = "new"  # placeholder ID of objects a dry run would create


def candidate_vrfs(nb, tenant_id):
    """VRFs of the switch's tenant and of its tenant group."""
    tenant = nb.tenancy.tenants.get(tenant_id)
    group_id = related_id(tenant, "group") if tenant else None
    found = {vrf.id: vrf for vrf in nb.ipam.vrfs.filter(tenant_id=tenant_id)}
    if group_id:
        found.update({vrf.id: vrf for vrf in nb.ipam.vrfs.filter(tenant_group_id=group_id)})
    return list(found.values())


def vrf_choice_label(vrf):
    owner = getattr(getattr(vrf, "tenant", None), "name", None)
    return vrf.name + (f" ({owner})" if owner else "")


def _network_choice(config, network):
    """Stored decision ``{"vrf_id", "vrf_name", "create"}`` for a network, or ``None``."""
    choice = (config.get("network_map") or {}).get(network)
    if choice is not None:
        return choice
    # Profiles up to 4.6.0 stored a VRF name and approved prefixes per switch.
    legacy_vrfs, legacy_create = config.get("network_vrfs") or {}, config.get("create_prefixes")
    if network not in legacy_vrfs and legacy_create is None:
        return None
    return {"vrf_id": None, "vrf_name": legacy_vrfs.get(network) or "", "create": network in (legacy_create or [])}


def _resolve_vrf_choice(choice, vrf_records, tenant_id):
    """Existing VRF ID, ``None`` for global or the name of a VRF still to create."""
    by_id = {vrf.id: vrf for vrf in vrf_records}
    if choice.get("vrf_id") in by_id:
        return choice["vrf_id"]
    name = (choice.get("vrf_name") or "").strip()
    if not name:
        return None
    named = sorted((vrf for vrf in vrf_records if vrf.name.casefold() == name.casefold()),
                   key=lambda vrf: related_id(vrf, "tenant") != tenant_id)
    return named[0].id if named else name


def _sync_vrfs(nb, config, snapshot, tenant_id, dry_run, report):
    """``{network: VRF ID or None}``; new VRFs are created for the switch's tenant."""
    vrf_records = candidate_vrfs(nb, tenant_id)
    created = {}
    result = {}
    for network in snapshot_networks(snapshot):
        choice = _network_choice(config, network) or {}
        resolved = _resolve_vrf_choice(choice, vrf_records, tenant_id)
        if isinstance(resolved, str):
            if resolved not in created:
                report["vrfs_created"] += 1
                report["changes"].append(f"VRF {resolved} neu")
                created[resolved] = _NEW if dry_run else nb.ipam.vrfs.create({"name": resolved, "tenant": tenant_id}).id
            resolved = created[resolved]
        result[network] = resolved
    return result


def _sync_prefixes(nb, config, snapshot, tenant_id, vrf_ids, dry_run, report):
    """Create missing prefixes that were approved; existing ones stay untouched."""
    for network in snapshot_networks(snapshot):
        vrf_id = vrf_ids.get(network)
        if not (_network_choice(config, network) or {}).get("create"):
            continue
        if vrf_id != _NEW and list(nb.ipam.prefixes.filter(prefix=network, vrf_id=vrf_id or "null")):
            continue
        try:
            report["prefixes_created"] += 1
            if not vrf_id:
                vrf_name = "global"
            elif vrf_id == _NEW:
                vrf_name = f"VRF {_network_choice(config, network).get('vrf_name')}"
            else:
                vrf_name = f"VRF {getattr(nb.ipam.vrfs.get(vrf_id), 'name', vrf_id)}"
            report["changes"].append(f"Prefix {network} neu ({vrf_name})")
            if not dry_run:
                nb.ipam.prefixes.create({"prefix": network, "status": "active", "tenant": tenant_id, "vrf": vrf_id})
        except Exception as error:  # noqa: BLE001
            report["errors"].append(f"Prefix {network}: {error}")


def _sync_vlans(nb, config, snapshot, tenant_id, site_id, dry_run, report):
    """``{VLAN ID: NetBox ID}``; missing VLANs are only created when approved."""
    _vlans, matches = _switch_vlan_matches(nb, config, snapshot, tenant_id, site_id)
    approved = {int(vid) for vid, choice in (config.get("vlan_map") or {}).items() if choice.get("action") == "new"}
    approved |= {int(vid) for vid in config.get("create_vlans") or []}  # profiles of version 4.4.0
    present = {}
    for vid, (record, method, name) in matches.items():
        if record is not None:
            present[vid] = record.id
            continue
        if method != "new" or vid not in approved:
            continue
        try:
            report["vlans_created"] += 1
            report["changes"].append(f"VLAN {vid} „{name}“ neu")
            # A different tenant may be chosen per VLAN in the review dialog.
            chosen_tenant = ((config.get("vlan_map") or {}).get(str(vid)) or {}).get("tenant") or tenant_id
            payload = {"vid": vid, "name": name[:64], "status": "active", "tenant": chosen_tenant}
            if site_id:
                payload["site"] = site_id
            present[vid] = _NEW if dry_run else nb.ipam.vlans.create(payload).id
        except Exception as error:  # noqa: BLE001
            report["errors"].append(f"VLAN {vid}: {error}")
    return present


def _record_mode(record):
    mode = getattr(record, "mode", None)
    return getattr(mode, "value", mode) or ""


def _vlan_assignment(interface, vlan_ids, record=None):
    """802.1Q mode and VLANs of an interface; empty when ``record`` already matches."""
    vlans = interface.get("vlans")
    if not vlans or netbox_interface_type(interface) == "virtual":
        return {}
    untagged = next((vlan_ids[vid] for vid in vlans.get("untagged", []) if vid in vlan_ids), None)
    tagged = sorted((vlan_ids[vid] for vid in vlans.get("tagged", []) if vid in vlan_ids), key=str)
    if untagged is None and not tagged and not vlans.get("tagged_all"):
        return {}  # none of its VLANs exists in NetBox – leave the port alone
    if vlans.get("tagged_all"):
        wanted = {"mode": "tagged-all", "untagged_vlan": untagged, "tagged_vlans": []}
    elif vlans.get("tagged"):
        wanted = {"mode": "tagged", "untagged_vlan": untagged, "tagged_vlans": tagged}
    else:
        wanted = {"mode": "access", "untagged_vlan": untagged, "tagged_vlans": []}
    if record is None:
        return wanted
    current = {
        "mode": _record_mode(record),
        "untagged_vlan": related_id(record, "untagged_vlan"),
        "tagged_vlans": sorted((getattr(item, "id", item) for item in getattr(record, "tagged_vlans", None) or []),
                               key=str),
    }
    # NetBox validates the VLANs against the mode, so all three are sent together.
    return {} if current == wanted else wanted


def _new_report():
    return {
        "interfaces_created": 0, "interfaces_updated": 0, "interfaces_unchanged": 0,
        "ips_created": 0, "ips_updated": 0, "ips_unchanged": 0,
        "cables_created": 0, "cables_skipped": 0,
        "vrfs_created": 0, "prefixes_created": 0, "vlans_created": 0,
        "changes": [], "errors": [],
    }


def sync_snapshot_to_netbox(nb, config, snapshot, create_cables=True, dry_run=False, log=_noop):
    """Write a discovery snapshot to NetBox.

    With ``dry_run`` nothing is written; the returned report lists what would
    change. Interface types are only set when an interface is created, so
    device-type templates and manual corrections in NetBox are preserved.
    """
    report = _new_report()
    device, error = _resolve_device(nb, config)
    if not error:
        tenant_id, error = _require_tenant(device)
    if error:
        report["errors"].append(error)
        return report
    device_id = device.id
    vrf_ids = _sync_vrfs(nb, config, snapshot, tenant_id, dry_run, report)
    _sync_prefixes(nb, config, snapshot, tenant_id, vrf_ids, dry_run, report)
    vlan_ids = _sync_vlans(nb, config, snapshot, tenant_id, related_id(device, "site"), dry_run, report)
    ignored = ignored_vlans(config.get("vlan_map"))
    matches = match_interfaces(snapshot["interfaces"], list(nb.dcim.interfaces.filter(device_id=device_id)),
                               config.get("interface_map"))
    records = {}

    for interface in snapshot["interfaces"]:
        key = interface_key(interface["name"])
        record, method = matches[interface["name"]]
        port = dict(interface, vlans=without_vlans(interface.get("vlans"), ignored))
        label = vlan_label(port["vlans"])
        try:
            if record:
                changes = {}
                if bool(getattr(record, "enabled", True)) != bool(interface.get("enabled", True)):
                    changes["enabled"] = bool(interface.get("enabled", True))
                # Only written when VLANs are known, so manual labels survive
                # routed ports and snapshots without configuration.
                if label and (getattr(record, "label", "") or "") != label:
                    changes["label"] = label
                changes.update(_vlan_assignment(port, vlan_ids, record))
                if changes:
                    report["interfaces_updated"] += 1
                    texts = []
                    if "enabled" in changes:
                        texts.append("aktivieren" if changes["enabled"] else "deaktivieren")
                    if "label" in changes:
                        texts.append(f"Label „{label}“")
                    if "mode" in changes:
                        texts.append(f"VLAN-Modus {changes['mode']}")
                    target = record.name if method == "name" else f"{record.name} ({interface['name']})"
                    report["changes"].append(f"Interface {target}: {', '.join(texts)}")
                    if not dry_run:
                        record.update(changes)
                else:
                    report["interfaces_unchanged"] += 1
            else:
                payload = {
                    "device": device_id,
                    "name": interface["name"],
                    "type": netbox_interface_type(interface),
                    "enabled": bool(interface.get("enabled", True)),
                }
                if label:
                    payload["label"] = label
                payload.update(_vlan_assignment(port, vlan_ids))
                if interface.get("description"):
                    payload["description"] = interface["description"][:200]
                report["interfaces_created"] += 1
                report["changes"].append(f"Interface {interface['name']} neu ({payload['type']})"
                                         + (f", Label „{label}“" if label else ""))
                if not dry_run:
                    record = nb.dcim.interfaces.create(payload)
            if record and not dry_run and interface.get("mac_address"):
                assign_mac(nb, record, interface["mac_address"], "dcim.interface")
            records[key] = record
        except Exception as error:  # noqa: BLE001
            report["errors"].append(f"Interface {interface['name']}: {error}")

    for interface in snapshot["interfaces"]:
        key = interface_key(interface["name"])
        if key not in records:
            continue
        record = records[key]
        for address in interface.get("ip_addresses", []):
            try:
                parsed = ipaddress.ip_interface(address)
                vrf_id = vrf_ids.get(str(parsed.network))
                if vrf_id == _NEW:
                    candidates = []  # the VRF only exists after the real run
                else:
                    candidates = list(nb.ipam.ip_addresses.filter(address=str(parsed.ip), vrf_id=vrf_id or "null"))
                if len(candidates) > 1:
                    raise ValueError("mehrere IP-Adressen mit diesem Host in derselben VRF gefunden")
                payload = {"address": address, "status": "active", "assigned_object_type": "dcim.interface",
                           "tenant": tenant_id, "vrf": vrf_id}
                if record:
                    payload["assigned_object_id"] = record.id
                if candidates:
                    current = candidates[0]
                    differs = (
                        str(current.address) != address
                        or getattr(current, "assigned_object_type", None) != "dcim.interface"
                        or getattr(current, "assigned_object_id", None) != payload.get("assigned_object_id")
                        or related_id(current, "tenant") != tenant_id
                    )
                    if differs:
                        report["ips_updated"] += 1
                        report["changes"].append(f"IP {address} → {interface['name']}")
                        if not dry_run:
                            current.update(payload)
                    else:
                        report["ips_unchanged"] += 1
                else:
                    report["ips_created"] += 1
                    report["changes"].append(f"IP {address} neu auf {interface['name']}")
                    if not dry_run:
                        nb.ipam.ip_addresses.create(payload)
            except Exception as error:  # noqa: BLE001
                report["errors"].append(f"IP-Adresse {address} auf {interface['name']}: {error}")

    if create_cables:
        lookup = _RemoteLookup(nb)
        manual_devices = config.get("neighbor_devices") or {}
        for neighbor in snapshot["neighbors"]:
            label = f"{neighbor.get('local_port')} → {neighbor.get('system_name')} {neighbor.get('remote_port')}"
            try:
                key = interface_key(neighbor["local_port"])
                if key not in records:
                    continue
                local = records[key]
                if local is not None and getattr(local, "cable", None):
                    report["cables_skipped"] += 1
                    continue
                if neighbor["local_port"] in manual_devices:
                    # 0 means "no device" was chosen in the review dialog.
                    chosen = int(manual_devices[neighbor["local_port"]] or 0)
                    remote_device = nb.dcim.devices.get(chosen) if chosen else None
                else:
                    remote_device = lookup.device(neighbor)
                if not remote_device:
                    report["cables_skipped"] += 1
                    continue
                if remote_device.id == device_id:
                    continue
                remote = lookup.interface(remote_device, neighbor)
                if not remote or getattr(remote, "cable", None):
                    report["cables_skipped"] += 1
                    continue
                report["cables_created"] += 1
                report["changes"].append(f"Kabel {device.name} {label}")
                if not dry_run:
                    nb.dcim.cables.create({
                        "a_terminations": [{"object_type": "dcim.interface", "object_id": local.id}],
                        "b_terminations": [{"object_type": "dcim.interface", "object_id": remote.id}],
                        "status": "connected",
                        "tenant": tenant_id,
                    })
                    remote.cable = True  # avoid a second cable from the reverse direction
            except Exception as error:  # noqa: BLE001
                report["errors"].append(f"Nachbar {label}: {error}")
    return report


def describe_snapshot(snapshot):
    return (
        f"{len(snapshot['interfaces'])} Schnittstellen, {len(snapshot.get('ip_addresses', []))} IP-Adressen, "
        f"{len(snapshot.get('lldp_neighbors', []))} LLDP- und {len(snapshot.get('cdp_neighbors', []))} CDP-Nachbarn"
    )

