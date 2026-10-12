from types import SimpleNamespace

from fake_netbox import FakeNetBox, Record

from netbox_sync.core.switches import build_snapshot, sync_snapshot_to_netbox
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
