"""Network scanner: find hosts in IP ranges and compare them with NetBox (read only).

Reachability uses the Windows ICMP API (no administrator rights, no ping.exe),
ARP for the local subnet and TCP ports, so hosts that block ICMP are found as
well. Names come from reverse DNS and NetBIOS. Nothing is written to NetBox.
"""

import ctypes
import ipaddress
import re
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from netbox_sync.core.netbox import related_id

SERVICES = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP", 110: "POP3", 135: "RPC",
    139: "NetBIOS", 143: "IMAP", 161: "SNMP", 389: "LDAP", 443: "HTTPS", 445: "SMB", 515: "LPD", 631: "IPP",
    636: "LDAPS", 993: "IMAPS", 1433: "MSSQL", 3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC",
    5985: "WinRM", 5986: "WinRM-HTTPS", 8006: "Proxmox", 8080: "HTTP-Alt", 8443: "HTTPS-Alt", 9100: "Druck",
}
DEFAULT_PORTS = "21-23,25,53,80,135,139,443,445,3389,5900,5985,8006,8080,8443,9100"
# Probed on hosts that do not answer ping, to find hosts behind a firewall.
QUICK_PORTS = (445, 3389, 80, 443, 22)
MAX_HOSTS = 4096


class ScanError(ValueError):
    pass


def parse_ports(text):
    """``"21-23,80, 443"`` → sorted unique port numbers."""
    ports = set()
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ScanError(f"Ungültige Port-Angabe: {part}")
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if not 1 <= start <= end <= 65535:
            raise ScanError(f"Ungültiger Port-Bereich: {part}")
        ports.update(range(start, end + 1))
    if len(ports) > 1024:
        raise ScanError("Bitte höchstens 1024 Ports angeben.")
    return sorted(ports)


def expand_ips(text):
    """IPv4 addresses from ranges, networks and names (names are resolved)."""
    from netbox_sync.core.clients import ClientScanError, expand_targets
    try:
        targets = expand_targets(text)
    except ClientScanError as error:
        raise ScanError(str(error)) from error
    result = []
    for target in targets:
        try:
            address = ipaddress.ip_address(target)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.gethostbyname(target))
            except (OSError, ValueError) as error:
                raise ScanError(f"Name nicht auflösbar: {target}") from error
        if address.version != 4:
            raise ScanError(f"Nur IPv4 wird gescannt: {target}")
        if str(address) not in result:
            result.append(str(address))
    if not result:
        raise ScanError("Bitte IP-Adressen, Bereiche (192.0.2.1-254) oder Netze (192.0.2.0/24) angeben.")
    if len(result) > MAX_HOSTS:
        raise ScanError(f"Zu viele Adressen ({len(result)}) – höchstens {MAX_HOSTS} pro Scan.")
    return result


def local_networks():
    """IPv4 /24 networks of this computer (for the "own network" button)."""
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    networks = []
    for address in sorted(addresses):
        ip = ipaddress.ip_address(address)
        if not (ip.is_loopback or ip.is_link_local):
            network = str(ipaddress.ip_network(f"{address}/24", strict=False))
            if network not in networks:
                networks.append(network)
    return networks


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

def _inet_addr(ip):
    return struct.unpack("<I", socket.inet_aton(ip))[0]


if sys.platform == "win32":
    class _IpOptions(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class _EchoReply(ctypes.Structure):
        _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_ulong),
                    ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                    ("Options", _IpOptions)]

    _iphlpapi = ctypes.windll.iphlpapi
    _iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
    _iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
    _iphlpapi.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    _iphlpapi.SendARP.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]


def ping(ip, timeout_ms=800):
    """Round-trip time in ms, or ``None`` without answer."""
    if sys.platform != "win32":
        return None
    handle = _iphlpapi.IcmpCreateFile()
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        payload = b"netbox-sync-scan"
        buffer = ctypes.create_string_buffer(ctypes.sizeof(_EchoReply) + len(payload) + 8)
        count = _iphlpapi.IcmpSendEcho(handle, _inet_addr(ip), payload, len(payload), None, buffer,
                                       ctypes.sizeof(buffer), timeout_ms)
        if count:
            reply = _EchoReply.from_buffer(buffer)
            if reply.Status == 0:
                return int(reply.RoundTripTime)
        return None
    finally:
        _iphlpapi.IcmpCloseHandle(handle)


def arp_mac(ip):
    """MAC address via ARP – only for hosts in a directly connected subnet."""
    if sys.platform != "win32":
        return None
    mac = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(8)
    if _iphlpapi.SendARP(_inet_addr(ip), 0, mac, ctypes.byref(length)) != 0 or length.value < 6:
        return None
    value = ":".join(f"{byte:02X}" for byte in mac[:6])
    return None if value == "00:00:00:00:00:00" else value


def reverse_dns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""


