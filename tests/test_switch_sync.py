from types import SimpleNamespace

from fake_netbox import Endpoint, FakeNetBox, Record

from netbox_sync.core.switches import (
    build_snapshot,
    mask_config,
    match_interfaces,
    parse_lags,
    parse_stack,
    plan_switch_sync,
    port_label,
    stack_member,
    sync_snapshot_to_netbox,
)
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
    assert [(item["vid"], item["netbox_id"], item["method"], item["new_name"]) for item in plan["vlans"]] == [
        (1, None, "new", "VLAN1"), (10, 110, "vid", "VLAN10"), (20, None, "new", "Clients")]
    assert plan["networks"] == [{"network": "10.1.0.0/24", "vrf_id": 0, "vrf_name": "", "existing": [],
                                 "create": True, "stored": False}]

    # Not approved: nothing is created, only the existing VLAN 10 is assigned.
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert (report["vlans_created"], report["prefixes_created"], report["vrfs_created"]) == (0, 0, 0)
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert nb.ipam.vlans.created == [] and nb.ipam.prefixes.created == []
    assert nb.dcim.interfaces.get(1).untagged_vlan == 110 and nb.dcim.interfaces.get(1).mode == "access"
    assert nb.ipam.ip_addresses.created[0]["tenant"] == 7 and nb.ipam.ip_addresses.created[0]["vrf"] is None

    switch.update({"vlan_map": {"20": {"action": "new", "name": "Clients"}}, "network_map": {"10.1.0.0/24": {"vrf_id": None, "vrf_name": "Kunde-Mgmt", "create": True}}})
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


def test_vlans_of_sibling_tenants_are_matched_by_vid_and_name():
    nb, switch, snapshot = _numbered_setup()
    group = SimpleNamespace(id=3, name="Kunde")
    leo = SimpleNamespace(id=8, name="Leo")
    nb.ipam.vlans.records += [
        Record(nb.ipam.vlans, id=201, vid=2, name="Leo Server", tenant=leo, tenant_group=group),
        Record(nb.ipam.vlans, id=202, vid=2, name="Gast", tenant=SimpleNamespace(id=9), tenant_group=group),
        Record(nb.ipam.vlans, id=230, vid=30, name="Fremd", tenant=SimpleNamespace(id=99), tenant_group=None),
    ]
    snapshot["interfaces"][0]["vlans"] = {"untagged": [2], "tagged": [], "tagged_all": False}
    snapshot["vlan_names"] = {2: "Server_LEO", 30: "Drucker"}
    plan = plan_switch_sync(nb, switch, snapshot)
    by_vid = {item["vid"]: item for item in plan["vlans"]}
    assert (by_vid[2]["netbox_id"], by_vid[2]["method"]) == (201, "name")
    assert [candidate["id"] for candidate in by_vid[2]["candidates"]] == [202, 201]
    assert by_vid[30]["method"] == "new", "VLANs outside the tenant group are never used"

    switch["vlan_map"] = {"30": {"action": "new", "name": "Drucker-Netz"}, "20": {"action": "skip"}}
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == []
    assert nb.ipam.vlans.created == [{"vid": 30, "name": "Drucker-Netz", "status": "active", "tenant": 7}]
    assert nb.dcim.interfaces.get(1).untagged_vlan == 201
    assert getattr(nb.dcim.interfaces.get(2), "mode", None) is None, "skipped VLAN leaves the port alone"


