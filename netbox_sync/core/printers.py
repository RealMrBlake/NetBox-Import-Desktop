"""Printers found by an SNMP network scan, documented in NetBox.

Every address is pinged (or probed on the printer ports 9100/515/631), then
queried via SNMP. The credentials of the profile are tried in order – SNMPv2c
communities and SNMPv3 users – and the one that works is remembered per
address. Printers are recognised by the Host Resources MIB (hrDeviceType
printer) or the Printer MIB (RFC 3805), so this works for every manufacturer.
Devices that only answer on printer ports are listed as "printer without SNMP".

The sync reuses the access point import: devices are matched by serial number,
MAC address, name and IP address. Page counter, supplies, colour capability and
the date of the scan are optionally stored in custom fields.
"""

import datetime
import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from netbox_sync.core import netscan, snmp
from netbox_sync.core.ap_sync import CHUNK, APSyncEngine
from netbox_sync.core.netbox import normalize_mac, related_id, version_tuple

PRINTER_PORTS = (9100, 631, 515)

SYS = "1.3.6.1.2.1.1"
SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME = f"{SYS}.1.0", f"{SYS}.2.0", f"{SYS}.3.0"
SYS_CONTACT, SYS_NAME, SYS_LOCATION = f"{SYS}.4.0", f"{SYS}.5.0", f"{SYS}.6.0"
HR_DEVICE_TYPE = "1.3.6.1.2.1.25.3.2.1.2"
HR_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.3"
HR_PRINTER = "1.3.6.1.2.1.25.3.1.5"
HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1"
HR_PRINTER_ERRORS = "1.3.6.1.2.1.25.3.5.1.2"
PRT_SERIAL = "1.3.6.1.2.1.43.5.1.1.17"
PRT_INPUT = "1.3.6.1.2.1.43.8.2.1"
PRT_LIFE_COUNT = "1.3.6.1.2.1.43.10.2.1.4"
PRT_SUPPLIES = "1.3.6.1.2.1.43.11.1.1"
PRT_COLORANT = "1.3.6.1.2.1.43.12.1.1.4"
PRT_DISPLAY = "1.3.6.1.2.1.43.16.5.1.2"
PRT_ALERT_DESCR = "1.3.6.1.2.1.43.18.1.1.8"
IF_TABLE = "1.3.6.1.2.1.2.2.1"
IP_ADDR_IFINDEX = "1.3.6.1.2.1.4.20.1.2"
IP_ADDR_MASK = "1.3.6.1.2.1.4.20.1.3"
ENT_SOFTWARE_REV = "1.3.6.1.2.1.47.1.1.1.1.10"
ENT_FIRMWARE_REV = "1.3.6.1.2.1.47.1.1.1.1.9"

# IANA enterprise numbers of printer manufacturers (from sysObjectID).
ENTERPRISES = {
    11: "HP", 1347: "Kyocera", 2435: "Brother", 367: "Ricoh", 1602: "Canon", 18334: "Konica Minolta",
    641: "Lexmark", 253: "Xerox", 1248: "Epson", 2001: "OKI", 674: "Dell", 236: "Samsung", 297: "Fujifilm",
    1129: "Toshiba", 2385: "Sharp", 26696: "HP", 2699: "Printer Working Group", 1552: "Utax", 3369: "Sharp",
    10642: "Zebra", 683: "Zebra", 5530: "Develop", 4322: "Develop", 15315: "Muratec", 3793: "Panasonic",
}
MANUFACTURER_NAMES = (
    "Hewlett-Packard", "HP", "Kyocera", "Brother", "Ricoh", "Canon", "Konica Minolta", "Lexmark", "Xerox", "Epson",
    "OKI", "Dell", "Samsung", "Fujifilm", "Fuji Xerox", "Toshiba", "Sharp", "Utax", "TA Triumph-Adler", "Develop",
    "Zebra", "Muratec", "Panasonic", "Olivetti", "Kyocera Mita", "Konica", "Minolta", "Nashuatec", "Gestetner",
)
PRINTER_STATUS = {1: "sonstiges", 2: "unbekannt", 3: "bereit", 4: "druckt", 5: "wärmt auf"}
ERROR_BITS = ("wenig Papier", "kein Papier", "wenig Toner", "kein Toner", "Klappe offen", "Papierstau", "offline",
              "Service angefordert", "Papierfach fehlt", "Ausgabefach fehlt", "Verbrauchsmaterial fehlt",
              "Ausgabefach fast voll", "Ausgabefach voll", "Papierfach leer", "Wartung überfällig")
