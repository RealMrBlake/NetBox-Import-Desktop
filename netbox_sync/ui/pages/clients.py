import ipaddress
import json
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from netbox_sync.core import agent_client
from netbox_sync.core.client_sync import clean_manufacturer
from netbox_sync.core.clients import WINRM_HTTP_PORT, WINRM_HTTPS_PORT, os_label, os_version_label
from netbox_sync.ui import widgets
from netbox_sync.ui.client_help import ClientHelpDialog
from netbox_sync.ui.credentials import ask_credentials
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.widgets import (
    Badge,
    Banner,
    Card,
    EmptyState,
    PasswordField,
    SearchField,
    Table,
    button,
    checkbox,
    label,
)

STATUS = {
    "ok": ("OK", "success"),
    "offline": ("Nicht erreichbar", "text_faint"),
    "error": ("Fehler", "danger"),
    "pending": ("Nur AD", "warning"),
}


def _ips(row):
    return ", ".join(ip.split("/")[0] for interface in row.get("interfaces", []) for ip in interface.get("ips", []))


def _date(timestamp):
    return time.strftime("%d.%m.%Y", time.localtime(timestamp)) if timestamp else ""


def _tooltip(row):
    lines = [row["name"] + (f" ({row['host']})" if row.get("host") and row["host"] != row["name"] else "")]
    if row.get("message"):
        lines.append(f"⚠ {row['message']}")
    for caption, value in (
        ("Modell", " ".join(part for part in (row.get("manufacturer"), row.get("model")) if part)),
        ("Maschinentyp", row.get("machine_type")),
        ("Seriennummer", row.get("serial")),
        ("BIOS", row.get("bios_version")),
        ("Betriebssystem", " ".join(part for part in (os_label(row), os_version_label(row),
                                                      row.get("os_architecture")) if part)),
        ("CPU", f"{row['cpu']} ({row['cores']} Kerne)" if row.get("cpu") else ""),
        ("RAM", f"{row['memory'] / 1024 ** 3:.0f} GB" if row.get("memory") else ""),
        ("Angemeldet", row.get("user")),
        ("Letzter Start", (row.get("last_boot") or "").replace("T", " ")),
        ("Quelle", f"NetBox-Agent {row.get('agent_version', '')}".strip() if row.get("source") == "agent" else ""),
        ("Software", f"{row['software_count']} Programme (laut Agent)" if row.get("software_count") else ""),
        ("Datenträger", ", ".join(f"{disk['model']} ({disk['size'] / 1e9:.0f} GB)" for disk in row.get("disks") or [])),
        ("AD", (row.get("ad") or {}).get("dn")),
    ):
        if value:
            lines.append(f"{caption}: {value}")
    for interface in row.get("interfaces", []):
        lines.append(f"  {interface['name']}  {interface.get('mac', '')}  {', '.join(interface.get('ips', []))}"
                     + ("  (DHCP)" if interface.get("dhcp") else ""))
    return "\n".join(lines)


