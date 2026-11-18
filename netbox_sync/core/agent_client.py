"""Query the NetBox Windows Agent in "listen" mode (protocol v1).

The agent answers ``GET /v1/info`` without authentication. The inventory at
``GET /v1/inventory`` needs a signature (HMAC-SHA256 with a key derived from
the shared scan key) over a fresh nonce and the current time, and comes back
encrypted with AES-256-GCM, so serial numbers and software lists never cross
the network in clear text even without TLS certificates.

The decrypted inventory has the same shape as the WinRM scan
(:func:`netbox_sync.core.wsman.query_client`), so it runs through the same
table and NetBox transfer as the agentless client scan.
"""

import base64
import hashlib
import hmac
import http.client
import json
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_PORT = 8731
PROTOCOL = 1
AGENT_NAME = "netbox-windows-agent"


class AgentError(Exception):
    """The host answered, but not as an agent we can read (wrong key, other service …)."""


def derive_key(secret, purpose):
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).digest()


def new_scan_key():
    """Random shared key for the agents of one customer (URL-safe, 43 characters)."""
    return secrets.token_urlsafe(32)


def _get(host, port, path, headers=None, timeout=5.0):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read(16 * 1024 * 1024)
        return response.status, body
    finally:
        connection.close()


def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AgentError("keine gültige Antwort (kein NetBox-Agent?)") from error


def info(host, port=DEFAULT_PORT, timeout=3.0):
    status, body = _get(host, port, "/v1/info", timeout=timeout)
    data = _json(body)
    if status != 200 or data.get("agent") != AGENT_NAME:
        raise AgentError("kein NetBox-Agent auf diesem Port")
    return data


def inventory(host, key, port=DEFAULT_PORT, timeout=60.0):
    """Decrypted inventory dict of one agent."""
    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    signature = hmac.new(derive_key(key, "netbox-agent auth v1"), f"GET\n/v1/inventory\n{nonce}\n{ts}".encode(),
                         hashlib.sha256).hexdigest()
    status, body = _get(host, port, f"/v1/inventory?nonce={nonce}&ts={ts}", {"X-Agent-Signature": signature},
                        timeout)
    data = _json(body)
    if status != 200:
        reason = data.get("error", f"HTTP {status}")
        raise AgentError({
            "wrong key": "Agent-Schlüssel stimmt nicht",
            "clock skew too large": "Uhrzeit von Client und diesem Rechner weicht mehr als 5 Minuten ab",
            "address not allowed": "dieser Rechner ist in allowed_networks des Agenten nicht freigegeben",
        }.get(reason, f"Agent meldet: {reason}"))
    try:
        payload = AESGCM(derive_key(key, "netbox-agent enc v1")).decrypt(
            base64.b64decode(data["iv"]), base64.b64decode(data["data"]), nonce.encode())
    except Exception as error:  # noqa: BLE001 - InvalidTag, KeyError, binascii.Error
        raise AgentError("Antwort lässt sich nicht entschlüsseln (falscher Schlüssel?)") from error
    return json.loads(payload.decode("utf-8"))


def query(host, key, port=DEFAULT_PORT, timeout=60.0):
    """Raw result for :func:`netbox_sync.core.clients.apply_scan` – never raises."""
    try:
        result = inventory(host, key, port, timeout)
    except AgentError as error:
        return {"host": host, "ok": False, "error": str(error)}
    except (OSError, http.client.HTTPException) as error:
        return {"host": host, "ok": False, "error": f"Agent nicht erreichbar: {error}"}
    result.update(host=host, ok=True)
    return result


def agent_config(key, port=DEFAULT_PORT, allowed_networks=(), collect_software=True):
    """``config.json`` for agents in listen mode (no NetBox access needed on the clients)."""
    return {
        "mode": "listen",
        "listen_port": port,
        "scan_key": key,
        "allowed_networks": list(allowed_networks),
        "inventory_cache_seconds": 300,
        "collect_software": collect_software,
        "max_software_entries": 250,
    }
