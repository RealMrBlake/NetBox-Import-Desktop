from types import SimpleNamespace

from fake_netbox import FakeNetBox, Record

from netbox_sync.core.switches import build_snapshot, match_interfaces, plan_switch_sync, sync_snapshot_to_netbox
from test_switches import COMWARE_BRIEF, COMWARE_LLDP


def _setup():
    nb = FakeNetBox()
    tenant = SimpleNamespace(id=7, name="Muster")
    nb.dcim.devices.records += [
        Record(nb.dcim.devices, id=1, name="access-01", tenant=tenant),
        Record(nb.dcim.devices, id=2, name="core-sw", tenant=None),
    ]
    # Interfaces created from the NetBox device type use full Comware names.
    for index, name in enumerate(("GigabitEthernet1/0/1", "GigabitEthernet1/0/2"), start=1):
        nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=index, name=name, device=SimpleNamespace(id=1),
                                                 type=SimpleNamespace(value="1000base-x-sfp"), enabled=True, cable=None))
    nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=50, name="GigabitEthernet1/0/24",
                                             device=SimpleNamespace(id=2), enabled=True, cable=None))
    snapshot = build_snapshot({"interfaces": COMWARE_BRIEF, "neighbors": COMWARE_LLDP}, "hpe_comware")
    switch = {"id": "s", "name": "access-01", "device_id": 1}
    return nb, switch, snapshot


def test_dry_run_writes_nothing():
    nb, switch, snapshot = _setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=True, dry_run=True)
    assert report["errors"] == []
    assert report["interfaces_created"] == 1          # Ten-GigabitEthernet1/0/49
    assert report["interfaces_updated"] == 1          # GE1/0/2 is ADM -> disable
    assert report["interfaces_unchanged"] == 1
    assert report["cables_created"] == 1             # FQDN "core-sw.example.local" -> core-sw
    assert nb.dcim.interfaces.created == [] and nb.dcim.interfaces.updates == [] and nb.dcim.cables.created == []


def test_sync_matches_short_names_and_keeps_existing_types():
    nb, switch, snapshot = _setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=True)
    assert report["errors"] == []
    names = [payload["name"] for payload in nb.dcim.interfaces.created]
    assert names == ["Ten-GigabitEthernet1/0/49"], "no GE1/0/x duplicates next to template interfaces"
    assert nb.dcim.interfaces.created[0]["type"] == "10gbase-x-sfpp"
    # Only the admin state changed – the template type must not be overwritten.
    assert nb.dcim.interfaces.updates == [(2, {"enabled": False})]
    cable = nb.dcim.cables.created[0]
    assert cable["a_terminations"][0]["object_id"] == 1
    assert cable["b_terminations"][0]["object_id"] == 50

    # A second run finds nothing left to do.
    nb.dcim.interfaces.get(1).cable = True
    again = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=True, dry_run=True)
    assert again["changes"] == []


def test_offline_switch_is_resolved_by_name():
    nb, _switch, snapshot = _setup()
    report = sync_snapshot_to_netbox(nb, {"id": "s", "name": "access-01", "device_id": None}, snapshot,
                                     create_cables=False, dry_run=True)
    assert report["errors"] == []
    assert report["interfaces_unchanged"] == 1

    missing = sync_snapshot_to_netbox(nb, {"id": "s", "name": "unbekannt"}, snapshot, dry_run=True)
    assert missing["errors"] == ["NetBox-Gerät „unbekannt“ nicht gefunden"]


def _numbered_setup():
    """NetBox device type with ports "1" … "3", switch reports "1/1/x"."""
    nb = FakeNetBox()
    group = SimpleNamespace(id=3, name="Kunde")
    nb.tenancy.tenants.records.append(Record(nb.tenancy.tenants, id=7, name="Muster", group=group))
    nb.dcim.devices.records += [
        Record(nb.dcim.devices, id=1, name="access-01", tenant=SimpleNamespace(id=7), tenant_group=group),
        Record(nb.dcim.devices, id=2, name="server-01", tenant=SimpleNamespace(id=7), tenant_group=group),
        Record(nb.dcim.devices, id=3, name="fremd", tenant=None, tenant_group=None),
    ]
    for index, name in enumerate(("1", "2", "3"), start=1):
        nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=index, name=name, device=SimpleNamespace(id=1),
                                                 type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None,
                                                 label=""))
    nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=20, name="eth0", device=SimpleNamespace(id=2),
                                             type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None))
    interfaces = [
        {"name": f"1/1/{number}", "enabled": True, "connected": True, "media": "1GbT", "speed_mbps": 1000,
         "ip_addresses": [], "vlans": {"untagged": [10 * number], "tagged": [], "tagged_all": False}}
        for number in (1, 2, 3)
    ]
    neighbor = {"local_port": "1/1/2", "system_name": "unbekannt", "remote_port": "eth0", "port_description": "",
                "management_ip": "", "protocol": "LLDP"}
    snapshot = {"interfaces": interfaces, "neighbors": [neighbor]}
    return nb, {"id": "s", "name": "access-01", "device_id": 1}, snapshot


