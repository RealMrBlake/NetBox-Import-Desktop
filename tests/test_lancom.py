from netbox_sync.core import lancom
from netbox_sync.core.switches import build_snapshot, netbox_interface_type, suggest_platform

SYSTEM = """
Model Name                   : LANCOM GS-2326P+
Location                     : Keller
Device Name                  : SW-08
Firmware Version             : v3.34.0205RU4
Serial Number                : 4006846720000000
Host MAC Address             : 00-a0-57-00-00-01
SW-08(system)#"""

STATUS = """
Port/Media  State    Link           Auto Nego  Speed Duplex  Rx Pause  Tx Pause
----------  -------- -------------- ---------  ------------  --------  --------
1/UTP       Enabled  Down           Enabled    Down          Disabled  Disabled
2/UTP       Enabled  Up             Enabled    1G Full       Disabled  Disabled
3/UTP       Disabled Down           Enabled    Down          Disabled  Disabled
4/UTP       Enabled  Up             Enabled    100 Full      Disabled  Disabled
25/SFP      Enabled  Up             Enabled    1G Full       Disabled  Disabled
SW-08(port)#"""

PORT_CONFIG = """
Port  State    Speed Duplex  Flow Control  Max. Frame  Excessive  Power
Description
--------------------------------------------------------------------------------
1     Enabled  Auto          Disabled      9600        Discard    Disabled
Uplink
--------------------------------------------------------------------------------
2     Enabled  Auto          Disabled      9600        Discard    Disabled
AP005
--------------------------------------------------------------------------------
3     Disabled Auto          Disabled      9600        Discard    Disabled

--------------------------------------------------------------------------------
4     Enabled  Auto          Disabled      9600        Discard    Disabled

--------------------------------------------------------------------------------
25    Enabled  SFP_Auto_AMS  Disabled      9600        Discard    Disabled

SW-08(port)#"""

VLANS = """
VID   VLAN Name                         User        Ports
----  --------------------------------  ----------  -----------------
36    LAN                               Static      1-4,25
136   MOBILE                            Static      1-2
236   GUEST                             Static      1,3
SW-08(vlan)#"""

VLAN_PORTS = """
Port  PVID  Frame Type  Ingress Filter  Egress Rule  Port Type
----  ----  ----------  --------------  -----------  -------------
1     1     All         Disabled        Trunk        C-Port
2     36    All         Disabled        Hybrid       C-Port
3     36    All         Disabled        Hybrid       C-Port
4     36    All         Disabled        Access       C-Port
25    1     All         Disabled        Hybrid       C-Port
SW-08(vlan)#"""

IP = """
                       Configured      Current
                       --------------- ---------------
DHCP Client          : Disabled
IP Address           : 10.0.0.8        10.0.0.8
IP Mask              : 255.255.255.0   255.255.255.0
VLAN ID              : 36              36
SW-08(ip)#"""


def outputs():
    return {"system": SYSTEM, "status": STATUS, "port_config": PORT_CONFIG, "vlans": VLANS,
            "vlan_ports": VLAN_PORTS, "ip": IP, "neighbors": ""}


def test_snapshot():
    snapshot = build_snapshot(outputs(), "lancom_sx3")
    ports = {item["name"]: item for item in snapshot["interfaces"]}
    assert ports["1"]["description"] == "Uplink" and not ports["1"]["connected"]
    assert ports["2"]["connected"] and ports["2"]["speed_mbps"] == 1000
    assert ports["4"]["speed_mbps"] == 100
    assert ports["3"]["enabled"] is False
    assert ports["1"]["vlans"] == {"untagged": [], "tagged": [36, 136, 236], "tagged_all": False}
    assert ports["2"]["vlans"] == {"untagged": [36], "tagged": [136], "tagged_all": False}
    assert ports["4"]["vlans"]["untagged"] == [36]
    assert ports["25"]["vlans"] == {"untagged": [], "tagged": [36], "tagged_all": False}
    assert netbox_interface_type(ports["25"]) == "1000base-x-sfp"
    assert netbox_interface_type(ports["1"]) == "1000base-t"
    assert ports["vlan36"]["ip_addresses"] == ["10.0.0.8/24"]
    assert snapshot["vlan_names"] == {36: "LAN", 136: "MOBILE", 236: "GUEST"}
    assert snapshot["system"]["model"] == "LANCOM GS-2326P+"
    assert "show port-config" in snapshot["config_backup"]