SUPPLY_TYPES = {3: "Toner", 4: "Resttoner", 5: "Tinte", 6: "Tintenpatrone", 7: "Tintenbehälter", 9: "Trommel",
                10: "Entwickler", 15: "Fixiereinheit", 20: "Transfereinheit", 21: "Tonerkassette",
                22: "Fixieröl", 32: "Heftklammern"}

CUSTOM_FIELDS = (
    ("printer_page_count", "Seitenzähler", "integer"),
    ("printer_supplies", "Verbrauchsmaterial", "longtext"),
    ("printer_color", "Farbdrucker", "boolean"),
    ("printer_last_scan", "Letzter Drucker-Scan", "date"),
)
ROLE_HINTS = ("drucker", "printer", "multifunktion", "mfp", "mfd", "multifunction")


class PrinterError(ValueError):
    pass


# ----------------------------------------------------------------------
# Reading a printer
# ----------------------------------------------------------------------

def _first(values):
    return next((value for _key, value in sorted(values.items(), key=lambda item: snmp.oid_key(item[0]))
                 if snmp.text(value)), None)


def manufacturer_from(object_id, *texts):
    for text in texts:
        for name in MANUFACTURER_NAMES:
            if re.search(rf"\b{re.escape(name)}\b", text or "", re.IGNORECASE):
                return {"Hewlett-Packard": "HP", "Kyocera Mita": "Kyocera", "Fuji Xerox": "Fujifilm",
                        "Konica": "Konica Minolta", "Minolta": "Konica Minolta"}.get(name, name)
    parts = snmp.oid_key(object_id) if object_id else ()
    if parts[:6] == (1, 3, 6, 1, 4, 1) and len(parts) > 6:
        return ENTERPRISES.get(parts[6], "")
    return ""


def clean_model(text, manufacturer):
    model = re.sub(r"\s+", " ", snmp.text(text)).strip(" ;,")
    model = re.split(r";|,| - |\bFW\b|\bFirmware\b|\bS/N\b", model)[0].strip()
    for prefix in (manufacturer, "Hewlett-Packard", "HP", "Kyocera Mita", "Konica Minolta", "KONICA MINOLTA"):
        if prefix and model.casefold().startswith(prefix.casefold() + " "):
            model = model[len(prefix) + 1:].strip()
    return model[:100]


def mac_text(value):
    if isinstance(value, bytes) and len(value) == 6 and any(value):
        return normalize_mac(value.hex()) or ""
    return normalize_mac(snmp.text(value)) or "" if value else ""


def error_flags(value):
    if not isinstance(value, bytes):
        return []
    flags = []
    for index, name in enumerate(ERROR_BITS):
        byte, bit = divmod(index, 8)
        if byte < len(value) and value[byte] & (0x80 >> bit):
            flags.append(name)
    return flags


def supplies_from(walked, colorants):
    """List of ``{"name", "type", "level", "max", "percent"}`` from prtMarkerSuppliesTable."""
    supplies = []
    for _index, row in sorted(snmp.table(walked, PRT_SUPPLIES).items()):
        name = snmp.text(row.get(6))
        kind = SUPPLY_TYPES.get(row.get(5), "")
        level, maximum = row.get(9), row.get(8)
        if not name and not kind:
            continue
        colour = colorants.get(row.get(3)) if row.get(3) else ""
        percent = None
        if isinstance(level, int) and isinstance(maximum, int) and level >= 0 and maximum > 0:
            percent = max(0, min(100, round(level * 100 / maximum)))
        state = {-3: "vorhanden", -2: "unbekannt", -1: "sonstiges"}.get(level, "") if isinstance(level, int) else ""
        if isinstance(level, int) and level >= 0 and maximum in (-1, -2, None):
            state = f"{level}"
        supplies.append({"name": name or " ".join(filter(None, (kind, colour))), "type": kind, "colour": colour,
                         "level": level, "max": maximum, "percent": percent, "state": state})
    return supplies


def supplies_text(supplies):
    lines = []
    for item in supplies:
        value = f"{item['percent']} %" if item["percent"] is not None else (item["state"] or "–")
        lines.append(f"{item['name']}: {value}")
    return "\n".join(lines)


def trays_from(walked):
    trays = []
    for _index, row in sorted(snmp.table(walked, PRT_INPUT).items()):
        name = snmp.text(row.get(13)) or snmp.text(row.get(18)) or f"Fach {len(trays) + 1}"
        media = snmp.text(row.get(12))
        capacity, current = row.get(9), row.get(10)
        level = ""
        if isinstance(current, int) and isinstance(capacity, int) and capacity > 0 and current >= 0:
            level = f"{current}/{capacity} Blatt"
        elif current == -3:
            level = "Papier vorhanden"
        elif current == 0:
            level = "leer"
        trays.append({"name": name, "media": media, "capacity": capacity if isinstance(capacity, int) else None,
                      "level": level})
    return trays