def test_new_vlan_can_get_another_tenant_of_the_group():
    nb, switch, snapshot = _numbered_setup()
    group = SimpleNamespace(id=3, name="Kunde")
    nb.tenancy.tenants.records.append(Record(nb.tenancy.tenants, id=8, name="Leo", group=group))
    nb.tenancy.tenants.records.append(Record(nb.tenancy.tenants, id=9, name="Fremd", group=None))
    plan = plan_switch_sync(nb, switch, snapshot)
    assert [row["name"] for row in plan["tenants"]] == ["Leo", "Muster"]
    assert {item["new_tenant"] for item in plan["vlans"]} == {7}

    switch["vlan_map"] = {"10": {"action": "new", "name": "Leo Server", "tenant": 8}}
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert nb.ipam.vlans.created == [{"vid": 10, "name": "Leo Server", "status": "active", "tenant": 8}]
    # NetBox resolves the tenant group through the tenant; the fake needs it set.
    nb.ipam.vlans.records[-1].tenant_group = group
    again = plan_switch_sync(nb, switch, snapshot)["vlans"][0]
    assert (again["netbox_id"], again["method"]) == (nb.ipam.vlans.records[-1].id, "manual"), "never created twice"


def test_ignored_vlans_are_left_out_everywhere():
    nb, switch, snapshot = _numbered_setup()
    nb.ipam.vlans.records.append(Record(nb.ipam.vlans, id=110, vid=10, name="Server", tenant=SimpleNamespace(id=7)))
    snapshot["interfaces"][0]["vlans"] = {"untagged": [10], "tagged": [20, 30], "tagged_all": False}
    switch["vlan_map"] = {"20": {"action": "ignore"}, "30": {"action": "new", "name": "Drucker"}}
    plan = plan_switch_sync(nb, switch, snapshot)
    by_vid = {item["vid"]: item for item in plan["vlans"]}
    assert (by_vid[20]["method"], by_vid[20]["stored"]) == ("ignore", True)
    assert by_vid[10]["stored"] is False
    assert {item["name"]: item["vlan_label"] for item in plan["interfaces"]}["1/1/1"] == "10,30"

    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == []
    assert [payload["vid"] for payload in nb.ipam.vlans.created] == [30]
    port = nb.dcim.interfaces.get(1)
    assert port.label == "10,30" and port.tagged_vlans == [nb.ipam.vlans.records[-1].id]


def test_vlan_decisions_move_from_switches_to_the_profile(tmp_path):
    from netbox_sync.core.config import ProfileStore
    store = ProfileStore(tmp_path / "config.json")
    profile = {"name": "Kunde", "switches": [{"id": "s", "name": "sw", "password": "x",
                                              "vlan_map": {"5": {"action": "ignore"}}, "create_vlans": [6]}]}
    store.save({"Kunde": profile}, "Kunde")
    loaded = store.load()[0]["Kunde"]
    assert loaded["vlan_map"] == {"5": {"action": "ignore"}}
    assert "vlan_map" not in loaded["switches"][0] and "create_vlans" not in loaded["switches"][0]


def test_vrfs_of_the_tenant_group_are_offered_and_found():
    nb, switch, snapshot = _numbered_setup()
    group = SimpleNamespace(id=3, name="Kunde")
    rz = SimpleNamespace(id=8, name="RZ")
    nb.ipam.vrfs.records += [
        Record(nb.ipam.vrfs, id=60, name="Mgmt", tenant=rz, tenant_group=group),
        Record(nb.ipam.vrfs, id=61, name="Fremd", tenant=SimpleNamespace(id=99), tenant_group=None),
    ]
    nb.ipam.prefixes.records.append(Record(nb.ipam.prefixes, id=70, prefix="10.10.167.0/24", vrf=SimpleNamespace(id=60)))
    snapshot["interfaces"][0]["ip_addresses"] = ["10.10.167.5/24"]

    plan = plan_switch_sync(nb, switch, snapshot)
    assert plan["vrfs"] == [{"id": 60, "label": "Mgmt (RZ)"}], "group VRFs only"
    assert plan["networks"][0] == {"network": "10.10.167.0/24", "vrf_id": 60, "vrf_name": "", "existing": [60],
                                   "create": True, "stored": False}

    switch["network_map"] = {"10.10.167.0/24": {"vrf_id": 60, "vrf_name": "", "create": False}}
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == [] and report["prefixes_created"] == 0 and nb.ipam.vrfs.created == []
    assert nb.ipam.ip_addresses.created[0]["vrf"] == 60
    assert plan_switch_sync(nb, switch, snapshot)["networks"][0]["stored"] is True