def test_detection_helpers():
    assert lancom.is_lancom(SYSTEM)
    assert suggest_platform("LANCOM Systems", "GS-2326P+") == "lancom_sx3"


class FakeChannel:
    """Answers like the mode-based CLI and records what was sent."""

    def __init__(self):
        self.sent, self.mode, self.buffer = [], "", ""

    def send(self, data):
        if not data.endswith("\r"):
            self.buffer += data  # like the real CLI: "\n" is only echoed, nothing is executed
            return
        command = data.strip()
        self.sent.append(command)
        answers = {("system", "show"): SYSTEM, ("port", "show status"): STATUS,
                   ("port", "show configuration"): PORT_CONFIG, ("vlan", "show vlan"): VLANS,
                   ("vlan", "show port-config"): VLAN_PORTS, ("ip", "show"): IP}
        if command == "exit":
            self.mode = ""
            output = ""
        elif not self.mode and command in ("system", "port", "vlan", "ip", "lldp"):
            self.mode = command
            output = ""
        else:
            output = answers.get((self.mode, command), "% Unrecognized command")
        self.buffer += output + f"\nSW-08{'(' + self.mode + ')' if self.mode else ''}# "

    def recv_ready(self):
        return bool(self.buffer)

    def recv(self, _size):
        data, self.buffer = self.buffer, ""
        return data.encode()


def test_run_commands_switches_modes():
    channel = FakeChannel()
    result = lancom.run_commands(channel)
    assert result["vlans"].strip().startswith("VID") and result["neighbors"] == ""
    assert channel.sent[:3] == ["system", "show", "exit"]
    assert channel.mode == ""
    assert "show info 1-4,25" in channel.sent


LLDP_CONFIG = """
Interval    : 30
              Port        System   System      System      Management CDP
Port Mode     Description Name     Description Capability  Address    awareness
---- -------- ----------- -------- ----------- ----------- ---------- ---------
1    Disabled Enabled     Enabled  Enabled     Enabled     Enabled    Enabled
2    Disabled Enabled     Enabled  Enabled     Enabled     Enabled    Enabled
SW-08(lldp)#"""


def test_lldp_disabled_is_reported():
    snapshot = build_snapshot(dict(outputs(), lldp_config=LLDP_CONFIG, neighbors="No LLDP entries found"),
                              "lancom_sx3")
    assert snapshot["lldp_disabled"] is True and snapshot["neighbors"] == []
    enabled = LLDP_CONFIG.replace("1    Disabled", "1    Enabled ")
    assert build_snapshot(dict(outputs(), lldp_config=enabled), "lancom_sx3")["lldp_disabled"] is False


def test_enable_lldp_on_disabled_ports():
    channel = FakeChannel()
    state = {"config": LLDP_CONFIG}
    original = channel.send

    def send(data):
        command = data.strip()
        if channel.mode == "lldp" and command == "show config":
            channel.sent.append(command)
            channel.buffer += state["config"] + "\nSW-08(lldp)# "
        elif channel.mode == "lldp" and command.startswith("mode "):
            channel.sent.append(command)
            state["config"] = LLDP_CONFIG.replace("Disabled Enabled", "Enabled  Enabled")
            channel.buffer += "\nSW-08(lldp)# "
        else:
            original(data)

    channel.send = send
    result = lancom.run_commands(channel, enable_lldp=True, wait=0)
    assert "mode 1-2 enable" in channel.sent
    assert result["lldp_enabled_automatically"] is True
    snapshot = build_snapshot(dict(outputs(), **result), "lancom_sx3")
    assert snapshot["lldp_enabled_automatically"] and not snapshot["lldp_disabled"]
    assert all(isinstance(value, str) for value in snapshot["raw"].values())


def test_auto_detection_falls_back_to_carriage_return(monkeypatch):
    from netbox_sync.core import switches

    real = switches._read_available

    def quick(channel, timeout=20, initialize_session=False):
        return real(channel, timeout=min(timeout, 0.3), initialize_session=initialize_session)

    monkeypatch.setattr(switches, "_read_available", quick)
    channel = FakeChannel()
    assert switches.detect_platform(channel, "SW-08# ") == "lancom_sx3"
    assert channel.mode == ""
