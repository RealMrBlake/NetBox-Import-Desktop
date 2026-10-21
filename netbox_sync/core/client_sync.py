"""Write scanned Windows clients to NetBox as devices.

Every client becomes a ``dcim.device`` with manufacturer, device type
(model), platform (operating system) and serial number. Network adapters,
MAC addresses and IP addresses are optional. With ``dry_run`` nothing is
written; the report lists what would change.
"""

import re

from netbox_sync.core.netbox import assign_mac, normalize_mac, related_id
from netbox_sync.core.clients import os_label


def _noop(*_args, **_kwargs):
    pass


MANUFACTURERS = {
    "lenovo": "Lenovo", "hp": "HP", "hewlett-packard": "HP", "hpe": "HPE", "dell inc.": "Dell", "dell": "Dell",
    "microsoft corporation": "Microsoft", "fujitsu": "Fujitsu", "fujitsu client computing limited": "Fujitsu",
    "asustek computer inc.": "ASUS", "acer": "Acer", "vmware, inc.": "VMware", "intel corporation": "Intel",
    "micro-star international co., ltd.": "MSI", "gigabyte technology co., ltd.": "Gigabyte",
    "apple inc.": "Apple", "samsung electronics co., ltd.": "Samsung", "medion": "Medion",
    "wortmann_ag": "Wortmann (TERRA)", "wortmann ag": "Wortmann (TERRA)", "panasonic corporation": "Panasonic",
    "dynabook inc.": "Dynabook", "toshiba": "Toshiba", "qemu": "QEMU", "innotek gmbh": "VirtualBox",
}

INVALID_SERIALS = {
    "", "0", "none", "default string", "to be filled by o.e.m.", "system serial number", "not specified",
    "not applicable", "123456789", "0123456789", "chassis serial number", "n/a",
}


def clean_manufacturer(name):
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or name.casefold() in ("to be filled by o.e.m.", "system manufacturer", "default string"):
        return ""
    return MANUFACTURERS.get(name.casefold(), name if not name.isupper() or len(name) <= 4 else name.title())


def clean_serial(serial):
    serial = (serial or "").strip()
    return "" if serial.casefold() in INVALID_SERIALS else serial[:50]


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold().replace("ä", "ae").replace("ö", "oe")
                  .replace("ü", "ue").replace("ß", "ss")).strip("-")
    return slug[:100] or "unbekannt"


def interface_type(interface):
    return "other-wireless" if interface.get("wireless") else "other"