def test_legacy_network_choices_still_work_and_migrate(tmp_path):
    nb, switch, snapshot = _numbered_setup()
    snapshot["interfaces"][0]["ip_addresses"] = ["10.1.0.1/24"]
    switch.update({"network_vrfs": {"10.1.0.0/24": ""}, "create_prefixes": ["10.1.0.0/24"]})
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert report["prefixes_created"] == 1

    from netbox_sync.core.config import ProfileStore
    store = ProfileStore(tmp_path / "config.json")
    store.save({"Kunde": {"name": "Kunde", "switches": [dict(switch, password="x")]}}, "Kunde")
    loaded = store.load()[0]["Kunde"]
    assert loaded["network_map"] == {"10.1.0.0/24": {"vrf_id": None, "vrf_name": "", "create": True}}
    assert "network_vrfs" not in loaded["switches"][0]


CX_STACK_CONFIG = """
hostname Switch36
vsf member 1
    type jl661a
    link 1 1/1/49
    link 2 1/1/50
vsf member 2
    type jl661a
    link 1 2/1/49
    link 2 2/1/51
interface lag 1
    no shutdown
    no routing
    vlan trunk native 1
    vlan trunk allowed 10,200-204
interface 1/1/1
    no routing
    vlan access 10
interface 1/1/52
    lag 1
interface 2/1/1
    no routing
    vlan access 10
interface 2/1/52
    lag 1
interface vlan 201
    ip address 172.16.201.2/24
"""


def _stack_snapshot():
    interfaces = [
        {"name": name, "enabled": True, "connected": True, "media": "1GbT", "speed_mbps": 1000, "ip_addresses": [],
         "vlans": {"untagged": [10], "tagged": [], "tagged_all": False}}
        for name in ("1/1/1", "1/1/2", "2/1/1", "2/1/2")
    ] + [{"name": "lag1", "enabled": True, "connected": True, "ip_addresses": [], "vlans": None}]
    return {"platform": "aruba_cx", "interfaces": interfaces, "neighbors": [],
            "stack": parse_stack(CX_STACK_CONFIG, "aruba_cx", interfaces)}


def test_aruba_cx_vsf_stack_is_detected():
    stack = parse_stack(CX_STACK_CONFIG, "aruba_cx")
    assert stack == [{"member": 1, "model": "jl661a", "mac": ""}, {"member": 2, "model": "jl661a", "mac": ""}]
    assert stack_member("2/1/5", "aruba_cx") == 2 and stack_member("lag1", "aruba_cx") is None
    assert stack_member("GigabitEthernet2/0/5", "hpe_comware") == 2 and stack_member("2/A3", "aruba_aos") == 2
    assert parse_stack("vsf member 1\n    type jl661a\n", "aruba_cx") == [], "one member is no stack"
    aos = 'stacking\n   member 1 type "JL256A" mac-address 9020c2-000001\n   member 2 type "JL256A"\n   exit\n'
    assert [(item["member"], item["model"]) for item in parse_stack(aos, "aruba_aos")] == [(1, "JL256A"), (2, "JL256A")]


