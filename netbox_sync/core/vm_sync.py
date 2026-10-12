"""Import virtual machines, interfaces, IP addresses and prefixes into NetBox."""

import ipaddress

from netbox_sync.core.netbox import assign_mac, related_id, related_name
from netbox_sync.core.virtualization import ip_with_prefix, valid_guest_ip

GLOBAL_VRF = None  # NetBox "global" routing table


def _noop(*_args, **_kwargs):
    return None


def vrf_label(vrf):
    rd = getattr(vrf, "rd", None)
    return f"{vrf.name} (RD: {rd})" if rd else vrf.name


def suggested_prefixlen(interface):
    """IPv4 defaults to /24 because guests often report host masks; IPv6 keeps its prefix."""
    return 24 if interface.version == 4 else interface.network.prefixlen


def prefix_choices(host_address, detected_prefixlen):
    host = ipaddress.ip_address(host_address)
    if host.version == 4:
        return list(range(8, 31))
    return sorted({32, 40, 48, 52, 56, 60, 64, 80, 96, 112, 120, 124, 126, 127, 128, detected_prefixlen})


class VMSyncEngine:
    """Holds NetBox lookup data and performs the VM import.

    The engine is used from worker threads. It never touches UI objects; all
    feedback goes through the ``log`` callback ``log(text, level="info")``.
    """

    def __init__(self, nb, log=_noop):
        self.nb = nb
        self.log = log
        self.clusters = {}
        self.cluster_tenants = {}
        self.tenants = {}
        self.vrfs = []
        self.platforms = []
        self.existing_vms = {}
        self._reset_run_state()

    def _reset_run_state(self):
        self.synced_prefixes = set()
        self.prefix_plans = []
        self.preexisting_prefixes = []

    # ------------------------------------------------------------------
    # NetBox lookup data
    # ------------------------------------------------------------------

    def load_targets(self):
        self.log("Lade Cluster, Mandanten, VRFs und Plattformen aus NetBox …")
        clusters = list(self.nb.virtualization.clusters.all())
        self.clusters = {item.name: item.id for item in clusters}
        self.tenants = {item.name: item.id for item in self.nb.tenancy.tenants.all()}
        self.vrfs = list(self.nb.ipam.vrfs.all())
        self.platforms = list(self.nb.dcim.platforms.all())
        self.cluster_tenants = {}
        for cluster in clusters:
            tenant_id = related_id(cluster, "tenant")
            tenant_name = related_name(cluster, "tenant")
            if tenant_id and tenant_name:
                self.cluster_tenants[cluster.name] = {"id": tenant_id, "name": tenant_name}
                self.tenants.setdefault(tenant_name, tenant_id)
        self.existing_vms = {}
        for vm in self.nb.virtualization.virtual_machines.filter(brief=True):
            self.existing_vms.setdefault(vm.name, []).append(vm.id)
        self.log(
            f"NetBox: {len(self.clusters)} Cluster, {len(self.tenants)} Mandanten, "
            f"{len(self.vrfs)} VRFs, {sum(map(len, self.existing_vms.values()))} VMs"
        )

    def tenant_vrfs(self, tenant_id):
        return sorted(
            (vrf for vrf in self.vrfs if related_id(vrf, "tenant") == tenant_id),
            key=lambda item: item.name.casefold(),
        )

    def vrf_name(self, vrf_id):
        if vrf_id is None:
            return "Global"
        return next((vrf.name for vrf in self.vrfs if vrf.id == vrf_id), f"VRF #{vrf_id}")

    def get_or_create_tenant_vrf(self, vrf_name, tenant_id):
        existing = next(
            (vrf for vrf in self.vrfs if vrf.name == vrf_name and related_id(vrf, "tenant") == tenant_id),
            None,
        )
        if existing:
            self.log(f"Vorhandene VRF verwendet: {vrf_name}")
            return existing.id
        vrf = self.nb.ipam.vrfs.create({
            "name": vrf_name,
            "tenant": tenant_id,
            "enforce_unique": True,
            "description": "Automatisch erstellt durch NetBox Synchronizer",
        })
        self.vrfs.append(vrf)
        self.log(f"VRF erstellt: {vrf_name}", "success")
        return vrf.id

    def get_platform_id(self, guest_os):
        guest_os = (guest_os or "").lower()
        if not guest_os:
            return None
        for platform in self.platforms:
            name = platform.name.lower()
            if name in guest_os or guest_os in name:
                return platform.id
        return None

    # ------------------------------------------------------------------
    # Prefix lookups
    # ------------------------------------------------------------------

    def find_prefixes_in_vrf(self, network, vrf_id):
        return [
            prefix for prefix in self.nb.ipam.prefixes.filter(prefix=str(network))
            if related_id(prefix, "vrf") == vrf_id
        ]

    def find_covering_prefixes(self, host_address, allowed_vrf_ids):
        host = ipaddress.ip_address(host_address)
        try:
            candidates = list(self.nb.ipam.prefixes.filter(contains=str(host)))
        except Exception as error:  # noqa: BLE001
            self.log(f"Präfixsuche für {host} nicht möglich: {error}", "warning")
            return []
        matches = []
        for prefix in candidates:
            prefix_vrf_id = related_id(prefix, "vrf")
            if prefix_vrf_id not in allowed_vrf_ids:
                continue
            try:
                parsed = ipaddress.ip_network(str(prefix.prefix))
            except ValueError:
                continue
            if host in parsed:
                matches.append((parsed.prefixlen, prefix, str(parsed), prefix_vrf_id))
        return matches

    def _allowed_vrfs(self, tenant_id, default_vrf_id):
        return {vrf.id for vrf in self.tenant_vrfs(tenant_id)} | {default_vrf_id}

    @staticmethod
    def _reusable_prefix(covering, default_vrf_id):
        default_matches = [match for match in covering if match[3] == default_vrf_id]
        if default_matches:
            return max(default_matches, key=lambda match: match[0])
        if len({match[3] for match in covering}) == 1:
            return max(covering, key=lambda match: match[0])
        return None

    # ------------------------------------------------------------------
    # Import plan
    # ------------------------------------------------------------------

    def detect_missing_networks(self, vms, tenant_id, default_vrf_id):
        """Return networks of the selected VMs that NetBox does not know yet."""
        self._reset_run_state()
        detected = {}
        for vm in vms:
            for interface in vm.get("interfaces", []):
                for ip_value in dict.fromkeys(interface.get("ips", [])):
                    address = ip_with_prefix(ip_value) if valid_guest_ip(ip_value) else None
                    if not address:
                        continue
                    parsed = ipaddress.ip_interface(address)
                    length = suggested_prefixlen(parsed)
                    network = ipaddress.ip_network(f"{parsed.ip}/{length}", strict=False)
                    entry = detected.setdefault(str(network), {
                        "host_address": parsed.ip,
                        "host_addresses": set(),
                        "detected_prefixlen": parsed.network.prefixlen,
                        "suggested_prefixlen": length,
                        "suggested_network": network,
                        "vms": set(),
                    })
                    entry["host_addresses"].add(parsed.ip)
                    entry["vms"].add(vm["name"])

        allowed = self._allowed_vrfs(tenant_id, default_vrf_id)
        missing = []
        for item in detected.values():
            group_is_missing = False
            for host in item["host_addresses"]:
                reusable = self._reusable_prefix(self.find_covering_prefixes(host, allowed), default_vrf_id)
                if reusable:
                    cached = reusable[1:]
                    if cached not in self.preexisting_prefixes:
                        self.preexisting_prefixes.append(cached)
                else:
                    group_is_missing = True
            if group_is_missing:
                missing.append(item)
        self.log(
            f"Netzwerkprüfung: {len(detected)} Netz(e) erkannt, {len(missing)} davon fehlen in NetBox"
        )
        return missing

    def apply_network_plan(self, decisions, tenant_id):
        """Create VRFs requested in the plan and remember the confirmed networks."""
        for decision in decisions:
            if decision.get("create_vrf"):
                decision["vrf_id"] = self.get_or_create_tenant_vrf(decision["vrf_name"], tenant_id)
                decision["create_vrf"] = False
        self.prefix_plans = list(decisions)

    def classify_vms(self, vms):
        """Return how many selected VMs are new or already exist in NetBox."""
        new = sum(1 for vm in vms if vm["name"] not in self.existing_vms)
        return new, len(vms) - new

    # ------------------------------------------------------------------
    # VM synchronization
    # ------------------------------------------------------------------

    def _find_vm(self, name, cluster_id):
        in_cluster = list(self.nb.virtualization.virtual_machines.filter(name=name, cluster_id=cluster_id))
        if in_cluster:
            return in_cluster[0]
        anywhere = list(self.nb.virtualization.virtual_machines.filter(name=name))
        if len(anywhere) == 1:
            self.log(f"{name}: in anderem Cluster gefunden – wird in den Zielcluster verschoben", "warning")
            return anywhere[0]
        if len(anywhere) > 1:
            raise RuntimeError("mehrere gleichnamige VMs in anderen Clustern – Zuordnung nicht eindeutig")
        return None

    def _find_ip(self, host_address, vrf_id):
        candidates = list(self.nb.ipam.ip_addresses.filter(
            address=host_address, vrf_id=vrf_id if vrf_id is not None else "null",
        ))
        if len(candidates) > 1:
            raise RuntimeError(f"IP-Adresse {host_address} ist in dieser VRF mehrfach vorhanden")
        return candidates[0] if candidates else None

    def sync_vm(self, source_vm, cluster_id, tenant_id, vrf_id):
        """Create or update one VM. Returns ``"created"`` or ``"updated"``."""
        payload = {
            "name": source_vm["name"], "cluster": cluster_id, "status": source_vm["status"],
            "vcpus": source_vm["vcpus"], "memory": source_vm["memory"], "disk": source_vm["disk"],
            "tenant": tenant_id,
        }
        platform_id = self.get_platform_id(source_vm.get("guest_os"))
        if platform_id:
            payload["platform"] = platform_id

        netbox_vm = self._find_vm(source_vm["name"], cluster_id)
        if netbox_vm:
            netbox_vm.update(payload)
            outcome = "updated"
            self.log(f"VM aktualisiert: {source_vm['name']}")
        else:
            netbox_vm = self.nb.virtualization.virtual_machines.create(payload)
            outcome = "created"
            self.existing_vms.setdefault(source_vm["name"], []).append(netbox_vm.id)
            self.log(f"VM erstellt: {source_vm['name']}", "success")

        primary_ipv4 = None
        primary_ipv6 = None
        for interface in source_vm.get("interfaces", []):
            netbox_interface = self.nb.virtualization.interfaces.get(
                virtual_machine_id=netbox_vm.id, name=interface["name"]
            )
            interface_payload = {"virtual_machine": netbox_vm.id, "name": interface["name"], "enabled": True}
            if netbox_interface:
                netbox_interface.update(interface_payload)
            else:
                netbox_interface = self.nb.virtualization.interfaces.create(interface_payload)
            try:
                assign_mac(self.nb, netbox_interface, interface.get("mac"), "virtualization.vminterface")
            except Exception as error:  # noqa: BLE001 - MAC is optional metadata
                self.log(f"{source_vm['name']}/{interface['name']}: MAC-Adresse nicht gesetzt ({error})", "warning")

            for ip_value in dict.fromkeys(interface.get("ips", [])):
                address = ip_with_prefix(ip_value) if valid_guest_ip(ip_value) else None
                if not address:
                    continue
                effective_vrf_id = self.sync_prefix_tenant(address, tenant_id, vrf_id)
                ip_payload = {
                    "address": address, "status": "active", "tenant": tenant_id, "vrf": effective_vrf_id,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": netbox_interface.id,
                }
                try:
                    host_address = str(ipaddress.ip_interface(address).ip)
                    ip_object = self._find_ip(host_address, effective_vrf_id)
                    if ip_object:
                        ip_object.update(ip_payload)
                    else:
                        ip_object = self.nb.ipam.ip_addresses.create(ip_payload)
                except Exception as error:
                    raise RuntimeError(f"IP-Adresse {address}: {error}") from error
                version = ipaddress.ip_interface(address).version
                if version == 4 and not primary_ipv4:
                    primary_ipv4 = ip_object.id
                elif version == 6 and not primary_ipv6:
                    primary_ipv6 = ip_object.id

        primary = {}
        if primary_ipv4:
            primary["primary_ip4"] = primary_ipv4
        if primary_ipv6:
            primary["primary_ip6"] = primary_ipv6
        if primary:
            netbox_vm.update(primary)
        return outcome

    def sync_prefix_tenant(self, address, tenant_id, default_vrf_id):
        """Assign the tenant to the matching prefix and apply the confirmed network plan."""
        parsed_interface = ipaddress.ip_interface(address)
        host_address = parsed_interface.ip
        network = str(parsed_interface.network)
        vrf_id = default_vrf_id

        for existing_prefix, existing_network, existing_vrf_id in self.preexisting_prefixes:
            if host_address in ipaddress.ip_network(existing_network):
                if (existing_network, existing_vrf_id) not in self.synced_prefixes:
                    existing_prefix.update({"tenant": tenant_id})
                    self.synced_prefixes.add((existing_network, existing_vrf_id))
                    self.log(f"Vorhandenes Netzwerk übernommen: {existing_network}")
                return existing_vrf_id

        for cached_network, cached_vrf in self.synced_prefixes:
            if host_address in ipaddress.ip_network(cached_network):
                return cached_vrf

        cache_key = (network, vrf_id)
        candidates = self.find_prefixes_in_vrf(network, vrf_id)
        if len(candidates) > 1:
            self.log(f"Präfix {network} nicht geändert: mehrere passende Präfixe gefunden", "warning")
            return vrf_id

        try:
            if candidates:
                candidates[0].update({"tenant": tenant_id})
                self.log(f"Mandant am Präfix aktualisiert: {network}")
            else:
                covering = self.find_covering_prefixes(host_address, self._allowed_vrfs(tenant_id, vrf_id))
                reusable = self._reusable_prefix(covering, vrf_id)
                if reusable:
                    _, existing_prefix, existing_network, existing_vrf_id = reusable
                    existing_prefix.update({"tenant": tenant_id})
                    self.synced_prefixes.add((existing_network, existing_vrf_id))
                    self.log(f"Vorhandenes Netzwerk übernommen: {existing_network}")
                    return existing_vrf_id
                matching_plans = [
                    decision for decision in self.prefix_plans
                    if host_address in ipaddress.ip_network(decision["network"])
                ]
                if not matching_plans:
                    self.log(f"Kein Präfix für {host_address} angelegt (nicht im Netzwerkplan)")
                    return vrf_id
                decision = max(matching_plans, key=lambda item: ipaddress.ip_network(item["network"]).prefixlen)
                network = decision["network"]
                vrf_id = decision["vrf_id"]
                cache_key = (network, vrf_id)
                selected = self.find_prefixes_in_vrf(network, vrf_id)
                if len(selected) > 1:
                    self.log(f"Präfix {network} nicht geändert: mehrere passende Präfixe gefunden", "warning")
                    return vrf_id
                if selected:
                    selected[0].update({"tenant": tenant_id})
                    self.log(f"Mandant am Präfix aktualisiert: {network}")
                else:
                    payload = {"prefix": network, "status": "active", "tenant": tenant_id}
                    if vrf_id is not None:
                        payload["vrf"] = vrf_id
                    self.nb.ipam.prefixes.create(payload)
                    self.log(f"Präfix erstellt: {network}", "success")
        except Exception as error:
            raise RuntimeError(f"Präfix {network}: {error}") from error

        self.synced_prefixes.add(cache_key)
        return vrf_id

    # ------------------------------------------------------------------
    # IPAM batch tools
    # ------------------------------------------------------------------

    def create_vrfs_batch(self, tenant_name, names):
        tenant_id = self.tenants[tenant_name]
        created = reused = 0
        errors = []
        for name in names:
            exists = any(vrf.name == name for vrf in self.tenant_vrfs(tenant_id))
            try:
                self.get_or_create_tenant_vrf(name, tenant_id)
                reused += exists
                created += not exists
            except Exception as error:  # noqa: BLE001
                errors.append(f"{name}: {error}")
                self.log(f"VRF {name}: {error}", "error")
        return created, reused, errors

    def create_prefixes_batch(self, tenant_name, vrf_id, values):
        tenant_id = self.tenants[tenant_name]
        created = reused = 0
        canonical = []
        errors = []
        for value in values:
            try:
                network = ipaddress.ip_network(value, strict=False)
                network_text = str(network)
                candidates = self.find_prefixes_in_vrf(network_text, vrf_id)
                if len(candidates) > 1:
                    raise ValueError("mehrere identische Präfixe in dieser VRF gefunden")
                if candidates:
                    candidates[0].update({"tenant": tenant_id})
                    reused += 1
                else:
                    payload = {"prefix": network_text, "status": "active", "tenant": tenant_id}
                    if vrf_id is not None:
                        payload["vrf"] = vrf_id
                    self.nb.ipam.prefixes.create(payload)
                    created += 1
                canonical.append(network_text)
                self.log(f"Netzwerk verarbeitet: {network_text}")
            except Exception as error:  # noqa: BLE001
                errors.append(f"{value}: {error}")
                self.log(f"Netzwerk {value}: {error}", "error")
        return created, reused, canonical, errors