class ClientSyncEngine:
    def __init__(self, nb, log=_noop):
        self.nb = nb
        self.log = log
        self.sites = []
        self.roles = []
        self.tenants = []
        self.existing = {}
        self._manufacturers = {}
        self._device_types = {}
        self._platforms = {}
        self._planned = set()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def load_targets(self):
        self.log("Lade Standorte, Rollen, Mandanten und Geräte aus NetBox …")
        self.sites = sorted(((item.name, item.id) for item in self.nb.dcim.sites.all()), key=lambda x: x[0].casefold())
        self.roles = sorted(((item.name, item.id) for item in self.nb.dcim.device_roles.all()),
                            key=lambda x: x[0].casefold())
        self.tenants = sorted(((item.name, item.id) for item in self.nb.tenancy.tenants.all()),
                              key=lambda x: x[0].casefold())
        self.load_existing()

    def load_existing(self):
        self.existing = {}
        for device in self.nb.dcim.devices.filter(brief=True):
            if getattr(device, "name", None):
                self.existing.setdefault(str(device.name).casefold(), device.id)
        return self.existing

    def create_role(self, name):
        role = self.nb.dcim.device_roles.create({"name": name, "slug": slugify(name), "color": "2196f3",
                                                 "vm_role": False})
        self.roles.append((role.name, role.id))
        self.log(f"Geräterolle angelegt: {name}", "success")
        return role.id

    def _manufacturer(self, name, report, dry_run):
        key = name.casefold()
        if key in self._manufacturers:
            return self._manufacturers[key]
        record = (self.nb.dcim.manufacturers.get(slug=slugify(name))
                  or next(iter(self.nb.dcim.manufacturers.filter(name__ie=name)), None))
        if record is None:
            if dry_run:
                self._note(report, ("manufacturer", key), f"Hersteller anlegen: {name}")
                return None
            record = self.nb.dcim.manufacturers.create({"name": name, "slug": slugify(name)})
            report["objects_created"] += 1
        self._manufacturers[key] = record.id
        return record.id

    def _device_type(self, manufacturer_name, manufacturer_id, model, report, dry_run):
        key = (manufacturer_name.casefold(), model.casefold())
        if key in self._device_types:
            return self._device_types[key]
        record = None
        if manufacturer_id is not None:
            record = next(iter(self.nb.dcim.device_types.filter(manufacturer_id=manufacturer_id, model__ie=model)),
                          None)
        if record is None:
            if dry_run:
                self._note(report, ("type",) + key, f"Gerätetyp anlegen: {manufacturer_name} {model}")
                return None
            record = self.nb.dcim.device_types.create({
                "manufacturer": manufacturer_id, "model": model, "slug": slugify(f"{manufacturer_name}-{model}"),
                "u_height": 0, "is_full_depth": False,
            })
            report["objects_created"] += 1
        self._device_types[key] = record.id
        return record.id

    def _platform(self, name, report, dry_run):
        key = name.casefold()
        if key in self._platforms:
            return self._platforms[key]
        record = (next(iter(self.nb.dcim.platforms.filter(name__ie=name)), None)
                  or self.nb.dcim.platforms.get(slug=slugify(name)))
        if record is None:
            if dry_run:
                self._note(report, ("platform", key), f"Plattform anlegen: {name}")
                return None
            record = self.nb.dcim.platforms.create({"name": name, "slug": slugify(name)})
            report["objects_created"] += 1
        self._platforms[key] = record.id
        return record.id

    def _note(self, report, key, text):
        if key not in self._planned:
            self._planned.add(key)
            report["changes"].append(text)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    @staticmethod
    def new_report():
        return {"devices_created": 0, "devices_updated": 0, "devices_unchanged": 0, "objects_created": 0,
                "interfaces_created": 0, "ips_created": 0, "changes": [], "errors": [], "items": []}

    def sync(self, clients, site_id, role_id, tenant_id=None, interfaces=True, update_existing=True, dry_run=True,
             progress=_noop, cancelled=lambda: False):
        self._planned = set()
        report = self.new_report()
        for index, client in enumerate(clients):
            if cancelled():
                break
            progress(index, len(clients), client["name"])
            try:
                changes = self.sync_client(client, site_id, role_id, tenant_id, interfaces, update_existing,
                                           dry_run, report)
            except Exception as error:  # noqa: BLE001 - keep going with the other clients
                report["errors"].append(f"{client['name']}: {error}")
                self.log(f"{client['name']}: {error}", "error")
                continue
            report["items"].append((client["name"], changes))
        return report

    def sync_client(self, client, site_id, role_id, tenant_id, interfaces, update_existing, dry_run, report):
        changes = []
        manufacturer = clean_manufacturer(client.get("manufacturer")) or "Unbekannt"
        model = (client.get("model") or "").strip() or "Unbekanntes Modell"
        manufacturer_id = self._manufacturer(manufacturer, report, dry_run)
        type_id = self._device_type(manufacturer, manufacturer_id, model, report, dry_run)
        platform = os_label(client)
        platform_id = self._platform(platform, report, dry_run) if platform else None
        serial = clean_serial(client.get("serial"))
        name = client["name"]

        device = next(iter(self.nb.dcim.devices.filter(name__ie=name)), None)
        if device is None:
            changes.append(f"Gerät anlegen · {manufacturer} {model}" + (f" · {platform}" if platform else "")
                           + (f" · S/N {serial}" if serial else ""))
            report["devices_created"] += 1
            if dry_run:
                if interfaces:
                    for interface in client.get("interfaces", []):
                        changes.append(f"Schnittstelle {interface['name']} anlegen"
                                       + (f" ({', '.join(interface['ips'])})" if interface.get("ips") else ""))
                        report["interfaces_created"] += 1
                        report["ips_created"] += len(interface.get("ips", []))
                return changes
            payload = {"name": name, "device_type": type_id, "role": role_id, "site": site_id, "status": "active"}
            if platform_id:
                payload["platform"] = platform_id
            if serial:
                payload["serial"] = serial
            if tenant_id:
                payload["tenant"] = tenant_id
            device = self.nb.dcim.devices.create(payload)
            self.existing[name.casefold()] = device.id
        elif not update_existing:
            report["devices_unchanged"] += 1
            return ["Vorhanden – nicht verändert (Aktualisieren ausgeschaltet)"]
        else:
            updates = {}
            if type_id is None or related_id(device, "device_type") != type_id:
                updates["device_type"] = type_id
                changes.append(f"Modell → {manufacturer} {model}")
            if platform and (platform_id is None or related_id(device, "platform") != platform_id):
                updates["platform"] = platform_id
                changes.append(f"Plattform → {platform}")
            if serial and (getattr(device, "serial", "") or "") != serial:
                updates["serial"] = serial
                changes.append(f"Seriennummer → {serial}")
            if tenant_id and related_id(device, "tenant") is None:
                updates["tenant"] = tenant_id
                changes.append("Mandant setzen")
            if updates:
                report["devices_updated"] += 1
                if not dry_run:
                    device.update(updates)
        if interfaces:
            changes.extend(self._sync_interfaces(device, client, dry_run, report))
        if not changes:
            report["devices_unchanged"] += 1
        return changes

    def _sync_interfaces(self, device, client, dry_run, report):
        changes = []
        existing = {str(item.name).casefold(): item for item in self.nb.dcim.interfaces.filter(device_id=device.id)}
        primary = None
        for interface in client.get("interfaces", []):
            record = existing.get(interface["name"].casefold())
            mac = normalize_mac(interface.get("mac"))
            if record is None and mac:
                record = next((item for item in existing.values()
                               if normalize_mac(getattr(item, "mac_address", None)) == mac), None)
            if record is None:
                changes.append(f"Schnittstelle {interface['name']} anlegen")
                report["interfaces_created"] += 1
                if not dry_run:
                    record = self.nb.dcim.interfaces.create({
                        "device": device.id, "name": interface["name"][:64], "type": interface_type(interface),
                        "description": interface.get("description", "")[:200],
                    })
            if record is not None and mac and not dry_run:
                assign_mac(self.nb, record, mac, "dcim.interface")
            for address in interface.get("ips", []):
                ip, change = self._sync_ip(address, record, interface.get("dhcp"), dry_run, report)
                if change:
                    changes.append(change)
                if ip is not None and primary is None and ":" not in address and interface.get("gateway"):
                    primary = ip
        if primary is not None and not dry_run and related_id(device, "primary_ip4") != primary.id:
            device.update({"primary_ip4": primary.id})
        return changes

    def _sync_ip(self, address, interface, dhcp, dry_run, report):
        matches = [ip for ip in self.nb.ipam.ip_addresses.filter(address=address.split("/")[0])
                   if related_id(ip, "vrf") is None]
        if not matches:
            report["ips_created"] += 1
            if dry_run or interface is None:
                return None, f"IP-Adresse {address} anlegen"
            ip = self.nb.ipam.ip_addresses.create({
                "address": address, "status": "dhcp" if dhcp else "active",
                "assigned_object_type": "dcim.interface", "assigned_object_id": interface.id,
            })
            return ip, f"IP-Adresse {address} anlegen"
        ip = matches[0]
        assigned = getattr(ip, "assigned_object_id", None)
        if interface is not None and assigned == interface.id:
            return ip, None
        if assigned:
            return None, f"IP-Adresse {address} ist bereits einem anderen Objekt zugeordnet – übersprungen"
        if not dry_run and interface is not None:
            ip.update({"assigned_object_type": "dcim.interface", "assigned_object_id": interface.id})
        return ip, f"IP-Adresse {address} zuordnen"