def test_stack_members_go_to_their_own_netbox_devices():
    nb, switch, _snapshot = _numbered_setup()
    snapshot = _stack_snapshot()
    # Member 2 is its own NetBox device whose device type named the ports "1/1/x".
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=4, name="access-01-2", tenant=SimpleNamespace(id=7)))
    for index, name in enumerate(("1/1/1", "1/1/2"), start=60):
        nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=index, name=name, device=SimpleNamespace(id=4),
                                                 type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None,
                                                 label=""))

    plan = plan_switch_sync(nb, switch, snapshot)
    assert [(entry["member"], entry["device_id"], entry["ports"]) for entry in plan["stack"]] == [(1, 1, 2), (2, None, 2)]
    assert plan["skipped_ports"] == 2
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert report["notes"] == ["Stack-Mitglied 2: kein NetBox-Gerät gewählt – 2 Ports übersprungen"]

    switch["stack_devices"] = {"1": 1, "2": 4}
    plan = plan_switch_sync(nb, switch, snapshot)
    by_name = {item["name"]: item for item in plan["interfaces"]}
    assert by_name["2/1/1"]["owner_id"] == 4 and by_name["2/1/1"]["netbox_name"] == "1/1/1"
    assert by_name["1/1/1"]["owner_id"] == 1 and by_name["1/1/1"]["netbox_name"] == "1"
    assert plan["owner_interfaces"]["4"] == ["1/1/1", "1/1/2"]
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert nb.dcim.interfaces.get(60).label == "10" and nb.dcim.interfaces.get(1).label == "10"
    assert [(payload["device"], payload["name"]) for payload in nb.dcim.interfaces.created] == [(1, "lag1")]


def test_stack_is_filled_in_netbox_utilities_when_the_plugin_exists():
    nb, switch, _snapshot = _numbered_setup()
    snapshot = _stack_snapshot()
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=4, name="access-01-2", tenant=SimpleNamespace(id=7)))
    switch["stack_devices"] = {"1": 1, "2": 4}
    # Without the plugin the stack is simply not written.
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert not report["errors"] and not any("Switch-Stack" in change for change in report["changes"])

    nb.plugins = SimpleNamespace(utilities=SimpleNamespace(switch_stacks=Endpoint()))
    stacks = nb.plugins.utilities.switch_stacks
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert "Switch-Stack access-01 neu mit Mitgliedern access-01-2 (NetBox Utilities)" in report["changes"]
    assert stacks.created == []
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert stacks.created == [{"device": 1, "members": [4]}]
    stacks.records[0].device = SimpleNamespace(id=1)
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert not any("Switch-Stack" in change for change in report["changes"]), "already complete"

    # Members entered by hand stay, missing ones are added.
    stacks.records[0].members = [9]
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert stacks.updates[-1] == (stacks.records[0].id, {"members": [9, 4]})


def test_virtual_chassis_positions_are_suggested():
    nb, switch, _snapshot = _numbered_setup()
    snapshot = _stack_snapshot()
    nb.dcim.devices.get(1).virtual_chassis = SimpleNamespace(id=9)
    nb.dcim.devices.get(1).vc_position = 1
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=4, name="access-01-2", tenant=SimpleNamespace(id=7),
                                          virtual_chassis=SimpleNamespace(id=9), vc_position=2))
    plan = plan_switch_sync(nb, switch, snapshot)
    assert [entry["device_id"] for entry in plan["stack"]] == [1, 4]


CX_LAG_BRIEF = """
Port           Native  Mode   Type           Enabled Status  Reason                  Speed   Description
1/1/1          1       trunk  10G-SR         yes     up                              10000   SOEM-E1
1/1/2          1       trunk  10G-SR         yes     up                              10000   SOEM-E1
1/1/23         120     access 1G-BT          yes     up                              1000    FritzBox
lag1           1       trunk  --             yes     up      --                      20000   Etage 1 Uplink
"""

CX_LAG_CONFIG = """vlan 1
vlan 100
interface lag 1
    no routing
    vlan trunk native 1
    vlan trunk allowed 100,110,120,201,209-213
interface 1/1/1
    no shutdown
    lag 1
interface 1/1/2
    no shutdown
    lag 1
interface 1/1/23
    no routing
    vlan access 120
"""


