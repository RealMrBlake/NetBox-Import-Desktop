# NetBox VM Import Desktop

Windows-Desktopanwendung zum Importieren und Synchronisieren virtueller Maschinen aus VMware ESXi/vSphere und Proxmox VE nach NetBox.

## Funktionen

- Mehrere VMware- und Proxmox-Quellen pro Kundenprofil
- QEMU/KVM-VMs und optional LXC-Container aus Proxmox VE
- Mit Windows DPAPI verschlüsselte Passwörter, API-Token und Token-Secrets
- Automatische Übernahme des Cluster-Mandanten mit verbindlicher Bestätigung vor dem Sync
- Mandantenzuweisung für IPv4-/IPv6-Adressen und erkannte NetBox-Präfixe
- Bestätigungsdialog mit Subnetzgrößen- und vorausgefüllter VRF-Auswahl sowie optionaler VRF-Erstellung
- Auswahl vorhandener Mandanten-VRFs oder automatische VRF-Erstellung pro Cluster mit frei wählbarem Namen
- Eigenes Fenster zur Stapelanlage mehrerer VRFs und Netzwerke pro Mandant
- Wiederverwendung bereits vorhandener, zur IP passender Präfixe bei erneuten Imports
- Optionale Unterstützung selbstsignierter TLS-Zertifikate
- Synchronisierung von VMs, Hardwaredaten, Interfaces und IP-Adressen
- Übernahme primärer IPv4- und IPv6-Adressen

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