_NBSTAT_QUERY = (b"\x13\x37\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                 b"\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00\x00\x21\x00\x01")


def parse_nbstat(data):
    """``(computer name, workgroup, MAC)`` from a NetBIOS node status answer."""
    if len(data) < 57:
        return "", "", None
    count = data[56]
    offset = 57
    name = group = ""
    for _index in range(count):
        entry = data[offset:offset + 18]
        if len(entry) < 18:
            break
        text = entry[:15].decode("latin-1").strip()
        suffix, flags = entry[15], struct.unpack(">H", entry[16:18])[0]
        if suffix == 0x00 and not flags & 0x8000 and not name:
            name = text
        elif suffix == 0x00 and flags & 0x8000 and not group:
            group = text
        offset += 18
    mac_bytes = data[offset:offset + 6]
    mac = ":".join(f"{byte:02X}" for byte in mac_bytes) if len(mac_bytes) == 6 and any(mac_bytes) else None
    return name, group, mac


def netbios(ip, timeout=0.8):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(_NBSTAT_QUERY, (ip, 137))
            data, _address = sock.recvfrom(2048)
    except OSError:
        return "", "", None
    return parse_nbstat(data)


def tcp_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_host(ip, ports, timeout=0.6):
    """Probe one address. ``alive`` is false when nothing answered."""
    started = time.monotonic()
    rtt = ping(ip, int(timeout * 1000) + 200)
    mac = arp_mac(ip)
    open_ports = []
    alive = rtt is not None or mac is not None
    if not alive:
        # Windows hosts often block ping; a few typical ports reveal them anyway.
        for port in QUICK_PORTS:
            if tcp_open(ip, port, timeout):
                open_ports.append(port)
                alive = True
                break
    if not alive:
        return {"ip": ip, "alive": False}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(ports)))) as pool:
        results = pool.map(lambda port: (port, tcp_open(ip, port, timeout)), [p for p in ports if p not in open_ports])
        open_ports += [port for port, is_open in results if is_open]
    name, workgroup, nb_mac = netbios(ip)
    return {
        "ip": ip, "alive": True, "rtt": rtt, "mac": mac or nb_mac, "dns_name": reverse_dns(ip),
        "netbios_name": name, "workgroup": workgroup,
        "ports": [{"port": port, "service": SERVICES.get(port, "")} for port in sorted(open_ports)],
        "duration": round(time.monotonic() - started, 2),
    }


def scan(ips, ports, on_result, cancelled=lambda: False, workers=64, timeout=0.6):
    """Scan ``ips`` in parallel; ``on_result`` gets every host as soon as it is done."""
    with ThreadPoolExecutor(max_workers=min(workers, len(ips))) as pool:
        futures = {pool.submit(scan_host, ip, ports, timeout): ip for ip in ips}
        for future in as_completed(futures):
            if cancelled():
                for pending in futures:
                    pending.cancel()
                break
            try:
                on_result(future.result())
            except Exception as error:  # noqa: BLE001 - one host must not stop the scan
                on_result({"ip": futures[future], "alive": False, "error": str(error)})


# --------------------------------------------------------------------------
# NetBox comparison (read only)
# --------------------------------------------------------------------------

def _assigned_name(record):
    obj = getattr(record, "assigned_object", None)
    if obj is None:
        return "", ""
    for attribute, kind in (("device", "Gerät"), ("virtual_machine", "VM")):
        parent = getattr(obj, attribute, None)
        if parent is not None:
            return getattr(parent, "name", "") or "", kind
    return getattr(obj, "name", "") or "", ""


def netbox_lookup(nb, ips):
    """``{ip: [entry, …]}`` for all NetBox IP addresses of the scanned hosts.

    Queries one ``parent`` filter per /24 instead of one request per host.
    """
    wanted = set(ips)
    blocks = sorted({str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in ips})
    found = {}
    for block in blocks:
        for record in nb.ipam.ip_addresses.filter(parent=block):
            host = str(record.address).split("/")[0]
            if host not in wanted:
                continue
            name, kind = _assigned_name(record)
            vrf = getattr(getattr(record, "vrf", None), "name", None)
            status = getattr(record, "status", None)
            found.setdefault(host, []).append({
                "id": record.id,
                "dns_name": getattr(record, "dns_name", "") or "",
                "object": name, "object_kind": kind,
                "vrf": vrf or "", "tenant_id": related_id(record, "tenant"),
                "tenant": getattr(getattr(record, "tenant", None), "name", "") or "",
                "status": getattr(status, "label", None) or getattr(status, "value", None) or str(status or ""),
                "description": getattr(record, "description", "") or "",
                "url": getattr(record, "display_url", None) or "",
            })
    return found


def _short(name):
    return (name or "").split(".")[0].strip().casefold()


def compare(host, entries):
    """Comparison state of one scanned host with its NetBox entries."""
    alive = host.get("alive")
    if not entries:
        return "missing" if alive else "unknown"
    if not alive:
        return "offline"
    names = {_short(host.get("dns_name")), _short(host.get("netbios_name"))} - {""}
    if not names:
        return "match"  # nothing to compare against – the address is documented
    for entry in entries:
        if names & ({_short(entry["dns_name"]), _short(entry["object"])} - {""}):
            return "match"
    return "differs"


COMPARE_TEXT = {
    "match": "stimmt überein",
    "differs": "Name abweichend",
    "missing": "nicht in NetBox",
    "offline": "in NetBox, nicht erreichbar",
    "unknown": "",
    "pending": "…",
}