def read_printer(client, ip):
    """Printer data via SNMP, or ``None`` when the device is no printer."""
    base = client.get([SYS_DESCR, SYS_OBJECT_ID, SYS_NAME, SYS_LOCATION, SYS_CONTACT, SYS_UPTIME])
    types = client.walk(HR_DEVICE_TYPE, limit=200)
    printer_indexes = [snmp.oid_key(oid)[-1] for oid, value in types.items() if value == HR_PRINTER]
    serials = client.walk(PRT_SERIAL, limit=20)
    counters = client.walk(PRT_LIFE_COUNT, limit=20)
    if not printer_indexes and not serials and not counters:
        return None
    descr = client.get([f"{HR_DEVICE_DESCR}.{index}" for index in printer_indexes]) if printer_indexes else {}
    status = client.get([f"{oid}.{index}" for index in printer_indexes[:1]
                         for oid in (HR_PRINTER_STATUS, HR_PRINTER_ERRORS)]) if printer_indexes else {}
    colorants_raw = client.walk(PRT_COLORANT, limit=50)
    colorants = {snmp.oid_key(oid)[-1]: snmp.text(value) for oid, value in colorants_raw.items()}
    supplies = supplies_from(client.walk(PRT_SUPPLIES, limit=600), colorants)
    trays = trays_from(client.walk(PRT_INPUT, limit=600))
    display = [snmp.text(value) for value in client.walk(PRT_DISPLAY, limit=20).values() if snmp.text(value)]
    alerts = list(dict.fromkeys(snmp.text(value) for value in client.walk(PRT_ALERT_DESCR, limit=100).values()
                                if snmp.text(value)))
    interfaces = snmp.table(client.walk(IF_TABLE, limit=800), IF_TABLE)
    addressing = client.get([f"{IP_ADDR_IFINDEX}.{ip}", f"{IP_ADDR_MASK}.{ip}"])
    firmware = snmp.text(_first(client.walk(ENT_SOFTWARE_REV, limit=50)) or _first(
        client.walk(ENT_FIRMWARE_REV, limit=50)))

    sys_descr = snmp.text(base.get(SYS_DESCR))
    object_id = base.get(SYS_OBJECT_ID) or ""
    model_text = snmp.text(next(iter(descr.values()), "")) if descr else ""
    manufacturer = manufacturer_from(object_id, model_text, sys_descr)
    model = clean_model(model_text or sys_descr, manufacturer)
    if not firmware:
        match = re.search(r"(?:firmware|fw|version|ver\.?)\s*[:=]?\s*([\w.\-]+)", sys_descr, re.IGNORECASE)
        firmware = match.group(1) if match else ""

    if_index = addressing.get(f"{IP_ADDR_IFINDEX}.{ip}")
    mac = mac_text(interfaces.get((if_index,), {}).get(6)) if isinstance(if_index, int) else ""
    if not mac:
        for _index, row in sorted(interfaces.items()):
            if row.get(3) in (6, 71, 117) and mac_text(row.get(6)):
                mac = mac_text(row.get(6))
                break
    prefix_length = None
    mask = addressing.get(f"{IP_ADDR_MASK}.{ip}")
    if isinstance(mask, str):
        try:
            prefix_length = ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
        except ValueError:
            prefix_length = None

    colours = [name for name in colorants.values() if name]
    colour = any(name.casefold() not in ("black", "schwarz", "unknown", "other") for name in colours) or any(
        item["colour"] and item["colour"].casefold() not in ("black", "schwarz") for item in supplies)
    page_count = max((value for value in counters.values() if isinstance(value, int)), default=None)
    status_value = status.get(f"{HR_PRINTER_STATUS}.{printer_indexes[0]}") if printer_indexes else None
    errors = error_flags(status.get(f"{HR_PRINTER_ERRORS}.{printer_indexes[0]}")) if printer_indexes else []
    serial = snmp.text(_first(serials)).upper()
    return {
        "name": snmp.text(base.get(SYS_NAME)), "sys_descr": sys_descr, "object_id": object_id,
        "manufacturer": manufacturer, "model": model, "serial": serial, "firmware": firmware,
        "site_hint": snmp.text(base.get(SYS_LOCATION)), "contact": snmp.text(base.get(SYS_CONTACT)),
        "uptime": base.get(SYS_UPTIME), "mac": mac, "prefix_length": prefix_length,
        "page_count": page_count, "color": bool(colour), "supplies": supplies, "trays": trays,
        "status": PRINTER_STATUS.get(status_value, ""), "errors": errors, "display": display, "alerts": alerts,
    }