def test_lags_are_parsed_for_all_platforms():
    assert parse_lags(CX_LAG_CONFIG, "aruba_cx") == {"1/1/1": "lag1", "1/1/2": "lag1"}
    assert parse_lags("trunk 49-50 trk1 lacp\ntrunk 1/A1-1/A2 Trk2 trunk\n", "aruba_aos") == {
        "49": "Trk1", "50": "Trk1", "1/a1": "Trk2", "1/a2": "Trk2"}
    comware = "interface GigabitEthernet1/0/49\n port link-aggregation group 3\n#\n"
    assert parse_lags(comware, "hpe_comware") == {"ge1/0/49": "Bridge-Aggregation3"}


def test_lag_members_take_the_lag_vlans_and_name_the_lag_in_the_label():
    snapshot = build_snapshot({"interfaces": CX_LAG_BRIEF, "neighbors": "", "config": CX_LAG_CONFIG}, "aruba_cx")
    ports = {item["name"]: item for item in snapshot["interfaces"]}
    assert ports["1/1/1"]["lag"] == "lag1" and ports["1/1/23"]["lag"] is None
    assert port_label(ports["1/1/1"]["vlans"], ports["1/1/1"]["lag"]) == "1,100,110,120,201,209-213 (LAG 1)"
    assert port_label(ports["1/1/23"]["vlans"], ports["1/1/23"]["lag"]) == "120"
    assert port_label(None, "Trk2") == "(LAG 2)"
    assert len(port_label({"untagged": [], "tagged": list(range(2, 400, 2)), "tagged_all": False}, "lag1")) <= 64
    assert "    lag 1" in snapshot["raw"]["vlan_config"]


def test_lag_members_are_assigned_to_their_lag_in_netbox():
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=30, name="lag1", device=SimpleNamespace(id=1),
                                             type=SimpleNamespace(value="lag"), enabled=True, cable=None, label=""))
    snapshot["interfaces"].append({"name": "lag1", "enabled": True, "connected": True, "ip_addresses": [],
                                   "vlans": None})
    for item in snapshot["interfaces"][:2]:
        item["lag"] = "lag1"
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert "Interface 1: LAG lag1" in report["changes"] and nb.dcim.interfaces.updates == []
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert nb.dcim.interfaces.get(1).lag == 30 and nb.dcim.interfaces.get(1).label == "10 (LAG 1)"
    assert not getattr(nb.dcim.interfaces.get(3), "lag", None)
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True)
    assert not any("LAG lag1" in change for change in report["changes"]), "already assigned"


class _AttachmentEndpoint(Endpoint):
    def create(self, payload):
        record = super().create(payload)
        if self.assignments is not None:
            record.assignments = []
        return record

    def filter(self, **kwargs):
        query = kwargs.pop("q", "")
        return [item for item in super().filter(**kwargs) if query in (getattr(item, "description", "") or "")]


def _attachments(newer=True):
    attachments = _AttachmentEndpoint()
    attachments.assignments = [] if newer else None
    return SimpleNamespace(netbox_attachments=attachments, netbox_attachment_assignments=Endpoint())


def test_config_backup_masks_secrets():
    config = ('hostname core\nuser admin group administrators password ciphertext AQBapW==\n'
              'snmp-server community geheim\nradius-server host 10.0.0.1 key plaintext topsecret\n')
    masked = mask_config(config)
    assert "AQBapW" not in masked and "geheim" not in masked and "topsecret" not in masked
    assert "hostname core" in masked and "password <entfernt>" in masked


def test_config_is_attached_only_when_it_changed():
    nb, switch, snapshot = _numbered_setup()
    snapshot["config_backup"] = "hostname access-01"
    # Without the plugin nothing happens.
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, backup_config=True)
    assert not report["errors"]

    nb.plugins = SimpleNamespace(netbox_attachments=_attachments())
    app = nb.plugins.netbox_attachments
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True, backup_config=True)
    assert any(change.startswith("Konfigurations-Backup: Anhang „Running-Config access-01")
               for change in report["changes"]) and app.netbox_attachments.created == []
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, backup_config=True)
    payload = app.netbox_attachments.created[0]
    assert payload["file"][1].getvalue() == b"hostname access-01" and payload["file"][0].endswith(".txt")
    assert app.netbox_attachment_assignments.created == [
        {"attachment": app.netbox_attachments.records[0].id, "object_type": "dcim.device", "object_id": 1}]

    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, dry_run=True, backup_config=True)
    assert not any("Konfigurations-Backup" in change for change in report["changes"]), "unchanged"
    snapshot["config_backup"] = "hostname access-01\nvlan 10"
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, backup_config=True)
    assert len(app.netbox_attachments.created) == 2


