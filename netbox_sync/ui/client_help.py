"""Setup guide for the agentless client scan (GPO, firewall, permissions) with a connection test."""

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from netbox_sync.core.clients import WINRM_HTTP_PORT, WINRM_HTTPS_PORT, new_row, os_label, scan_clients
from netbox_sync.ui.dialogs import _Dialog
from netbox_sync.ui.tasks import Task
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Badge, Banner, button, label

GPO_COMMANDS = """# Auf dem Admin-PC (PowerShell): Erreichbarkeit eines Clients prüfen
Test-WSMan -ComputerName PC0815.firma.local -Authentication Negotiate

# WMI-Abfrage so, wie der NetBox Synchronizer sie ausführt
Get-CimInstance -ComputerName PC0815.firma.local -ClassName Win32_ComputerSystem |
    Select-Object Name, Manufacturer, Model, UserName

# Auf dem Client (als Administrator): Richtlinie anwenden und Listener prüfen
gpupdate /force
winrm enumerate winrm/config/listener
Get-Service WinRM

# Ohne GPO, nur für einzelne Rechner (als Administrator auf dem Client)
Enable-PSRemoting -Force"""


def _guide_html():
    accent = theme.color("accent")
    muted = theme.color("text_muted")
    code_bg = theme.color("log_bg")
    return f"""
<style>
h2 {{ font-size: 13pt; margin: 14px 0 6px 0; }}
h3 {{ font-size: 10.5pt; margin: 12px 0 4px 0; color: {accent}; }}
p, li {{ line-height: 140%; }}
.path {{ font-family: 'Cascadia Mono', Consolas, monospace; background: {code_bg}; }}
.muted {{ color: {muted}; }}
td {{ padding: 3px 10px 3px 0; vertical-align: top; }}
</style>

<h2>So funktioniert die Abfrage</h2>
<p>Der NetBox Synchronizer liest Name, Hersteller, Modell, Seriennummer, Betriebssystem und Netzwerkkarten
<b>ohne Agent</b> über <b>WMI/CIM via WinRM</b> aus (TCP-Port {WINRM_HTTP_PORT}, bei HTTPS {WINRM_HTTPS_PORT}).
Die Anmeldung erfolgt mit Kerberos – der Datenverkehr ist dadurch auch über HTTP verschlüsselt. Es werden
ausschließlich Daten gelesen; auf den Clients wird nichts installiert oder verändert.</p>
<p>Voraussetzungen:</p>
<ul>
<li>WinRM ist auf den Clients aktiv und die Firewall lässt Port {WINRM_HTTP_PORT} aus dem Admin-Netz zu
(per Gruppenrichtlinie, siehe unten).</li>
<li>Das verwendete Konto ist <b>lokaler Administrator</b> auf den Clients – Mitglieder der
„Domänen-Admins“ sind das standardmäßig.</li>
<li>Die Clients werden über ihren <b>DNS-Namen</b> angesprochen (bei Auswahl aus dem Active Directory
automatisch). Reine IP-Adressen funktionieren mit Kerberos nicht.</li>
<li>Die Rechner müssen eingeschaltet sein. Nicht erreichbare Clients erscheinen trotzdem mit den Daten aus dem AD.</li>
</ul>

<h2>Gruppenrichtlinie einrichten</h2>
<p>In der <b>Gruppenrichtlinienverwaltung</b> (gpmc.msc) eine neue GPO anlegen, z. B. „WinRM für
Inventarisierung“, und mit der OU verknüpfen, in der die Client-Computerkonten liegen. Dann bearbeiten:</p>

<h3>1. WinRM-Dienst zulassen</h3>
<p class="path">Computerkonfiguration › Richtlinien › Administrative Vorlagen › Windows-Komponenten ›
Windows-Remoteverwaltung (Windows Remote Management, WinRM) › WinRM-Dienst</p>
<p>„<b>Remoteserververwaltung über WinRM zulassen</b>“ (bei älteren Vorlagen „Automatische Konfiguration
von Listenern zulassen“) → <b>Aktiviert</b>. IPv4-Filter: <b>*</b> oder besser nur die Client-Netze,
IPv6-Filter: <b>*</b> oder leer.</p>

<h3>2. Dienst automatisch starten</h3>
<p class="path">Computerkonfiguration › Richtlinien › Windows-Einstellungen › Sicherheitseinstellungen ›
Systemdienste</p>
<p>„<b>Windows-Remoteverwaltung (WS-Verwaltung)</b>“ → Richtlinie definieren → Startmodus
<b>Automatisch</b>.</p>

<h3>3. Firewall öffnen</h3>
<p class="path">Computerkonfiguration › Richtlinien › Windows-Einstellungen › Sicherheitseinstellungen ›
Windows Defender Firewall mit erweiterter Sicherheit › Eingehende Regeln</p>
<p>Neue Regel → <b>Vordefiniert: „Windows-Remoteverwaltung“</b> → nur die Regel für das Profil
<b>Domäne</b> (bzw. „Domäne, Privat“) übernehmen → Verbindung zulassen. Empfehlung: in den Eigenschaften der
Regel unter <i>Bereich › Remote-IP-Adressen</i> nur das Admin-Netz bzw. diesen Rechner eintragen.</p>

<h3>4. Anwenden und prüfen</h3>
<p>Die Richtlinie greift nach dem nächsten Neustart bzw. nach spätestens ~90 Minuten, sofort mit
<span class="path">gpupdate /force</span> auf dem Client. Danach unten im Reiter
<b>„Verbindung testen“</b> einen Client prüfen.</p>

<h2>Berechtigungen</h2>
<p>Am einfachsten mit einem Domänen-Admin-Konto anmelden. Für ein eigenes Inventar-Konto ohne Domänen-Admin-Rechte:</p>
<ul>
<li>AD-Gruppe anlegen, z. B. „NetBox-Inventar“, und das Konto aufnehmen.</li>
<li>Per Gruppenrichtlinie die Gruppe zur lokalen Gruppe <b>Administratoren</b> hinzufügen:<br>
<span class="path">Computerkonfiguration › Einstellungen › Systemsteuerungseinstellungen › Lokale Benutzer und
Gruppen › Neu › Lokale Gruppe › Administratoren (integriert) › Hinzufügen</span></li>
</ul>
<p class="muted">Die Remote-UAC-Einschränkung („LocalAccountTokenFilterPolicy“) betrifft nur lokale Konten –
mit Domänenkonten ist dafür nichts einzustellen.</p>

<h2>Anmeldung im Programm</h2>
<ul>
<li><b>Anmelden …</b> öffnet das Windows-Anmeldefenster. Das Konto wird gegen die Domäne geprüft und das
Kennwort nur bis zum Beenden des Programms im Arbeitsspeicher gehalten – es wird nie gespeichert.</li>
<li><b>Aktuelle Windows-Anmeldung verwenden</b>: kein Kennwort nötig, wenn das Programm bereits unter einem
berechtigten Konto läuft (z. B. „Als anderer Benutzer ausführen“).</li>
</ul>

<h2>Fehlerbilder</h2>
<table>
<tr><td><b>Nicht erreichbar / Port geschlossen</b></td>
<td>Client ausgeschaltet, Firewall-Regel fehlt oder GPO noch nicht angewendet.</td></tr>
<tr><td><b>WinRM antwortet nicht</b></td>
<td>Port offen, aber kein Listener: Schritt 1 der GPO prüfen, <span class="path">winrm enumerate
winrm/config/listener</span>.</td></tr>
<tr><td><b>Zugriff verweigert</b></td><td>Konto ist kein lokaler Administrator auf dem Client.</td></tr>
<tr><td><b>Kerberos / TrustedHosts</b></td>
<td>Zugriff per IP-Adresse oder auf eine fremde Domäne: DNS-Namen verwenden.</td></tr>
<tr><td><b>Rechnername nicht auflösbar</b></td><td>DNS-Eintrag fehlt oder ist veraltet.</td></tr>
</table>
"""


