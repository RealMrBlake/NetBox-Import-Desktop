"""Tiny in-memory stand-in for the parts of pynetbox the sync code uses."""

import itertools
from types import SimpleNamespace

_ids = itertools.count(1000)


class Record(SimpleNamespace):
    def __init__(self, endpoint=None, **values):
        super().__init__(**values)
        self._endpoint = endpoint

    def update(self, values):
        changed = any(getattr(self, key, None) != value for key, value in values.items())
        for key, value in values.items():
            setattr(self, key, value)
        if changed and self._endpoint is not None:
            self._endpoint.updates.append((self.id, dict(values)))
        return changed


def _matches(record, key, value):
    if key == "brief":
        return True
    if key.endswith("__ie"):
        return str(getattr(record, key[:-4], "")).casefold() == str(value).casefold()
    if key == "vrf_id":
        vrf = getattr(record, "vrf", None)
        vrf_id = vrf if isinstance(vrf, int) else getattr(vrf, "id", None)
        return (vrf_id is None) if value == "null" else vrf_id == value
    if key == "address":
        return str(getattr(record, "address", "")).split("/")[0] == str(value).split("/")[0]
    if key.endswith("_id") and not hasattr(record, key):
        related = getattr(record, key[:-3], None)
        return getattr(related, "id", related) == value
    return getattr(record, key, None) == value


class Endpoint:
    def __init__(self, records=()):
        self.records = []
        self.created = []
        self.updates = []
        for values in records:
            self.records.append(Record(self, **values))

    def all(self):
        return list(self.records)

    def filter(self, **kwargs):
        return [record for record in self.records if all(_matches(record, k, v) for k, v in kwargs.items())]

    def get(self, *args, **kwargs):
        if args:
            kwargs = {"id": args[0]}
        found = self.filter(**kwargs)
        if len(found) > 1:
            raise ValueError("get() returned more than one result")
        return found[0] if found else None

    def create(self, payload):
        record = Record(self, id=next(_ids), cable=None, **payload)
        self.records.append(record)
        self.created.append(payload)
        return record


class FakeNetBox:
    netbox_version = "4.3.0"

    def __init__(self):
        self.dcim = SimpleNamespace(devices=Endpoint(), interfaces=Endpoint(), cables=Endpoint(),
                                    mac_addresses=Endpoint(), platforms=Endpoint(),
                                    manufacturers=Endpoint(), device_types=Endpoint(), device_roles=Endpoint(),
                                    sites=Endpoint())
        self.ipam = SimpleNamespace(ip_addresses=Endpoint(), prefixes=Endpoint(), vrfs=Endpoint())
        self.virtualization = SimpleNamespace(clusters=Endpoint(), cluster_types=Endpoint(), virtual_machines=Endpoint(),
                                            interfaces=Endpoint())
        self.tenancy = SimpleNamespace(tenants=Endpoint())