# ----------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------

def credential_label(record):
    try:
        return snmp.Credential(record).label
    except snmp.SnmpError:
        return "ungültig"


def _ordered(credentials, preferred_id):
    if not preferred_id:
        return list(credentials)
    return sorted(credentials, key=lambda record: record.get("id") != preferred_id)


def scan_host(ip, credentials, preferred=None, timeout=1.0):
    """``{"ip", "alive", "printer": bool, ...}`` for one address."""
    alive = netscan.ping(ip) is not None
    ports = []
    if not alive:
        ports = [port for port in PRINTER_PORTS if netscan.tcp_open(ip, port, 0.6)]
        if not ports:
            return {"ip": ip, "alive": False, "printer": False}
    result = {"ip": ip, "alive": True, "printer": False, "snmp": "", "credential_id": None, "notes": []}
    try:
        client, notes = snmp.connect(ip, _ordered(credentials, (preferred or {}).get(ip)), timeout=timeout)
        result["notes"] += notes
        info = read_printer(client, ip)
        result["snmp"] = client.credential.label
        result["credential_id"] = client.credential.record.get("id")
        if info:
            result.update(info, printer=True)
            if not result["mac"]:
                result["mac"] = netscan.arp_mac(ip) or ""
            return result
    except snmp.SnmpAuthError as error:
        result["notes"].append(str(error))
    except snmp.SnmpError as error:
        if not isinstance(error, snmp.SnmpTimeout):
            result["notes"].append(f"SNMP-Fehler: {error}")
    except OSError as error:
        result["notes"].append(f"SNMP-Fehler: {error}")
    if result["snmp"]:
        return result  # answers SNMP but is no printer
    if not ports:
        ports = [port for port in PRINTER_PORTS if netscan.tcp_open(ip, port, 0.6)]
    if 9100 in ports or (631 in ports and 515 in ports):
        result.update(printer=True, name=netscan.reverse_dns(ip) or "", mac=netscan.arp_mac(ip) or "",
                      manufacturer="", model="", serial="", firmware="", site_hint="", supplies=[], trays=[],
                      page_count=None, color=None, errors=[], display=[], alerts=[], status="",
                      prefix_length=None)
        result["notes"].append("Druckerports offen (" + ", ".join(map(str, ports)) + "), aber keine SNMP-Antwort"
                               " – Zugangsdaten prüfen")
    return result


def scan(ips, credentials, on_result, cancelled=lambda: False, preferred=None, workers=48, timeout=1.0):
    if not credentials:
        raise PrinterError("Bitte mindestens eine SNMP-Community oder einen SNMPv3-Benutzer hinterlegen.")
    for record in credentials:
        snmp.Credential(record)  # validates protocols early
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_host, ip, credentials, preferred, timeout): ip for ip in ips}
        for future in as_completed(futures):
            if cancelled():
                for pending in futures:
                    pending.cancel()
                break
            try:
                on_result(future.result())
            except Exception as error:  # noqa: BLE001 - one host must not stop the scan
                on_result({"ip": futures[future], "alive": True, "printer": False, "notes": [str(error)]})


def to_import(host):
    """Scan result → record for the sync (same keys as an access point)."""
    name = host.get("name") or netscan.reverse_dns(host["ip"]) or f"Drucker {host['ip']}"
    issues = list(host.get("notes", []))
    if host.get("errors"):
        issues.append("Gerät meldet: " + ", ".join(host["errors"]))
    return dict(host, name=re.sub(r"\s+", " ", name).strip()[:64], serial=(host.get("serial") or "").upper(),
                mac=normalize_mac(host.get("mac")) or "", issues=issues, site_hint=host.get("site_hint") or "")


def mark_duplicates(printers):
    for key, text in (("serial", "Seriennummer"), ("mac", "MAC-Adresse")):
        groups = {}
        for printer in printers:
            if printer.get(key):
                groups.setdefault(printer[key], []).append(printer)
        for value, members in groups.items():
            if len(members) > 1:
                for printer in members:
                    others = ", ".join(other["ip"] for other in members if other is not printer)
                    printer["issues"].append(f"{text} {value} doppelt (auch {others})")


def printer_key(printer):
    return printer.get("serial") or printer.get("mac") or printer["ip"]


# ----------------------------------------------------------------------
# NetBox
# ----------------------------------------------------------------------

