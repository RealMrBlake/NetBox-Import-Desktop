from netbox_sync.core import switches
from netbox_sync.core.switches import (
    CLI_PROMPT,
    COMWARE_PROMPT,
    build_snapshot,
    interface_key,
    netbox_interface_type,
    parse_interface_ips,
    parse_interfaces,
    parse_neighbors,
    suggest_platform,
)

AOS_BRIEF = """
 Status and Counters - Port Status

                          | Intrusion                           Flow  Bcast
  Port         Type       | Alert     Enabled Status Mode       Ctrl  Limit
  ------------ ---------- + --------- ------- ------ ---------- ----- ------
  1            100/1000T  | No        Yes     Up     1000FDx    off   0
  2            100/1000T  | No        No      Down   1000FDx    off   0
  49           SFP+SR     | No        Yes     Up     10GigFD    off   0
  Trk1                    | No        Yes     Up     1000FDx    off   0
switch#
"""

CX_BRIEF = """
--------------------------------------------------------------------------------------------------------
Port      Native  Mode   Type           Enabled Status  Reason                  Speed   Description
          VLAN                                                                  (Mb/s)
--------------------------------------------------------------------------------------------------------
1/1/1     1       access 1GbT           yes     up                              1000    Uplink
1/1/2     1       access 1GbT           no      down    Administratively down   --      --
1/1/49    --      routed SFP+DAC1       yes     up                              10000   --
core-sw#
"""

COMWARE_BRIEF = """
Brief information on interfaces in bridge mode:
Link: ADM - administratively down; Stby - standby
Speed: (a) - auto
Duplex: (a)/A - auto; H - half; F - full
Type: A - access; T - trunk; H - hybrid
Interface            Link Speed   Duplex Type PVID Description
GE1/0/1              UP   1G(a)   F(a)   A    1
GE1/0/2              ADM  auto    A      A    1
XGE1/0/49            UP   10G     F      T    1
<comware>
"""

COMWARE_LLDP = """
LLDP neighbor-information of port 1[GigabitEthernet1/0/1]:
LLDP agent nearest-bridge:
 LLDP neighbor index : 1
 Update time         : 0 days, 0 hours, 1 minutes, 1 seconds
 Chassis type        : MAC address
 Chassis ID          : 0011-2233-4455
 Port ID type        : Interface name
 Port ID             : GigabitEthernet1/0/24
 Time to live        : 121
 Port description    : GigabitEthernet1/0/24 Interface
 System name         : core-sw.example.local
 Management address type           : IPv4
 Management address                : 192.0.2.1
<comware>
"""

CX_LLDP = """
Port                           : 1/1/1
Neighbor Entries               : 1
Neighbor Chassis-Name          : access-01
Neighbor Chassis-ID            : 00:11:22:33:44:55
Neighbor Port-ID               : 1/1/52
Neighbor Port-Desc             : 1/1/52
Neighbor Management-Address    : 10.0.0.12

Port                           : 1/1/2
Neighbor Entries               : 1
Neighbor Chassis-Name          : esx01
Neighbor Port-ID               : vmnic0
core-sw#
"""

AOS_LLDP = """
 LLDP Remote Device Information Detail

  Local Port   : 24
  ChassisType  : mac-address
  ChassisId    : 00 11 22 33 44 55
  PortType     : local
  PortId       : 1/1/1
  SysName      : core-sw
  System Descr : Aruba JL...
  PortDescr    : 1/1/1
switch#
"""


def test_aos_brief_uses_columns_and_no_garbage_description():
    interfaces = {item["name"]: item for item in parse_interfaces(AOS_BRIEF, "aruba_aos")}
    assert set(interfaces) == {"1", "2", "49", "Trk1"}
    assert interfaces["1"]["enabled"] and interfaces["1"]["connected"]
    assert interfaces["1"]["speed_mbps"] == 1000
    assert not interfaces["2"]["enabled"] and not interfaces["2"]["connected"]
    assert interfaces["49"]["speed_mbps"] == 10000
    assert all(item["description"] == "" for item in interfaces.values())
    assert netbox_interface_type(interfaces["49"]) == "10gbase-x-sfpp"
    assert netbox_interface_type(interfaces["1"]) == "1000base-t"
    assert netbox_interface_type(interfaces["Trk1"]) == "lag"


def test_cx_brief():
    interfaces = {item["name"]: item for item in parse_interfaces(CX_BRIEF, "aruba_cx")}
    assert set(interfaces) == {"1/1/1", "1/1/2", "1/1/49"}
    assert interfaces["1/1/1"]["connected"]
    assert not interfaces["1/1/2"]["enabled"]
    assert netbox_interface_type(interfaces["1/1/49"]) == "10gbase-x-sfpp"


