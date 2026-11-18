"""Printers: SNMP network scan, comparison with NetBox and transfer (reuses the access point workflow)."""

import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
)

from netbox_sync.core import netscan, printers, snmp
from netbox_sync.ui import widgets
from netbox_sync.ui.dialogs import _Dialog
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.pages.access_points import STATE_DOT, STATE_TEXT, AccessPointsPage
from netbox_sync.ui.tasks import logger
from netbox_sync.ui.widgets import (
    Card,
    EmptyState,
    PasswordField,
    SearchableComboBox,
    SearchField,
    Table,
    button,
    checkbox,
    form_dialog_buttons,
    label,
)

MODEL_ACTIONS = (("create", "Gerätetyp neu anlegen"), ("map", "Vorhandenen Gerätetyp verwenden"),
                 ("skip", "Diesmal überspringen"), ("ignore", "Dauerhaft ignorieren"))
AUTH_CHOICES = ["", *snmp.AUTH_PROTOCOLS]
PRIV_CHOICES = ["", *snmp.PRIV_PROTOCOLS]


def _supplies_short(row):
    parts = []
    for item in row.get("supplies") or []:
        if item["percent"] is None:
            continue
        name = item.get("colour") or item.get("type") or item["name"]
        parts.append(f"{name} {item['percent']} %")
    return " · ".join(parts)


def _supplies_low(row):
    return any(item["percent"] is not None and item["percent"] <= 10 for item in row.get("supplies") or [])


def _supplies_tooltip(row):
    lines = [printers.supplies_text(row.get("supplies") or [])]
    trays = row.get("trays") or []
    if trays:
        lines.append("\nPapierfächer:")
        lines += [f"{tray['name']}: {tray['level'] or '–'}" + (f" ({tray['media']})" if tray["media"] else "")
                  for tray in trays]
    return "\n".join(line for line in lines if line)


def _status(row):
    return ", ".join(filter(None, [row.get("status", ""), *(row.get("errors") or [])]))


def _status_tooltip(row):
    lines = []
    if row.get("display"):
        lines.append("Display: " + " | ".join(row["display"]))
    if row.get("alerts"):
        lines += ["Meldungen:"] + [f"• {alert}" for alert in row["alerts"]]
    if row.get("sys_descr"):
        lines.append(f"\n{row['sys_descr']}")
    return "\n".join(lines)


