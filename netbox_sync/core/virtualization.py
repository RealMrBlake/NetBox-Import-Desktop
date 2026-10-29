"""Read virtual machines from VMware vSphere and Proxmox VE."""

import ipaddress
import re
import ssl

import requests
import urllib3

SOURCE_TYPES = {"vmware": "VMware vSphere", "proxmox": "Proxmox VE"}


def _noop(*_args, **_kwargs):
    return None


# --------------------------------------------------------------------------
# IP helpers
# --------------------------------------------------------------------------

def valid_guest_ip(value):
    try:
        address = ipaddress.ip_interface(value).ip if "/" in value else ipaddress.ip_address(value)
        return not (address.is_loopback or address.is_link_local or address.is_unspecified)
    except ValueError:
        return False


def ip_with_prefix(value):
    """Return ``address/prefix``; guess /24 or /64 if the hypervisor omits it."""
    try:
        if "/" in value:
            return str(ipaddress.ip_interface(value))
        address = ipaddress.ip_address(value)
        return f"{address}/24" if address.version == 4 else f"{address}/64"
    except ValueError:
        return None


def source_endpoint(source):
    return source.get("host") or source.get("url") or ""


# --------------------------------------------------------------------------
# VMware
# --------------------------------------------------------------------------

def load_vmware_source(source, log=_noop):
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    log(f"Verbinde zu VMware: {source['name']} …")
    context = ssl._create_unverified_context() if source.get("ignore_ssl") else None
    service_instance = SmartConnect(
        host=source["host"], user=source["username"], pwd=source["password"], sslContext=context
    )
    try:
        content = service_instance.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            # Fetch every required VM property in one PropertyCollector call.
            # Accessing properties on each ManagedObject separately is extremely
            # slow on vCenter because every access may cause another SOAP request.
            traversal = vim.PropertyCollector.TraversalSpec(
                name="vmViewTraversal", type=vim.view.ContainerView, path="view", skip=False,
            )
            object_spec = vim.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
            property_spec = vim.PropertyCollector.PropertySpec(
                type=vim.VirtualMachine, all=False, pathSet=[
                    "name", "config.hardware.memoryMB", "config.hardware.numCPU",
                    "config.hardware.device", "config.guestFullName", "config.template",
                    "runtime.powerState", "runtime.host", "guest.net",
                ],
            )
            filter_spec = vim.PropertyCollector.FilterSpec(objectSet=[object_spec], propSet=[property_spec])
            result = content.propertyCollector.RetrievePropertiesEx(
                [filter_spec], vim.PropertyCollector.RetrieveOptions()
            )
            objects = list(result.objects or []) if result else []
            while result and result.token:
                result = content.propertyCollector.ContinueRetrievePropertiesEx(result.token)
                objects.extend(result.objects or [])
            host_clusters = _vmware_host_clusters(content, vim)
            normalized = []
            for item in objects:
                properties = {prop.name: prop.val for prop in (item.propSet or [])}
                if properties.get("name") and not properties.get("config.template"):
                    vm = _normalize_vmware_vm(properties, source, vim)
                    host = properties.get("runtime.host")
                    vm["cluster"], vm["host"] = host_clusters.get(getattr(host, "_moId", None), ("", ""))
                    normalized.append(vm)
            return normalized
        finally:
            view.Destroy()
    finally:
        Disconnect(service_instance)


def _vmware_host_clusters(content, vim):
    """Map ESXi host IDs to ``(cluster name, host name)``; standalone hosts use their own name as cluster."""
    result = {}
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.ComputeResource], True)
    try:
        for resource in view.view:
            name = resource.name
            for host in resource.host or []:
                result[host._moId] = (name, host.name)
    finally:
        view.Destroy()
    return result


def _normalize_vmware_vm(properties, source, vim):
    disks = sum(
        round(device.capacityInKB / 1024)
        for device in properties.get("config.hardware.device", []) or []
        if isinstance(device, vim.vm.device.VirtualDisk)
    )
    interfaces = []
    for index, net in enumerate(properties.get("guest.net", []) or []):
        mac = getattr(net, "macAddress", None)
        if not mac:
            continue
        interfaces.append({
            "name": getattr(net, "device", None) or f"NIC-{index}",
            "mac": mac,
            "ips": _vmware_guest_ips(net),
        })
    return {
        "name": properties["name"],
        "source": source["name"],
        "source_type": "VMware",
        "kind": "VM",
        "status": "active" if "poweredOn" in str(properties.get("runtime.powerState", "")) else "offline",
        "vcpus": int(properties.get("config.hardware.numCPU", 0) or 0),
        "memory": int(properties.get("config.hardware.memoryMB", 0) or 0),
        "disk": disks,
        "guest_os": properties.get("config.guestFullName", "") or "",
        "interfaces": interfaces,
    }


