import pytest
from fake_netbox import FakeNetBox, Record

from netbox_sync.core import exchange
from netbox_sync.core.client_sync import ClientSyncEngine, clean_manufacturer, clean_serial
from netbox_sync.core.clients import (
    ClientScanError,
    ad_filter,
    apply_scan,
    expand_targets,
    filetime_to_unix,
    friendly_error,
    new_row,
    os_label,
    os_version_label,
)

RAW = {
    "host": "pc01.firma.local", "ok": True, "name": "pc01", "domain": "firma.local", "part_of_domain": True,
    "manufacturer": "LENOVO", "model": "21MW", "product_name": "21MW", "product_version": "ThinkBook 16 G7 ARP",
    "serial": "PW0K84A9", "chassis": [10], "user": "FIRMA\\max", "memory": 17179869184, "cpu": "AMD  Ryzen 5",
    "cores": 6, "os": "Microsoft Windows 11 Pro", "os_version": "10.0.26100", "os_build": "26100",
    "os_display_version": "24H2",
    "nics": [
        {"name": "Ethernet", "description": "Intel(R) Ethernet I219-LM", "mac": "84:BA:59:60:C8:B7",
         "ips": ["192.168.65.43", "fe80::1"], "masks": ["255.255.255.0", "64"], "gateway": ["192.168.65.1"],
         "dhcp": True, "physical": True, "speed": 1000000000},
        {"name": "WLAN", "description": "Intel(R) Wi-Fi 6E AX211", "mac": "84:BA:59:60:C8:B8",
         "ips": ["10.0.5.20"], "masks": ["255.255.0.0"], "gateway": [""], "dhcp": True, "physical": True},
        {"name": "OpenVPN", "description": "OpenVPN Data Channel Offload", "mac": "", "ips": ["10.8.0.2"],
         "masks": ["255.255.255.0"], "physical": True},
        {"name": "", "description": "Sophos TAP", "mac": "00:FF:90:54:10:16", "ips": ["100.64.0.1"],
         "masks": ["255.255.0.0"], "physical": False},
    ],
}


def test_expand_targets_supports_names_ranges_and_networks():
    hosts = expand_targets("pc1, PC1\n10.0.0.1-3; 192.168.1.0/30 # Kommentar\n10.0.1.5-10.0.1.6")
    assert hosts == ["pc1", "10.0.0.1", "10.0.0.2", "10.0.0.3", "192.168.1.1", "192.168.1.2", "10.0.1.5",
                     "10.0.1.6"]
    with pytest.raises(ClientScanError):
        expand_targets("10.0.0.0/8")
    with pytest.raises(ClientScanError):
        expand_targets("10.0.0.9-10.0.0.1")


def test_comment_lines_are_ignored():
    assert expand_targets("#nur Kommentar\npc2") == ["pc2"]


def test_apply_scan_cleans_model_and_filters_virtual_adapters():
    row = apply_scan(new_row("pc01.firma.local"), RAW, now=1.0)
    assert row["status"] == "ok" and row["name"] == "PC01" and row["id"] == "pc01"
    assert row["model"] == "ThinkBook 16 G7 ARP" and row["machine_type"] == "21MW"
    assert row["chassis"] == "Notebook" and row["cpu"] == "AMD Ryzen 5"
    assert [interface["name"] for interface in row["interfaces"]] == ["Ethernet", "WLAN"]
    assert row["interfaces"][0]["ips"] == ["192.168.65.43/24"]
    assert row["interfaces"][1]["wireless"] and row["interfaces"][1]["gateway"] == []
    assert os_label(row) == "Windows 11 Pro"
    assert os_version_label(row) == "24H2 (Build 26100)"


def test_apply_scan_error_gets_hint():
    row = apply_scan(new_row("pc02"), {"host": "pc02", "ok": False, "error": "Zugriff verweigert"})
    assert row["status"] == "error" and "Administratorrechte" in row["message"]
    assert "Kerberos" in friendly_error("The WinRM client cannot process the request ... TrustedHosts list")


def test_ad_filter_and_filetime():
    assert ad_filter({"include_servers": False, "only_enabled": True}) == (
        "(&(objectCategory=computer)(operatingSystem=Windows*)(!(operatingSystem=*Server*))"
        "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))")
    assert "Server" not in ad_filter({"include_servers": True, "only_enabled": False})
    assert filetime_to_unix(116444736000000000) == 0
    row = new_row("pc03.firma.local", ad={"name": "PC03", "os": "Windows 10 Pro", "os_version": "10.0 (19045)"})
    assert row["id"] == "pc03" and row["status"] == "pending"
    assert os_label(row) == "Windows 10 Pro" and os_version_label(row) == "10.0 (19045)"


