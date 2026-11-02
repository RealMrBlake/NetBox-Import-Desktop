from types import SimpleNamespace

import pytest
from fake_netbox import FakeNetBox, Record

from netbox_sync.core.ap_sync import APImportError, APSyncEngine, ap_key, parse_csv

CSV = (
    '"AP Name","MAC Address","Serial Number","Internal IP address","Model","Config Status",'
    '"Active Channel for 2.4 GHz","Current Site","Firmware","Uptime"\n'
    '"Kita Sonne EG ","7c:5a:1c:00:00:01","p52000000000001","10.10.230.101","APX320","UP_TO_DATE","6",'
    '"Kita Sonnenschein","v2.3.6","1"\n'
    '"Kita Sonne OG","7C:5A:1C:00:00:02","P52000000000002","10.10.230.102","APX320","UP_TO_DATE","1",'
    '"Kita Sonnenschein","v2.3.6","1"\n'
    '"Büro ohne Standort","c8:4f:86:00:00:03","P52000000000003","10.10.230.102","AP6 420","UPDATE_PENDING","",'
    '"","v1.10.3192",""\n'
)


def _netbox():
    nb = FakeNetBox()
    group = SimpleNamespace(id=3, name="Kunde")
    nb.tenancy.tenants.records += [Record(nb.tenancy.tenants, id=7, name="Kunde RZ", group=group),
                                   Record(nb.tenancy.tenants, id=8, name="Kunde Kita", group=group)]
    nb.dcim.sites.records += [
        Record(nb.dcim.sites, id=20, name="Kita Sonnenschein Jena", tenant=SimpleNamespace(id=8), tenant_group=group),
        Record(nb.dcim.sites, id=21, name="Geschäftsstelle", tenant=SimpleNamespace(id=7), tenant_group=group),
    ]
    nb.dcim.device_roles.records.append(Record(nb.dcim.device_roles, id=30, name="Access Point"))
    sophos = Record(nb.dcim.manufacturers, id=40, name="Sophos", slug="sophos")
    nb.dcim.manufacturers.records.append(sophos)
    # Known by serial (other spelling), known only by name, and one missing in Sophos.
    nb.dcim.devices.records += [
        Record(nb.dcim.devices, id=101, name="AP alt", serial="P52000000000002", tenant=SimpleNamespace(id=8),
               tenant_group=group, manufacturer=sophos, site=SimpleNamespace(id=20), description="Firmware v2.3.5",
               primary_ip4=None),
        Record(nb.dcim.devices, id=102, name="Kita Sonne EG", serial="", tenant=SimpleNamespace(id=8),
               tenant_group=group, manufacturer=sophos, site=SimpleNamespace(id=20), description="",
               primary_ip4=None),
        Record(nb.dcim.devices, id=103, name="Abgebaut", serial="X1", tenant=SimpleNamespace(id=8),
               tenant_group=group, manufacturer=sophos, site=SimpleNamespace(id=20), description="",
               primary_ip4=None),
    ]
    return nb


def test_sophos_export_is_parsed_and_checked():
    aps = parse_csv(CSV.encode("utf-8"))
    assert [ap["name"] for ap in aps] == ["Kita Sonne EG", "Kita Sonne OG", "Büro ohne Standort"]
    assert aps[1]["mac"] == "7C:5A:1C:00:00:02" and aps[0]["serial"] == "P52000000000001"
    assert aps[2]["issues"][0] == "kein Standort in Sophos"
    assert any("10.10.230.102 doppelt" in issue for issue in aps[1]["issues"])
    assert parse_csv(CSV.replace(",", ";").encode("cp1252"))[2]["name"] == "Büro ohne Standort"
    with pytest.raises(APImportError):
        parse_csv(b"foo,bar\n1,2\n")


def test_existing_aps_are_found_by_serial_and_name_and_missing_ones_reported():
    nb = _netbox()
    engine = APSyncEngine(nb)
    engine.load_context(7)
    assert [site["name"] for site in engine.ap_sites] == ["Geschäftsstelle", "Kita Sonnenschein Jena"]
    assert engine.suggest_site("Kita Sonnenschein") == 20 and engine.suggest_site("") is None
    aps = parse_csv(CSV)
    only_netbox = engine.match(aps)
    assert [(ap["device"].id if ap["device"] else None, ap["method"]) for ap in aps] == [
        (102, "Name"), (101, "Seriennummer"), (None, "")]
    assert [device.name for device in only_netbox] == ["Abgebaut"]


def test_sync_creates_new_aps_with_site_tenant_and_updates_existing():
    nb = _netbox()
    engine = APSyncEngine(nb)
    engine.load_context(7)
    aps = parse_csv(CSV)
    engine.match(aps)
    sites = {ap_key(aps[2]): {"site": 21}}

    preview = engine.sync(aps, sites, role_id=30, dry_run=True)
    assert preview["errors"] == [] and preview["devices_created"] == 1 and preview["devices_updated"] == 2
    assert nb.dcim.devices.created == [] and nb.ipam.ip_addresses.created == []

    report = engine.sync(aps, sites, role_id=30, dry_run=False)
    assert report["errors"] == []
    created = nb.dcim.devices.created[-1]
    assert created["name"] == "Büro ohne Standort" and created["site"] == 21 and created["tenant"] == 7
    assert created["serial"] == "P52000000000003" and created["description"] == "Firmware v1.10.3192"
    renamed = nb.dcim.devices.get(101)
    assert renamed.name == "Kita Sonne OG" and renamed.description == "Firmware v2.3.6"
    assert nb.dcim.devices.get(102).serial == "P52000000000001"
    assert [payload["tenant"] for payload in nb.ipam.ip_addresses.created] == [8, 8]
    # The duplicate IP of the third AP is not taken away from the second one.
    assert any("bereits einem anderen Objekt zugeordnet" in change for change in dict(report["items"])[
        "Büro ohne Standort"])


def test_new_ap_without_site_is_an_error_not_a_guess():
    nb = _netbox()
    engine = APSyncEngine(nb)
    engine.load_context(7)
    aps = parse_csv(CSV)
    engine.match(aps)
    report = engine.sync(aps, {}, role_id=30, dry_run=True)
    assert report["errors"][0].startswith("Büro ohne Standort: kein Standort")


def test_site_is_optional_with_default_site_and_tenant_can_be_chosen():
    nb = _netbox()
    engine = APSyncEngine(nb)
    engine.load_context(7)
    assert [tenant["name"] for tenant in engine.group_tenants] == ["Kunde Kita", "Kunde RZ"]
    aps = parse_csv(CSV)
    engine.match(aps)
    # No site for the new AP: it goes to the default site, with an own tenant.
    engine.sync(aps, {ap_key(aps[2]): {"site": None, "tenant": 8}}, role_id=30, dry_run=False, default_site=21)
    created = nb.dcim.devices.created[-1]
    assert (created["site"], created["tenant"]) == (21, 8)
