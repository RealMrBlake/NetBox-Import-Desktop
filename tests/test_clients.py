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



INSTANCE = """<p:Win32_NetworkAdapterConfiguration xmlns:p="http://schemas.microsoft.com/wbem/wsman/1/wmi/root/cimv2/Win32_NetworkAdapterConfiguration" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:cim="http://schemas.dmtf.org/wbem/wscim/1/common">
<p:Description>Intel(R) Ethernet</p:Description><p:DHCPEnabled>true</p:DHCPEnabled>
<p:IPAddress>192.168.65.43</p:IPAddress><p:IPAddress>fe80::1</p:IPAddress>
<p:DefaultIPGateway xsi:nil="true"/>
<p:InstallDate><cim:Datetime>2025-12-23T17:23:16Z</cim:Datetime></p:InstallDate>
</p:Win32_NetworkAdapterConfiguration>"""


def test_parse_wsman_instance():
    from netbox_sync.core import wsman

    item = wsman.parse_instance(INSTANCE)
    assert item["Description"] == "Intel(R) Ethernet" and wsman.as_bool(item["DHCPEnabled"])
    assert item["IPAddress"] == ["192.168.65.43", "fe80::1"]
    assert item["DefaultIPGateway"] is None and wsman.as_list(item["DefaultIPGateway"]) == []
    assert item["InstallDate"] == "2025-12-23T17:23:16Z"


def test_ad_location_defaults_to_own_domain(monkeypatch):
    from netbox_sync.core import clients

    monkeypatch.setenv("USERDNSDOMAIN", "FIRMA.LOCAL")
    assert clients.ad_location({}) == "DC=FIRMA,DC=LOCAL"
    assert clients.ad_location({"ad_server": "dc01.firma.local"}) == "dc01.firma.local"
    assert clients.ad_location({"ad_server": "dc01", "ad_base": "OU=PCs,DC=firma,DC=local"}) == \
        "dc01/OU=PCs,DC=firma,DC=local"


def test_scan_runs_in_process(monkeypatch):
    from netbox_sync.core import clients

    monkeypatch.setattr(clients, "resolves", lambda host: host != "unknown")
    monkeypatch.setattr(clients, "port_open", lambda host, port: host != "off")
    rows = [new_row(host) for host in ("pc01.firma.local", "off", "unknown")]
    seen = []
    results = clients.scan_clients(rows, query=lambda host: dict(RAW, host=host), on_result=seen.append)
    status = {row["host"]: row["status"] for row in results}
    assert status == {"pc01.firma.local": "ok", "off": "offline", "unknown": "offline"}
    assert len(seen) == 3


def test_agent_software_goes_into_a_managed_comment_block():
    nb, client = _setup()
    raw = dict(RAW, source="agent", agent_version="0.2.0",
               software=[{"name": "7-Zip", "version": "24.08", "publisher": "Igor Pavlov"},
                         {"name": "A | B", "version": "", "publisher": ""}],
               disks=[{"model": "Micron SSD", "size": 512105932800, "serial": "M1"}],
               memory_modules=[{"bank": "DIMM 0", "manufacturer": "Samsung", "part_number": "M425", "capacity": 2 ** 34}])
    agent_client = apply_scan(new_row("pc01.firma.local"), raw)
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    preview = engine.sync([agent_client], 1, 2, dry_run=True)
    assert "Inventarbericht in den Kommentaren (2 Programme)" in preview["items"][0][1]
    engine.sync([agent_client], 1, 2, dry_run=False)
    device = nb.dcim.devices.records[0]
    assert "| 7-Zip | 24.08 | Igor Pavlov |" in device.comments and "| A / B |" in device.comments
    assert "Micron SSD – 512 GB – S/N M1" in device.comments and "DIMM 0: 16 GB Samsung M425" in device.comments

    # Hand-written notes survive, an unchanged inventory (only a new scan time) is no update.
    device.comments = "Notiz vom Admin\n\n" + device.comments
    again = ClientSyncEngine(nb)
    again.load_targets()
    rescanned = dict(agent_client, scanned_at=agent_client["scanned_at"] + 3600)
    assert again.sync([rescanned], 1, 2, dry_run=True)["devices_updated"] == 0
    changed = dict(rescanned, software=rescanned["software"] + [{"name": "Firefox", "version": "140", "publisher": ""}])
    report = again.sync([changed], 1, 2, dry_run=False)
    assert "Inventarbericht aktualisieren (3 Programme)" in report["items"][0][1]
    assert device.comments.startswith("Notiz vom Admin") and device.comments.count("<!-- netbox-sync:inventory -->") == 1
    assert "| Firefox | 140 |  |" in device.comments


