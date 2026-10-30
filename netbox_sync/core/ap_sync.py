"""Sophos access points from a Sophos Central CSV export into NetBox.

Existing devices are found globally by serial number and MAC address and by
name within the tenant group. Sites come from the Sophos "Current Site" column
and are mapped to NetBox sites of the tenant group (remembered per profile).
Every created object gets a tenant: the one of the NetBox site, otherwise the
selected default tenant.
"""

import csv
import io
import ipaddress
import re

from netbox_sync.core.client_sync import ClientSyncEngine
from netbox_sync.core.netbox import assign_mac, normalize_mac, related_id, uses_mac_objects

MANUFACTURER = "Sophos"
PLATFORM = "Sophos AP"
INTERFACE_NAME = "eth0"
CHUNK = 50

COLUMNS = {
    "name": ("ap name", "name"),
    "mac": ("mac address", "mac"),
    "serial": ("serial number", "serial", "s/n"),
    "ip": ("internal ip address", "ip address", "ip"),
    "model": ("model",),
    "site_hint": ("current site", "site"),
    "firmware": ("firmware",),
    "config_status": ("config status",),
}


class APImportError(ValueError):
    pass


def _compact(text):
    return re.sub(r"[^0-9a-z]", "", (text or "").casefold().replace("ä", "ae").replace("ö", "oe")
                  .replace("ü", "ue").replace("ß", "ss"))


