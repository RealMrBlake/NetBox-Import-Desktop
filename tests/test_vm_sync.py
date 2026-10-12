from types import SimpleNamespace

from fake_netbox import FakeNetBox, Record

from netbox_sync.core.vm_sync import VMSyncEngine


def _engine():
    nb = FakeNetBox()
    nb.virtualization.clusters.records.append(Record(nb.virtualization.clusters, id=1, name="RZ1",
                                                     tenant=SimpleNamespace(id=10, name="Muster")))
    nb.tenancy.tenants.records.append(Record(nb.tenancy.tenants, id=10, name="Muster"))
    nb.ipam.vrfs.records.append(Record(nb.ipam.vrfs, id=5, name="Muster - Prod", rd=None,
                                       tenant=SimpleNamespace(id=10)))
    engine = VMSyncEngine(nb)
    engine.load_targets()
    return nb, engine


VM = {
    "name": "web01", "source": "vc", "status": "active", "vcpus": 2, "memory": 4096, "disk": 40960,
    "guest_os": "Debian", "kind": "VM",
    "interfaces": [{"name": "NIC-0", "mac": "00:50:56:aa:bb:cc", "ips": ["10.20.30.11/24", "fe80::1"]}],
}


def test_load_targets_and_cluster_tenant():
    _nb, engine = _engine()
    assert engine.clusters == {"RZ1": 1}
    assert engine.cluster_tenants["RZ1"]["name"] == "Muster"
    assert [vrf.name for vrf in engine.tenant_vrfs(10)] == ["Muster - Prod"]


def test_detect_and_import_creates_prefix_vm_ip_and_mac():
    nb, engine = _engine()
    missing = engine.detect_missing_networks([VM], tenant_id=10, default_vrf_id=5)
    assert [str(item["suggested_network"]) for item in missing] == ["10.20.30.0/24"]
    engine.apply_network_plan([{"network": "10.20.30.0/24", "vrf_id": 5, "create_vrf": False, "vrf_name": None}], 10)

    assert engine.sync_vm(VM, cluster_id=1, tenant_id=10, vrf_id=5) == "created"
    assert nb.ipam.prefixes.created == [{"prefix": "10.20.30.0/24", "status": "active", "tenant": 10, "vrf": 5}]
    ip = nb.ipam.ip_addresses.created[0]
    assert ip["address"] == "10.20.30.11/24" and ip["vrf"] == 5 and ip["tenant"] == 10
    assert nb.dcim.mac_addresses.created[0]["mac_address"] == "00:50:56:AA:BB:CC"
    vm = nb.virtualization.virtual_machines.records[0]
    assert vm.primary_ip4 == nb.ipam.ip_addresses.records[0].id

    # Second run updates instead of duplicating.
    assert engine.sync_vm(VM, cluster_id=1, tenant_id=10, vrf_id=5) == "updated"
    assert len(nb.virtualization.virtual_machines.records) == 1
    assert len(nb.ipam.ip_addresses.records) == 1


def test_global_vrf_import():
    nb, engine = _engine()
    engine.detect_missing_networks([VM], tenant_id=10, default_vrf_id=None)
    engine.apply_network_plan([{"network": "10.20.30.0/24", "vrf_id": None, "create_vrf": False, "vrf_name": None}], 10)
    engine.sync_vm(VM, cluster_id=1, tenant_id=10, vrf_id=None)
    assert "vrf" not in nb.ipam.prefixes.created[0]
    assert nb.ipam.ip_addresses.created[0]["vrf"] is None
