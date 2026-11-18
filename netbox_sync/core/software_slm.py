"""Installed software of agent clients as netbox-slm installations.

The netbox-slm plugin stores software products, versions and installations; the client software
overview of netbox-utilities shows them per tenant. Names, versions and the marker comment match
netbox-utilities, which transfers the same report from the device comments on every save. Both
ways can run side by side without creating duplicates, and neither removes installations
entered by hand.
"""

import re

from netbox_sync.core.netbox import related_id

_UNKNOWN = object()
# Same text as netbox-utilities: only installations with this comment are removed again.
SYNC_MARKER = "Automatisch aus dem Inventarbericht übernommen (netbox-utilities)."
UNKNOWN_VERSION = "unbekannt"

_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|llc|l\.l\.c\.|ltd\.?|limited|gmbh(\s*&\s*co\.?\s*kg)?|ag|se|"
    r"s\.?a\.?|s\.?r\.?l\.?|b\.?v\.?|co\.?|kg|plc|oy|ab|a/s|pty)$",
    re.IGNORECASE,
)


def _text(value):
    """Whitespace collapsed, ``|`` replaced – exactly what netbox-utilities reads from the report table."""
    return " ".join(str(value or "").replace("|", "/").split())


def clean_name(name, version):
    """Remove a version the vendor appended to the product name ("Foo 1.2", "Foo - 1.2.3")."""
    name = _text(name)
    version = str(version or "").strip()
    if not version:
        return name
    match = re.match(r"^(?P<base>.+?)\s*(?:-\s*)?v?(?P<tail>\d[\w.\-+]*)$", name)
    if match:
        tail = match.group("tail")
        if version.startswith(tail) or tail.startswith(version):
            return match.group("base").rstrip(" -")
    return name


def publisher_name(publisher):
    """Manufacturer name for a software publisher: "Adobe Inc." → "Adobe", "Microsoft Corporation" → "Microsoft"."""
    from netbox_sync.core.client_sync import clean_manufacturer

    name = _text(publisher)
    mapped = clean_manufacturer(name)
    if mapped.casefold() != name.casefold():
        return mapped[:100]
    previous = None
    while name and name != previous:
        previous = name
        name = _LEGAL_SUFFIX.sub("", name).strip(" ,.-")
    return (name or _text(publisher))[:100]


def software_entries(software):
    """``(name, version, publisher)`` per program, unique by name and version like netbox-utilities."""
    entries = {}
    for item in software or []:
        version = _text(item.get("version")) or UNKNOWN_VERSION
        name = clean_name(item.get("name"), version)[:128]
        if not name:
            continue
        version = version[:64]
        entries.setdefault((name.casefold(), version.casefold()), (name, version, _text(item.get("publisher"))))
    return entries


