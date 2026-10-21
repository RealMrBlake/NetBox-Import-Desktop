"""In-process access to WinRM (WSMan.Automation) and Active Directory (ADSI).

Both run through Windows COM inside the application process. No PowerShell
or other helper process is started – endpoint protection such as Sophos
"Application Lockdown" terminates applications that spawn script hosts.
Authentication uses the Windows stack (Kerberos/Negotiate) with either the
signed-in user or explicit credentials.
"""

import xml.etree.ElementTree as ET

WMI_CIMV2 = "http://schemas.microsoft.com/wbem/wsman/1/wmi/root/cimv2/"
STD_REG_PROV = "http://schemas.microsoft.com/wbem/wsman/1/wmi/root/default/StdRegProv"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
HKEY_LOCAL_MACHINE = 2147483650

# WSManSessionFlags
FLAG_UTF8 = 0x1
FLAG_CRED_USERNAME_PASSWORD = 0x1000
FLAG_USE_NEGOTIATE = 0x20000
FLAG_USE_SSL = 0x8000000

ADS_SECURE_AUTHENTICATION = 0x1
ADS_USE_SEALING = 0x40


class WSManError(RuntimeError):
    pass


def _com():
    import pythoncom
    import pywintypes
    import win32com.client

    return pythoncom, pywintypes, win32com.client


class com_thread:
    """Initialise COM for the calling worker thread."""

    def __enter__(self):
        pythoncom = _com()[0]
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        return self

    def __exit__(self, *_exc):
        _com()[0].CoUninitialize()
        return False


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_instance(text):
    """Turn a WS-Man CIM instance (XML) into ``{property: value or [values]}``."""
    root = ET.fromstring(text)
    result = {}
    for child in root:
        name = _local(child.tag)
        if child.get(XSI_NIL) == "true":
            value = None
        elif len(child):
            value = (child[0].text or "").strip()   # e.g. <cim:Datetime>
        else:
            value = (child.text or "").strip()
        if name in result:
            if not isinstance(result[name], list):
                result[name] = [result[name]]
            result[name].append(value)
        else:
            result[name] = value
    return result


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_bool(value):
    return str(value).strip().lower() == "true"


def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _error_text(error, session=None):
    """Readable message (with HRESULT) from a COM error or a WSMan fault."""
    details = ""
    info = getattr(error, "excepinfo", None)
    if session is not None:
        try:
            fault = session.Error or ""
        except Exception:  # noqa: BLE001 - the session may already be unusable
            fault = ""
        if fault:
            try:
                root = ET.fromstring(fault)
                code = root.get("Code", "")
                message = " ".join(text.strip() for text in root.itertext() if text.strip())
                details = f"{message} (WSMan {code})" if code else message
            except ET.ParseError:
                details = fault
    if not details:
        details = (info[2] if info and len(info) > 2 and info[2] else "") or str(error)
    hresult = getattr(error, "hresult", None)
    if isinstance(info, tuple) and len(info) > 5 and info[5]:
        hresult = info[5]
    if isinstance(hresult, int):
        details = f"{details} [0x{hresult & 0xFFFFFFFF:08X}]"
    return details


class Session:
    def __init__(self, host, credential=None, use_ssl=False, port=None, timeout=30):
        _pythoncom, self._pywintypes, client = _com()
        self.wsman = client.Dispatch("WSMan.Automation")
        flags = FLAG_UTF8 | FLAG_USE_NEGOTIATE
        options = None
        if credential is not None:
            options = self.wsman.CreateConnectionOptions()
            options.UserName = credential.username
            options.Password = credential.password
            flags |= FLAG_CRED_USERNAME_PASSWORD
        if use_ssl:
            flags |= FLAG_USE_SSL
        scheme = "https" if use_ssl else "http"
        port = port or (5986 if use_ssl else 5985)
        self.session = self.wsman.CreateSession(f"{scheme}://{host}:{port}/wsman", flags, options)
        self.session.Timeout = int(timeout * 1000)

    def _fail(self, error):
        raise WSManError(_error_text(error, self.session)) from error

    def enumerate(self, cim_class):
        try:
            enumerator = self.session.Enumerate(WMI_CIMV2 + cim_class, "", "", 0)
            items = []
            while not enumerator.AtEndOfStream:
                items.append(parse_instance(enumerator.ReadItem()))
            return items
        except self._pywintypes.com_error as error:
            self._fail(error)

    def first(self, cim_class):
        items = self.enumerate(cim_class)
        return items[0] if items else {}

    def registry_string(self, key, value):
        parameters = (
            f'<p:GetStringValue_INPUT xmlns:p="{STD_REG_PROV}">'
            f"<p:hDefKey>{HKEY_LOCAL_MACHINE}</p:hDefKey>"
            f"<p:sSubKeyName>{key}</p:sSubKeyName><p:sValueName>{value}</p:sValueName>"
            "</p:GetStringValue_INPUT>"
        )
        try:
            result = parse_instance(self.session.Invoke("GetStringValue", STD_REG_PROV, parameters, 0))
        except (self._pywintypes.com_error, ET.ParseError):
            return ""
        return result.get("sValue") or ""


def _datetime(value):
    """CIM datetimes arrive as ISO strings; keep second precision."""
    return (value or "")[:19]


