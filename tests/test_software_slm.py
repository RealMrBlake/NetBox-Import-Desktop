from types import SimpleNamespace

from fake_netbox import Endpoint, FakeNetBox, Record

from netbox_sync.core.client_sync import ClientSyncEngine
from netbox_sync.core.clients import apply_scan, new_row
from netbox_sync.core.software_slm import SYNC_MARKER, clean_name, publisher_name, software_entries
from test_clients import RAW, _setup

SOFTWARE = [
    {"name": "7-Zip 24.08", "version": "24.08", "publisher": "Igor Pavlov"},
    {"name": "Mozilla Firefox 140.0", "version": "140.0", "publisher": "Mozilla"},
    {"name": "Microsoft Edge", "version": "142.0.1", "publisher": "Microsoft Corporation"},
    {"name": "Tool", "version": "", "publisher": ""},
]


def _with_slm(nb):
    nb.plugins = SimpleNamespace(slm=SimpleNamespace(softwareproducts=Endpoint(), softwareproductversions=Endpoint(),
                                                     softwareproductinstallations=Endpoint()))
    return nb.plugins.slm


def _agent(software=SOFTWARE):
    return apply_scan(new_row("pc01.firma.local"), dict(RAW, source="agent", software=software))


def test_names_match_netbox_utilities():
    assert clean_name("Mozilla Firefox 140.0", "140.0") == "Mozilla Firefox"
    assert clean_name("Foo - 1.2.3", "1.2.3.4") == "Foo"
    assert clean_name("Office 2024", "16.0") == "Office 2024"
    assert publisher_name("Microsoft Corporation") == "Microsoft"
    assert publisher_name("Adobe Inc.") == "Adobe" and publisher_name("Google LLC") == "Google"
    assert publisher_name("Igor Pavlov") == "Igor Pavlov"
    entries = software_entries(SOFTWARE + [{"name": "tool", "version": None, "publisher": ""}])
    assert entries[("tool", "unbekannt")] == ("Tool", "unbekannt", "")
    assert len(entries) == 4


def test_software_is_off_by_default():
    nb, _client = _setup()
    slm = _with_slm(nb)
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    engine.sync([_agent()], 1, 2, dry_run=False)
    assert slm.softwareproductinstallations.records == []


def test_software_creates_manufacturers_products_versions_and_installations():
    nb, _client = _setup()
    slm = _with_slm(nb)
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    preview = engine.sync([_agent()], 1, 2, dry_run=True, software=True)
    assert preview["errors"] == [] and preview["software_installed"] == 4
    assert "Softwareprodukt anlegen: Microsoft Edge (Microsoft)" in preview["changes"]
    assert "Hersteller anlegen: Igor Pavlov" in preview["changes"]
    assert slm.softwareproducts.created == [] and nb.dcim.manufacturers.created == []

    report = engine.sync([_agent()], 1, 2, dry_run=False, software=True)
    assert report["errors"] == [] and report["software_installed"] == 4
    device = nb.dcim.devices.records[0]
    products = {item.name: item for item in slm.softwareproducts.records}
    assert set(products) == {"7-Zip", "Mozilla Firefox", "Microsoft Edge", "Tool"}
    manufacturers = {item.id: item.name for item in nb.dcim.manufacturers.records}
    assert manufacturers[products["Microsoft Edge"].manufacturer] == "Microsoft"
    assert not hasattr(products["Tool"], "manufacturer")
    versions = {(item.software_product, item.name) for item in slm.softwareproductversions.records}
    assert (products["Tool"].id, "unbekannt") in versions
    installations = slm.softwareproductinstallations.records
    assert len(installations) == 4 and all(item.device == device.id and item.comments == SYNC_MARKER
                                           for item in installations)

    # second run: nothing to do
    again = ClientSyncEngine(nb)
    again.load_targets()
    report = again.sync([_agent()], 1, 2, dry_run=True, software=True)
    assert report["software_installed"] == 0 and report["software_removed"] == 0 and report["changes"] == []


def _installation(slm, nb, device_id, product, version, comments):
    slm.softwareproductinstallations.records.append(Record(
        slm.softwareproductinstallations, id=len(slm.softwareproductinstallations.records) + 900, device=device_id,
        software_product=SimpleNamespace(id=product.id, name=product.name),
        version=SimpleNamespace(id=version.id, name=version.name), comments=comments))


def test_removed_software_goes_but_manual_installations_stay():
    nb, _client = _setup()
    slm = _with_slm(nb)
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    engine.sync([_agent()], 1, 2, dry_run=False, software=True)
    device = nb.dcim.devices.records[0]
    product = slm.softwareproducts.create({"name": "Lizenz-Tool"})
    version = slm.softwareproductversions.create({"software_product": product.id, "name": "1.0"})
    _installation(slm, nb, device.id, product, version, "von Hand")
    # nested objects as the REST API returns them
    for item in slm.softwareproductinstallations.records:
        if isinstance(item.software_product, int):
            product_record = slm.softwareproducts.get(item.software_product)
            version_record = slm.softwareproductversions.get(item.version)
            item.software_product = SimpleNamespace(id=product_record.id, name=product_record.name)
            item.version = SimpleNamespace(id=version_record.id, name=version_record.name)

    again = ClientSyncEngine(nb)
    again.load_targets()
    report = again.sync([_agent(SOFTWARE[:2])], 1, 2, dry_run=False, software=True)
    assert report["software_removed"] == 2 and report["software_installed"] == 0
    left = {item.software_product.name for item in slm.softwareproductinstallations.records}
    assert left == {"7-Zip", "Mozilla Firefox", "Lizenz-Tool"}


def test_products_from_netbox_utilities_get_the_manufacturer():
    nb, _client = _setup()
    slm = _with_slm(nb)
    slm.softwareproducts.records.append(Record(slm.softwareproducts, id=50, name="Microsoft Edge", manufacturer=None,
                                               description="Microsoft Corporation"))
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([_agent(SOFTWARE[2:3])], 1, 2, dry_run=False, software=True)
    product = slm.softwareproducts.records[0]
    assert report["errors"] == [] and len(slm.softwareproducts.records) == 1
    assert product.description == "" and product.manufacturer == nb.dcim.manufacturers.records[-1].id
    assert slm.softwareproductinstallations.records[0].software_product == 50


def test_without_slm_plugin_software_is_skipped():
    nb, _client = _setup()
    logged = []
    engine = ClientSyncEngine(nb, lambda text, level="info": logged.append(level))
    engine.load_targets()
    report = engine.sync([_agent()], 1, 2, dry_run=False, software=True)
    assert report["errors"] == [] and report["software_installed"] == 0 and "warning" in logged
    assert len(nb.dcim.devices.records) == 1