class ClientsPage(Page):
    title = "Windows-Clients"
    subtitle = ("Windows-Rechner ohne Agent per WMI/CIM über WinRM abfragen und als Geräte nach NetBox "
                "übernehmen.")

    def __init__(self, window):
        super().__init__(window)
        self.header.add_action(button("Einrichtungshilfe", "info", on_click=self.show_help,
                                      tooltip="Gruppenrichtlinie, Firewall und Berechtigungen einrichten, "
                                              "Verbindung zu einem Client testen."))
        self.file_button = self.header.add_action(button("Aus Datei laden …", "folder", on_click=self.load_file,
                                                         tooltip="Clients aus einer im Offline-Modus erstellten "
                                                                 "Exportdatei (JSON) laden."))
        self.scan_button = self.header.add_action(button("Abfragen", "play", "primary", on_click=self.scan))

        # Targets & logon --------------------------------------------------
        targets = Card(padding=16, spacing=10)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(16)
        mode_row.addWidget(label("Ziele", object_name="CardTitle"))
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for key, text in (("ad", "Active Directory"), ("hosts", "Hostnamen / IP-Bereiche"),
                          ("agent", "NetBox-Agent (Netzwerk-Scan)")):
            radio = QRadioButton(text)
            radio.setCursor(Qt.PointingHandCursor)
            self.mode_group.addButton(radio)
            self.mode_buttons[key] = radio
            mode_row.addWidget(radio)
            radio.toggled.connect(lambda checked, k=key: checked and self._set_mode(k))
        mode_row.addStretch(1)
        targets.add(mode_row)

        self.mode_stack = QStackedWidget()
        ad_panel = QWidget()
        grid = QGridLayout(ad_panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        domain = os.environ.get("USERDNSDOMAIN", "").lower()
        self.ad_server = QLineEdit()
        self.ad_server.setPlaceholderText(f"leer = eigene Domäne{f' ({domain})' if domain else ''}")
        self.ad_base = QLineEdit()
        self.ad_base.setPlaceholderText("OU=Clients,DC=firma,DC=local – leer = ganze Domäne")
        grid.addWidget(label("Domäne / DC"), 0, 0)
        grid.addWidget(self.ad_server, 0, 1)
        grid.addWidget(label("Such-OU"), 0, 2)
        grid.addWidget(self.ad_base, 0, 3)
        filters = QHBoxLayout()
        filters.setSpacing(14)
        self.only_enabled = checkbox("Nur aktivierte Konten")
        self.include_servers = checkbox("Server einschließen")
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 3650)
        self.max_age.setSuffix(" Tagen")
        self.max_age.setSpecialValueText("beliebig")
        self.max_age.setFixedWidth(120)
        filters.addWidget(self.only_enabled)
        filters.addWidget(self.include_servers)
        filters.addSpacing(8)
        filters.addWidget(label("Letzte Anmeldung innerhalb von", muted=True))
        filters.addWidget(self.max_age)
        filters.addStretch(1)
        grid.addLayout(filters, 1, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 2)
        self.mode_stack.addWidget(ad_panel)
        self.hosts = QPlainTextEdit()
        self.hosts.setPlaceholderText("Ein Eintrag pro Zeile oder durch Komma getrennt, z. B.\n"
                                      "PC0815.firma.local   10.0.10.0/24   10.0.20.10-50")
        self.hosts.setFixedHeight(76)
        self.mode_stack.addWidget(self.hosts)
        self.mode_stack.addWidget(self._build_agent_panel())
        # QPlainTextEdit reports a tall size hint even with a fixed height.
        self.mode_stack.setFixedHeight(max(ad_panel.sizeHint().height(), self.hosts.height(),
                                           self.agent_panel.sizeHint().height()))
        targets.add(self.mode_stack)
        self.separator = widgets.hline()
        targets.add(self.separator)

        self.logon_widget = QWidget()
        logon = QHBoxLayout(self.logon_widget)
        logon.setContentsMargins(0, 0, 0, 0)
        logon.setSpacing(10)
        logon.addWidget(label("Anmeldung", object_name="CardTitle"))
        self.account_badge = Badge("", "neutral")
        logon.addWidget(self.account_badge)
        self.login_button = button("Anmelden …", "shield", on_click=self.login,
                                   tooltip="Windows-Anmeldefenster öffnen (Domänenkonto mit Administratorrechten "
                                           "auf den Clients).")
        self.logout_button = button("Abmelden", variant="ghost", on_click=self.logout)
        logon.addWidget(self.login_button)
        logon.addWidget(self.logout_button)
        logon.addStretch(1)
        self.current_user = checkbox("Aktuelle Windows-Anmeldung verwenden",
                                     tooltip=f"Ohne Kennwortabfrage mit dem Konto, unter dem das Programm läuft "
                                             f"({os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}).")
        self.use_ssl = checkbox(f"HTTPS (Port {WINRM_HTTPS_PORT})",
                                tooltip=f"Standard ist HTTP auf Port {WINRM_HTTP_PORT} – mit Kerberos ebenfalls "
                                        "verschlüsselt. HTTPS erfordert einen Zertifikats-Listener auf den Clients.")
        logon.addWidget(self.current_user)
        logon.addWidget(self.use_ssl)
        targets.add(self.logon_widget)
        self.body.addWidget(targets)

        self.help_banner = Banner("Die Clients müssen einmalig für WinRM vorbereitet werden (Gruppenrichtlinie, "
                                  "Firewall). Die Einrichtungshilfe zeigt alle Schritte.", "info",
                                  "Einrichtungshilfe öffnen", self.show_help)
        self.body.addWidget(self.help_banner)

        # Results ------------------------------------------------------------
        self.stack = QStackedWidget()
        self.empty = EmptyState("monitor", "Noch keine Clients abgefragt",
                                "Ziele wählen, anmelden und „Abfragen“ – erreichbare Rechner werden parallel "
                                "ausgelesen.", "Jetzt abfragen", self.scan,
                                secondary=("Einrichtungshilfe", self.show_help))
        self.stack.addWidget(self.empty)
        card = Card(padding=0, spacing=0)
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 14, 16, 12)
        bar.setSpacing(8)
        self.search = SearchField("Name, Modell, Seriennummer, Benutzer, IP …")
        self.status_filter = QComboBox()
        self.status_filter.addItem("Alle Zustände", None)
        for key, (text, _token) in STATUS.items():
            self.status_filter.addItem(text, key)
        self.netbox_filter = QComboBox()
        for text, value in (("NetBox: alle", None), ("Nur neue", "new"), ("Nur vorhandene", "existing")):
            self.netbox_filter.addItem(text, value)
        bar.addWidget(self.search, 3)
        bar.addWidget(self.status_filter, 1)
        bar.addWidget(self.netbox_filter, 1)
        card.add(bar)

        columns = [
            Column("name", "Name", 170, tooltip=_tooltip),
            Column("netbox", "NetBox", 90, value=self._netbox_state,
                   color=lambda row: "accent" if self._netbox_state(row) == "Neu" else "text_muted"),
            Column("status", "Status", 150, value=lambda row: STATUS.get(row["status"], ("?", ""))[0],
                   dot=lambda row: STATUS.get(row["status"], ("", "text_faint"))[1],
                   tooltip=lambda row: row.get("message") or STATUS.get(row["status"], ("",))[0]),
            Column("manufacturer", "Hersteller", 110, value=lambda row: clean_manufacturer(row.get("manufacturer"))),
            Column("model", "Modell", 190),
            Column("serial", "Seriennummer", 120),
            Column("os", "Betriebssystem", 190, value=os_label),
            Column("os_version", "Version", 120, value=os_version_label,
                   sort=lambda row: tuple(int(part) for part in (row.get("os_version") or "0").split(".")
                                          if part.isdigit())),
            Column("chassis", "Bauform", 95),
            Column("user", "Angemeldet", 140),
            Column("ips", "IP-Adressen", 160, value=_ips),
            Column("last_logon", "AD-Anmeldung", 105, value=lambda row: _date((row.get("ad") or {}).get("last_logon")),
                   sort=lambda row: (row.get("ad") or {}).get("last_logon") or 0),
        ]
        self.model = RowTableModel(columns, checkable=True, row_id=lambda row: row["id"])
        self.proxy = FilterProxy(self.model)
        self.table = Table()
        self.table.setModel(self.proxy)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.table.doubleClicked.connect(self._toggle)
        card.add(self.table, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(16, 10, 16, 12)
        footer.setSpacing(8)
        footer.addWidget(button("Alle sichtbaren auswählen", variant="ghost", on_click=self.select_visible))
        footer.addWidget(button("Nur neue auswählen", variant="ghost", on_click=self.select_new))
        footer.addWidget(button("Auswahl aufheben", variant="ghost", on_click=self.clear_selection))
        footer.addStretch(1)
        self.selection_label = label("", muted=True)
        footer.addWidget(self.selection_label)
        self.rescan_button = button("Erneut abfragen", "refresh", on_click=self.rescan,
                                    tooltip="Nur die angehakten Clients erneut abfragen.")
        footer.addWidget(self.rescan_button)
        self.sync_button = button("Nach NetBox übertragen …", "upload", "primary", on_click=self.sync)
        footer.addWidget(self.sync_button)
        card.add(footer)
        self.stack.addWidget(card)
        self.body.addWidget(self.stack, 1)

        # Signals ------------------------------------------------------------
        self.ad_server.editingFinished.connect(lambda: self._save(ad_server=self.ad_server.text().strip()))
        self.ad_base.editingFinished.connect(lambda: self._save(ad_base=self.ad_base.text().strip()))
        self.only_enabled.toggled.connect(lambda value: self._save(only_enabled=value))
        self.include_servers.toggled.connect(lambda value: self._save(include_servers=value))
        self.max_age.valueChanged.connect(lambda value: self._save(max_age_days=value))
        self.hosts.textChanged.connect(lambda: self._save(hosts=self.hosts.toPlainText()))
        self.current_user.toggled.connect(self._current_user_toggled)
        self.use_ssl.toggled.connect(lambda value: self._save(use_ssl=value))
        self.agent_targets.textChanged.connect(lambda: self._save(agent_targets=self.agent_targets.toPlainText()))
        self.agent_port.valueChanged.connect(lambda value: self._save(agent_port=value))
        self.agent_networks.editingFinished.connect(
            lambda: self._save(agent_networks=self.agent_networks.text().strip()))
        self.search.textChanged.connect(self.proxy.set_text)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.netbox_filter.currentIndexChanged.connect(self._apply_filters)
        self.model.checked_changed.connect(self._update_selection)
        self.state.clients_changed.connect(self.refresh)
        self.state.profile_switched.connect(self.load_options)
        self.state.profile_switched.connect(self.refresh)
        self.state.settings_changed.connect(self.refresh)
        self.state.netbox_changed.connect(self.refresh)
        self.load_options()
        self.refresh()

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def _save(self, **values):
        self.state.update_client_options(**values)

    def load_options(self):
        options = self.state.client_options
        controls = (self.ad_server, self.ad_base, self.only_enabled, self.include_servers, self.max_age, self.hosts,
                    self.current_user, self.use_ssl, self.agent_targets, self.agent_port, self.agent_networks,
                    *self.mode_buttons.values())
        for widget in controls:
            widget.blockSignals(True)
        self.ad_server.setText(options.get("ad_server", ""))
        self.ad_base.setText(options.get("ad_base", ""))
        self.only_enabled.setChecked(bool(options.get("only_enabled", True)))
        self.include_servers.setChecked(bool(options.get("include_servers")))
        self.max_age.setValue(int(options.get("max_age_days", 90) or 0))
        if self.hosts.toPlainText() != options.get("hosts", ""):
            self.hosts.setPlainText(options.get("hosts", ""))
        self.current_user.setChecked(bool(options.get("use_current_user")))
        self.use_ssl.setChecked(bool(options.get("use_ssl")))
        if self.agent_targets.toPlainText() != options.get("agent_targets", ""):
            self.agent_targets.setPlainText(options.get("agent_targets", ""))
        self.agent_port.setValue(int(options.get("agent_port") or agent_client.DEFAULT_PORT))
        self.agent_networks.setText(options.get("agent_networks", ""))
        mode = options.get("mode", "ad")
        self.mode_buttons.get(mode, self.mode_buttons["ad"]).setChecked(True)
        for widget in controls:
            widget.blockSignals(False)
        self._show_mode(mode)
        self._update_account()
        self._update_agent_key()

    def _set_mode(self, mode):
        self._show_mode(mode)
        self._save(mode=mode)

    def _show_mode(self, mode):
        self.mode_stack.setCurrentIndex({"ad": 0, "hosts": 1, "agent": 2}.get(mode, 0))
        agent = mode == "agent"
        # The agent needs no Windows logon – its shared key replaces it.
        self.logon_widget.setVisible(not agent)
        self.separator.setVisible(not agent)
        self.help_banner.set(
            "Der NetBox-Agent wird per Intune oder GPO verteilt und stellt sein Inventar verschlüsselt bereit – "
            "ohne WinRM und ohne NetBox-Zugang auf den Clients. Die Einrichtungshilfe zeigt die Verteilung."
            if agent else
            "Die Clients müssen einmalig für WinRM vorbereitet werden (Gruppenrichtlinie, Firewall). Die "
            "Einrichtungshilfe zeigt alle Schritte.")

    # ------------------------------------------------------------------
    # NetBox Windows Agent
    # ------------------------------------------------------------------

    def _build_agent_panel(self):
        self.agent_panel = QWidget()
        grid = QGridLayout(self.agent_panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.agent_targets = QPlainTextEdit()
        self.agent_targets.setPlaceholderText("IP-Bereiche oder Hostnamen, z. B. 10.0.10.0/24, 10.0.20.10-50")
        self.agent_targets.setFixedHeight(52)
        grid.addWidget(label("Ziele"), 0, 0)
        grid.addWidget(self.agent_targets, 0, 1, 1, 5)
        self.agent_key = PasswordField("noch kein Schlüssel")
        self.agent_key.setReadOnly(True)
        grid.addWidget(label("Agent-Schlüssel"), 1, 0)
        grid.addWidget(self.agent_key, 1, 1)
        self.agent_key_new = button("Neu erzeugen", "refresh", on_click=self.new_agent_key,
                                    tooltip="Neuen Schlüssel für die Agenten dieses Kunden erzeugen")
        self.agent_key_copy = button("Kopieren", "copy", variant="ghost", on_click=self.copy_agent_key)
        self.agent_config = button("Agent-Konfiguration speichern …", "download", on_click=self.save_agent_config,
                                   tooltip="config.json für die Verteilung per Intune oder GPO erzeugen")
        grid.addWidget(self.agent_key_new, 1, 2)
        grid.addWidget(self.agent_key_copy, 1, 3)
        grid.addWidget(self.agent_config, 1, 4, 1, 2)
        self.agent_port = QSpinBox()
        self.agent_port.setRange(1, 65535)
        self.agent_port.setFixedWidth(100)
        self.agent_networks = QLineEdit()
        self.agent_networks.setPlaceholderText("leer = jede Adresse, z. B. 10.0.0.0/24 (Netz dieses Rechners)")
        grid.addWidget(label("Port"), 2, 0)
        grid.addWidget(self.agent_port, 2, 1, Qt.AlignLeft)
        grid.addWidget(label("Freigegebene Netze"), 2, 2)
        grid.addWidget(self.agent_networks, 2, 3, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return self.agent_panel

    def _update_agent_key(self):
        key = (self.state.profile or {}).get("agent_key", "")
        self.agent_key.setText(key)
        self.agent_key_copy.setEnabled(bool(key))
        self.agent_config.setEnabled(bool(key))
        self.agent_key_new.setText("Neu erzeugen" if key else "Schlüssel erzeugen")

    def new_agent_key(self):
        if self.state.profile is None:
            return
        if self.state.profile.get("agent_key") and not widgets.confirm(
                self.window, "Neuen Schlüssel erzeugen?",
                "Bereits verteilte Agenten lassen sich danach erst wieder abfragen, wenn sie die neue "
                "Konfiguration erhalten haben.", "Neu erzeugen", destructive=True):
            return
        self.state.edit_profile(agent_key=agent_client.new_scan_key())
        self._update_agent_key()
        self.window.toast("Neuer Agent-Schlüssel erzeugt – Konfiguration neu verteilen.", "info")

    def copy_agent_key(self):
        QGuiApplication.clipboard().setText(self.agent_key.text())
        self.window.toast("Agent-Schlüssel kopiert.", "info")

    def save_agent_config(self):
        key = (self.state.profile or {}).get("agent_key", "")
        if not key:
            return
        networks = [part.strip() for part in self.agent_networks.text().replace(";", ",").split(",") if part.strip()]
        for network in networks:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError:
                widgets.message(self.window, "Freigegebene Netze prüfen",
                                f"„{network}“ ist kein Netz in CIDR-Schreibweise (z. B. 10.0.0.0/24).", "warning")
                return
        path, _filter = QFileDialog.getSaveFileName(self.window, "Agent-Konfiguration speichern", "config.json",
                                                    "JSON (*.json)")
        if not path:
            return
        data = agent_client.agent_config(key, self.agent_port.value(),
                                         [str(ipaddress.ip_network(n, strict=False)) for n in networks])
        try:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
        except OSError as error:
            widgets.error(self.window, "Speichern fehlgeschlagen", str(error))
            return
        self.window.toast("config.json gespeichert – zusammen mit agent.exe per Intune oder GPO verteilen.",
                          "success")

    def _current_user_toggled(self, value):
        self._save(use_current_user=value)
        self._update_account()

    def _update_account(self):
        credential = self.state.client_credential
        use_current = self.current_user.isChecked()
        if use_current:
            user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
            self.account_badge.set(f"Aktuelle Anmeldung · {user}", "neutral")
        elif credential is not None:
            self.account_badge.set(f"Angemeldet · {credential.username}", "success")
        else:
            self.account_badge.set("Nicht angemeldet", "warning")
        self.login_button.setVisible(not use_current)
        self.logout_button.setVisible(not use_current and credential is not None)
        self.login_button.setText("Anderes Konto …" if credential is not None else "Anmelden …")

    def login(self):
        try:
            credential = ask_credentials(self.window, self.state.client_options.get("username", ""))
        except Exception as error:  # noqa: BLE001 - CredUI failures are shown to the user
            widgets.error(self.window, "Anmeldung nicht möglich", str(error))
            return False
        if credential is None:
            return False
        self.state.client_credential = credential
        self._save(username=credential.username)
        self._update_account()
        self.window.toast(f"Angemeldet als {credential.username}", "info", 2500)
        return True

    def logout(self):
        self.state.client_credential = None
        self._update_account()

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _netbox_state(self, row):
        engine = self.state.client_engine
        if engine is None or self.state.offline:
            return ""
        return "Vorhanden" if row["name"].casefold() in engine.existing else "Neu"

    def refresh(self):
        self._update_account()
        offline = self.state.offline
        self.table.setColumnHidden(1, offline)
        self.netbox_filter.setVisible(not offline)
        if offline and self.netbox_filter.currentIndex():
            self.netbox_filter.setCurrentIndex(0)
        rows = self.state.clients
        self.stack.setCurrentIndex(1 if rows else 0)
        self.help_banner.setVisible(not rows)
        self.model.set_rows(rows)
        self._apply_filters()
        self.scan_button.setText("Erneut abfragen" if rows else "Abfragen")

    def _apply_filters(self, *_args):
        status = self.status_filter.currentData()
        netbox_state = self.netbox_filter.currentData()
        self.proxy.set_predicate("status", (lambda row: row["status"] == status) if status else None)
        if netbox_state:
            wanted = "Neu" if netbox_state == "new" else "Vorhanden"
            self.proxy.set_predicate("netbox", lambda row: self._netbox_state(row) == wanted)
        else:
            self.proxy.set_predicate("netbox", None)
        self._update_selection()

    def _toggle(self, index):
        row = self.proxy.row_at(index)
        self.model.set_checked({row["id"]}, row["id"] not in self.model.checked)

    def select_visible(self):
        self.model.set_checked({row["id"] for row in self.proxy.visible_rows()})

    def select_new(self):
        self.model.set_checked(set(self.model.checked), False)
        self.model.set_checked({row["id"] for row in self.proxy.visible_rows()
                                if row["status"] == "ok" and self._netbox_state(row) != "Vorhanden"})

    def clear_selection(self):
        self.model.set_checked(set(self.model.checked), False)

    def _update_selection(self):
        checked = self.model.checked_rows()
        ready = [row for row in checked if row["status"] == "ok"]
        visible, total = self.proxy.rowCount(), len(self.model.rows)
        counts = {key: sum(1 for row in self.model.rows if row["status"] == key) for key in STATUS}
        shown = f"{visible} von {total} angezeigt" if visible != total else f"{total} Clients"
        self.selection_label.setText(f"{shown} · {counts['ok']} OK · {len(checked)} ausgewählt")
        verb = "exportieren" if self.state.offline else "nach NetBox übertragen"
        self.sync_button.setText(f"{len(ready)} {verb} …" if ready else f"{verb[0].upper()}{verb[1:]} …")
        busy = self.tasks.busy
        reason = ("Bitte erfolgreich abgefragte Clients (Status „OK“) anhaken." if not ready else "Bitte warten …")
        widgets.set_enabled(self.sync_button, bool(ready) and not busy, reason)
        widgets.set_enabled(self.rescan_button, bool(checked) and not busy,
                            "Bitte Clients anhaken." if not checked else "Bitte warten …")

    def set_busy(self, busy):
        self.scan_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)
        self.login_button.setEnabled(not busy)
        self._update_selection()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _ensure_logon(self):
        if self.current_user.isChecked() or self.state.client_credential is not None:
            return True
        return self.login()

    def scan(self):
        if self.state.profile is None:
            return
        self.ad_server.editingFinished.emit()
        self.ad_base.editingFinished.emit()
        if self.state.client_options.get("mode") == "agent":
            self.actions.scan_clients()
        elif self._ensure_logon():
            self.actions.scan_clients()

    def rescan(self):
        rows = self.model.checked_rows()
        if rows and (self.state.client_options.get("mode") == "agent" or self._ensure_logon()):
            self.actions.scan_clients(rows)

    def load_file(self):
        self.actions.import_clients_file()

    def sync(self):
        rows = [row for row in self.model.checked_rows() if row["status"] == "ok"]
        if not rows:
            return
        if self.state.offline:
            self.actions.export_file(clients=rows)
        else:
            self.actions.sync_clients(rows)

    def show_help(self, tab=None):
        if tab is None or isinstance(tab, bool):  # button signals pass "checked"
            tab = 3 if self.state.client_options.get("mode") == "agent" else 0
        ClientHelpDialog(self.window, self.state, tab).exec()
