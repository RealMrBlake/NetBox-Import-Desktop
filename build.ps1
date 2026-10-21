<#
.SYNOPSIS
    Baut NetBox Synchronizer: Tests, PyInstaller-Build, Inno-Setup-Installer und portable EXE.

.EXAMPLE
    .\build.ps1                  # alles
    .\build.ps1 -SkipInstaller   # nur dist\NetBoxSynchronizer\ (und portable EXE)
    .\build.ps1 -SkipPortable    # ohne dist\portable\NetBox VM Import.exe
    .\build.ps1 -SkipTests
#>
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$SkipPortable,
    [string]$InnoSetupCompiler
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Erstelle virtuelle Umgebung …" -ForegroundColor Cyan
    py -3 -m venv .venv
    if ($LASTEXITCODE) { throw "Virtuelle Umgebung konnte nicht erstellt werden." }
}

Write-Host "Installiere Abhängigkeiten …" -ForegroundColor Cyan
& $python -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
if ($LASTEXITCODE) { throw "pip install fehlgeschlagen." }

if (-not $SkipTests) {
    Write-Host "Führe Tests aus …" -ForegroundColor Cyan
    & $python -m pytest -q
    if ($LASTEXITCODE) { throw "Tests fehlgeschlagen – Build abgebrochen." }
}

$version = (Select-String -Path "netbox_sync\__init__.py" -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Baue NetBox Synchronizer $version …" -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean --log-level WARN NetBoxSynchronizer.spec
if ($LASTEXITCODE) { throw "PyInstaller fehlgeschlagen." }

if (-not $SkipPortable) {
    Write-Host "Baue portable EXE …" -ForegroundColor Cyan
    & $python -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist\portable --workpath build\portable NetBoxSynchronizer.spec -- --portable
    if ($LASTEXITCODE) { throw "PyInstaller (portabel) fehlgeschlagen." }
    Write-Host "Fertig: dist\portable\NetBox VM Import.exe" -ForegroundColor Green
}

if ($SkipInstaller) {
    Write-Host "Fertig: dist\NetBoxSynchronizer\NetBoxSynchronizer.exe" -ForegroundColor Green
    return
}

if (-not $InnoSetupCompiler) {
    $candidates = @(
        (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $InnoSetupCompiler = $candidates | Select-Object -First 1
}
if (-not $InnoSetupCompiler) {
    Write-Warning "Inno Setup 6 wurde nicht gefunden. Installation z. B. mit: winget install JRSoftware.InnoSetup"
    Write-Warning "Der Programmordner liegt trotzdem unter dist\NetBoxSynchronizer\."
    exit 1
}

Write-Host "Erzeuge Installer …" -ForegroundColor Cyan
& $InnoSetupCompiler /Q "/DAppVersion=$version" "installer\NetBoxSynchronizer.iss"
if ($LASTEXITCODE) { throw "Inno Setup fehlgeschlagen." }
Write-Host "Fertig: dist\installer\NetBoxSynchronizer-Setup-$version.exe" -ForegroundColor Green