def test_ports_are_mapped_by_number_and_labelled_with_vlans():
    nb, switch, snapshot = _numbered_setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == [] and report["interfaces_created"] == 0
    assert sorted(nb.dcim.interfaces.updates) == [(1, {"label": "10"}), (2, {"label": "20"}), (3, {"label": "30"})]


def test_ports_are_mapped_by_order_and_manual_choice_wins():
    nb, switch, snapshot = _numbered_setup()
    for item, name in zip(snapshot["interfaces"], ("1/0/1", "1/0/2", "2/0/1")):
        item["name"] = name  # "2/0/1" breaks the unique port numbers
    matches = match_interfaces(snapshot["interfaces"], nb.dcim.interfaces.filter(device_id=1))
    assert {name: (record.name, method) for name, (record, method) in matches.items()} == {
        "1/0/1": ("1", "order"), "1/0/2": ("2", "number"), "2/0/1": ("3", "order")}
    manual = match_interfaces(snapshot["interfaces"], nb.dcim.interfaces.filter(device_id=1), {"2/0/1": ""})
    assert manual["2/0/1"] == (None, "new")


def test_plan_offers_tenant_group_devices_and_manual_neighbor_creates_cable():
    nb, switch, snapshot = _numbered_setup()
    plan = plan_switch_sync(nb, switch, snapshot)
    assert plan["device_scope"] == "Mandantengruppe Kunde"
    assert [device["name"] for device in plan["devices"]] == ["server-01"]
    assert plan["neighbors"][0]["device_id"] is None
    assert {item["name"]: item["method"] for item in plan["interfaces"]}["1/1/1"] == "number"

    report = sync_snapshot_to_netbox(nb, switch, snapshot, dry_run=True)
    assert report["cables_created"] == 0
    switch["neighbor_devices"] = {"1/1/2": 2}
    report = sync_snapshot_to_netbox(nb, switch, snapshot)
    cable = nb.dcim.cables.created[0]
    assert cable["a_terminations"][0]["object_id"] == 2 and cable["b_terminations"][0]["object_id"] == 20


def test_missing_vlans_prefixes_and_vrfs_are_only_created_when_approved():
    nb, switch, snapshot = _numbered_setup()
    nb.ipam.vlans.records.append(Record(nb.ipam.vlans, id=110, vid=10, name="Server", tenant=SimpleNamespace(id=7)))
    snapshot["interfaces"][2]["vlans"] = {"untagged": [1], "tagged": [10, 20], "tagged_all": False}
    snapshot["interfaces"][0]["ip_addresses"] = ["10.1.0.1/24"]
    snapshot["vlan_names"] = {20: "Clients"}

    plan = plan_switch_sync(nb, switch, snapshot)
    assert [(item["vid"], item["netbox_name"], item["name"]) for item in plan["vlans"]] == [
        (1, None, "VLAN1"), (10, "Server", "VLAN10"), (20, None, "Clients")]
    assert plan["networks"] == [{"network": "10.1.0.0/24", "vrf": "", "existing_vrfs": [], "create": True}]

    # Not approved: nothing is created, only the existing VLAN 10 is assigned.
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert (report["vlans_created"], report["prefixes_created"], report["vrfs_created"]) == (0, 0, 0)
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert nb.ipam.vlans.created == [] and nb.ipam.prefixes.created == []
    assert nb.dcim.interfaces.get(1).untagged_vlan == 110 and nb.dcim.interfaces.get(1).mode == "access"
    assert nb.ipam.ip_addresses.created[0]["tenant"] == 7 and nb.ipam.ip_addresses.created[0]["vrf"] is None

    switch.update({"create_vlans": [20], "create_prefixes": ["10.1.0.0/24"],
                   "network_vrfs": {"10.1.0.0/24": "Kunde-Mgmt"}})
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == []
    assert nb.ipam.vrfs.created == [{"name": "Kunde-Mgmt", "tenant": 7}]
    vrf_id = nb.ipam.vrfs.records[0].id
    assert nb.ipam.prefixes.created == [{"prefix": "10.1.0.0/24", "status": "active", "tenant": 7, "vrf": vrf_id}]
    assert nb.ipam.vlans.created == [{"vid": 20, "name": "Clients", "status": "active", "tenant": 7}]
    port = nb.dcim.interfaces.get(3)
    assert port.mode == "tagged" and sorted(port.tagged_vlans) == sorted([110, nb.ipam.vlans.records[1].id])


def test_switch_without_tenant_is_refused():
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.devices.get(1).tenant = None
    assert "keinen Mandanten" in plan_switch_sync(nb, switch, snapshot)["error"]
    report = sync_snapshot_to_netbox(nb, switch, snapshot)
    assert "keinen Mandanten" in report["errors"][0] and nb.dcim.interfaces.updates == []


def test_vlan_names_are_parsed():
    from netbox_sync.core.switches import parse_vlan_names
    assert parse_vlan_names('vlan 10\n   name "Server"\n   exit\nvlan 20\n name Clients\n#\n') == {
        10: "Server", 20: "Clients"}