def _vmware_guest_ips(net):
    """Prefer VMware-reported prefix lengths and retain legacy IP fallback."""
    ips = []
    ip_config = getattr(net, "ipConfig", None)
    for entry in (getattr(ip_config, "ipAddress", None) or []):
        value = getattr(entry, "ipAddress", None)
        prefix = getattr(entry, "prefixLength", None)
        if value and valid_guest_ip(value):
            ips.append(f"{value}/{prefix}" if prefix is not None else value)

    known = {str(ipaddress.ip_interface(value).ip if "/" in value else ipaddress.ip_address(value)) for value in ips}
    for value in (getattr(net, "ipAddress", None) or []):
        if not valid_guest_ip(value):
            continue
        normalized = str(ipaddress.ip_address(value))
        if normalized not in known:
            ips.append(value)
            known.add(normalized)
    return ips


# --------------------------------------------------------------------------
# Proxmox
# --------------------------------------------------------------------------

def _proxmox_session(source):
    session = requests.Session()
    session.verify = not source.get("ignore_ssl")
    if source.get("ignore_ssl"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session.headers["Authorization"] = f"PVEAPIToken={source['token_id']}={source['token_secret']}"
    return session


def _proxmox_get(session, base_url, path):
    response = session.get(f"{base_url}/api2/json{path}", timeout=30)
    if response.status_code == 401:
        raise PermissionError("Proxmox hat Token-ID oder Token-Secret abgelehnt.")
    response.raise_for_status()
    return response.json().get("data")


def load_proxmox_source(source, log=_noop):
    log(f"Verbinde zu Proxmox: {source['name']} …")
    session = _proxmox_session(source)
    resources = _proxmox_get(session, source["url"], "/cluster/resources?type=vm") or []
    log(f"{source['name']}: Cluster-API meldet {len(resources)} Ressourcen")

    # Some restricted API tokens may not see the cluster-wide resource
    # endpoint. Try the per-node endpoints as a compatible fallback.
    if not resources:
        resources = _proxmox_resources_by_node(session, source)
        log(f"{source['name']}: Node-Abfrage meldet {len(resources)} Ressourcen")

    cluster_name = _proxmox_cluster_name(session, source)
    result = []
    for resource in resources:
        kind = resource.get("type")
        if kind not in ("qemu", "lxc") or _is_template(resource.get("template")):
            continue
        if kind == "lxc" and not source.get("include_lxc", True):
            continue
        try:
            guest = _normalize_proxmox_guest(session, source, resource)
            guest["cluster"] = cluster_name or resource.get("node", "")
            guest["host"] = resource.get("node", "")
            result.append(guest)
        except Exception as error:  # noqa: BLE001 - one broken guest must not stop the source
            log(f"{source['name']} / VMID {resource.get('vmid')}: {error}", "warning")
    if not result:
        raise RuntimeError(
            "API erreichbar, aber keine sichtbaren VMs/LXCs gefunden. "
            "Dem Benutzer und dem API-Token mindestens PVEAuditor auf /vms zuweisen."
        )
    return result


def _proxmox_cluster_name(session, source):
    """Name of the Proxmox cluster, or ``""`` for a standalone node (the node name is used then)."""
    try:
        status = _proxmox_get(session, source["url"], "/cluster/status") or []
    except (requests.RequestException, PermissionError, ValueError):
        return ""
    return next((item.get("name", "") for item in status if item.get("type") == "cluster"), "")


def _proxmox_resources_by_node(session, source):
    resources = []
    kinds = ["qemu"] + (["lxc"] if source.get("include_lxc", True) else [])
    for node_data in _proxmox_get(session, source["url"], "/nodes") or []:
        node = node_data.get("node")
        if not node:
            continue
        for kind in kinds:
            for guest in _proxmox_get(session, source["url"], f"/nodes/{node}/{kind}") or []:
                resources.append({**guest, "node": node, "type": kind})
    return resources


def _is_template(value):
    return str(value).strip().lower() in ("1", "true", "yes")


def _normalize_proxmox_guest(session, source, resource):
    kind = resource["type"]
    node = resource["node"]
    vmid = resource["vmid"]
    config = _proxmox_get(session, source["url"], f"/nodes/{node}/{kind}/{vmid}/config") or {}
    interfaces = proxmox_interfaces(config, kind)
    if kind == "qemu" and resource.get("status") == "running":
        _add_qemu_agent_ips(session, source, node, vmid, interfaces)
    return {
        "name": resource.get("name") or f"{kind}-{vmid}",
        "source": source["name"],
        "source_type": "Proxmox",
        "kind": "LXC" if kind == "lxc" else "VM",
        "status": "active" if resource.get("status") == "running" else "offline",
        "vcpus": int(resource.get("maxcpu") or config.get("cores") or 0),
        "memory": round(int(resource.get("maxmem") or 0) / 1024 / 1024) or int(config.get("memory") or 0),
        "disk": round(int(resource.get("maxdisk") or 0) / 1024 / 1024),
        "guest_os": proxmox_os_name(config.get("ostype", ""), kind),
        "interfaces": interfaces,
    }


def proxmox_interfaces(config, kind):
    interfaces = []
    for key in sorted(config):
        if not re.fullmatch(r"net\d+", key):
            continue
        values = parse_proxmox_options(str(config[key]))
        if kind == "qemu":
            mac = next(
                (value for name, value in values.items() if name in ("virtio", "e1000", "e1000e", "vmxnet3", "rtl8139")),
                None,
            )
            name = key
            ips = []
        else:
            mac = values.get("hwaddr")
            name = values.get("name", key)
            ip_value = values.get("ip", "")
            ips = [ip_value] if ip_value and ip_value not in ("dhcp", "manual") else []
            ip6_value = values.get("ip6", "")
            if ip6_value and ip6_value not in ("dhcp", "auto", "manual"):
                ips.append(ip6_value)
        if mac:
            interfaces.append({"name": name, "mac": mac, "ips": ips})
    return interfaces


def parse_proxmox_options(value):
    result = {}
    for part in value.split(","):
        if "=" in part:
            key, item = part.split("=", 1)
            result[key.strip().lower()] = item.strip()
    return result


def _add_qemu_agent_ips(session, source, node, vmid, interfaces):
    try:
        payload = _proxmox_get(session, source["url"], f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
    except (requests.RequestException, ValueError, KeyError):
        # The guest agent is optional; configured MAC addresses are still useful.
        return
    agent_interfaces = payload.get("result", []) if isinstance(payload, dict) else payload
    by_mac = {interface["mac"].lower(): interface for interface in interfaces}
    for agent_interface in agent_interfaces or []:
        target = by_mac.get((agent_interface.get("hardware-address") or "").lower())
        if not target:
            continue
        for address in agent_interface.get("ip-addresses", []):
            ip = address.get("ip-address")
            prefix = address.get("prefix")
            if ip and valid_guest_ip(ip):
                target["ips"].append(f"{ip}/{prefix}" if prefix is not None else ip)


def proxmox_os_name(ostype, kind):
    names = {
        "win11": "Microsoft Windows 11", "win10": "Microsoft Windows 10/2016/2019",
        "win8": "Microsoft Windows 8/2012", "win7": "Microsoft Windows 7/2008 R2",
        "winxp": "Microsoft Windows XP", "l26": "Linux 2.6+", "l24": "Linux 2.4",
        "solaris": "Solaris", "other": "Other",
    }
    return names.get(ostype, "Linux Container" if kind == "lxc" else ostype)


# --------------------------------------------------------------------------
# Common entry points
# --------------------------------------------------------------------------

def load_source(source, log=_noop):
    if source.get("type") == "proxmox":
        return load_proxmox_source(source, log)
    return load_vmware_source(source, log)


def test_source(source):
    """Check credentials quickly and return a short human readable result."""
    if source.get("type") == "proxmox":
        session = _proxmox_session(source)
        version = _proxmox_get(session, source["url"], "/version") or {}
        return f"Proxmox VE {version.get('version', '?')} erreichbar"

    from pyVim.connect import Disconnect, SmartConnect

    context = ssl._create_unverified_context() if source.get("ignore_ssl") else None
    instance = SmartConnect(host=source["host"], user=source["username"], pwd=source["password"], sslContext=context)
    try:
        about = instance.RetrieveContent().about
        return f"{about.fullName} erreichbar"
    finally:
        Disconnect(instance)
