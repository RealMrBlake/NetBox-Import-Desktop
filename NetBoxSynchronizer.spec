# -*- mode: python ; coding: utf-8 -*-
# Build: python -m PyInstaller --noconfirm --clean NetBoxSynchronizer.spec
# Produces a one-folder build in dist/NetBoxSynchronizer that the Inno Setup
# installer (installer/NetBoxSynchronizer.iss) packages.
#
# Portable: python -m PyInstaller --noconfirm --clean NetBoxSynchronizer.spec -- --portable
# Produces a single self-extracting "NetBox VM Import.exe".

import re
import sys
from pathlib import Path

PORTABLE = "--portable" in sys.argv

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH)
META = (ROOT / "netbox_sync" / "__init__.py").read_text(encoding="utf-8")
VERSION = re.search(r'APP_VERSION = "([^"]+)"', META).group(1)
PUBLISHER = re.search(r'APP_PUBLISHER = "([^"]+)"', META).group(1)
NUMBERS = tuple(int(part) for part in (VERSION.split(".") + ["0", "0", "0"])[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo([StringTable("040704B0", [
            StringStruct("CompanyName", PUBLISHER),
            StringStruct("FileDescription", "NetBox Synchronizer"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "NetBoxSynchronizer"),
            StringStruct("OriginalFilename", "NetBoxSynchronizer.exe"),
            StringStruct("ProductName", "NetBox Synchronizer"),
            StringStruct("ProductVersion", VERSION),
            StringStruct("LegalCopyright", f"© {PUBLISHER}"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0407, 1200])]),
    ],
)

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[("netbox_sync/resources", "resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
    noarchive=False,
    optimize=0,
)
# Qt ships much more than a widget application needs. Dropping the software
# OpenGL fallback, unused image plugins and foreign translations saves ~30 MB.
UNNEEDED = {
    "opengl32sw.dll", "qdirect2d.dll", "qtiff.dll", "qwebp.dll", "qjpeg.dll", "qgif.dll", "qicns.dll",
    "qtga.dll", "qwbmp.dll", "qpdf.dll",
}
a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() not in UNNEEDED]
a.datas = [
    item for item in a.datas
    if "translations" not in Path(item[0]).parts or Path(item[0]).name.startswith(("qtbase_de", "qt_de"))
]

pyz = PYZ(a.pure)

EXE_OPTIONS = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["netbox_sync/resources/app.ico"],
    version=version_info,
)

if PORTABLE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="NetBox VM Import", upx_exclude=[],
              runtime_tmpdir=None, **EXE_OPTIONS)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="NetBoxSynchronizer", **EXE_OPTIONS)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="NetBoxSynchronizer",
    )
