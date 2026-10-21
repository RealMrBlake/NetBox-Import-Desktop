"""Windows logon prompt (CredUI) for the client scan.

Shows the standard Windows security dialog, checks the entered domain account
against the domain controller and returns it as a :class:`Credential`. The
password only lives in memory for the current session.
"""

import ctypes
import sys

from netbox_sync.core.clients import Credential

ERROR_CANCELLED = 1223
ERROR_LOGON_FAILURE = 1326
ERROR_ACCOUNT_RESTRICTION = 1327
ERROR_PASSWORD_EXPIRED = 1330
ERROR_ACCOUNT_DISABLED = 1331
ERROR_ACCOUNT_LOCKED_OUT = 1909
CRED_PACK_PROTECTED_CREDENTIALS = 0x1
LOGON32_LOGON_NETWORK = 3
LOGON32_PROVIDER_DEFAULT = 0

LOGON_ERRORS = {
    ERROR_LOGON_FAILURE: "Benutzername oder Kennwort ist falsch.",
    ERROR_ACCOUNT_RESTRICTION: "Für dieses Konto gelten Anmeldeeinschränkungen.",
    ERROR_PASSWORD_EXPIRED: "Das Kennwort ist abgelaufen.",
    ERROR_ACCOUNT_DISABLED: "Das Konto ist deaktiviert.",
    ERROR_ACCOUNT_LOCKED_OUT: "Das Konto ist gesperrt.",
}

if sys.platform == "win32":
    from ctypes import wintypes

    class _CredUiInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hwndParent", wintypes.HWND),
                    ("pszMessageText", wintypes.LPCWSTR), ("pszCaptionText", wintypes.LPCWSTR),
                    ("hbmBanner", wintypes.HBITMAP)]

    _credui = ctypes.WinDLL("credui", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32")

    _prompt = _credui.CredUIPromptForWindowsCredentialsW
    _prompt.argtypes = [ctypes.POINTER(_CredUiInfo), wintypes.DWORD, ctypes.POINTER(wintypes.ULONG), wintypes.LPCVOID,
                        wintypes.ULONG, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG),
                        ctypes.POINTER(wintypes.BOOL), wintypes.DWORD]
    _prompt.restype = wintypes.DWORD
    _pack = _credui.CredPackAuthenticationBufferW
    _pack.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                      ctypes.POINTER(wintypes.DWORD)]
    _pack.restype = wintypes.BOOL
    _unpack = _credui.CredUnPackAuthenticationBufferW
    _unpack.argtypes = [wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPWSTR,
                        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _unpack.restype = wintypes.BOOL
    _logon = _advapi.LogonUserW
    _logon.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.POINTER(wintypes.HANDLE)]
    _logon.restype = wintypes.BOOL
    _ole32.CoTaskMemFree.argtypes = [wintypes.LPVOID]


def _packed_username(username):
    """Pre-fill the user name in the dialog."""
    size = wintypes.DWORD(0)
    _pack(0, username, "", None, ctypes.byref(size))
    if not size.value:
        return None, 0
    buffer = ctypes.create_string_buffer(size.value)
    if not _pack(0, username, "", buffer, ctypes.byref(size)):
        return None, 0
    return buffer, size.value


def check_logon(username, password):
    """Validate the account at the domain controller. Returns an error text or ``None``.

    Failures that do not prove wrong credentials (e.g. no DC reachable) are
    not reported so the scan can still try.
    """
    domain = None
    user = username
    if "\\" in username:
        domain, user = username.split("\\", 1)
    token = wintypes.HANDLE()
    if _logon(user, domain, password, LOGON32_LOGON_NETWORK, LOGON32_PROVIDER_DEFAULT, ctypes.byref(token)):
        ctypes.windll.kernel32.CloseHandle(token)
        return None
    return LOGON_ERRORS.get(ctypes.get_last_error())


def prompt_credentials(parent=None, username="", message=None, error_code=0):
    """Show the Windows logon dialog. Returns a :class:`Credential` or ``None`` if cancelled."""
    if sys.platform != "win32":
        raise RuntimeError("Das Windows-Anmeldefenster ist nur unter Windows verfügbar.")
    info = _CredUiInfo()
    info.cbSize = ctypes.sizeof(_CredUiInfo)
    info.hwndParent = int(parent.winId()) if parent is not None else None
    info.pszCaptionText = "NetBox Synchronizer – Windows-Clients"
    info.pszMessageText = message or ("Domänenkonto mit lokalen Administratorrechten auf den Clients "
                                      "(z. B. FIRMA\\admin). Das Kennwort wird nur für diese Sitzung im "
                                      "Arbeitsspeicher gehalten.")
    package = wintypes.ULONG(0)
    in_buffer, in_size = _packed_username(username) if username else (None, 0)
    out_buffer = wintypes.LPVOID()
    out_size = wintypes.ULONG(0)
    save = wintypes.BOOL(False)
    result = _prompt(ctypes.byref(info), error_code, ctypes.byref(package), in_buffer, in_size,
                     ctypes.byref(out_buffer), ctypes.byref(out_size), ctypes.byref(save), 0)
    if result == ERROR_CANCELLED:
        return None
    if result != 0:
        raise ctypes.WinError(result)
    try:
        user = ctypes.create_unicode_buffer(514)
        domain = ctypes.create_unicode_buffer(338)
        password = ctypes.create_unicode_buffer(514)
        sizes = [wintypes.DWORD(514), wintypes.DWORD(338), wintypes.DWORD(514)]
        if not _unpack(CRED_PACK_PROTECTED_CREDENTIALS, out_buffer, out_size.value, user, ctypes.byref(sizes[0]),
                       domain, ctypes.byref(sizes[1]), password, ctypes.byref(sizes[2])):
            raise ctypes.WinError(ctypes.get_last_error())
        name = user.value if not domain.value else f"{domain.value}\\{user.value}"
        credential = Credential(name, password.value)
        ctypes.memset(password, 0, ctypes.sizeof(password))
        return credential
    finally:
        if out_buffer.value:
            ctypes.memset(out_buffer.value, 0, out_size.value)
            _ole32.CoTaskMemFree(out_buffer)


def ask_credentials(parent=None, username=""):
    """Prompt until the account is accepted by the domain or the user cancels."""
    error_code, message = 0, None
    while True:
        credential = prompt_credentials(parent, username, message, error_code)
        if credential is None:
            return None
        problem = check_logon(credential.username, credential.password)
        if problem is None:
            return credential
        username = credential.username
        error_code, message = ERROR_LOGON_FAILURE, f"Anmeldung fehlgeschlagen: {problem}"
