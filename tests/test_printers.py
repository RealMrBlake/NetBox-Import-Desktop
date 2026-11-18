from types import SimpleNamespace

import pytest
from fake_netbox import FakeNetBox, Record
from snmp_agent import Agent

from netbox_sync.core import printers, snmp
from netbox_sync.core.printers import PrinterSyncEngine, printer_key, read_printer, supplies_text, to_import

IP = "10.20.0.50"
MIB = {
    printers.SYS_DESCR: "HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153,EEPROM JSI24090012",
    printers.SYS_OBJECT_ID: ("oid", "1.3.6.1.4.1.11.2.3.9.1"),
    printers.SYS_NAME: "NPI1A2B3C",
    printers.SYS_LOCATION: "Geschäftsstelle 1. OG",
    printers.SYS_CONTACT: "IT",
    f"{printers.HR_DEVICE_TYPE}.1": ("oid", printers.HR_PRINTER),
    f"{printers.HR_DEVICE_TYPE}.2": ("oid", "1.3.6.1.2.1.25.3.1.4"),
    f"{printers.HR_DEVICE_DESCR}.1": "HP Color LaserJet Pro M454dw",
    f"{printers.HR_PRINTER_STATUS}.1": 3,
    f"{printers.HR_PRINTER_ERRORS}.1": b"\x20\x00",  # low toner
    f"{printers.PRT_SERIAL}.1": "vnb3k12345",
    f"{printers.PRT_LIFE_COUNT}.1.1": ("counter", 48211),
    f"{printers.PRT_SUPPLIES}.3.1.1": 1, f"{printers.PRT_SUPPLIES}.3.1.2": 2,
    f"{printers.PRT_SUPPLIES}.5.1.1": 21, f"{printers.PRT_SUPPLIES}.5.1.2": 21,
    f"{printers.PRT_SUPPLIES}.6.1.1": "Black Cartridge HP W2030A",
    f"{printers.PRT_SUPPLIES}.6.1.2": "Cyan Cartridge HP W2031A",
    f"{printers.PRT_SUPPLIES}.8.1.1": 2400, f"{printers.PRT_SUPPLIES}.8.1.2": 100,
    f"{printers.PRT_SUPPLIES}.9.1.1": 600, f"{printers.PRT_SUPPLIES}.9.1.2": -3,
    f"{printers.PRT_COLORANT}.1.1": "black", f"{printers.PRT_COLORANT}.1.2": "cyan",
    f"{printers.PRT_INPUT}.9.1.2": 250, f"{printers.PRT_INPUT}.10.1.2": 120,
    f"{printers.PRT_INPUT}.13.1.2": "Fach 2",
    f"{printers.PRT_DISPLAY}.1.1": "Bereit",
    f"{printers.IF_TABLE}.2.2": "eth0", f"{printers.IF_TABLE}.3.2": 6,
    f"{printers.IF_TABLE}.6.2": bytes.fromhex("a0b1c2d3e4f5"),
    f"{printers.IP_ADDR_IFINDEX}.{IP}": 2,
    f"{printers.IP_ADDR_MASK}.{IP}": ("ip", "255.255.255.0"),
    "1.3.6.1.2.1.47.1.1.1.1.10.1": "20240115",
}
USER = {"version": "3", "username": "scan", "auth_protocol": "SHA", "auth_password": "authpass123",
        "priv_protocol": "AES", "priv_password": "privpass123"}


@pytest.fixture
def agent():
    instance = Agent(MIB, community="geheim", users={"scan": USER})
    yield instance
    instance.close()


def _client(agent, record):
    return snmp.SnmpClient("127.0.0.1", record, timeout=0.5, retries=0, port=agent.port)


def test_v2c_get_and_walk(agent):
    client = _client(agent, {"version": "2c", "community": "geheim"})
    assert client.get([printers.SYS_NAME])[printers.SYS_NAME] == b"NPI1A2B3C"
    walked = client.walk(printers.PRT_SUPPLIES)
    assert len(walked) == 10 and all(snmp.in_subtree(oid, printers.PRT_SUPPLIES) for oid in walked)
    with pytest.raises(snmp.SnmpTimeout):
        _client(agent, {"version": "2c", "community": "public"}).get([printers.SYS_NAME])


@pytest.mark.parametrize("auth, priv", [("SHA", "AES"), ("MD5", "DES"), ("SHA-256", "AES-256"), ("SHA-512", ""),
                                        ("SHA-224", "AES-192")])
