"""Agentless inventory of Windows clients via WMI/CIM over WinRM.

Targets come from Active Directory (computer objects) or from a list of host
names, IP addresses and ranges. Each reachable client is queried with
``Get-CimInstance`` through Windows PowerShell, so authentication uses the
Windows stack (Kerberos/Negotiate) – either the signed-in user or credentials
from the Windows logon prompt. Passwords are handed to PowerShell via stdin and
never appear on a command line or on disk.
"""

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

WINRM_HTTP_PORT = 5985
WINRM_HTTPS_PORT = 5986
MAX_RANGE = 4096

DEFAULT_CLIENT_OPTIONS = {
    "mode": "ad",                 # ad | hosts
    "ad_server": "",              # domain or domain controller, empty = own domain
    "ad_base": "",                # search base (OU DN), empty = whole domain
    "include_servers": False,
    "only_enabled": True,
    "max_age_days": 90,           # skip computers without logon for N days (0 = all)
    "hosts": "",
    "use_current_user": False,
    "username": "",               # remembered for the logon prompt, never the password
    "use_ssl": False,
    # Last choices in the NetBox transfer dialog.
    "netbox_site": None,
    "netbox_role": "Client",
    "netbox_tenant": None,
    "sync_interfaces": True,
    "update_existing": True,
}

# Win32_SystemEnclosure.ChassisTypes
CHASSIS = {
    3: "Desktop", 4: "Desktop", 5: "Desktop", 6: "Mini-Tower", 7: "Tower", 8: "Notebook", 9: "Notebook",
    10: "Notebook", 11: "Handheld", 12: "Dockingstation", 13: "All-in-One", 14: "Notebook", 15: "Desktop",
    16: "Desktop", 17: "Server", 23: "Server", 24: "Desktop", 30: "Tablet", 31: "Convertible", 32: "Detachable",
    34: "Embedded", 35: "Mini-PC", 36: "Stick-PC",
}

_FILETIME_EPOCH = 11644473600


@dataclass
class Credential:
    """Credentials from the Windows logon prompt. ``None`` means the signed-in user."""

    username: str
    password: str = field(repr=False)


class ClientScanError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

def _range(first, last):
    start, end = ipaddress.ip_address(first), ipaddress.ip_address(last)
    if start.version != end.version or int(end) < int(start):
        raise ClientScanError(f"Ungültiger Bereich: {first} – {last}")
    if int(end) - int(start) >= MAX_RANGE:
        raise ClientScanError(f"Bereich {first} – {last} ist zu groß (maximal {MAX_RANGE} Adressen).")
    return [str(ipaddress.ip_address(value)) for value in range(int(start), int(end) + 1)]


def expand_targets(text):
    """Turn names, IPs, CIDR networks and ranges (``10.0.0.10-50``) into a list of hosts."""
    hosts = []
    tokens = [token for line in (text or "").splitlines() for token in re.split(r"[\s,;]+", line.split("#", 1)[0])]
    for token in tokens:
        if not token:
            continue
        if "/" in token:
            try:
                network = ipaddress.ip_network(token, strict=False)
            except ValueError as error:
                raise ClientScanError(f"Ungültiges Netz: {token}") from error
            if network.num_addresses > MAX_RANGE + 2:
                raise ClientScanError(f"Netz {token} ist zu groß (maximal {MAX_RANGE} Adressen).")
            hosts.extend(str(address) for address in (network.hosts() if network.num_addresses > 2 else network))
        elif re.fullmatch(r"[\d.]+-[\d.]+", token):
            first, last = token.split("-", 1)
            if "." not in last:
                last = first.rsplit(".", 1)[0] + "." + last
            hosts.extend(_range(first, last))
        else:
            hosts.append(token)
    seen, result = set(), []
    for host in hosts:
        if host.casefold() not in seen:
            seen.add(host.casefold())
            result.append(host)
    return result


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolves(host):
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# PowerShell
# --------------------------------------------------------------------------

def powershell_path():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return candidate if os.path.exists(candidate) else (shutil.which("powershell") or "powershell.exe")