def test_winrm_scan_leaves_comments_alone():
    nb, client = _setup()
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    engine.sync([client], 1, 2, dry_run=False)
    assert "comments" not in nb.dcim.devices.created[0]


def _with_inventory_plugin(nb):
    from types import SimpleNamespace

    from fake_netbox import Endpoint
    nb.plugins = SimpleNamespace(inventory=SimpleNamespace(assets=Endpoint()))
    return nb.plugins.inventory.assets


def test_new_client_gets_an_asset_linked_to_the_device():
    nb, client = _setup()
    assets = _with_inventory_plugin(nb)
    agent_row = apply_scan(new_row("pc01.firma.local"), dict(RAW, source="agent", uuid="4C4C-1234",
                                                             software=[{"name": "7-Zip", "version": "24", "publisher": ""}]))
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    preview = engine.sync([agent_row], 1, 2, dry_run=True)
    assert preview["assets_created"] == 1 and "Asset im Inventory-Plugin anlegen" in preview["items"][0][1]
    assert assets.created == []

    engine.sync([agent_row], 1, 2, dry_run=False)
    device = nb.dcim.devices.records[0]
    asset = assets.created[0]
    assert asset["serial"] == "PW0K84A9" and asset["device"] == device.id and asset["status"] == "used"
    assert asset["device_type"] == device.device_type and asset["description"] == "Windows-Client; UUID 4C4C-1234; Zuletzt angemeldet: FIRMA\max"
    assert "| 7-Zip | 24 |  |" in asset["comments"]

    again = ClientSyncEngine(nb)
    again.load_targets()
    report = again.sync([agent_row], 1, 2, dry_run=True)
    assert report["assets_created"] == 0 and report["assets_updated"] == 0 and report["items"][0][1] == []


def test_existing_asset_is_linked_and_updated_by_serial():
    nb, client = _setup()
    assets = _with_inventory_plugin(nb)
    assets.records.append(Record(assets, id=500, name="Lager-Notebook", serial="PW0K84A9", status="stored",
                                 device=None, device_type=None, description="", comments="Kaufbeleg 4711"))
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client], 1, 2, dry_run=False)
    asset = assets.records[0]
    device = nb.dcim.devices.records[0]
    assert report["assets_updated"] == 1 and report["assets_created"] == 0
    assert asset.device == device.id and asset.status == "used" and asset.name == "PC01"
    assert asset.comments == "Kaufbeleg 4711"  # WinRM scan: no inventory report, comments untouched
    assert any(change.startswith("Asset aktualisieren: Name, Status, Gerätetyp, Gerät") for change in report["items"][0][1])


def test_without_inventory_plugin_no_asset_is_touched():
    nb, client = _setup()
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    report = engine.sync([client], 1, 2, dry_run=False)
    assert report["errors"] == [] and report["assets_created"] == 0 and engine.asset_endpoint() is None


def test_asset_gets_the_tenant_of_the_transfer_or_the_device():
    from types import SimpleNamespace
    nb, client = _setup()
    assets = _with_inventory_plugin(nb)
    engine = ClientSyncEngine(nb)
    engine.load_targets()
    engine.sync([client], 1, 2, tenant_id=7, dry_run=False)
    assert assets.created[0]["tenant"] == 7

    # an existing device keeps its tenant; an asset without tenant gets the device's one
    nb2, client2 = _setup()
    assets2 = _with_inventory_plugin(nb2)
    nb2.dcim.devices.records.append(Record(nb2.dcim.devices, id=300, name="PC01", tenant=SimpleNamespace(id=9),
                                           device_type=None, platform=None, serial="", primary_ip4=None))
    assets2.records.append(Record(assets2, id=501, name="PC01", serial="PW0K84A9", status="used", device=None,
                                  device_type=None, tenant=None, description="", comments=""))
    again = ClientSyncEngine(nb2)
    again.load_targets()
    report = again.sync([client2], 1, 2, dry_run=False)
    assert report["errors"] == [] and assets2.records[0].tenant == 9 and assets2.records[0].device == 300


def test_nul_characters_are_stripped_and_user_is_in_the_report():
    from netbox_sync.core.client_sync import inventory_report
    from netbox_sync.core.clients import apply_scan
    raw = {"ok": True, "source": "agent", "name": "pc01", "user": "FIRMA\max",
           "software": [{"name": "Office", "version": "16", "publisher": "Microsoft\x00"}]}
    row = apply_scan({"name": "PC01"}, raw, now=0)
    assert row["software"][0]["publisher"] == "Microsoft"
    assert "- **Zuletzt angemeldet:** FIRMA\max" in inventory_report(row)