def query_client(host, credential=None, use_ssl=False, port=None, timeout=30):
    """Read inventory data from one client. Returns the raw result dict used by ``apply_scan``."""
    result = {"host": host, "ok": False}
    try:
        session = Session(host, credential, use_ssl, port, timeout)
        system = session.first("Win32_ComputerSystem")
        os_info = session.first("Win32_OperatingSystem")
        bios = session.first("Win32_BIOS")
        product = session.first("Win32_ComputerSystemProduct")
        enclosure = session.first("Win32_SystemEnclosure")
        processors = session.enumerate("Win32_Processor")
        adapters = {as_int(item.get("InterfaceIndex")): item for item in session.enumerate("Win32_NetworkAdapter")
                    if as_bool(item.get("PhysicalAdapter"))}
        nics = []
        for config in session.enumerate("Win32_NetworkAdapterConfiguration"):
            if not as_bool(config.get("IPEnabled")):
                continue
            adapter = adapters.get(as_int(config.get("InterfaceIndex")))
            nics.append({
                "name": (adapter or {}).get("NetConnectionID") or "",
                "description": config.get("Description") or "",
                "mac": config.get("MACAddress") or "",
                "ips": as_list(config.get("IPAddress")),
                "masks": as_list(config.get("IPSubnet")),
                "gateway": as_list(config.get("DefaultIPGateway")),
                "dhcp": as_bool(config.get("DHCPEnabled")),
                "physical": adapter is not None,
                "speed": as_int((adapter or {}).get("Speed")),
            })
        display = session.registry_string(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "DisplayVersion")
    except WSManError as error:
        result["error"] = str(error)
        return result
    except Exception as error:  # noqa: BLE001 - one broken client must not stop the scan
        result["error"] = _error_text(error)
        return result
    result.update({
        "ok": True,
        "name": system.get("Name") or "",
        "domain": system.get("Domain") or "",
        "part_of_domain": as_bool(system.get("PartOfDomain")),
        "manufacturer": system.get("Manufacturer") or "",
        "model": system.get("Model") or "",
        "product_name": product.get("Name") or "",
        "product_version": product.get("Version") or "",
        "serial": bios.get("SerialNumber") or "",
        "bios_version": bios.get("SMBIOSBIOSVersion") or "",
        "chassis": [as_int(value) for value in as_list(enclosure.get("ChassisTypes"))],
        "user": system.get("UserName") or "",
        "memory": as_int(system.get("TotalPhysicalMemory")),
        "cpu": (processors[0].get("Name") or "") if processors else "",
        "cores": sum(as_int(item.get("NumberOfCores")) for item in processors),
        "os": os_info.get("Caption") or "",
        "os_version": os_info.get("Version") or "",
        "os_build": os_info.get("BuildNumber") or "",
        "os_display_version": display,
        "os_architecture": os_info.get("OSArchitecture") or "",
        "installed": _datetime(os_info.get("InstallDate")),
        "last_boot": _datetime(os_info.get("LastBootUpTime")),
        "nics": nics,
    })
    return result


# --------------------------------------------------------------------------
# Active Directory (ADSI via ADODB)
# --------------------------------------------------------------------------

AD_ATTRIBUTES = ("name", "dNSHostName", "operatingSystem", "operatingSystemVersion", "lastLogonTimestamp",
                 "distinguishedName", "description", "userAccountControl")


def _field(record, name):
    value = record.Fields(name).Value
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    return value


def _large_integer(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return (int(value.HighPart) << 32) + (int(value.LowPart) & 0xFFFFFFFF)
    except Exception:  # noqa: BLE001 - unexpected COM type
        return 0


def search_ad(location, ldap_filter, credential=None):
    """Query computer objects. ``location`` is ``server/base`` or ``base`` (without ``LDAP://``)."""
    _pythoncom, pywintypes, client = _com()
    try:
        connection = client.Dispatch("ADODB.Connection")
        connection.Provider = "ADsDSOObject"
        if credential is not None:
            connection.Properties("User ID").Value = credential.username
            connection.Properties("Password").Value = credential.password
            connection.Properties("Encrypt Password").Value = True
            connection.Properties("ADSI Flag").Value = ADS_SECURE_AUTHENTICATION | ADS_USE_SEALING
        connection.Open("Active Directory Provider")
        command = client.Dispatch("ADODB.Command")
        command.ActiveConnection = connection
        command.Properties("Page Size").Value = 1000
        command.CommandText = f"<LDAP://{location}>;{ldap_filter};{','.join(AD_ATTRIBUTES)};subtree"
        records = command.Execute()[0]
        rows = []
        while not records.EOF:
            rows.append({
                "name": _field(records, "name") or "",
                "dns": _field(records, "dNSHostName") or "",
                "os": _field(records, "operatingSystem") or "",
                "os_version": _field(records, "operatingSystemVersion") or "",
                "last_logon": _large_integer(_field(records, "lastLogonTimestamp")),
                "dn": _field(records, "distinguishedName") or "",
                "description": _field(records, "description") or "",
                "disabled": bool(as_int(_field(records, "userAccountControl")) & 2),
            })
            records.MoveNext()
        connection.Close()
        return rows
    except pywintypes.com_error as error:
        raise WSManError(_error_text(error)) from error