def test_cleaners():
    assert clean_manufacturer("LENOVO") == "Lenovo"
    assert clean_manufacturer("Dell Inc.") == "Dell"
    assert clean_manufacturer("To Be Filled By O.E.M.") == ""
    assert clean_manufacturer("FOOBAR COMPUTERS") == "Foobar Computers"
    assert clean_serial("Default string") == "" and clean_serial(" ABC123 ") == "ABC123"


def _setup():
    nb = FakeNetBox()
    nb.dcim.sites.records.append(Record(nb.dcim.sites, id=1, name="Zentrale", slug="zentrale"))
    nb.dcim.device_roles.records.append(Record(nb.dcim.device_roles, id=2, name="Client", slug="client"))
    client = apply_scan(new_row("pc01.firma.local"), RAW)
    return nb, client


def test_dry_run_writes_nothing():
    nb, client = _setup()
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client, dict(client, name="PC02", id="pc02")], 1, 2, dry_run=True)
    assert report["errors"] == []
    assert report["devices_created"] == 2 and report["interfaces_created"] == 4
    assert report["changes"].count("Hersteller anlegen: Lenovo") == 1
    assert nb.dcim.devices.created == [] and nb.dcim.manufacturers.created == []


def test_sync_creates_and_then_is_idempotent():
    nb, client = _setup()
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client], 1, 2, tenant_id=None, dry_run=False)
    assert report["errors"] == [] and report["devices_created"] == 1
    device = nb.dcim.devices.records[0]
    assert device.name == "PC01" and device.serial == "PW0K84A9" and device.site == 1 and device.role == 2
    assert nb.dcim.manufacturers.records[0].name == "Lenovo"
    assert nb.dcim.device_types.records[0].model == "ThinkBook 16 G7 ARP"
    assert nb.dcim.platforms.records[0].name == "Windows 11 Pro"
    assert {item.name for item in nb.dcim.interfaces.records} == {"Ethernet", "WLAN"}
    ip = next(ip for ip in nb.ipam.ip_addresses.records if ip.address == "192.168.65.43/24")
    assert ip.status == "dhcp"
    assert device.primary_ip4 == ip.id

    again = ClientSyncEngine(nb)
    again.load_targets()
    report = again.sync([client], 1, 2, dry_run=True)
    assert report["devices_created"] == 0 and report["devices_updated"] == 0
    assert report["items"][0][1] == []


def test_existing_ip_on_other_device_is_left_alone():
    nb, client = _setup()
    nb.ipam.ip_addresses.records.append(Record(nb.ipam.ip_addresses, id=99, address="10.0.5.20/16",
                                               assigned_object_id=4711, vrf=None))
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client], 1, 2, dry_run=False)
    assert any("bereits einem anderen Objekt" in change for change in report["items"][0][1])
    assert nb.ipam.ip_addresses.records[0].assigned_object_id == 4711


def test_existing_device_gets_serial_update():
    nb, client = _setup()
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=5, name="pc01", serial="", device_type=None,
                                          platform=None, tenant=None, primary_ip4=None))
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client], 1, 2, interfaces=False, dry_run=False)
    assert report["devices_updated"] == 1
    assert nb.dcim.devices.records[0].serial == "PW0K84A9"
    assert len(nb.dcim.devices.records) == 1


def test_exchange_round_trip_with_clients(tmp_path):
    path = tmp_path / "clients.nbsync.json"
    client = apply_scan(new_row("pc01.firma.local"), RAW)
    exchange.write_export(path, exchange.build_export(clients=[client], profile_name="Kunde"))
    data = exchange.read_export(path)
    assert data["clients"][0]["serial"] == "PW0K84A9" and data["clients"][0]["id"] == "pc01"
    assert data["vms"] == [] and data["switches"] == []


def test_old_exchange_files_have_no_clients(tmp_path):
    path = tmp_path / "old.json"
    exchange.write_export(path, {"format": exchange.FORMAT, "version": 1, "vms": [], "switches": []})
    assert exchange.read_export(path)["clients"] == []


def test_profile_keeps_client_options(tmp_path, monkeypatch):
    from netbox_sync.core import config

    monkeypatch.setattr(config, "protect", lambda value: value)
    monkeypatch.setattr(config, "unprotect", lambda value: value)
    store = config.ProfileStore(tmp_path / "config.json")
    profile = config.empty_profile("Kunde")
    profile["clients"].update(mode="hosts", hosts="pc1", username="FIRMA\\admin")
    store.save({"Kunde": profile}, "Kunde")
    loaded, _active, settings = store.load()
    assert loaded["Kunde"]["clients"]["hosts"] == "pc1"
    assert loaded["Kunde"]["clients"]["username"] == "FIRMA\\admin"
    assert "password" not in loaded["Kunde"]["clients"]
    assert settings["parallel_wmi"] == 8