def test_config_backup_with_older_attachment_plugin():
    nb, switch, snapshot = _numbered_setup()
    snapshot["config_backup"] = "hostname access-01"
    nb.plugins = SimpleNamespace(netbox_attachments=_attachments(newer=False))
    app = nb.plugins.netbox_attachments
    sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False, backup_config=True)
    payload = app.netbox_attachments.created[0]
    assert (payload["object_type"], payload["object_id"]) == ("dcim.device", 1)
    assert app.netbox_attachment_assignments.created == []


def test_cable_to_access_point_that_reports_its_mac_as_port():
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=5, name="PC-AP-1", tenant=SimpleNamespace(id=7)))
    nb.dcim.interfaces.records += [
        Record(nb.dcim.interfaces, id=50, name="LAN", device=SimpleNamespace(id=5), mac_address="C8:4F:86:88:96:8E",
               type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None),
        Record(nb.dcim.interfaces, id=51, name="LAN2", device=SimpleNamespace(id=5), mac_address=None,
               type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None),
    ]
    snapshot["neighbors"][0].update(system_name="AP6420-PA3007MFJ3BDX1A", remote_port="c8:4f:86:88:96:8e")
    switch["neighbor_devices"] = {"1/1/2": 5}
    sync_snapshot_to_netbox(nb, switch, snapshot)
    assert nb.dcim.cables.created[0]["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 50}]

    # A device with a single port is cabled there even if neither name nor MAC match.
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=5, name="PC-AP-1", tenant=SimpleNamespace(id=7)))
    nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=50, name="eth0", device=SimpleNamespace(id=5),
                                             type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None))
    snapshot["neighbors"][0].update(system_name="AP6420", remote_port="c8:4f:86:88:96:cc")
    switch["neighbor_devices"] = {"1/1/2": 5}
    sync_snapshot_to_netbox(nb, switch, snapshot)
    assert nb.dcim.cables.created[0]["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 50}]


def _cabled_setup(far_type="dcim.interface"):
    """Port 1/1/2 (NetBox "2") has a cable to "old-device eth9"; LLDP reports server-01 eth0."""
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.interfaces.get(2).cable = SimpleNamespace(id=70)
    far = {"object_type": far_type, "object_id": 99,
           "object": {"name": "eth9", "device": {"name": "old-device"}}}
    nb.dcim.cables.records.append(Record(nb.dcim.cables, id=70, a_terminations=[far],
                                         b_terminations=[{"object_type": "dcim.interface", "object_id": 2}]))
    switch["neighbor_devices"] = {"1/1/2": 2}
    return nb, switch, snapshot


def test_differing_cable_is_only_reported_by_default():
    nb, switch, snapshot = _cabled_setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot)
    assert report["cables_updated"] == 0 and nb.dcim.cables.updates == []
    assert any("in NetBox mit old-device eth9 verbunden – nicht angepasst" in note for note in report["notes"])


def test_differing_cable_is_moved_when_allowed():
    nb, switch, snapshot = _cabled_setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot, dry_run=True, update_cables=True)
    assert report["cables_updated"] == 1 and nb.dcim.cables.updates == []
    assert any(change.startswith("⚠ Kabel access-01") and "old-device eth9" in change for change in report["changes"])
    sync_snapshot_to_netbox(nb, switch, snapshot, update_cables=True)
    assert nb.dcim.cables.updates == [(70, {
        "a_terminations": [{"object_type": "dcim.interface", "object_id": 2}],
        "b_terminations": [{"object_type": "dcim.interface", "object_id": 20}]})]
    assert nb.dcim.cables.created == []


