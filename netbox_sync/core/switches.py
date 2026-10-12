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
        return {
            "platform": platform,
            "interfaces": interfaces,
            "ipv4_interfaces": ip_outputs[0],
            "ipv6_interfaces": ip_outputs[1],
            "neighbors": neighbors,
            "cdp_neighbors": cdp_neighbors,
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


def build_snapshot(outputs, platform):
    interfaces = parse_interfaces(outputs["interfaces"], platform)
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
        "raw": outputs,
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
        return None


def _new_report():
    return {
        "interfaces_created": 0, "interfaces_updated": 0, "interfaces_unchanged": 0,
        "ips_created": 0, "ips_updated": 0, "ips_unchanged": 0,
        "cables_created": 0, "cables_skipped": 0,
        "changes": [], "errors": [],
    }


def sync_snapshot_to_netbox(nb, config, snapshot, create_cables=True, dry_run=False, log=_noop):
    """Write a discovery snapshot to NetBox.

    With ``dry_run`` nothing is written; the returned report lists what would
    change. Interface types are only set when an interface is created, so
    device-type templates and manual corrections in NetBox are preserved.
    """
    report = _new_report()
    device_id = int(config["device_id"])
    device = nb.dcim.devices.get(device_id)
    if not device:
        report["errors"].append(f"NetBox-Gerät #{device_id} existiert nicht mehr")
        return report
    tenant_id = related_id(device, "tenant")
    existing = _device_interfaces(nb, device_id)
    records = {}

    for interface in snapshot["interfaces"]:
        key = interface_key(interface["name"])
        record = existing.get(key)
        try:
            if record:
                changes = {}
                if bool(getattr(record, "enabled", True)) != bool(interface.get("enabled", True)):
                    changes["enabled"] = bool(interface.get("enabled", True))
                if changes:
                    report["interfaces_updated"] += 1
                    state = "aktivieren" if changes["enabled"] else "deaktivieren"
                    report["changes"].append(f"Interface {record.name}: {state}")
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
                if interface.get("description"):
                    payload["description"] = interface["description"][:200]
                report["interfaces_created"] += 1
                report["changes"].append(f"Interface {interface['name']} neu ({payload['type']})")
                if not dry_run:
                    record = nb.dcim.interfaces.create(payload)
                    existing[key] = record
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
                host = str(ipaddress.ip_interface(address).ip)
                candidates = [
                    item for item in nb.ipam.ip_addresses.filter(address=host)
                    if related_id(item, "vrf") is None
                ]
                if len(candidates) > 1:
                    raise ValueError("mehrere globale IP-Adressen mit diesem Host gefunden")
                payload = {"address": address, "status": "active", "assigned_object_type": "dcim.interface"}
                if record:
                    payload["assigned_object_id"] = record.id
                if tenant_id:
                    payload["tenant"] = tenant_id
                if candidates:
                    current = candidates[0]
                    differs = (
                        str(current.address) != address
                        or getattr(current, "assigned_object_type", None) != "dcim.interface"
                        or getattr(current, "assigned_object_id", None) != payload.get("assigned_object_id")
                        or (tenant_id and related_id(current, "tenant") != tenant_id)
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