def test_v3_auth_and_privacy(auth, priv):
    record = dict(USER, auth_protocol=auth, priv_protocol=priv)
    agent = Agent(MIB, users={"scan": record})
    try:
        client = _client(agent, record)
        assert client.get([printers.SYS_NAME])[printers.SYS_NAME] == b"NPI1A2B3C"
        assert len(client.walk(printers.PRT_SUPPLIES)) == 10
    finally:
        agent.close()


def test_connect_tries_all_credentials_and_explains_v3_errors(agent):
    wrong_v3 = dict(USER, id="b", auth_password="falsch-falsch")
    client, notes = snmp.connect("127.0.0.1", [{"id": "a", "version": "2c", "community": "public"}, wrong_v3,
                                               dict(USER, id="c")], timeout=0.3, retries=0, port=agent.port)
    assert client.credential.record["id"] == "c"
    assert notes == ["SNMPv3 scan: falsches Authentifizierungs-Passwort oder -Protokoll"]
    with pytest.raises(snmp.SnmpAuthError, match="unbekannter Benutzer"):
        snmp.connect("127.0.0.1", [dict(USER, username="nobody")], timeout=0.3, retries=0, port=agent.port)


def test_printer_data_is_read_from_standard_mibs(agent):
    info = read_printer(_client(agent, {"version": "2c", "community": "geheim"}), IP)
    assert info["manufacturer"] == "HP" and info["model"] == "Color LaserJet Pro M454dw"
    assert info["serial"] == "VNB3K12345" and info["mac"] == "A0:B1:C2:D3:E4:F5" and info["prefix_length"] == 24
    assert info["page_count"] == 48211 and info["color"] is True and info["firmware"] == "20240115"
    assert info["site_hint"] == "Geschäftsstelle 1. OG" and info["status"] == "bereit"
    assert info["errors"] == ["wenig Toner"] and info["display"] == ["Bereit"]
    assert info["trays"] == [{"name": "Fach 2", "media": "", "capacity": 250, "level": "120/250 Blatt"}]
    assert supplies_text(info["supplies"]) == "Black Cartridge HP W2030A: 25 %\nCyan Cartridge HP W2031A: vorhanden"


def test_non_printer_is_ignored():
    agent = Agent({printers.SYS_NAME: "switch", printers.SYS_OBJECT_ID: ("oid", "1.3.6.1.4.1.9.1.1")})
    try:
        assert read_printer(_client(agent, {"version": "2c", "community": "public"}), IP) is None
    finally:
        agent.close()


def _netbox():
    nb = FakeNetBox()
    nb.tenancy.tenants.records.append(Record(nb.tenancy.tenants, id=7, name="Kunde", group=None))
    nb.dcim.sites.records.append(Record(nb.dcim.sites, id=21, name="Geschäftsstelle", tenant=SimpleNamespace(id=7)))
    nb.dcim.device_roles.records.append(Record(nb.dcim.device_roles, id=31, name="Drucker"))
    old = Record(nb.dcim.devices, id=201, name="Drucker EG", serial="", tenant=SimpleNamespace(id=7),
                 role=SimpleNamespace(id=31), site=SimpleNamespace(id=21), description="", primary_ip4=None,
                 custom_fields={"printer_page_count": 100})
    nb.dcim.devices.records.append(old)
    nb.dcim.interfaces.records.append(Record(nb.dcim.interfaces, id=301, name="eth0", device=old, mac_address=None))
    nb.ipam.ip_addresses.records.append(Record(nb.ipam.ip_addresses, id=401, address="10.20.0.60/24", vrf=None,
                                               assigned_object_type="dcim.interface",
                                               assigned_object=SimpleNamespace(id=301, device=SimpleNamespace(id=201)),
                                               assigned_object_id=301, tenant=None))
    return nb