class SnmpCredentialsDialog(_Dialog):
    """SNMPv2c communities and SNMPv3 users of a profile, tried in this order."""

    COLUMNS = ("Version", "Community", "Benutzer (v3)", "Auth", "Auth-Passwort", "Privacy", "Privacy-Passwort")

    def __init__(self, parent, credentials):
        super().__init__(parent, "SNMP-Zugangsdaten",
                         "Jede Adresse wird mit diesen Zugangsdaten in dieser Reihenfolge abgefragt – erst die "
                         "funktionierenden des letzten Scans. SNMPv2c-Communities und SNMPv3-Benutzer lassen sich "
                         "mischen. Passwörter werden mit Windows DPAPI verschlüsselt im Profil gespeichert.",
                         width=980)
        self.resize(1080, 460)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        for column, width in enumerate((80, 160, 150, 100, 170, 100, 170)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.rows = []
        for record in credentials:
            self._add(record)
        actions = QHBoxLayout()
        actions.addWidget(button("SNMPv2c-Community", "plus", on_click=lambda: self._add({"version": "2c"})))
        actions.addWidget(button("SNMPv3-Benutzer", "plus", on_click=lambda: self._add(
            {"version": "3", "auth_protocol": "SHA", "priv_protocol": "AES"})))
        actions.addWidget(button("Entfernen", "trash", on_click=self._remove))
        actions.addStretch(1)
        self.root.addWidget(self.table, 1)
        self.root.addLayout(actions)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Übernehmen")
        self.root.addWidget(buttons)
        self.result = []

    def _add(self, record):
        row = self.table.rowCount()
        self.table.insertRow(row)
        version = QComboBox()
        version.addItems(["2c", "3"])
        version.setCurrentText(str(record.get("version") or "2c"))
        community = PasswordField("public")
        community.setText(record.get("community", "public" if not self.rows else ""))
        username = QLineEdit(record.get("username", ""))
        auth = QComboBox()
        auth.addItems(AUTH_CHOICES)
        auth.setCurrentText(record.get("auth_protocol", ""))
        auth_password = PasswordField()
        auth_password.setText(record.get("auth_password", ""))
        priv = QComboBox()
        priv.addItems(PRIV_CHOICES)
        priv.setCurrentText(record.get("priv_protocol", ""))
        priv_password = PasswordField()
        priv_password.setText(record.get("priv_password", ""))
        cells = (version, community, username, auth, auth_password, priv, priv_password)
        for column, widget in enumerate(cells):
            self.table.setCellWidget(row, column, widget)
        entry = {"id": record.get("id") or uuid.uuid4().hex[:8], "cells": cells}
        self.rows.append(entry)

        def update(*_args):
            v3 = version.currentText() == "3"
            community.setEnabled(not v3)
            for widget in (username, auth, priv):
                widget.setEnabled(v3)
            auth_password.setEnabled(v3 and bool(auth.currentText()))
            priv_password.setEnabled(v3 and bool(priv.currentText()))
        for widget in (version, auth, priv):
            widget.currentIndexChanged.connect(update)
        update()

    def _remove(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        del self.rows[row]

    def accept(self):
        result = []
        for number, entry in enumerate(self.rows, 1):
            version, community, username, auth, auth_password, priv, priv_password = entry["cells"]
            if version.currentText() == "3":
                record = {"id": entry["id"], "version": "3", "username": username.text().strip(),
                          "auth_protocol": auth.currentText(), "auth_password": auth_password.text(),
                          "priv_protocol": priv.currentText(), "priv_password": priv_password.text()}
                problem = ("Benutzername fehlt" if not record["username"] else
                           "Privacy braucht Authentifizierung" if record["priv_protocol"] and not record["auth_protocol"]
                           else "Auth-Passwort mit mindestens 8 Zeichen nötig"
                           if record["auth_protocol"] and len(record["auth_password"]) < 8
                           else "Privacy-Passwort mit mindestens 8 Zeichen nötig"
                           if record["priv_protocol"] and len(record["priv_password"]) < 8 else "")
                if not record["auth_protocol"]:
                    record["auth_password"] = ""
                if not record["priv_protocol"]:
                    record["priv_password"] = ""
            else:
                record = {"id": entry["id"], "version": "2c", "community": community.text()}
                problem = "Community fehlt" if not record["community"] else ""
            if problem:
                self.show_error(f"Zeile {number}: {problem}.")
                return
            result.append(record)
        if not result:
            self.show_error("Bitte mindestens eine Community oder einen SNMPv3-Benutzer anlegen.")
            return
        self.result = result
        super().accept()


class ModelMappingDialog(_Dialog):
    """Decide per printer model that is missing in NetBox: create, map to an existing type, skip or ignore."""

    def __init__(self, parent, rows, device_types):
        super().__init__(parent, "Modelle zuordnen",
                         "Diese Modelle gibt es in NetBox noch nicht als Gerätetyp. Pro Modell: neu anlegen, einen "
                         "vorhandenen Gerätetyp verwenden, die Drucker dieses Modells diesmal überspringen oder dauerhaft "
                         "ignorieren (gespeichert im Profil).", width=900)
        self.resize(1040, 520)
        type_items = [(f"{row['manufacturer']} {row['model']}".strip(), row["id"]) for row in device_types]
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Hersteller / Modell (SNMP)", "Drucker", "Aktion", "Gerätetyp in NetBox"])
        self.rows = []
        for text, count, key, decision, suggestion in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(text))
            self.table.setItem(row, 1, QTableWidgetItem(str(count)))
            action = QComboBox()
            for value, caption in MODEL_ACTIONS:
                action.addItem(caption, value)
            device_type = SearchableComboBox("Gerätetyp suchen …")
            device_type.set_items(type_items, decision.get("device_type") or suggestion)
            chosen = decision.get("action") or ("map" if suggestion else "create")
            action.setCurrentIndex(next(index for index, (value, _c) in enumerate(MODEL_ACTIONS) if value == chosen))
            action.currentIndexChanged.connect(lambda _i, a=action, d=device_type: d.setEnabled(a.currentData() == "map"))
            device_type.setEnabled(chosen == "map")
            self.table.setCellWidget(row, 2, action)
            self.table.setCellWidget(row, 3, device_type)
            self.rows.append((key, text, action, device_type))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((330, 60, 250, 300)):
            self.table.setColumnWidth(column, width)
        self.root.addWidget(self.table, 1)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Übernehmen")
        self.root.addWidget(buttons)
        self.result = {}

    def accept(self):
        result = {}
        for key, text, action, device_type in self.rows:
            value = action.currentData()
            if value == "map" and device_type.valid_data() is None:
                self.show_error(f"Bitte für „{text}“ einen vorhandenen Gerätetyp wählen.")
                return
            result[key] = {"action": value, "device_type": device_type.valid_data() if value == "map" else None}
        self.result = result
        super().accept()


class PrintersPage(AccessPointsPage):
    title = "Drucker"
    subtitle = ("Drucker per SNMP (v2c und v3) im Netzwerk finden – herstellerübergreifend über die Printer-MIB – "
                "mit NetBox abgleichen und mit Seitenzähler und Verbrauchsmaterial dokumentieren.")
    settings_key = "printers"
    noun = "Drucker"
    source_name = "Scan"
    role_hints = printers.ROLE_HINTS
    key_func = staticmethod(printers.printer_key)
    mapping_header = "Drucker (IP, Modell, SNMP-Standort)"
    mapping_noun = "Drucker"

    def __init__(self, window):  # noqa: PLR0915 - builds the page
        Page.__init__(self, window)
        self.aps = []
        self.only_netbox = []
        self.engine = None
        self.roles = []
        self.file_name = ""
        self.scan_task = None
        self.others = []
        self._engine_args = {}
        self._compare_after_scan = False
        self._skipped = set()         # printer keys left out for this session
        self._skipped_models = set()  # model keys left out for this session

        source = Card("Scan", "Erreichbare Adressen werden per SNMP abgefragt; Geräte nur mit offenem Druckerport "
                              "(9100/IPP/LPD) erscheinen als „ohne SNMP“.")
        self.credentials_button = source.add_action(button("SNMP-Zugangsdaten …", "shield",
                                                           on_click=self.edit_credentials))
        self.load_button = source.add_action(button("Scannen", "play", "primary", on_click=self.scan))
        self.sync_button = source.add_action(button("Nach NetBox übertragen …", "upload", "primary",
                                                    on_click=self.sync))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        targets = QHBoxLayout()
        self.targets = QLineEdit()
        self.targets.setPlaceholderText("z. B. 192.168.1.1-254, 10.0.0.0/24")
        targets.addWidget(self.targets, 1)
        self.own_network = button("Eigenes Netz", "network", tooltip="Netze dieses Rechners übernehmen",
                                  on_click=self._own_network_menu)
        targets.addWidget(self.own_network)
        form.addRow("IP-Bereiche", targets)
        self.credentials_label = label("", muted=True)
        form.addRow("SNMP", self.credentials_label)
        self.role = SearchableComboBox("Geräterolle wählen …")
        self.role.setMinimumWidth(320)
        form.addRow("Geräterolle", self.role)
        site_row = QHBoxLayout()
        self.default_site = SearchableComboBox("Standort wählen …")
        self.default_site.setMinimumWidth(320)
        site_row.addWidget(self.default_site)
        site_row.addWidget(label("Für neue Drucker ohne zugeordneten Standort – NetBox verlangt für jedes Gerät "
                                 "einen.", muted=True), 1)
        form.addRow("Standard-Standort", site_row)
        options = QHBoxLayout()
        self.counters = checkbox("Seitenzähler && Verbrauchsmaterial in NetBox speichern", True,
                                 tooltip="Benutzerdefinierte Felder am Gerät (werden bei Bedarf angelegt): "
                                         "Seitenzähler, Verbrauchsmaterial, Farbdrucker, letzter Scan")
        self.rename = checkbox("Namen vorhandener Geräte aus SNMP übernehmen", False,
                               tooltip="sysName des Druckers – oft ein Werksname wie NPI1A2B3C")
        options.addWidget(self.counters)
        options.addWidget(self.rename)
        options.addStretch(1)
        form.addRow("", options)
        source.add(form)
        self.body.addWidget(source)

        result = Card("Abgleich")
        self.models_button = result.add_action(button("Modelle …", "server", on_click=self.edit_models))
        self.sites_button = result.add_action(button("Standorte && Mandanten …", "globe", on_click=self.map_sites))
        self.refresh_button = result.add_action(button("Neu abgleichen", "refresh", on_click=self.compare))
        filters = QHBoxLayout()
        self.search = SearchField("Name, Modell, S/N, MAC, IP, Standort …")
        self.only_open = checkbox("Nur neue und zu prüfende")
        filters.addWidget(self.search, 2)
        filters.addWidget(self.only_open)
        filters.addStretch(1)
        result.add(filters)
        columns = [
            Column("state", "Status", 110, value=lambda row: STATE_TEXT.get(row["state"], ""),
                   dot=lambda row: "warning" if row.get("issues") and row["state"] == "new"
                   else STATE_DOT.get(row["state"]), tooltip=lambda row: "\n".join(row.get("issues", []))),
            Column("name", "Name", 190),
            Column("ip", "IP-Adresse", 110),
            Column("manufacturer", "Hersteller", 100),
            Column("model", "Modell", 190),
            Column("serial", "Seriennummer", 130),
            Column("mac", "MAC-Adresse", 135),
            Column("page_count", "Seiten", 80,
                   value=lambda row: f"{row['page_count']:,}".replace(",", ".") if row.get("page_count") is not None
                   else "", sort=lambda row: row.get("page_count") or 0),
            Column("supplies", "Verbrauchsmaterial", 220, value=_supplies_short, tooltip=_supplies_tooltip,
                   color=lambda row: "warning" if _supplies_low(row) else None),
            Column("printer_status", "Zustand", 150, value=_status, tooltip=_status_tooltip,
                   color=lambda row: "warning" if row.get("errors") else "text_muted"),
            Column("site_hint", "Standort (SNMP)", 150),
            Column("netbox_site", "NetBox-Standort", 180,
                   color=lambda row: "warning" if row["state"] == "new" and not row.get("site_id")
                   else ("text_muted" if str(row.get("netbox_site", "")).startswith("Standard:") else None)),
            Column("tenant", "Mandant", 140),
            Column("netbox_device", "NetBox-Gerät", 160),
            Column("method", "Gefunden über", 110, color=lambda row: "text_muted"),
            Column("snmp", "SNMP", 150, value=lambda row: row.get("snmp") or ("ohne SNMP" if row.get("ip") else ""),
                   color=lambda row: "text_muted" if row.get("snmp") else "warning"),
            Column("issues", "Hinweise", 260, value=lambda row: "; ".join(row.get("issues", [])),
                   color=lambda row: "warning"),
        ]
        self.model = RowTableModel(columns, row_id=lambda row: row["key"])
        self.proxy = FilterProxy(self.model)
        self.table = Table()
        self.table.setModel(self.proxy)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.stack = QStackedWidget()
        self.stack.addWidget(EmptyState("search", "Noch nicht gescannt",
                                        "IP-Bereiche eintragen, SNMP-Zugangsdaten prüfen und „Scannen“ wählen. "
                                        "Danach wird automatisch mit NetBox abgeglichen – geschrieben wird erst nach "
                                        "der Vorschau."))
        self.stack.addWidget(self.table)
        result.add(self.stack, 1)
        self.summary = label("", muted=True)
        result.add(self.summary)
        self.body.addWidget(result, 1)

        self.search.textChanged.connect(self.proxy.set_text)
        self.only_open.toggled.connect(lambda value: self.proxy.set_predicate(
            "open", (lambda row: row["state"] in ("new", "check")) if value else None))
        self.role.activated.connect(self._role_changed)
        self.default_site.activated.connect(lambda _index: (self._store(default_site=self.default_site.valid_data()),
                                                            self._refresh()))
        self.rename.toggled.connect(lambda value: self._store(rename=value))
        self.counters.toggled.connect(lambda value: self._store(counters=value))
        self.targets.editingFinished.connect(lambda: self._store(targets=self.targets.text().strip()))
        self.targets.returnPressed.connect(self.scan)
        self.state.profile_switched.connect(self._profile_switched)
        self._profile_switched()

    # ------------------------------------------------------------------
    # Profile settings
    # ------------------------------------------------------------------

    def _credentials(self):
        if not self.state.profile:
            return []
        return self.state.profile.setdefault("snmp_credentials", [])

    def _profile_switched(self):
        self.aps, self.only_netbox, self.engine, self.roles, self.others = [], [], None, [], []
        self._skipped, self._skipped_models = set(), set()
        self.role.set_items([])
        self.default_site.set_items([])
        settings = self._settings()
        for box, key, default in ((self.rename, "rename", False), (self.counters, "counters", True)):
            box.blockSignals(True)
            box.setChecked(settings.get(key, default))
            box.blockSignals(False)
        self.targets.setText(settings.get("targets", ""))
        self._update_credentials_label()
        self._refresh()

    def _update_credentials_label(self):
        credentials = self._credentials()
        if not credentials:
            self.credentials_label.setText("Keine Zugangsdaten – es wird die Community „public“ (v2c) versucht.")
            return
        v2 = sum(1 for item in credentials if item.get("version") != "3")
        v3 = [item.get("username", "") for item in credentials if item.get("version") == "3"]
        parts = [f"{v2} SNMPv2c-Communit{'y' if v2 == 1 else 'ies'}"] if v2 else []
        if v3:
            parts.append("SNMPv3: " + ", ".join(v3))
        self.credentials_label.setText(" · ".join(parts))

    def edit_credentials(self):
        if self.state.profile is None:
            return
        dialog = SnmpCredentialsDialog(self.window, self._credentials())
        if dialog.exec():
            self.state.profile["snmp_credentials"] = dialog.result
            self.state.touch()
            self._update_credentials_label()

    def on_show(self):
        if not self.targets.text().strip():
            networks = netscan.local_networks()
            if networks:
                self.targets.setText(networks[0])
        super().on_show()

    def _own_network_menu(self):
        networks = netscan.local_networks()
        if not networks:
            self.window.toast("Kein eigenes IPv4-Netz gefunden.", "info")
            return
        menu = QMenu(self)
        for network in networks:
            menu.addAction(network, lambda n=network: self.targets.setText(n))
        if len(networks) > 1:
            menu.addSeparator()
            menu.addAction("Alle", lambda: self.targets.setText(", ".join(networks)))
        menu.exec(self.own_network.mapToGlobal(self.own_network.rect().bottomLeft()))

    def set_busy(self, busy):
        scanning = busy and self.scan_task is not None and self.tasks.current is self.scan_task
        self.load_button.setText("Stopp" if scanning else "Scannen")
        widgets.set_enabled(self.load_button, scanning or not busy, "Bitte warten …")
        for widget in (self.sites_button, self.refresh_button, self.credentials_button):
            widget.setEnabled(not busy)
        self._update_buttons()

    def _update_buttons(self):
        super()._update_buttons()
        widgets.set_enabled(self.models_button, bool(self.engine) and not self.tasks.busy, "Erst mit NetBox abgleichen.")
        widgets.set_enabled(self.refresh_button, bool(self.aps) and not self.tasks.busy, "Erst scannen.")
        widgets.set_enabled(self.sync_button, bool(self.engine) and not self.tasks.busy,
                            "Erst scannen." if not self.engine else "Bitte warten …")

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(self):
        if self.scan_task is not None and self.tasks.current is self.scan_task:
            self.tasks.cancel()
            return
        if self.tasks.busy or self.state.profile is None:
            return
        try:
            ips = netscan.expand_ips(self.targets.text())
        except netscan.ScanError as error:
            widgets.message(self.window, "Eingabe prüfen", str(error), "warning")
            return
        self._store(targets=self.targets.text().strip())
        credentials = [dict(item) for item in self._credentials()] or [{"id": "public", "version": "2c",
                                                                         "community": "public"}]
        preferred = dict(self._settings().get("snmp_hosts") or {})

        def work(ctx):
            ctx.log(f"Scanne {len(ips)} Adressen per SNMP ({len(credentials)} Zugangsdaten) …")
            found, done = [], [0]

            def on_result(host):
                done[0] += 1
                ctx.progress(done[0], len(ips), host["ip"])
                if host.get("alive"):
                    found.append(host)
            printers.scan(ips, credentials, on_result, lambda: ctx.cancelled, preferred)
            ctx.check_cancelled()
            return found

        def finished(hosts):
            items = [printers.to_import(host) for host in hosts if host.get("printer")]
            printers.mark_duplicates(items)
            self.others = [host for host in hosts if not host.get("printer") and host.get("notes")]
            remembered = dict(self._settings().get("snmp_hosts") or {})
            remembered.update({host["ip"]: host["credential_id"] for host in hosts if host.get("credential_id")})
            self._store(snmp_hosts=remembered)
            for host in self.others:
                logger("app").warning("%s: %s", host["ip"], "; ".join(host["notes"]))
            self.aps, self.only_netbox, self.engine = items, [], None
            self.file_name = ""
            without = sum(1 for item in items if not item.get("snmp"))
            text = f"{len(items)} Drucker gefunden" + (f", {without} ohne SNMP" if without else "") + (
                f", {len(self.others)} Gerät{'e' if len(self.others) != 1 else ''} mit SNMP-Problemen (siehe Protokoll)" if self.others else "")
            logger("app").log(25, "Drucker-Scan: %s", text)
            self.window.toast(text, "warning" if self.others or without else "success")
            self._refresh()
            # The scan task is still current here; the comparison starts once it has ended.
            self._compare_after_scan = self._netbox_ready() and bool(items)

        self.scan_task = self.tasks.run(work, title=f"{len(ips)} Adressen nach Druckern scannen", area="app",
                                        on_success=finished, on_error=lambda error: widgets.error(
                                            self.window, "Scan fehlgeschlagen", str(error)),
                                        on_finished=self._scan_ended)
        self.set_busy(True)

    def _scan_ended(self):
        self.scan_task = None
        self.set_busy(self.tasks.busy)
        if self._compare_after_scan:
            self._compare_after_scan = False
            self._load_lists(then_compare=True)

    # ------------------------------------------------------------------
    # NetBox
    # ------------------------------------------------------------------

    def _mapping_rows(self, new):
        """One row per printer – preset with the choice for its SNMP location, if any."""
        return [(" · ".join(filter(None, (printer["name"], printer["ip"], printer.get("model"))))
                 + (f" (SNMP-Standort: {printer['site_hint']})" if printer["site_hint"] else ""), 1,
                 ("ap", self.key_func(printer)), self._placement(printer))
                for printer in sorted(new, key=lambda item: tuple(int(part) for part in item["ip"].split(".")))]

    def _suggest_role(self, roles):
        return next((role_id for name, role_id in roles
                     if any(hint in name.casefold() for hint in self.role_hints)), None)

    def _role_changed(self, _index):
        self._store(role_id=self.role.valid_data())
        if self.engine and not self.tasks.busy:
            QTimer.singleShot(0, self.compare)  # "only in NetBox" depends on the role

    def compare(self):
        self._engine_args = {"role_id": self.role.valid_data() or self._settings().get("role_id"),
                             "counters": self.counters.isChecked()}
        super().compare()

    def _make_engine(self, nb, log):
        return printers.PrinterSyncEngine(nb, log, **self._engine_args)

    def _prepare_sync(self):
        self.engine.counters = self.counters.isChecked()
        self.engine.type_map = {key: value["device_type"] for key, value in self._model_decisions().items()
                                if value.get("action") == "map" and value.get("device_type")}
        return self.engine

    # ------------------------------------------------------------------
    # Ignoring printers and models
    # ------------------------------------------------------------------

    def _ignored(self):
        return set(self._settings().get("ignored") or [])

    def _model_decisions(self):
        return dict(self._settings().get("models") or {})

    def _model_action(self, printer):
        key = printers.model_key(printer)
        if key in self._skipped_models:
            return "skip"
        return self._model_decisions().get(key, {}).get("action")

    def _excluded(self, printer):
        key = self.key_func(printer)
        if key in self._skipped or key in self._ignored():
            return True
        # a skipped or ignored model only matters for printers that would be created
        return not printer.get("device") and self._model_action(printer) in ("skip", "ignore")

    def _missing_models(self):
        """``{model key: [printers]}`` of new printers whose device type is not in NetBox."""
        if not self.engine:
            return {}
        missing = {}
        for printer in self.aps:
            if printer.get("device") or self.key_func(printer) in self._skipped | self._ignored():
                continue
            if not self.engine.type_exists(printer):
                missing.setdefault(printers.model_key(printer), []).append(printer)
        return missing

    def edit_models(self):
        missing = self._missing_models()
        if not missing:
            self.window.toast("Alle Modelle der neuen Drucker gibt es bereits in NetBox.", "info")
            return True
        decisions = self._model_decisions()
        rows = []
        for key, members in sorted(missing.items()):
            first = members[0]
            decision = dict(decisions.get(key) or {})
            if key in self._skipped_models:
                decision["action"] = "skip"
            text = f"{first.get('manufacturer') or 'Unbekannt'} · {first.get('model') or '(kein Modell – nur Ports)'}"
            rows.append((text, len(members), key, decision, self.engine.suggest_type(first)))
        dialog = ModelMappingDialog(self.window, rows, self.engine.device_types)
        if not dialog.exec():
            return False
        for key, value in dialog.result.items():
            if value["action"] == "skip":
                self._skipped_models.add(key)
                decisions.pop(key, None)
            else:
                self._skipped_models.discard(key)
                decisions[key] = value
        self._store(models=decisions)
        self._refresh()
        return True

    def _set_ignored(self, rows, mode):
        keys = {row["key"] for row in rows if not str(row["key"]).startswith("netbox-")}
        ignored = self._ignored()
        if mode == "skip":
            self._skipped |= keys
        elif mode == "ignore":
            ignored |= keys
            self._skipped -= keys
        else:
            ignored -= keys
            self._skipped -= keys
            # also release the model of these printers
            models = {printers.model_key(row) for row in rows}
            self._skipped_models -= models
            decisions = self._model_decisions()
            for key in models:
                if decisions.get(key, {}).get("action") == "ignore":
                    decisions.pop(key)
            self._store(models=decisions)
        self._store(ignored=sorted(ignored))
        self._refresh()

    def _context_menu(self, position):
        rows = [self.proxy.row_at(index) for index in self.table.selectionModel().selectedRows()]
        rows = [row for row in rows if row and not str(row["key"]).startswith("netbox-")]
        if not rows:
            return
        count = f" ({len(rows)})" if len(rows) > 1 else ""
        menu = QMenu(self)
        menu.addAction(f"Diesmal ignorieren{count}", lambda: self._set_ignored(rows, "skip"))
        menu.addAction(f"Dauerhaft ignorieren{count}", lambda: self._set_ignored(rows, "ignore"))
        if any(row["state"] == "ignored" for row in rows):
            menu.addAction(f"Nicht mehr ignorieren{count}", lambda: self._set_ignored(rows, "release"))
        if self.engine and any(not row.get("device") for row in rows):
            menu.addSeparator()
            menu.addAction("Modell zuordnen …", self.edit_models)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def sync(self):
        if not self.engine or self.tasks.busy:
            return
        undecided = [key for key in self._missing_models()
                     if key not in self._skipped_models and key not in self._model_decisions()]
        if undecided and not self.edit_models():
            return
        super().sync()

    def _refresh(self):
        if self.engine:
            decisions = self._model_decisions()
            for printer in self.aps:
                printer["issues"] = [issue for issue in printer.get("issues", [])
                                     if not issue.startswith("Modell nicht in NetBox")]
                if printer.get("device") or self._excluded(printer) or self.engine.type_exists(printer):
                    continue
                decision = decisions.get(printers.model_key(printer), {})
                if decision.get("action") == "map":
                    name = next((f"{row['manufacturer']} {row['model']}" for row in self.engine.device_types
                                 if row["id"] == decision.get("device_type")), "?")
                    printer["issues"].append(f"Modell nicht in NetBox – wird als „{name}“ angelegt")
                elif decision.get("action") == "create":
                    printer["issues"].append("Modell nicht in NetBox – Gerätetyp wird neu angelegt")
                else:
                    printer["issues"].append("Modell nicht in NetBox – bitte unter „Modelle …“ entscheiden")
        super()._refresh()
        if self.others and self.summary.text():
            count = len(self.others)
            self.summary.setText(self.summary.text() + f" · {count} Gerät{'e' if count != 1 else ''} mit SNMP-Problemen")
