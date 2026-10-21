"""JSON exchange files for working without a NetBox connection.

VMs, Windows clients and switch discovery results are collected where the hypervisors and
switches are reachable, exported to a file and imported later on a machine
that can reach NetBox. Credentials are never written to the file.
"""

import json
import time
from pathlib import Path

from netbox_sync import APP_VERSION

FORMAT = "netbox-sync-export"
FORMAT_VERSION = 1
FILE_SUFFIX = ".nbsync.json"

# Switch fields that describe the device; everything else (credentials) stays local.
SWITCH_FIELDS = ("id", "name", "device_id", "platform", "host", "port")
VM_RUNTIME_FIELDS = ("id", "source_id")


class ExchangeError(ValueError):
    """The file is not a valid exchange file."""


def build_export(vms=(), switches=(), profile_name="", clients=()):
    """``switches`` is an iterable of ``(switch config, snapshot)`` pairs."""
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool_version": APP_VERSION,
        "profile": profile_name,
        "vms": [{key: value for key, value in vm.items() if key not in VM_RUNTIME_FIELDS} for vm in vms],
        "clients": [dict(client) for client in clients],
        "switches": [
            {"switch": {key: switch[key] for key in SWITCH_FIELDS if key in switch}, "snapshot": snapshot}
            for switch, snapshot in switches
        ],
    }


def write_export(path, data):
    path = Path(path)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def read_export(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except json.JSONDecodeError as error:
        raise ExchangeError(f"Keine gültige JSON-Datei: {error}") from error
    return validate(data)


def validate(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ExchangeError("Die Datei ist keine Exportdatei des NetBox Synchronizers.")
    version = data.get("version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ExchangeError(f"Exportformat Version {version} wird von dieser Programmversion nicht unterstützt. "
                            "Bitte den NetBox Synchronizer aktualisieren.")
    vms = data.get("vms") or []
    switches = data.get("switches") or []
    clients = data.setdefault("clients", [])
    if not isinstance(vms, list) or not isinstance(switches, list) or not isinstance(clients, list):
        raise ExchangeError("Die Datei ist beschädigt (Listen erwartet).")
    for vm in vms:
        if not isinstance(vm, dict) or not vm.get("name") or not vm.get("source"):
            raise ExchangeError("Die Datei enthält eine VM ohne Name oder Quelle.")
        vm.setdefault("interfaces", [])
        vm.setdefault("cluster", "")
        vm.setdefault("status", "offline")
        for key in ("vcpus", "memory", "disk"):
            vm[key] = int(vm.get(key) or 0)
    for client in clients:
        if not isinstance(client, dict) or not client.get("name"):
            raise ExchangeError("Die Datei enthält einen Client ohne Namen.")
        client.setdefault("host", client["name"])
        client["id"] = str(client["name"]).casefold()
        client.setdefault("status", "ok")
        client.setdefault("interfaces", [])
    for item in switches:
        switch = item.get("switch") if isinstance(item, dict) else None
        snapshot = item.get("snapshot") if isinstance(item, dict) else None
        if not isinstance(switch, dict) or not switch.get("name") or not isinstance(snapshot, dict) \
                or not isinstance(snapshot.get("interfaces"), list):
            raise ExchangeError("Die Datei enthält einen Switch ohne Name oder Ausleseergebnis.")
    return data


def prepare_vms(vms, prefix="file"):
    """Give imported VMs the runtime IDs the UI expects (``id``, ``source_id``)."""
    result = []
    for index, vm in enumerate(vms):
        item = dict(vm)
        item["source_id"] = f"{prefix}:{item['source']}"
        item["id"] = f"{item['source_id']}:{index}:{item['name']}"
        result.append(item)
    return result


def merge_switches(profile_switches, items, new_id):
    """Match imported switches to profile entries by NetBox device ID or name.

    Unknown switches are appended without credentials. Returns
    ``(snapshots by profile switch id, number of switches added)``.
    """
    by_device = {switch.get("device_id"): switch for switch in profile_switches if switch.get("device_id")}
    by_name = {switch.get("name", "").casefold(): switch for switch in profile_switches}
    snapshots, added = {}, 0
    for item in items:
        imported = item["switch"]
        target = by_device.get(imported.get("device_id")) if imported.get("device_id") else None
        target = target or by_name.get(imported["name"].casefold())
        if target is None:
            target = {
                "id": new_id(),
                "name": imported["name"],
                "device_id": imported.get("device_id"),
                "platform": imported.get("platform", "auto"),
                "host": imported.get("host", ""),
                "port": int(imported.get("port", 22)),
                "username": "",
                "password": "",
                "enabled": False,
            }
            profile_switches.append(target)
            by_name[target["name"].casefold()] = target
            added += 1
        snapshots[target["id"]] = item["snapshot"]
    return snapshots, added
