"""NetBox Windows Agent protocol v1 against a small in-process agent (same rules as the C# agent)."""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from netbox_sync.core import agent_client
from netbox_sync.core.clients import apply_scan, new_row, scan_clients

KEY = "test-key-0123456789abcdef"
INVENTORY = {
    "ok": True, "source": "agent", "agent_version": "0.2.0", "name": "pc0815", "domain": "firma.local",
    "part_of_domain": True, "manufacturer": "LENOVO", "model": "21MW", "product_name": "21MW",
    "product_version": "ThinkBook 16 G7 ARP", "serial": "PW0K84A9", "bios_version": "N3BET", "chassis": [10],
    "user": "FIRMA\\anna", "memory": 16 * 1024 ** 3, "cpu": "AMD Ryzen 5", "cores": 6,
    "os": "Microsoft Windows 11 Pro", "os_version": "10.0.26100", "os_build": "26100", "os_display_version": "24H2",
    "os_architecture": "64-Bit", "installed": "2025-01-01T10:00:00", "last_boot": "2026-11-18T08:00:00",
    "nics": [{"name": "Ethernet", "description": "Realtek PCIe GbE", "mac": "84:BA:59:60:C8:B7",
              "ips": ["192.168.65.43", "fe80::1"], "masks": ["255.255.255.0", "64"], "gateway": ["192.168.65.1"],
              "dhcp": True, "physical": True, "speed": 1000000000},
             {"name": "", "description": "OpenVPN", "mac": "", "ips": ["10.8.0.2"], "masks": ["255.255.255.0"],
              "gateway": [], "dhcp": False, "physical": False, "speed": 0}],
    "disks": [{"device_id": "\\\\.\\PHYSICALDRIVE0", "model": "Samsung SSD", "serial": "S1", "size": 512110190592}],
    "memory_modules": [], "software": [{"name": "7-Zip", "version": "24.08", "publisher": "Igor Pavlov"}],
}


class _Handler(BaseHTTPRequestHandler):
    seen = set()

    def log_message(self, *_args):
        pass

    def _send(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path == "/v1/info":
            return self._send(200, {"agent": "netbox-windows-agent", "version": "0.2.0", "protocol": 1})
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        nonce, ts = query.get("nonce", ""), query.get("ts", "0")
        if abs(time.time() - int(ts)) > 300:
            return self._send(401, {"error": "clock skew too large"})
        expected = hmac.new(agent_client.derive_key(KEY, "netbox-agent auth v1"),
                            f"GET\n/v1/inventory\n{nonce}\n{ts}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Agent-Signature", "")):
            return self._send(401, {"error": "wrong key"})
        if nonce in self.seen:
            return self._send(401, {"error": "nonce reused"})
        self.seen.add(nonce)
        iv = os.urandom(12)
        data = AESGCM(agent_client.derive_key(KEY, "netbox-agent enc v1")).encrypt(
            iv, json.dumps(INVENTORY).encode(), nonce.encode())
        self._send(200, {"protocol": 1, "iv": base64.b64encode(iv).decode(), "data": base64.b64encode(data).decode()})


@pytest.fixture
def agent():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def test_info_and_encrypted_inventory(agent):
    assert agent_client.info("127.0.0.1", agent)["version"] == "0.2.0"
    raw = agent_client.query("127.0.0.1", KEY, agent)
    assert raw["ok"] and raw["host"] == "127.0.0.1" and raw["serial"] == "PW0K84A9"


def test_wrong_key_is_explained(agent):
    raw = agent_client.query("127.0.0.1", "another-key-0123456789", agent)
    assert raw == {"host": "127.0.0.1", "ok": False, "error": "Agent-Schlüssel stimmt nicht"}


def test_unreachable_agent_is_reported_not_raised():
    raw = agent_client.query("127.0.0.1", KEY, 1, timeout=1)
    assert not raw["ok"] and raw["error"].startswith("Agent nicht erreichbar")


def test_agent_inventory_runs_through_the_client_scan(agent):
    rows = scan_clients([new_row("127.0.0.1")], query=lambda host: agent_client.query(host, KEY, agent),
                        port=agent, service="Agent")
    row = rows[0]
    assert row["status"] == "ok" and row["name"] == "PC0815" and row["source"] == "agent"
    # Lenovo: marketing name from the product version, VPN adapter (not physical) left out
    assert row["model"] == "ThinkBook 16 G7 ARP" and row["machine_type"] == "21MW"
    assert [(item["name"], item["ips"]) for item in row["interfaces"]] == [("Ethernet", ["192.168.65.43/24"])]
    assert row["software_count"] == 1 and row["disks"][0]["model"] == "Samsung SSD"


def test_offline_host_names_the_agent_port():
    row = scan_clients([new_row("127.0.0.1")], query=lambda host: {}, port=1, service="Agent")[0]
    assert row["status"] == "offline" and "Agent-Port 1" in row["message"]


def test_generated_configuration_is_valid_for_the_agent():
    key = agent_client.new_scan_key()
    config = agent_client.agent_config(key, 8731, ["10.0.0.0/24"])
    assert len(key) >= 16 and config["mode"] == "listen" and config["scan_key"] == key
    assert config["allowed_networks"] == ["10.0.0.0/24"] and config["listen_port"] == 8731
    assert apply_scan(new_row("x"), dict(INVENTORY, host="x"))["os_display_version"] == "24H2"