def test_comware_names_are_expanded_and_adm_is_disabled():
    interfaces = {item["name"]: item for item in parse_interfaces(COMWARE_BRIEF, "hpe_comware")}
    assert set(interfaces) == {"GigabitEthernet1/0/1", "GigabitEthernet1/0/2", "Ten-GigabitEthernet1/0/49"}
    assert not interfaces["GigabitEthernet1/0/2"]["enabled"]
    assert interfaces["GigabitEthernet1/0/1"]["speed_mbps"] == 1000


def test_interface_key_matches_vendor_spellings():
    assert interface_key("GE1/0/1") == interface_key("GigabitEthernet1/0/1") == interface_key("Gi1/0/1")
    assert interface_key("XGE1/0/49") == interface_key("Ten-GigabitEthernet1/0/49") == interface_key("Te1/0/49")
    assert interface_key("Vlan-interface10") == interface_key("vlan10") == interface_key("Vlan 10")
    assert interface_key("1/1/1") == "1/1/1"
    assert interface_key("1/1/1") != interface_key("1/1/11")


def test_comware_lldp_neighbor_and_snapshot_link():
    neighbors = parse_neighbors(COMWARE_LLDP)
    assert neighbors == [{
        "local_port": "GigabitEthernet1/0/1",
        "system_name": "core-sw.example.local",
        "remote_port": "GigabitEthernet1/0/24",
        "port_description": "GigabitEthernet1/0/24 Interface",
        "management_ip": "192.0.2.1",
        "protocol": "LLDP",
    }]
    snapshot = build_snapshot(
        {"interfaces": COMWARE_BRIEF, "neighbors": COMWARE_LLDP, "ipv4_interfaces": "", "ipv6_interfaces": ""},
        "hpe_comware",
    )
    linked = next(item for item in snapshot["interfaces"] if item["name"] == "GigabitEthernet1/0/1")
    assert linked["neighbor"]["system_name"] == "core-sw.example.local"


def test_cx_and_aos_lldp_records():
    cx = parse_neighbors(CX_LLDP)
    assert [(item["local_port"], item["system_name"], item["remote_port"]) for item in cx] == [
        ("1/1/1", "access-01", "1/1/52"),
        ("1/1/2", "esx01", "vmnic0"),
    ]
    assert cx[0]["management_ip"] == "10.0.0.12"
    aos = parse_neighbors(AOS_LLDP)
    assert [(item["local_port"], item["system_name"], item["remote_port"]) for item in aos] == [
        ("24", "core-sw", "1/1/1"),
    ]


def test_ip_parsing_creates_virtual_interfaces():
    ipv4 = """
Interface              IP Address/Mask      VRF     Status
---------------------------------------------------------
vlan10                 10.0.10.1/24         default up
1/1/49                 10.255.0.1/31        default up
core-sw#
"""
    entries = parse_interface_ips(ipv4, "", "aruba_cx")
    assert {"interface": "vlan10", "address": "10.0.10.1/24"} in entries
    snapshot = build_snapshot({"interfaces": CX_BRIEF, "neighbors": "", "ipv4_interfaces": ipv4}, "aruba_cx")
    vlan = next(item for item in snapshot["interfaces"] if item["name"] == "vlan10")
    assert vlan["virtual"] and vlan["ip_addresses"] == ["10.0.10.1/24"]
    uplink = next(item for item in snapshot["interfaces"] if item["name"] == "1/1/49")
    assert uplink["ip_addresses"] == ["10.255.0.1/31"]


def test_prompts():
    assert CLI_PROMPT.search("output\nswitch# ")
    assert CLI_PROMPT.search("output\n<comware>")
    assert CLI_PROMPT.search("output\n[comware]")
    assert not CLI_PROMPT.search("LLDP neighbor-information of port 1[GigabitEthernet1/0/1]:\n")
    assert COMWARE_PROMPT.search("banner\n<HPE-5130>")
    assert not COMWARE_PROMPT.search("banner\ncx-switch# ")


def test_suggest_platform():
    assert suggest_platform("Aruba", "6100 48G CL4 4SFP+") == "aruba_cx"
    assert suggest_platform("HPE", "Aruba 2930F-48G") == "aruba_aos"
    assert suggest_platform("HPE", "FlexNetwork 5130 24G") == "hpe_comware"
    assert suggest_platform("ArubaOS-CX") == "aruba_cx"
    assert suggest_platform("Cisco", "C9300") == "auto"


def test_speed_parsing():
    assert switches.speed_mbps("1000FDx") == 1000
    assert switches.speed_mbps("10GigFD") == 10000
    assert switches.speed_mbps("UP   1G(a)   F(a)") == 1000
