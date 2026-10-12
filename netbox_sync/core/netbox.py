"""Small helpers around pynetbox that both sync engines share."""

import re

import pynetbox
import requests
import urllib3


class NetBoxError(RuntimeError):
    pass


class _TimeoutSession(requests.Session):
    """pynetbox issues requests without a timeout; never hang the UI forever."""

    def __init__(self, timeout):
        super().__init__()
        self.default_timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(method, url, **kwargs)


def connect(url, token, ignore_ssl=False, timeout=60):
    """Create a pynetbox API object and verify that URL and token work."""
    url = (url or "").strip().rstrip("/")
    if not url or not token:
        raise NetBoxError("Bitte NetBox-URL und API-Token im Bereich „Verbindungen“ eintragen.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    nb = pynetbox.api(url, token=token)
    session = _TimeoutSession(timeout)
    session.verify = not ignore_ssl
    if ignore_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    nb.http_session = session
    try:
        status = nb.status()
    except requests.exceptions.SSLError as error:
        raise NetBoxError(
            "TLS-Zertifikat von NetBox ist nicht vertrauenswürdig. "
            "Bei selbstsignierten Zertifikaten „Zertifikatsprüfung deaktivieren“ aktivieren."
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise NetBoxError(f"NetBox unter {url} ist nicht erreichbar.") from error
    except pynetbox.RequestError as error:
        if getattr(error.req, "status_code", None) in (401, 403):
            raise NetBoxError("NetBox hat den API-Token abgelehnt (fehlende Berechtigung oder ungültig).") from error
        raise NetBoxError(f"NetBox-Anfrage fehlgeschlagen: {error}") from error
    nb.netbox_version = str(status.get("netbox-version", "") or nb.version or "")
    return nb


def version_tuple(nb):
    text = getattr(nb, "netbox_version", "") or getattr(nb, "version", "") or ""
    numbers = re.findall(r"\d+", text)
    return tuple(int(value) for value in numbers[:2]) or (0, 0)


def uses_mac_objects(nb):
    """NetBox 4.2 moved MAC addresses into their own model."""
    return version_tuple(nb) >= (4, 2)


def related_id(record, attribute):
    value = getattr(record, attribute, None)
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    if isinstance(value, int):
        return value
    return getattr(value, "id", None)


def related_name(record, attribute):
    value = getattr(record, attribute, None)
    if value is None:
        return ""
    if isinstance(value, dict):
        return value.get("name") or value.get("display") or ""
    return getattr(value, "name", None) or getattr(value, "display", None) or ""


def choice_value(record, attribute):
    value = getattr(record, attribute, None)
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("value")
    return getattr(value, "value", value)


def normalize_mac(value):
    if not value:
        return None
    compact = re.sub(r"[^0-9a-fA-F]", "", str(value)).upper()
    if len(compact) != 12:
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def assign_mac(nb, interface, mac, object_type):
    """Attach a MAC address to an interface for old and new NetBox versions."""
    mac = normalize_mac(mac)
    if not mac:
        return False
    if not uses_mac_objects(nb):
        current = normalize_mac(getattr(interface, "mac_address", None))
        if current == mac:
            return False
        interface.update({"mac_address": mac})
        return True

    matches = list(nb.dcim.mac_addresses.filter(
        mac_address=mac, assigned_object_type=object_type, assigned_object_id=interface.id,
    ))
    record = matches[0] if matches else nb.dcim.mac_addresses.create({
        "mac_address": mac,
        "assigned_object_type": object_type,
        "assigned_object_id": interface.id,
    })
    if related_id(interface, "primary_mac_address") != record.id:
        interface.update({"primary_mac_address": record.id})
        return True
    return not matches
