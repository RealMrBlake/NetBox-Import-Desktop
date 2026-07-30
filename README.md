# NetBox VM Import Desktop

Windows-Desktopanwendung zum Synchronisieren virtueller Maschinen sowie HPE-/Aruba-Switche nach NetBox.

## Funktionen

- Mehrere VMware- und Proxmox-Quellen pro Kundenprofil
- QEMU/KVM-VMs und optional LXC-Container aus Proxmox VE
- Mit Windows DPAPI verschlüsselte Passwörter, API-Token und Token-Secrets
- Automatische Übernahme des Cluster-Mandanten mit verbindlicher Bestätigung vor dem Sync
- Mandantenzuweisung für IPv4-/IPv6-Adressen und erkannte NetBox-Präfixe
- Gemeinsame Importmaske für alle neu erkannten Netze mit Subnetzgröße und vorausgefüllter VRF-Auswahl
- Mehrere neue VRFs direkt in der Netzwerkmaske anlegen; Namensfelder erscheinen nur bei Bedarf
- Auswahl vorhandener Mandanten-VRFs oder automatische VRF-Erstellung pro Cluster mit frei wählbarem Namen
- Wiederverwendung bereits vorhandener, zur IP passender Präfixe bei erneuten Imports
- Optionale Unterstützung selbstsignierter TLS-Zertifikate
- Synchronisierung von VMs, Hardwaredaten, Interfaces und IP-Adressen
- Übernahme primärer IPv4- und IPv6-Adressen
- Getrennte Haupt-Tabs für Virtualisierung und Switche
- Gemeinsame, synchronisierte Kundenprofil- und NetBox-Zielansicht in beiden Tabs
- Verschlüsselte SSH-Zugänge pro bestehendem NetBox-Switch
- SSH-Erkennung für Aruba CX, ArubaOS-Switch/ProCurve und HPE Comware
- Durchsuchbare, nach Mandant filterbare Auswahl vorhandener NetBox-Geräte
- Automatische Aktivierung von global deaktiviertem LLDP auf unterstützten Switches
- Synchronisierung von Switch-Schnittstellen und optionalen LLDP-Verbindungen als NetBox-Kabel
- Auslesen und Synchronisieren vorhandener IPv4-/IPv6-Adressen auf Switch-Schnittstellen
- Spezieller LLDP-Detailparser mit Tabellen-Fallback für Aruba-6100-Switches

## Verwendung

Die fertige Windows-Anwendung befindet sich unter `dist/NetBox VM Import.exe`.

Profile werden im Benutzerprofil unter `%APPDATA%\NetBox VM Import\config.json` gespeichert. Passwort und Token sind an das jeweilige Windows-Benutzerkonto und den Rechner gebunden verschlüsselt.

## Entwicklung

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## EXE erstellen

```powershell
pyinstaller "NetBox VM Import.spec"
```
