"""Updates from the Gitea releases of the project.

The latest release is read anonymously from the Gitea API. The installed
version updates by running the new installer silently (it restarts the
application afterwards); the portable EXE is swapped in place: Windows allows
renaming a running EXE, so the old file is moved aside and removed on the next
start.
"""

import re
import subprocess
import sys
from pathlib import Path

import requests

from netbox_sync import APP_VERSION

REPOSITORY_API = "https://git.mrblake.cc/api/v1/repos/MrBlake/NetBox-Import-Desktop"
LATEST_RELEASE_URL = f"{REPOSITORY_API}/releases/latest"
PORTABLE_ASSET = "NetBox VM Import.exe"
INSTALLER_ASSET = re.compile(r"(?i)^NetBoxSynchronizer-Setup-[\d.]+\.exe$")
INSTALLER_ARGUMENTS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class UpdateError(RuntimeError):
    pass


def parse_version(text):
    """``"v4.6.0"`` → ``(4, 6, 0)``."""
    numbers = [int(part) for part in re.findall(r"\d+", text or "")[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))


def install_kind():
    """``"installer"``, ``"portable"`` or ``None`` when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = Path(sys.executable).resolve().parent
    bundle = Path(getattr(sys, "_MEIPASS", exe_dir)).resolve()
    # A one-file (portable) build unpacks itself into a temporary folder.
    return "installer" if bundle == exe_dir or exe_dir in bundle.parents else "portable"


def check_for_update(current=APP_VERSION, timeout=10):
    """Return the newer release or ``None``."""
    try:
        response = requests.get(LATEST_RELEASE_URL, timeout=timeout, headers={"Accept": "application/json"})
        response.raise_for_status()
        release = response.json()
    except (requests.RequestException, ValueError) as error:
        raise UpdateError(f"Update-Server nicht erreichbar: {error}") from error
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = release.get("tag_name") or ""
    if parse_version(tag) <= parse_version(current):
        return None
    return {
        "version": tag.lstrip("vV"),
        "name": release.get("name") or tag,
        "notes": release.get("body") or "",
        "page": release.get("html_url") or "",
        "assets": {asset["name"]: {"url": asset["browser_download_url"], "size": int(asset.get("size") or 0)}
                   for asset in release.get("assets", []) if asset.get("browser_download_url")},
    }


def pick_asset(release, kind):
    """``(name, asset)`` matching the running installation, or ``None``."""
    for name, asset in release["assets"].items():
        if kind == "portable" and name.casefold() == PORTABLE_ASSET.casefold():
            return name, asset
        if kind == "installer" and INSTALLER_ASSET.match(name):
            return name, asset
    return None


def download(asset, target, progress=None, cancelled=None, timeout=30):
    """Stream ``asset`` to ``target``; the size is verified before the file is kept."""
    target = Path(target)
    partial = target.with_name(target.name + ".part")
    try:
        with requests.get(asset["url"], stream=True, timeout=timeout) as response:
            response.raise_for_status()
            total = asset.get("size") or int(response.headers.get("Content-Length") or 0)
            done = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if cancelled and cancelled():
                        raise UpdateError("Download abgebrochen.")
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except requests.RequestException as error:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Download fehlgeschlagen: {error}") from error
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if total and done != total:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Download unvollständig ({done} von {total} Bytes).")
    partial.replace(target)
    return target


def portable_download_path():
    exe = Path(sys.executable)
    return exe.with_name(exe.stem + ".new.exe")


def apply_portable(new_exe):
    """Swap the running portable EXE for ``new_exe`` and start it."""
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    old.unlink(missing_ok=True)
    exe.replace(old)
    try:
        Path(new_exe).replace(exe)
    except OSError:
        old.replace(exe)
        raise
    subprocess.Popen([str(exe)], close_fds=True, creationflags=_DETACHED)  # noqa: S603


def apply_installer(setup_exe):
    """Run the installer silently; it closes and restarts the application."""
    subprocess.Popen([str(setup_exe), *INSTALLER_ARGUMENTS], close_fds=True, creationflags=_DETACHED)  # noqa: S603


def remove_leftovers():
    """Delete the previous portable EXE left behind by an update."""
    if install_kind() != "portable":
        return
    exe = Path(sys.executable)
    for leftover in (exe.with_name(exe.stem + ".old.exe"), portable_download_path()):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