def test_sync_creates_fields_and_printers_and_updates_counters(agent):
    info = read_printer(_client(agent, {"version": "2c", "community": "geheim"}), IP)
    new = to_import(dict(info, ip=IP, snmp="SNMPv2c", notes=[]))
    known = to_import(dict(info, ip="10.20.0.60", snmp="SNMPv2c", notes=[], serial="", mac="",
                           name="Drucker EG", page_count=200))
    nb = _netbox()
    nb.dcim.manufacturers.records.append(Record(nb.dcim.manufacturers, id=50, name="HP", slug="hp"))
    nb.dcim.device_types.records.append(Record(nb.dcim.device_types, id=61, model="Color LaserJet Pro M454dw",
                                               manufacturer=SimpleNamespace(id=50, name="HP")))
    engine = PrinterSyncEngine(nb, role_id=31)
    engine.load_context(7)
    items = [new, known]
    engine.match(items)
    assert known["method"] in ("Name", "IP-Adresse") and new.get("device") is None

    preview = engine.sync(items, {printer_key(new): {"site": 21, "tenant": 7}}, role_id=31, dry_run=True)
    assert preview["errors"] == [] and nb.extras.custom_fields.created == []
    assert any("Seitenzähler" in change for change in preview["changes"])

    report = engine.sync(items, {printer_key(new): {"site": 21, "tenant": 7}}, role_id=31, dry_run=False)
    assert report["errors"] == []
    assert {field["name"] for field in nb.extras.custom_fields.created} == {name for name, *_ in printers.CUSTOM_FIELDS}
    created = nb.dcim.devices.created[-1]
    assert created["name"] == "NPI1A2B3C" and created["serial"] == "VNB3K12345" and "platform" not in created
    assert created["custom_fields"]["printer_page_count"] == 48211 and created["custom_fields"]["printer_color"]
    assert created["device_type"] == 61
    assert nb.dcim.manufacturers.created == [] and nb.dcim.device_types.created == []
    assert nb.ipam.ip_addresses.created[-1]["address"] == f"{IP}/24"
    assert nb.dcim.devices.get(201).custom_fields["printer_page_count"] == 200


def test_hp_jetdirect_model_comes_from_printer_name_not_generic_sysdescr():
    mib = {key: value for key, value in MIB.items() if not key.startswith(printers.HR_DEVICE_DESCR)}
    mib[printers.HP_DEVICE_ID] = "MFG:Hewlett-Packard;CMD:PJL,PCL;MDL:HP LaserJet M402dn;CLS:PRINTER;"
    agent = Agent(mib)
    try:
        info = read_printer(_client(agent, {"version": "2c", "community": "public"}), IP)
        assert info["model"] == "LaserJet M402dn" and info["manufacturer"] == "HP"
        mib[f"{printers.PRT_PRINTER_NAME}.1"] = "HP LaserJet Pro M404dn"
    finally:
        agent.close()
    agent = Agent(mib)
    try:
        assert read_printer(_client(agent, {"version": "2c", "community": "public"}), IP)["model"] == \
            "LaserJet Pro M404dn"
    finally:
        agent.close()


def test_missing_model_can_be_mapped_to_an_existing_device_type():
    nb = _netbox()
    nb.dcim.manufacturers.records.append(Record(nb.dcim.manufacturers, id=50, name="HP", slug="hp"))
    nb.dcim.device_types.records.append(Record(nb.dcim.device_types, id=60, model="LaserJet Pro M404",
                                               manufacturer=SimpleNamespace(id=50, name="HP")))
    printer = to_import({"ip": "10.20.0.70", "name": "Drucker24", "manufacturer": "HP", "model": "LaserJet Pro M404dn",
                         "serial": "PHCLJ40175", "mac": "", "snmp": "SNMPv2c", "notes": []})
    engine = PrinterSyncEngine(nb, role_id=31, counters=False)
    engine.load_context(7)
    engine.match([printer])
    assert not engine.type_exists(printer) and engine.suggest_type(printer) == 60
    engine.type_map = {printers.model_key(printer): 60}
    report = engine.sync([printer], {printer_key(printer): {"site": 21, "tenant": 7}}, role_id=31, dry_run=False)
    assert report["errors"] == [] and nb.dcim.device_types.created == []
    assert nb.dcim.devices.created[-1]["device_type"] == 60


def test_missing_device_type_is_never_created():
    nb = _netbox()
    printer = to_import({"ip": "10.20.0.71", "name": "Drucker25", "manufacturer": "Kyocera", "model": "ECOSYS M5526",
                         "serial": "", "mac": "", "snmp": "SNMPv2c", "notes": []})
    engine = PrinterSyncEngine(nb, role_id=31, counters=False)
    engine.load_context(7)
    engine.match([printer])
    for dry_run in (True, False):
        report = engine.sync([printer], {printer_key(printer): {"site": 21, "tenant": 7}}, role_id=31,
                             dry_run=dry_run, default_site=21)
        assert len(report["errors"]) == 1 and "fehlt in NetBox" in report["errors"][0]
    assert nb.dcim.manufacturers.created == [] and nb.dcim.device_types.created == []
    assert nb.dcim.devices.created == []