def parse_csv(data):
    """Access points from a Sophos Central export (bytes or text)."""
    if isinstance(data, bytes):
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                data = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    data = data.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(data[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(data), dialect)
    header = [cell.strip().casefold() for cell in next(reader, [])]
    index = {}
    for key, names in COLUMNS.items():
        index[key] = next((header.index(name) for name in names if name in header), None)
    if index["name"] is None or (index["serial"] is None and index["mac"] is None):
        raise APImportError("Die Datei sieht nicht wie ein Sophos-AP-Export aus (Spalten „AP Name“ und "
                            "„Serial Number“ bzw. „MAC Address“ fehlen).")
    aps = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue

        def cell(key):
            position = index[key]
            return row[position].strip() if position is not None and position < len(row) else ""
        ip = cell("ip")
        try:
            ip = str(ipaddress.ip_address(ip)) if ip else ""
        except ValueError:
            ip = ""
        aps.append({
            "name": re.sub(r"\s+", " ", cell("name")).strip(),
            "mac": normalize_mac(cell("mac")) or "",
            "serial": cell("serial").upper(),
            "ip": ip,
            "model": cell("model"),
            "site_hint": re.sub(r"\s+", " ", cell("site_hint")).strip(),
            "firmware": cell("firmware"),
            "config_status": cell("config_status"),
        })
    if not aps:
        raise APImportError("Die Datei enthält keine Access Points.")
    _mark_duplicates(aps)
    return aps


def _mark_duplicates(aps):
    for ap in aps:
        ap["issues"] = []
        if not ap["site_hint"]:
            ap["issues"].append("kein Standort in Sophos")
    for key, text in (("ip", "IP-Adresse"), ("serial", "Seriennummer"), ("mac", "MAC-Adresse")):
        groups = {}
        for ap in aps:
            if ap[key]:
                groups.setdefault(ap[key], []).append(ap)
        for value, members in groups.items():
            if len(members) > 1:
                for ap in members:
                    others = ", ".join(other["name"] for other in members if other is not ap)
                    ap["issues"].append(f"{text} {value} doppelt (auch {others})")


def ap_key(ap):
    """Stable key of an AP for remembered choices."""
    return ap["serial"] or ap["mac"] or ap["name"]


class APSyncEngine(ClientSyncEngine):
    """Reuses the manufacturer/device type/platform helpers of the client import."""

    def __init__(self, nb, log=None):
        super().__init__(nb, log or (lambda *_args, **_kwargs: None))
        self.tenant_id = None
        self.tenant_ids = set()
        self.ap_sites = []
        self.netbox_aps = []

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def load_context(self, tenant_id):
        """Sites and existing Sophos devices of the tenant's tenant group."""
        self.tenant_id = tenant_id
        tenant = self.nb.tenancy.tenants.get(tenant_id)
        if tenant is None:
            raise APImportError(f"Mandant #{tenant_id} existiert nicht.")
        group_id = related_id(tenant, "group")
        scope = {"tenant_group_id": group_id} if group_id else {"tenant_id": tenant_id}
        self.tenant_ids = {tenant_id}
        if group_id:
            self.tenant_ids |= {item.id for item in self.nb.tenancy.tenants.filter(group_id=group_id)}
        tenants = {item.id: item.name for item in self.nb.tenancy.tenants.all()} if group_id else {tenant_id: tenant.name}
        self.ap_sites = sorted(({"id": site.id, "name": site.name, "tenant_id": related_id(site, "tenant"),
                                 "tenant": tenants.get(related_id(site, "tenant"), "")}
                                for site in self.nb.dcim.sites.filter(**scope)), key=lambda row: row["name"].casefold())
        self.roles = sorted(((item.name, item.id) for item in self.nb.dcim.device_roles.all()),
                            key=lambda pair: pair[0].casefold())
        manufacturer = (self.nb.dcim.manufacturers.get(slug="sophos")
                        or next(iter(self.nb.dcim.manufacturers.filter(name__ie=MANUFACTURER)), None))
        self.netbox_aps = list(self.nb.dcim.devices.filter(manufacturer_id=manufacturer.id, **scope)) \
            if manufacturer else []
        return self.ap_sites

    def suggest_site(self, hint):
        """NetBox site for a Sophos site name, or ``None`` when unclear."""
        wanted = _compact(hint)
        if not wanted:
            return None
        exact = [site for site in self.ap_sites if _compact(site["name"]) == wanted]
        if len(exact) == 1:
            return exact[0]["id"]
        partial = [site for site in self.ap_sites
                   if wanted in _compact(site["name"]) or (_compact(site["name"]) and _compact(site["name"]) in wanted)]
        return partial[0]["id"] if len(partial) == 1 else None

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _devices_by(self, field, values):
        found = {}
        values = sorted(set(filter(None, values)))
        for start in range(0, len(values), CHUNK):
            for device in self.nb.dcim.devices.filter(**{field: values[start:start + CHUNK]}):
                key = str(getattr(device, field, "") or "").upper()
                found.setdefault(key, []).append(device)
        return found

    def _devices_by_mac(self, macs):
        found = {}
        macs = sorted(set(filter(None, macs)))
        for start in range(0, len(macs), CHUNK):
            chunk = macs[start:start + CHUNK]
            if uses_mac_objects(self.nb):
                records = self.nb.dcim.mac_addresses.filter(mac_address=chunk, assigned_object_type="dcim.interface")
                pairs = [(record.mac_address, related_id(getattr(record, "assigned_object", None), "device"))
                         for record in records]
            else:
                pairs = [(record.mac_address, related_id(record, "device"))
                         for record in self.nb.dcim.interfaces.filter(mac_address=chunk)]
            for mac, device_id in pairs:
                if device_id:
                    found.setdefault(normalize_mac(mac), set()).add(device_id)
        return found

    def match(self, aps):
        """Attach ``device``/``method`` to every AP and return NetBox APs missing in Sophos."""
        by_serial = self._devices_by("serial", [ap["serial"] for ap in aps])
        by_mac = self._devices_by_mac([ap["mac"] for ap in aps])
        by_name = {}
        for device in self.netbox_aps:
            by_name.setdefault(_compact(device.name), []).append(device)
        cache = {device.id: device for device in self.netbox_aps}
        for devices in by_serial.values():
            cache.update({device.id: device for device in devices})
        matched = set()
        for ap in aps:
            device, method = None, ""
            candidates = by_serial.get(ap["serial"], []) if ap["serial"] else []
            if len(candidates) == 1:
                device, method = candidates[0], "Seriennummer"
            elif len(candidates) > 1:
                ap["issues"].append(f"Seriennummer in NetBox {len(candidates)}× vergeben")
            if device is None and ap["mac"] and len(by_mac.get(ap["mac"], ())) == 1:
                device_id = next(iter(by_mac[ap["mac"]]))
                device = cache.get(device_id) or self.nb.dcim.devices.get(device_id)
                method = "MAC-Adresse"
            if device is None and len(by_name.get(_compact(ap["name"]), [])) == 1:
                device, method = by_name[_compact(ap["name"])][0], "Name"
            ap["device"], ap["method"] = device, method
            if device is not None:
                matched.add(device.id)
                tenant = related_id(device, "tenant")
                if tenant not in self.tenant_ids:
                    ap["issues"].append("NetBox-Gerät gehört " + (
                        f"Mandant „{getattr(device.tenant, 'name', tenant)}“ außerhalb der Mandantengruppe"
                        if tenant else "keinem Mandanten"))
                if ap["serial"] and getattr(device, "serial", "") and device.serial.upper() != ap["serial"]:
                    ap["issues"].append(f"Seriennummer in NetBox abweichend ({device.serial})")
        return [device for device in self.netbox_aps if device.id not in matched]

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _site_tenant(self, site_id):
        site = next((row for row in self.ap_sites if row["id"] == site_id), None)
        return (site or {}).get("tenant_id") or self.tenant_id

    def sync(self, aps, sites, role_id, rename=True, dry_run=True):
        """``sites`` maps ``ap_key`` to the NetBox site ID for new devices."""
        report = self.new_report()
        self._planned = set()
        for ap in aps:
            try:
                changes = self._sync_ap(ap, sites.get(ap_key(ap)), role_id, rename, dry_run, report)
            except Exception as error:  # noqa: BLE001 - one AP must not stop the others
                report["errors"].append(f"{ap['name']}: {error}")
                changes = [f"Fehler: {error}"]
            report["items"].append((ap["name"], changes))
        return report

    def _sync_ap(self, ap, site_id, role_id, rename, dry_run, report):
        device = ap.get("device")
        changes = []
        description = f"Firmware {ap['firmware']}" if ap["firmware"] else ""
        if device is not None:
            tenant = related_id(device, "tenant")
            if tenant is not None and tenant not in self.tenant_ids:
                report["devices_unchanged"] += 1
                return ["Übersprungen – Gerät gehört einem Mandanten außerhalb der Mandantengruppe"]
            updates = {}
            if rename and ap["name"] and device.name != ap["name"]:
                updates["name"] = ap["name"]
            if ap["serial"] and not getattr(device, "serial", ""):
                updates["serial"] = ap["serial"]
            if tenant is None:
                updates["tenant"] = self._site_tenant(related_id(device, "site"))
            if description and (getattr(device, "description", "") or "").startswith("Firmware") \
                    and device.description != description:
                updates["description"] = description
            if updates:
                report["devices_updated"] += 1
                changes.append("Aktualisieren: " + ", ".join(
                    f"{key} → {value}" for key, value in updates.items() if key != "tenant")
                    + (" (Mandant ergänzt)" if "tenant" in updates else ""))
                if not dry_run:
                    device.update(updates)
            else:
                report["devices_unchanged"] += 1
                changes.append(f"Vorhanden – gefunden über {ap['method']}")
            tenant_id = tenant or self._site_tenant(related_id(device, "site"))
        else:
            if not site_id:
                raise APImportError("kein NetBox-Standort zugeordnet")
            if not role_id:
                raise APImportError("keine Geräterolle gewählt")
            manufacturer_id = self._manufacturer(MANUFACTURER, report, dry_run)
            type_id = self._device_type(MANUFACTURER, manufacturer_id, ap["model"] or "Access Point", report, dry_run)
            platform_id = self._platform(PLATFORM, report, dry_run)
            tenant_id = self._site_tenant(site_id)
            payload = {"name": ap["name"], "device_type": type_id, "role": role_id, "site": site_id,
                       "tenant": tenant_id, "status": "active", "platform": platform_id}
            if ap["serial"]:
                payload["serial"] = ap["serial"]
            if description:
                payload["description"] = description
            report["devices_created"] += 1
            changes.append(f"Gerät anlegen ({ap['model'] or 'Access Point'})")
            if dry_run:
                if ap["mac"] or ap["ip"]:
                    report["interfaces_created"] += 1
                    changes.append(f"Schnittstelle {INTERFACE_NAME} anlegen" + (f" mit MAC {ap['mac']}" if ap["mac"]
                                                                               else ""))
                if ap["ip"]:
                    report["ips_created"] += 1
                    changes.append(f"IP-Adresse {ap['ip']} anlegen")
                return changes
            device = self.nb.dcim.devices.create(payload)
        if dry_run and device is None:
            return changes
        changes += self._sync_network(device, ap, tenant_id, dry_run, report)
        return changes

    def _interface(self, device, ap, dry_run, report):
        interfaces = list(self.nb.dcim.interfaces.filter(device_id=device.id))
        if ap["mac"]:
            for interface in interfaces:
                if normalize_mac(getattr(interface, "mac_address", None)) == ap["mac"]:
                    return interface, []
        named = next((item for item in interfaces if item.name == INTERFACE_NAME), None)
        if named or interfaces:
            return named or interfaces[0], []
        report["interfaces_created"] += 1
        if dry_run:
            return None, [f"Schnittstelle {INTERFACE_NAME} anlegen"]
        interface = self.nb.dcim.interfaces.create({"device": device.id, "name": INTERFACE_NAME, "type": "1000base-t"})
        return interface, [f"Schnittstelle {INTERFACE_NAME} anlegen"]

    def _sync_network(self, device, ap, tenant_id, dry_run, report):
        if not ap["mac"] and not ap["ip"]:
            return []
        interface, changes = self._interface(device, ap, dry_run, report)
        if ap["mac"] and interface is not None and not dry_run:
            if assign_mac(self.nb, interface, ap["mac"], "dcim.interface"):
                changes.append(f"MAC {ap['mac']} zugeordnet")
        if not ap["ip"]:
            return changes
        prefix = next(iter(self.nb.ipam.prefixes.filter(contains=ap["ip"], vrf_id="null")), None)
        length = str(getattr(prefix, "prefix", "")).split("/")[-1] if prefix else "32"
        address = f"{ap['ip']}/{length}"
        existing = list(self.nb.ipam.ip_addresses.filter(address=ap["ip"], vrf_id="null"))
        if existing:
            ip = existing[0]
            assigned = getattr(ip, "assigned_object_id", None)
            if interface is not None and assigned == interface.id:
                pass
            elif assigned:
                changes.append(f"IP-Adresse {ap['ip']} ist bereits einem anderen Objekt zugeordnet – übersprungen")
                return changes
            else:
                changes.append(f"IP-Adresse {ap['ip']} zuordnen")
                if not dry_run and interface is not None:
                    ip.update({"assigned_object_type": "dcim.interface", "assigned_object_id": interface.id,
                               "tenant": related_id(ip, "tenant") or tenant_id})
        else:
            report["ips_created"] += 1
            changes.append(f"IP-Adresse {address} anlegen")
            if dry_run or interface is None:
                return changes
            ip = self.nb.ipam.ip_addresses.create({
                "address": address, "status": "active", "tenant": tenant_id,
                "assigned_object_type": "dcim.interface", "assigned_object_id": interface.id,
            })
        if not dry_run and related_id(device, "primary_ip4") != ip.id:
            device.update({"primary_ip4": ip.id})
            changes.append(f"Primäre IP {ap['ip']}")
        return changes