class PrinterSyncEngine(APSyncEngine):
    """Access point import with printer identity, IP matching and custom fields."""

    def __init__(self, nb, log=None, role_id=None, counters=True):
        super().__init__(nb, log)
        self.role_id = role_id
        self.counters = counters
        self.today = datetime.date.today().isoformat()

    def _known_devices(self, scope):
        if not self.role_id:
            return []
        return list(self.nb.dcim.devices.filter(role_id=self.role_id, **scope))

    def _identity(self, printer):
        return printer.get("manufacturer") or "Unbekannt", printer.get("model") or "Drucker", None

    def _custom_values(self, printer):
        if not self.counters or not printer.get("snmp"):
            return {}
        values = {"printer_last_scan": self.today}
        if printer.get("page_count") is not None:
            values["printer_page_count"] = printer["page_count"]
        if printer.get("supplies"):
            values["printer_supplies"] = supplies_text(printer["supplies"])
        if printer.get("color") is not None:
            values["printer_color"] = bool(printer["color"])
        return values

    def _extra_payload(self, printer):
        values = self._custom_values(printer)
        return {"custom_fields": values} if values else {}

    def _extra_updates(self, device, printer):
        values = self._custom_values(printer)
        current = dict(getattr(device, "custom_fields", None) or {})
        changed = {key: value for key, value in values.items() if current.get(key) != value}
        if not changed or set(changed) == {"printer_last_scan"} and len(values) > 1:
            return {}, []
        texts = []
        if "printer_page_count" in changed:
            texts.append(f"Seitenzähler {current.get('printer_page_count') or '–'} → {changed['printer_page_count']}")
        if "printer_supplies" in changed:
            texts.append("Verbrauchsmaterial")
        if "printer_color" in changed:
            texts.append("Farbdrucker" if changed["printer_color"] else "Schwarzweiß")
        return {"custom_fields": dict(current, **values)}, texts or ["Scan-Datum"]

    def match(self, printers):
        """Base matching plus the IP address assigned to a device interface."""
        only_netbox = super().match(printers)
        open_items = [printer for printer in printers if printer.get("device") is None and printer.get("ip")]
        if not open_items:
            return only_netbox
        by_ip = {}
        ips = sorted({printer["ip"] for printer in open_items})
        for start in range(0, len(ips), CHUNK):
            for record in self.nb.ipam.ip_addresses.filter(address=ips[start:start + CHUNK]):
                if getattr(record, "assigned_object_type", None) != "dcim.interface":
                    continue
                device_id = related_id(getattr(record, "assigned_object", None), "device")
                if device_id:
                    by_ip.setdefault(str(record.address).split("/")[0], set()).add(device_id)
        matched = set()
        for printer in open_items:
            devices = by_ip.get(printer["ip"], set())
            if len(devices) == 1:
                device = self.nb.dcim.devices.get(next(iter(devices)))
                if device is not None:
                    printer["device"], printer["method"] = device, "IP-Adresse"
                    matched.add(device.id)
                    if related_id(device, "tenant") not in self.tenant_ids:
                        printer["issues"].append("NetBox-Gerät gehört " + (
                            f"Mandant „{getattr(device.tenant, 'name', '')}“ außerhalb der Mandantengruppe"
                            if related_id(device, "tenant") else "keinem Mandanten"))
        return [device for device in only_netbox if device.id not in matched]

    def ensure_custom_fields(self, dry_run, report):
        """Create the printer custom fields that are missing."""
        if not self.counters:
            return
        existing = {field.name for field in self.nb.extras.custom_fields.filter(
            name=[name for name, _label, _type in CUSTOM_FIELDS])}
        key = "object_types" if version_tuple(self.nb) >= (4, 0) else "content_types"
        for name, label, kind in CUSTOM_FIELDS:
            if name in existing:
                continue
            report["changes"].append(f"Benutzerdefiniertes Feld {label} ({name}) anlegen")
            report["objects_created"] += 1
            if not dry_run:
                self.nb.extras.custom_fields.create({
                    "name": name, "label": label, "type": kind, key: ["dcim.device"], "group_name": "Drucker",
                    "required": False, "description": "Aus dem SNMP-Drucker-Scan",
                })

    def sync(self, printers, placements, role_id, rename=True, dry_run=True, default_site=None):
        report = self.new_report()
        try:
            self.ensure_custom_fields(dry_run, report)
        except Exception as error:  # noqa: BLE001 - devices can still be documented
            report["errors"].append(f"Benutzerdefinierte Felder: {error}")
            self.counters = False
        result = super().sync(printers, placements, role_id, rename, dry_run, default_site)
        for key, value in result.items():
            if isinstance(value, list):
                report[key] = report.get(key, []) + value
            else:
                report[key] = report.get(key, 0) + value
        return report