def test_cables_to_patch_panels_and_busy_far_ends_are_kept():
    nb, switch, snapshot = _cabled_setup(far_type="dcim.frontport")
    report = sync_snapshot_to_netbox(nb, switch, snapshot, update_cables=True)
    assert report["cables_updated"] == 0 and nb.dcim.cables.updates == [] and not report["notes"]

    nb, switch, snapshot = _cabled_setup()
    nb.dcim.interfaces.get(20).cable = SimpleNamespace(id=71)
    report = sync_snapshot_to_netbox(nb, switch, snapshot, update_cables=True)
    assert nb.dcim.cables.updates == []
    assert any("Gegenstelle hat bereits ein anderes Kabel" in note for note in report["notes"])


def test_vlans_of_another_site_are_only_labelled():
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.devices.get(1).site = SimpleNamespace(id=2)
    nb.ipam.vlans.records += [
        Record(nb.ipam.vlans, id=110, vid=10, name="MOBILE", tenant=SimpleNamespace(id=7), site=SimpleNamespace(id=1)),
        Record(nb.ipam.vlans, id=120, vid=20, name="CLIENTS", tenant=SimpleNamespace(id=7), site=None),
    ]
    report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables=False)
    assert report["errors"] == []
    assert nb.dcim.interfaces.get(1).label == "10" and getattr(nb.dcim.interfaces.get(1), "untagged_vlan", None) is None
    assert nb.dcim.interfaces.get(2).untagged_vlan == 120, "global VLANs are always assigned"
    assert [note for note in report["notes"] if "VLAN 10" in note] == [
        "VLAN 10 „MOBILE“ gehört zu einem anderen Standort als access-01 – nur im Label vermerkt, nicht am Port "
        "zugewiesen (VLAN global machen, um es zuzuweisen)"]


def test_skipped_cables_are_explained():
    nb, switch, snapshot = _numbered_setup()
    report = sync_snapshot_to_netbox(nb, switch, snapshot, dry_run=True)
    assert report["cables_created"] == 0
    assert any("nicht in NetBox gefunden" in note for note in report["notes"])
    switch["neighbor_devices"] = {"1/1/2": 0}  # "no cable" is chosen deliberately: no note
    report = sync_snapshot_to_netbox(nb, switch, snapshot, dry_run=True)
    assert not any("übersprungen" in note for note in report["notes"])


def test_wireless_interfaces_are_no_cable_targets():
    """LANCOM LW-600: eth1 plus wlan0/wlan1; LLDP reports "LAN1" – wlan1 must not make it ambiguous."""
    nb, switch, snapshot = _numbered_setup()
    nb.dcim.devices.records.append(Record(nb.dcim.devices, id=5, name="AP-005", tenant=SimpleNamespace(id=7)))
    nb.dcim.interfaces.records += [
        Record(nb.dcim.interfaces, id=50, name="eth1", device=SimpleNamespace(id=5), mac_address=None,
               type=SimpleNamespace(value="1000base-t"), enabled=True, cable=None),
        Record(nb.dcim.interfaces, id=51, name="wlan0", device=SimpleNamespace(id=5), mac_address=None,
               type=SimpleNamespace(value="ieee802.11ax"), enabled=True, cable=None),
        Record(nb.dcim.interfaces, id=52, name="wlan1", device=SimpleNamespace(id=5), mac_address=None,
               type=SimpleNamespace(value="ieee802.11ax"), enabled=True, cable=None),
    ]
    snapshot["neighbors"][0].update(system_name="AP-005", remote_port="LAN1", port_description="LAN1")
    report = sync_snapshot_to_netbox(nb, switch, snapshot)
    assert report["errors"] == []
    assert nb.dcim.cables.created[0]["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 50}]