def _start_powershell(script, payload):
    if sys.platform != "win32":
        raise ClientScanError("Die Abfrage von Windows-Clients ist nur unter Windows verfügbar.")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(  # noqa: S603 - fixed executable, script passed encoded
        [powershell_path(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    process.stdin.write(json.dumps(payload).encode("utf-8"))
    process.stdin.close()
    return process


def _credential_payload(credential):
    if credential is None:
        return {"user": "", "password": ""}
    return {"user": credential.username, "password": credential.password}


_PS_PROLOG = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$job = [Console]::In.ReadToEnd() | ConvertFrom-Json
$cred = $null
if ($job.user) {
    $secure = ConvertTo-SecureString $job.password -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($job.user, $secure)
}
function Emit($value) { [Console]::Out.WriteLine(($value | ConvertTo-Json -Compress -Depth 6)); [Console]::Out.Flush() }
function Text($value) { if ($null -eq $value) { '' } else { ([string]$value).Trim() } }
function Stamp($value) { if ($value -is [datetime]) { $value.ToString('s') } else { '' } }
"""

_SCAN_SCRIPT = _PS_PROLOG + r"""
if ($job.ssl) { $option = New-CimSessionOption -UseSsl } else { $option = New-CimSessionOption -Protocol Wsman }
foreach ($target in $job.hosts) {
    $result = [ordered]@{ host = $target; ok = $false }
    $session = $null
    try {
        $params = @{ ComputerName = $target; SessionOption = $option; OperationTimeoutSec = [int]$job.timeout }
        if ($job.port) { $params.Port = [int]$job.port }
        if ($cred) { $params.Credential = $cred; $params.Authentication = 'Negotiate' }
        $session = New-CimSession @params
        $cs = Get-CimInstance -CimSession $session -ClassName Win32_ComputerSystem
        $os = Get-CimInstance -CimSession $session -ClassName Win32_OperatingSystem
        $bios = Get-CimInstance -CimSession $session -ClassName Win32_BIOS
        $product = Get-CimInstance -CimSession $session -ClassName Win32_ComputerSystemProduct
        $enclosure = @(Get-CimInstance -CimSession $session -ClassName Win32_SystemEnclosure) | Select-Object -First 1
        $cpu = @(Get-CimInstance -CimSession $session -ClassName Win32_Processor)
        $adapters = @{}
        foreach ($adapter in @(Get-CimInstance -CimSession $session -ClassName Win32_NetworkAdapter -Filter 'PhysicalAdapter = TRUE')) {
            $adapters[[int]$adapter.InterfaceIndex] = $adapter
        }
        $nics = @()
        foreach ($config in @(Get-CimInstance -CimSession $session -ClassName Win32_NetworkAdapterConfiguration -Filter 'IPEnabled = TRUE')) {
            $adapter = $adapters[[int]$config.InterfaceIndex]
            $nics += [ordered]@{
                name = Text $adapter.NetConnectionID
                description = Text $config.Description
                mac = Text $config.MACAddress
                ips = @($config.IPAddress | ForEach-Object { Text $_ })
                masks = @($config.IPSubnet | ForEach-Object { Text $_ })
                gateway = @($config.DefaultIPGateway | ForEach-Object { Text $_ })
                dhcp = [bool]$config.DHCPEnabled
                physical = [bool]($null -ne $adapter)
                speed = [int64]$adapter.Speed
            }
        }
        $displayVersion = ''
        try {
            $reg = Invoke-CimMethod -CimSession $session -Namespace root/default -ClassName StdRegProv -MethodName GetStringValue -Arguments @{ hDefKey = [uint32]2147483650; sSubKeyName = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion'; sValueName = 'DisplayVersion' }
            $displayVersion = Text $reg.sValue
        } catch { }
        $result.ok = $true
        $result.name = Text $cs.Name
        $result.domain = Text $cs.Domain
        $result.part_of_domain = [bool]$cs.PartOfDomain
        $result.manufacturer = Text $cs.Manufacturer
        $result.model = Text $cs.Model
        $result.product_name = Text $product.Name
        $result.product_version = Text $product.Version
        $result.serial = Text $bios.SerialNumber
        $result.bios_version = Text $bios.SMBIOSBIOSVersion
        $result.chassis = @($enclosure.ChassisTypes | ForEach-Object { [int]$_ })
        $result.user = Text $cs.UserName
        $result.memory = [int64]$cs.TotalPhysicalMemory
        $result.cpu = Text ($cpu | Select-Object -First 1).Name
        $result.cores = [int](($cpu | Measure-Object -Property NumberOfCores -Sum).Sum)
        $result.os = Text $os.Caption
        $result.os_version = Text $os.Version
        $result.os_build = Text $os.BuildNumber
        $result.os_display_version = $displayVersion
        $result.os_architecture = Text $os.OSArchitecture
        $result.installed = Stamp $os.InstallDate
        $result.last_boot = Stamp $os.LastBootUpTime
        $result.nics = $nics
    } catch {
        $result.error = Text $_.Exception.Message
    } finally {
        if ($session) { Remove-CimSession $session -ErrorAction SilentlyContinue }
    }
    Emit $result
}
"""

_AD_SCRIPT = _PS_PROLOG + r"""
try {
    if ($cred) {
        $plain = $cred.GetNetworkCredential()
        $root = New-Object System.DirectoryServices.DirectoryEntry($job.path, $cred.UserName, $plain.Password)
    } elseif ($job.path) {
        $root = New-Object System.DirectoryServices.DirectoryEntry($job.path)
    } else {
        $root = New-Object System.DirectoryServices.DirectoryEntry
    }
    $searcher = New-Object System.DirectoryServices.DirectorySearcher($root)
    $searcher.Filter = $job.filter
    $searcher.PageSize = 1000
    foreach ($name in 'name', 'dnshostname', 'operatingsystem', 'operatingsystemversion', 'lastlogontimestamp',
                      'distinguishedname', 'description', 'useraccountcontrol') {
        [void]$searcher.PropertiesToLoad.Add($name)
    }
    foreach ($item in $searcher.FindAll()) {
        $p = $item.Properties
        Emit ([ordered]@{
            name = Text ($p['name'] | Select-Object -First 1)
            dns = Text ($p['dnshostname'] | Select-Object -First 1)
            os = Text ($p['operatingsystem'] | Select-Object -First 1)
            os_version = Text ($p['operatingsystemversion'] | Select-Object -First 1)
            last_logon = [int64]($p['lastlogontimestamp'] | Select-Object -First 1)
            dn = Text ($p['distinguishedname'] | Select-Object -First 1)
            description = Text ($p['description'] | Select-Object -First 1)
            disabled = [bool](([int]($p['useraccountcontrol'] | Select-Object -First 1)) -band 2)
        })
    }
} catch {
    Emit ([ordered]@{ fatal = Text $_.Exception.Message })
}
"""


def _read_lines(process, cancelled):
    """Yield parsed JSON lines from a PowerShell process, killing it on cancel."""
    try:
        for raw in process.stdout:
            if cancelled():
                process.kill()
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("{"):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    finally:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _stderr(process):
    try:
        text = process.stderr.read().decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""
    if text.startswith("#< CLIXML"):
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
        text = "".join(parts).replace("_x000D__x000A_", "\n").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&amp;", "&")
    return text.strip()


# --------------------------------------------------------------------------
# Active Directory
# --------------------------------------------------------------------------

def ad_filter(options):
    parts = ["(objectCategory=computer)", "(operatingSystem=Windows*)"]
    if not options.get("include_servers"):
        parts.append("(!(operatingSystem=*Server*))")
    if options.get("only_enabled", True):
        parts.append("(!(userAccountControl:1.2.840.113556.1.4.803:=2))")
    return "(&" + "".join(parts) + ")"


def ad_path(options, credential):
    server = (options.get("ad_server") or "").strip().strip("/")
    base = (options.get("ad_base") or "").strip()
    if not server and not base and credential is None:
        return ""
    if not server and credential is not None:
        server = os.environ.get("USERDNSDOMAIN", "")
    if not server and not base:
        raise ClientScanError("Dieser Rechner ist keiner Domäne beigetreten. Bitte unter „Domäne / DC“ die Domäne "
                              "angeben, z. B. firma.local.")
    return "LDAP://" + "/".join(part for part in (server, base) if part)


def filetime_to_unix(value):
    if not value:
        return None
    return value / 10_000_000 - _FILETIME_EPOCH


def query_ad(options, credential=None, log=lambda *_args: None, cancelled=lambda: False, now=None):
    """Return computer objects from Active Directory as client rows (not yet scanned)."""
    payload = dict(_credential_payload(credential), path=ad_path(options, credential), filter=ad_filter(options))
    log(f"Suche Computerkonten im Active Directory{' unter ' + payload['path'] if payload['path'] else ''} …")
    process = _start_powershell(_AD_SCRIPT, payload)
    max_age = int(options.get("max_age_days") or 0)
    limit = ((now or time.time()) - max_age * 86400) if max_age else None
    rows, stale = [], 0
    for item in _read_lines(process, cancelled):
        if "fatal" in item:
            raise ClientScanError(f"Active-Directory-Abfrage fehlgeschlagen: {friendly_error(item['fatal'])}")
        if not item.get("name"):
            continue
        last_logon = filetime_to_unix(item.get("last_logon"))
        if limit and (last_logon is None or last_logon < limit):
            stale += 1
            continue
        rows.append(new_row(item.get("dns") or item["name"], ad={
            "name": item["name"], "dns": item.get("dns", ""), "os": item.get("os", ""),
            "os_version": item.get("os_version", ""), "last_logon": last_logon, "dn": item.get("dn", ""),
            "description": item.get("description", ""), "disabled": bool(item.get("disabled")),
        }))
    if process.returncode not in (0, None) and not rows:
        problem = _stderr(process)
        if problem:
            raise ClientScanError(f"Active-Directory-Abfrage fehlgeschlagen: {problem}")
    log(f"Active Directory: {len(rows)} Computer" + (f" ({stale} ohne Anmeldung seit {max_age} Tagen ausgelassen)"
                                                     if stale else ""), "success")
    return rows


# --------------------------------------------------------------------------
# Result rows
# --------------------------------------------------------------------------

def new_row(host, ad=None):
    name = (ad or {}).get("name") or (host if is_ip(host) else host.split(".")[0])
    return {
        "id": name.casefold(),
        "name": name.upper() if not is_ip(name) else name,
        "host": host,
        "status": "pending",         # pending | ok | offline | error
        "message": "",
        "ad": ad or None,
        "scanned_at": None,
    }


def clean_model(raw):
    manufacturer = (raw.get("manufacturer") or "").strip()
    model = (raw.get("model") or "").strip()
    # Lenovo reports the machine type in Model ("20W0003XGE") and the
    # marketing name in Win32_ComputerSystemProduct.Version ("ThinkPad T14 Gen 2").
    version = (raw.get("product_version") or "").strip()
    if manufacturer.upper().startswith("LENOVO") and version and version.upper() not in ("", "LENOVO", "NONE"):
        return version, model
    return model or (raw.get("product_name") or "").strip(), ""


def chassis_name(types):
    for value in types or ():
        if value in CHASSIS:
            return CHASSIS[value]
    return ""


def apply_scan(row, raw, now=None):
    """Merge the raw PowerShell result into a result row."""
    row = dict(row)
    row["scanned_at"] = now or time.time()
    if not raw.get("ok"):
        row["status"] = "error"
        row["message"] = friendly_error(raw.get("error", "Unbekannter Fehler"))
        row["error_raw"] = raw.get("error", "")
        return row
    model, machine_type = clean_model(raw)
    display = raw.get("os_display_version") or ""
    row.update({
        "status": "ok",
        "message": "",
        "name": (raw.get("name") or row["name"]).upper(),
        "domain": raw.get("domain", "") if raw.get("part_of_domain") else "",
        "manufacturer": raw.get("manufacturer", ""),
        "model": model,
        "machine_type": machine_type,
        "serial": raw.get("serial", ""),
        "bios_version": raw.get("bios_version", ""),
        "chassis": chassis_name(raw.get("chassis")),
        "user": raw.get("user", ""),
        "memory": int(raw.get("memory") or 0),
        "cpu": re.sub(r"\s+", " ", raw.get("cpu", "")),
        "cores": int(raw.get("cores") or 0),
        "os": raw.get("os", ""),
        "os_version": raw.get("os_version", ""),
        "os_build": raw.get("os_build", ""),
        "os_display_version": display,
        "os_architecture": raw.get("os_architecture", ""),
        "installed": raw.get("installed", ""),
        "last_boot": raw.get("last_boot", ""),
        # Only real adapters: VPN/TAP drivers report no MAC or are not physical.
        "interfaces": [_interface(nic) for nic in raw.get("nics") or [] if nic.get("physical") and nic.get("mac")],
    })
    row["id"] = row["name"].casefold()
    row.pop("error_raw", None)
    return row


def _interface(nic):
    ips = []
    masks = nic.get("masks") or []
    for index, address in enumerate(nic.get("ips") or []):
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            continue
        if ip.is_link_local or ip.is_loopback:
            continue
        mask = masks[index] if index < len(masks) else ""
        try:
            prefix = ipaddress.ip_network(f"{ip}/{mask}", strict=False).prefixlen if mask else ip.max_prefixlen
        except ValueError:
            prefix = ip.max_prefixlen
        ips.append(f"{ip}/{prefix}")
    description = nic.get("description", "")
    return {
        "name": nic.get("name") or description or "LAN",
        "description": description,
        "mac": nic.get("mac", ""),
        "ips": ips,
        "dhcp": bool(nic.get("dhcp")),
        "gateway": [value for value in nic.get("gateway") or [] if value],
        "wireless": bool(re.search(r"wi-?fi|wlan|wireless|802\.11", description, re.I)),
        "speed_mbps": int((nic.get("speed") or 0) // 1_000_000),
    }


def os_label(row):
    text = re.sub(r"^Microsoft\s+", "", row.get("os") or (row.get("ad") or {}).get("os") or "")
    return text.strip()


def os_version_label(row):
    display, build = row.get("os_display_version"), row.get("os_build")
    if display and build:
        return f"{display} (Build {build})"
    return display or (f"Build {build}" if build else (row.get("ad") or {}).get("os_version", ""))


# --------------------------------------------------------------------------
# Error messages
# --------------------------------------------------------------------------

_HINTS = (
    (r"access is denied|zugriff verweigert|0x80070005|nicht autorisiert|unauthorized",
     "Zugriff verweigert – das Konto braucht lokale Administratorrechte auf dem Client."),
    (r"user name or password is incorrect|benutzername oder kennwort|anmeldung fehlgeschlagen|logon failure|"
     r"unknown user name|unbekannter benutzername",
     "Anmeldung fehlgeschlagen – Benutzername oder Kennwort falsch."),
    (r"trustedhosts|vertrauenswürdige hosts",
     "Kerberos nicht möglich (IP-Adresse oder fremde Domäne). Bitte den DNS-Namen verwenden oder HTTPS einrichten."),
    (r"kerberos|0x80090311|0x8009030e|no authority could be contacted|keine autorität",
     "Kerberos-Anmeldung fehlgeschlagen – DNS-Name, Uhrzeit und Domänenvertrauen prüfen."),
    (r"cannot find the computer|kann den computer .* nicht finden|nicht gefunden|could not be resolved",
     "Rechnername kann nicht aufgelöst werden (DNS)."),
    (r"winrm cannot complete|winrm kann den vorgang nicht|destination is unreachable|nicht erreichbar|"
     r"cannot connect to the destination|keine verbindung mit dem in der anforderung|firewall|0x80338012",
     "WinRM antwortet nicht – Dienst, Listener oder Firewall auf dem Client prüfen (siehe Einrichtungshilfe)."),
    (r"timed out|zeitüberschreitung|timeout",
     "Zeitüberschreitung bei der Abfrage."),
    (r"server is not operational|server ist nicht funktionsfähig|referral",
     "Domänencontroller nicht erreichbar oder Such-OU falsch geschrieben."),
)


def friendly_error(text):
    text = (text or "").strip()
    for pattern, hint in _HINTS:
        if re.search(pattern, text, re.I):
            return hint
    first = text.splitlines()[0] if text else "Unbekannter Fehler"
    return first[:300]


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def _chunks(items, count):
    count = max(1, min(count, len(items)))
    return [items[index::count] for index in range(count)]


def scan_clients(rows, credential=None, use_ssl=False, parallel=8, timeout=30, log=lambda *_args: None,
                 on_result=lambda _row: None, cancelled=lambda: False):
    """Query every row's host. ``on_result`` is called for each finished row (any thread)."""
    port = WINRM_HTTPS_PORT if use_ssl else WINRM_HTTP_PORT
    by_host = {row["host"]: row for row in rows}
    results = []

    def reachable(row):
        host = row["host"]
        if not is_ip(host) and not resolves(host):
            return row, "Rechnername kann nicht aufgelöst werden (DNS)."
        if not port_open(host, port):
            return row, f"Nicht erreichbar – ausgeschaltet oder WinRM-Port {port} geschlossen."
        return row, None

    log(f"Prüfe Erreichbarkeit von {len(rows)} Client(s) auf Port {port} …")
    online = []
    with ThreadPoolExecutor(max_workers=min(64, max(1, len(rows)))) as pool:
        for future in as_completed([pool.submit(reachable, row) for row in rows]):
            if cancelled():
                break
            row, problem = future.result()
            if problem:
                finished = dict(row, status="offline", message=problem, scanned_at=time.time())
                results.append(finished)
                on_result(finished)
            else:
                online.append(row["host"])
    if cancelled() or not online:
        return results
    log(f"{len(online)} Client(s) erreichbar, frage WMI ab …")

    def run_chunk(hosts):
        payload = dict(_credential_payload(credential), hosts=hosts, ssl=use_ssl, port=port, timeout=timeout)
        process = _start_powershell(_SCAN_SCRIPT, payload)
        done = set()
        for raw in _read_lines(process, cancelled):
            row = by_host.get(raw.get("host"))
            if row is None:
                continue
            done.add(raw["host"])
            finished = apply_scan(row, raw)
            results.append(finished)
            on_result(finished)
        if not cancelled():
            problem = _stderr(process) or "PowerShell wurde unerwartet beendet."
            for host in hosts:
                if host not in done:
                    finished = dict(by_host[host], status="error", message=friendly_error(problem),
                                    scanned_at=time.time())
                    results.append(finished)
                    on_result(finished)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for future in as_completed([pool.submit(run_chunk, hosts) for hosts in _chunks(online, parallel)]):
            future.result()
    return results
