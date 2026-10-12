# NetBox Synchronizer

Windows-Desktopanwendung, die virtuelle Maschinen aus VMware vSphere und Proxmox VE sowie
HPE-/Aruba-Switche in NetBox dokumentiert.

## Installation

`NetBoxSynchronizer-Setup-<version>.exe` ausführen. Der Installer

- installiert standardmäßig **ohne Administratorrechte** für den aktuellen Benutzer
  (im Assistenten kann „für alle Benutzer“ gewählt werden),
- legt einen Startmenü-Eintrag und optional eine Desktop-Verknüpfung an,
- aktualisiert eine vorhandene Installation direkt,
- fragt bei der Deinstallation, ob Kundenprofile und Protokolle ebenfalls gelöscht werden sollen.

Bestehende Kundenprofile der Vorgängerversion („NetBox VM Import“) werden automatisch übernommen.

## Funktionen

**Allgemein**
- Kundenprofile mit NetBox-Ziel, Virtualisierungsquellen und Switch-Zugängen; Wechsel über die Seitenleiste
- Automatisches Speichern; Passwörter, Tokens und Secrets mit Windows DPAPI verschlüsselt
- Alle Netzwerkzugriffe laufen im Hintergrund – das Fenster bleibt bedienbar, Vorgänge lassen sich abbrechen
- Helles und dunkles Design (folgt auf Wunsch Windows)
- Protokoll mit Filter, Suche und Export; zusätzlich rotierende Logdatei unter `%APPDATA%\NetBox VM Import\logs`
- Tastenkürzel: `Strg+1…7` Navigation, `F5` neu laden/auslesen, `Strg+F` Suche

**Virtuelle Maschinen**
- Mehrere VMware- und Proxmox-Quellen pro Profil, parallel ausgelesen; Verbindungstest direkt im Dialog
- QEMU/KVM-VMs und optional LXC-Container; IP-Adressen über VMware Tools bzw. QEMU Guest Agent
- Tabelle mit Suche, Filtern (Quelle, Status, „neu“/„bereits in NetBox“) und Mehrfachauswahl
- Automatische Cluster-Zuordnung: Jeder vCenter-/ESXi- bzw. Proxmox-Cluster wird dem gleichnamigen (oder eindeutig
  ähnlichen) NetBox-Cluster zugeordnet; manuelle Zuordnungen merkt sich das Kundenprofil, fehlende Cluster können
  direkt in NetBox angelegt werden
- Import-Assistent in vier Schritten: **Ziel** (Cluster-Zuordnung, Mandant aus dem Cluster vorausgewählt, VRF oder global)
  → **Netzwerke** (fehlende Präfixe mit Größe und VRF prüfen, neue VRFs direkt anlegen)
  → **Prüfen** (Zusammenfassung aller Änderungen) → **Import** (Fortschritt, Live-Protokoll, Abbrechen)
- VMs werden pro Cluster eindeutig zugeordnet; vorhandene Präfixe werden wiederverwendet
- MAC-Adressen werden auch mit NetBox ≥ 4.2 (eigenes MAC-Modell) korrekt gesetzt

**Switche** (Aruba CX, ArubaOS-Switch/ProCurve, HPE Comware)
- Mehrere NetBox-Geräte in einem Schritt hinzufügen – Host aus der primären IP, Plattform aus Modell/Hersteller vorgeschlagen
- Plattform „Automatisch erkennen“ per SSH
- Parallele SSH-Abfragen; SSH-Hostschlüssel werden beim ersten Kontakt gespeichert und danach geprüft
- Schnittstellen, Interface-IP-Adressen sowie LLDP-/CDP-Nachbarn
- **Vorschau vor dem Schreiben**: alle Änderungen werden als Dry-Run berechnet und erst nach Bestätigung übertragen
- Interface-Typen aus Gerätetyp-Vorlagen bleiben unangetastet; Comware-Kurznamen (`GE1/0/1`) werden den
  vollständigen NetBox-Namen (`GigabitEthernet1/0/1`) zugeordnet
- Kabel aus LLDP/CDP auch bei FQDN-Systemnamen oder über die Management-IP
- Die Switch-Konfiguration wird nur verändert, wenn „LLDP bei Bedarf aktivieren“ ausdrücklich eingeschaltet ist

**VRFs & Netze**
- Mehrere VRFs oder Präfixe für einen Mandanten gesammelt anlegen

## Entwicklung

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python main.py          # Anwendung starten
python -m pytest        # Tests
```

Projektstruktur:

```
netbox_sync/
  core/      UI-unabhängige Logik: Konfiguration, NetBox, VMware/Proxmox, VM-Import, Switche
  ui/        Qt-Oberfläche (PySide6): Hauptfenster, Seiten, Dialoge, Import-Assistent, Theme
  resources/ Icons
installer/   Inno-Setup-Skript
tests/       pytest (Parser, Konfiguration, Sync gegen eine In-Memory-NetBox)
```

## Build & Installer

Voraussetzung für den Installer ist [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`).

```powershell
.\build.ps1                 # Tests, PyInstaller-Build und Installer
.\build.ps1 -SkipInstaller  # nur dist\NetBoxSynchronizer\
```

Ergebnis: `dist\installer\NetBoxSynchronizer-Setup-<version>.exe`. Die Version steht in `netbox_sync/__init__.py`.