class SlmSoftwareSync:
    """Brings the netbox-slm installations of a device in line with the scanned software list.

    ``engine`` is the :class:`ClientSyncEngine`; its manufacturer lookup and planned-change notes
    are shared, so a manufacturer used by hardware and software is created only once.
    """

    def __init__(self, nb, engine, log):
        self.nb = nb
        self.engine = engine
        self.log = log
        self._api = _UNKNOWN
        self._products = None   # name.casefold() -> record
        self._versions = None   # (product id, name.casefold()) -> record
        self._planned_products = {}  # dry run: name.casefold() -> placeholder id

    # ------------------------------------------------------------------
    # Plugin lookups
    # ------------------------------------------------------------------

    def api(self):
        """``plugins/slm`` when netbox-slm is installed and readable, else ``None``."""
        if self._api is _UNKNOWN:
            try:
                api = self.nb.plugins.slm
                list(api.softwareproducts.filter(name="netbox-sync-probe"))
                self._api = api
            except Exception as error:  # noqa: BLE001 - plugin missing (404), no permission or no plugins API
                self._api = None
                self.log(f"netbox-slm nicht verfügbar – Software wird nicht als Installation gepflegt ({error})",
                         "warning")
        return self._api

    def _load(self):
        if self._products is not None:
            return
        api = self.api()
        self._products, self._versions = {}, {}
        for product in api.softwareproducts.all():
            self._products.setdefault(str(product.name).casefold(), product)
        for version in api.softwareproductversions.all():
            key = (related_id(version, "software_product"), str(version.name).casefold())
            self._versions.setdefault(key, version)

    def _find_product(self, name):
        """Cache first, then NetBox – netbox-utilities may have created it from the comments meanwhile."""
        key = name.casefold()
        if key not in self._products:
            found = sorted((item for item in self.api().softwareproducts.filter(name=name)
                            if str(item.name).casefold() == key), key=lambda item: item.id)
            if found:
                self._products[key] = found[0]
        return self._products.get(key)

    def _find_version(self, product_id, name):
        key = (product_id, name.casefold())
        if key not in self._versions:
            found = sorted((item for item in self.api().softwareproductversions.filter(software_product_id=product_id,
                                                                                         name=name)
                            if str(item.name).casefold() == key[1]), key=lambda item: item.id)
            if found:
                self._versions[key] = found[0]
        return self._versions.get(key)

    def _product(self, name, publisher, report, dry_run):
        manufacturer = publisher_name(publisher) if publisher else ""
        manufacturer_id = self.engine._manufacturer(manufacturer, report, dry_run) if manufacturer else None
        product = self._find_product(name)
        if product is None:
            if dry_run:
                key = name.casefold()
                if key not in self._planned_products:
                    self._planned_products[key] = -(len(self._planned_products) + 1)
                    self.engine._note(report, ("slm-product", key), f"Softwareprodukt anlegen: {name}"
                                      + (f" ({manufacturer})" if manufacturer else ""))
                return self._planned_products[key]
            payload = {"name": name}
            if manufacturer_id:
                payload["manufacturer"] = manufacturer_id
            elif publisher:
                payload["description"] = publisher[:255]
            product = self.api().softwareproducts.create(payload)
            self._products[name.casefold()] = product
            report["objects_created"] += 1
        elif manufacturer and related_id(product, "manufacturer") is None:
            # netbox-utilities knows only existing manufacturers and keeps the publisher as description.
            self.engine._note(report, ("slm-manufacturer", name.casefold()),
                              f"Softwareprodukt {name}: Hersteller {manufacturer} setzen")
            if not dry_run and manufacturer_id:
                updates = {"manufacturer": manufacturer_id}
                if (getattr(product, "description", "") or "").strip() == publisher:
                    updates["description"] = ""
                product.update(updates)
                product.manufacturer = manufacturer_id
        return product.id

    def _version(self, product_id, name, report, dry_run):
        version = self._find_version(product_id, name) if product_id > 0 else None
        if version is None:
            if dry_run:
                return None
            version = self.api().softwareproductversions.create({"software_product": product_id, "name": name})
            self._versions[(product_id, name.casefold())] = version
            report["objects_created"] += 1
        return version.id

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(self, client, device, dry_run, report):
        """Create missing products, versions and installations; remove automatic ones that left the list.

        Only agent scans carry a software list – other clients and empty lists change nothing.
        """
        if client.get("source") != "agent" or not client.get("software") or self.api() is None:
            return []
        self._load()
        wanted = software_entries(client["software"])
        present = {}
        if device is not None:
            for installation in self.api().softwareproductinstallations.filter(device_id=device.id):
                key = (self._name(installation, "software_product"), self._name(installation, "version"))
                present.setdefault(key, installation)

        new = []
        for key, (name, version, publisher) in wanted.items():
            product_id = self._product(name, publisher, report, dry_run)
            if key in present:
                continue
            version_id = self._version(product_id, version, report, dry_run)
            new.append({"device": getattr(device, "id", None), "software_product": product_id, "version": version_id,
                        "comments": SYNC_MARKER})
        stale = [installation for key, installation in present.items()
                 if key not in wanted and (getattr(installation, "comments", "") or "") == SYNC_MARKER]

        if not dry_run:
            if new:
                self.api().softwareproductinstallations.create(new)
            if stale:
                self.api().softwareproductinstallations.delete(stale)
        report["software_installed"] += len(new)
        report["software_removed"] += len(stale)
        parts = [f"{len(new)} anlegen" if new else "", f"{len(stale)} entfernen" if stale else ""]
        return [f"Software-Installationen (netbox-slm): {', '.join(part for part in parts if part)}"] if new or stale \
            else []

    def _name(self, installation, attribute):
        """Name of the nested product/version; the brief nested object normally carries it already."""
        value = getattr(installation, attribute, None)
        name = getattr(value, "name", None) if not isinstance(value, (int, dict)) else (
            value.get("name") if isinstance(value, dict) else None)
        if name is None:
            ident = related_id(installation, attribute)
            pool = self._products.values() if attribute == "software_product" else self._versions.values()
            name = next((item.name for item in pool if item.id == ident), "")
        return str(name).casefold()