class ClientHelpDialog(_Dialog):
    def __init__(self, parent, state, tab=0):
        super().__init__(parent, "Einrichtungshilfe: Windows-Clients",
                         "Agentlose Abfrage über WMI/CIM und WinRM – so werden die Clients vorbereitet.", width=820)
        self.resize(900, 760)
        self.state = state
        self.tabs = QTabWidget()

        guide = QTextBrowser()
        guide.setOpenExternalLinks(False)
        guide.setHtml(_guide_html())
        self.tabs.addTab(guide, "Anleitung")

        commands = QWidget()
        layout = QVBoxLayout(commands)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(label("Nützliche Befehle zum Prüfen und für Einzelrechner ohne Gruppenrichtlinie:",
                               muted=True, wrap=True))
        self.commands = QTextBrowser()
        self.commands.setObjectName("LogView")
        self.commands.setPlainText(GPO_COMMANDS)
        layout.addWidget(self.commands, 1)
        copy = button("Kopieren", "copy", on_click=lambda: QGuiApplication.clipboard().setText(GPO_COMMANDS))
        layout.addWidget(copy, 0, Qt.AlignRight)
        self.tabs.addTab(commands, "Befehle")

        test = QWidget()
        layout = QVBoxLayout(test)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(label("Prüft Namensauflösung, WinRM-Port und WMI-Abfrage für einen Client – mit der "
                               "aktuellen Anmeldung der Seite „Windows-Clients“.", muted=True, wrap=True))
        row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("PC0815.firma.local")
        self.host.returnPressed.connect(self.run_test)
        row.addWidget(self.host, 1)
        self.test_button = button("Testen", "play", "primary", on_click=self.run_test)
        row.addWidget(self.test_button)
        layout.addLayout(row)
        self.account = label("", muted=True)
        layout.addWidget(self.account)
        self.result_badge = Badge("", "neutral")
        layout.addWidget(self.result_badge, 0, Qt.AlignLeft)
        self.result_banner = Banner("", "info")
        self.result_banner.hide()
        layout.addWidget(self.result_banner)
        layout.addStretch(1)
        self.tabs.addTab(test, "Verbindung testen")

        self.root.addWidget(self.tabs, 1)
        self.root.addWidget(button("Schließen", variant="primary", on_click=self.accept), 0, Qt.AlignRight)
        self.tabs.setCurrentIndex(tab)
        self._update_account()

    def _update_account(self):
        credential = self.state.client_credential
        options = (self.state.profile or {}).get("clients", {})
        if credential is not None:
            text = f"Anmeldung: {credential.username}"
        elif options.get("use_current_user"):
            text = "Anmeldung: aktuelle Windows-Anmeldung"
        else:
            text = "Noch nicht angemeldet – es wird die aktuelle Windows-Anmeldung verwendet."
        self.account.setText(text + (" · HTTPS" if options.get("use_ssl") else ""))

    def run_test(self):
        host = self.host.text().strip()
        if not host:
            return
        options = (self.state.profile or {}).get("clients", {})
        credential = self.state.client_credential
        use_ssl = bool(options.get("use_ssl"))
        self.test_button.setEnabled(False)
        self.result_banner.hide()
        self.result_badge.set("Teste …", "neutral")
        self._task = Task(lambda _ctx: scan_clients([new_row(host)], credential, use_ssl, parallel=1),
                          "client", "Verbindungstest")
        self._task.signals.succeeded.connect(self._show_result)
        self._task.signals.failed.connect(lambda error: self._show_result(
            [{"status": "error", "message": str(error)}]))
        self._task.signals.finished.connect(lambda: self.test_button.setEnabled(True))
        QThreadPool.globalInstance().start(self._task)

    def _show_result(self, rows):
        row = rows[0] if rows else {"status": "error", "message": "Kein Ergebnis."}
        if row["status"] == "ok":
            self.result_badge.set("Erfolgreich", "success")
            details = " · ".join(part for part in (row.get("manufacturer"), row.get("model"), os_label(row),
                                                    f"S/N {row['serial']}" if row.get("serial") else "") if part)
            self.result_banner.set(f"<b>{row['name']}</b> antwortet: {details}", "success")
        else:
            self.result_badge.set("Fehlgeschlagen", "error")
            self.result_banner.set(row.get("message") or "Unbekannter Fehler", "error")
        self.result_banner.show()
