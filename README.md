# NetBox Synchronizer

Windows-Desktopanwendung, die virtuelle Maschinen aus VMware vSphere und Proxmox VE, HPE-/Aruba-Switche
sowie Windows-Clients in NetBox dokumentiert.

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
- Tastenkürzel: `Strg+1…8` Navigation, `F5` neu laden/auslesen, `Strg+F` Suche

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

**Windows-Clients** (agentlos per WMI/CIM über WinRM)
- Ziele aus dem Active Directory (optional Such-OU, nur aktivierte Konten, letzte Anmeldung innerhalb von N Tagen,
  Server ein-/ausschließen) oder als Liste von Hostnamen, IP-Adressen, Netzen (`10.0.10.0/24`) und Bereichen
  (`10.0.20.10-50`)
- Anmeldung über das Windows-Anmeldefenster (Domänenkonto mit lokalen Adminrechten, wird gegen die Domäne geprüft)
  oder mit der aktuellen Windows-Anmeldung; das Kennwort bleibt nur im Arbeitsspeicher
- Liest Name, Hersteller, Modell (bei Lenovo der Modellname statt des Maschinentyps), Seriennummer, Bauform,
  Betriebssystem mit Version/Build, angemeldeten Benutzer, CPU/RAM sowie physische Netzwerkkarten mit MAC und IPs
- Parallele Abfrage; nicht erreichbare Clients bleiben mit den AD-Daten sichtbar, Fehler mit konkretem Hinweis
- Übernahme als NetBox-Geräte mit Standort, Rolle und Mandant; Hersteller, Gerätetypen und Plattformen werden bei
  Bedarf angelegt, optional Schnittstellen, MAC- und IP-Adressen – mit Vorschau vor dem Schreiben
- **Einrichtungshilfe** im Programm: Gruppenrichtlinie (WinRM, Dienst, Firewall), Berechtigungen, Fehlerbilder,
  Prüfbefehle und ein Verbindungstest für einzelne Clients

**Offline-Modus (ohne NetBox)**
- Checkbox „Offline-Modus (ohne NetBox)“ in der Seitenleiste – gilt global, unabhängig vom Kundenprofil
- VMs und Switche werden ohne NetBox-Verbindung ausgelesen; Switche lassen sich dann manuell per Name anlegen
- Ausgewählte VMs, Clients bzw. ausgelesene Switche werden als JSON-Datei (`*.nbsync.json`) exportiert – ohne Passwörter,
  Tokens oder Benutzernamen
- Auf einem Rechner mit NetBox-Zugang lädt „Aus Datei laden …“ (Seiten VMs, Switche und Windows-Clients) die Datei; danach laufen
  Import-Assistent bzw. Dry-Run-Vorschau wie gewohnt. Switche werden über NetBox-Geräte-ID oder Namen zugeordnet

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
  core/      UI-unabhängige Logik: Konfiguration, NetBox, VMware/Proxmox, VM-Import, Switche, Windows-Clients
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

Ergebnis: `dist\installer\NetBoxSynchronizer-Setup-<version>.exe` sowie die portable Variante
`dist\portable\NetBox VM Import.exe` (ohne Installation lauffähig; `-SkipPortable` lässt sie weg). Die Version steht in `netbox_sync/__init__.py`.
