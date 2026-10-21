import time
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QMenu, QSplitter, QStackedWidget

from netbox_sync.core.switches import PLATFORMS
from netbox_sync.ui import widgets
from netbox_sync.ui.dialogs import AddSwitchesDialog, RawDataDialog, SwitchDialog
from netbox_sync.ui.icons import icon
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Card, EmptyState, SearchField, Table, button, checkbox, label


def _speed(value):
    if not value:
        return ""
    return f"{value / 1000:g} Gbit/s" if value >= 1000 else f"{value} Mbit/s"


class SwitchesPage(Page):
    title = "Switche"
    subtitle = ("Bestehende NetBox-Geräte per SSH auslesen und Schnittstellen, IP-Adressen und LLDP/CDP-Verkabelung "
                "dokumentieren. Unterstützt Aruba CX, ArubaOS-Switch/ProCurve und HPE Comware.")

    def __init__(self, window):
        super().__init__(window)
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # Switch list -------------------------------------------------------
        list_card = Card("Switch-Zugänge", "Haken setzen, um Switche auszulesen oder zu synchronisieren.")
        self.add_button = list_card.add_action(button("Switche hinzufügen …", "plus", on_click=self.add_switches))
        self.discover_button = list_card.add_action(button("Auslesen", "play", "primary", on_click=self.discover))
        columns = [
            Column("name", "NetBox-Gerät", 200),
            Column("platform", "Plattform", 180, value=lambda row: PLATFORMS.get(row.get("platform"), row.get("platform"))),
            Column("host", "SSH-Host", 150, value=lambda row: f"{row.get('host', '')}:{row.get('port', 22)}"),
            Column("enabled", "Aktiv", 70, value=lambda row: "ja" if row.get("enabled", True) else "nein",
                   color=lambda row: None if row.get("enabled", True) else "text_faint"),
            Column("status", "Letztes Auslesen", 300, value=self._status_text, dot=self._status_dot,
                   tooltip=self._status_text),
        ]
        self.switch_model = RowTableModel(columns, checkable=True, row_id=lambda row: row["id"])
        self.switch_proxy = FilterProxy(self.switch_model)
        self.switch_table = Table()
        self.switch_table.setModel(self.switch_proxy)
        for index, column in enumerate(columns):
            self.switch_table.setColumnWidth(index, column.width)
        self.switch_table.doubleClicked.connect(lambda _index: self.edit_switch())
        self.switch_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.switch_table.customContextMenuRequested.connect(self._context_menu)
        self.switch_stack = QStackedWidget()
        self.switch_empty = EmptyState("network", "Noch keine Switche im Profil",
                                       "Wähle vorhandene Geräte aus NetBox aus und hinterlege den SSH-Zugang – "
                                       "mehrere Switche lassen sich in einem Schritt hinzufügen.",
                                       "Switche hinzufügen", self.add_switches)
        self.switch_stack.addWidget(self.switch_empty)
        self.switch_stack.addWidget(self.switch_table)
        list_card.add(self.switch_stack, 1)
        toolbar = QHBoxLayout()
        self.select_all_button = button("Alle auswählen", variant="ghost", on_click=self.select_all)
        self.edit_button = button("Bearbeiten", "pencil", on_click=self.edit_switch)
        self.remove_button = button("Entfernen", "trash", "danger", on_click=self.remove_switches)
        toolbar.addWidget(self.select_all_button)
        toolbar.addStretch(1)
        self.lldp_option = checkbox("LLDP bei Bedarf aktivieren",
                                    tooltip="Ist LLDP auf einem Switch global deaktiviert, wird es per Konfigurations"
                                            "befehl eingeschaltet. Das ändert die laufende Konfiguration des Switches!")
        toolbar.addWidget(self.lldp_option)
        toolbar.addSpacing(12)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.remove_button)
        list_card.add(toolbar)
        splitter.addWidget(list_card)

        # Results -------------------------------------------------------------
        result_card = Card("Ergebnisse", "Erkannte Schnittstellen und Nachbarn der ausgelesenen Switche.")
        self.file_button = result_card.add_action(button("Aus Datei laden …", "folder", on_click=self.load_file,
                                                         tooltip="Ausleseergebnisse aus einer im Offline-Modus "
                                                                 "erstellten Exportdatei (JSON) laden."))
        self.raw_button = result_card.add_action(button("SSH-Rohdaten", "terminal", on_click=self.show_raw))
        self.sync_button = result_card.add_action(button("Nach NetBox übertragen …", "upload", "primary",
                                                         on_click=self.sync))
        filters = QHBoxLayout()
        self.search = SearchField("Schnittstelle, IP, Nachbar …")
        self.switch_filter = QComboBox()
        self.switch_filter.setMinimumWidth(200)
        self.only_neighbors = checkbox("Nur mit Nachbar")
        self.only_up = checkbox("Nur aktive Ports")
        self.cables_option = checkbox("Kabel aus LLDP/CDP anlegen",
                                      tooltip="Legt Kabel zwischen Schnittstellen an, deren Gegenstelle in NetBox "
                                              "eindeutig gefunden wird. Bestehende Kabel werden nie verändert.")
        filters.addWidget(self.search, 2)
        filters.addWidget(self.switch_filter, 1)
        filters.addWidget(self.only_up)
        filters.addWidget(self.only_neighbors)
        filters.addStretch(1)
        filters.addWidget(self.cables_option)
        result_card.add(filters)
        result_columns = [
            Column("switch", "Switch", 140),
            Column("name", "Schnittstelle", 150),
            Column("state", "Status", 125, value=lambda row: ("Up" if row["connected"] else "Down")
                   if row["enabled"] else "Deaktiviert",
                   dot=lambda row: "success" if row["connected"] else ("text_faint" if row["enabled"] else "danger")),
            Column("speed", "Geschwindigkeit", 115, value=lambda row: _speed(row.get("speed_mbps")),
                   sort=lambda row: row.get("speed_mbps") or 0),
            Column("ips", "IP-Adressen", 160, value=lambda row: ", ".join(row.get("ip_addresses", []))),
            Column("neighbor", "Nachbar", 170, value=lambda row: (row.get("neighbor") or {}).get("system_name", "")),
            Column("remote_port", "Nachbar-Port", 110, value=lambda row: (row.get("neighbor") or {}).get("remote_port", "")),
            Column("protocol", "Quelle", 70, value=lambda row: (row.get("neighbor") or {}).get("protocol", ""),
                   color=lambda row: "text_muted"),
        ]
        self.result_model = RowTableModel(result_columns)
        self.result_proxy = FilterProxy(self.result_model)
        self.result_table = Table()
        self.result_table.setModel(self.result_proxy)
        for index, column in enumerate(result_columns):
            self.result_table.setColumnWidth(index, column.width)
        self.result_stack = QStackedWidget()
        self.result_empty = EmptyState("terminal", "Noch nichts ausgelesen",
                                       "Switche oben anhaken und „Auslesen“ wählen. Mehrere Switche werden parallel "
                                       "abgefragt.")
        self.result_stack.addWidget(self.result_empty)
        self.result_stack.addWidget(self.result_table)
        result_card.add(self.result_stack, 1)
        self.result_summary = label("", muted=True)
        result_card.add(self.result_summary)
        splitter.addWidget(result_card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.body.addWidget(splitter, 1)

        self.search.textChanged.connect(self.result_proxy.set_text)
        self.switch_filter.currentIndexChanged.connect(self._apply_result_filters)
        self.only_neighbors.toggled.connect(self._apply_result_filters)
        self.only_up.toggled.connect(self._apply_result_filters)
        self.switch_model.checked_changed.connect(self._update_buttons)
        self.switch_table.selectionModel().selectionChanged.connect(self._update_buttons)
        self.lldp_option.toggled.connect(lambda value: self.state.update_settings(enable_lldp=value))
        self.cables_option.toggled.connect(lambda value: self.state.update_settings(create_cables=value))
        self.state.switches_changed.connect(self.refresh)
        self.state.profile_switched.connect(self.refresh)
        self.state.profile_edited.connect(self.refresh_switches)
        self.state.settings_changed.connect(self._load_settings)
        self.state.settings_changed.connect(self._update_buttons)
        self._load_settings()
        self.refresh()

    # ------------------------------------------------------------------

    def _load_settings(self):
        for widget, key in ((self.lldp_option, "enable_lldp"), (self.cables_option, "create_cables")):
            widget.blockSignals(True)
            widget.setChecked(bool(self.state.settings.get(key)))
            widget.blockSignals(False)

    def _status_text(self, row):
        status = self.state.switch_status.get(row["id"])
        if not status:
            return "–"
        snapshot = self.state.switch_snapshots.get(row["id"])
        when = time.strftime("%H:%M", time.localtime(snapshot["discovered_at"])) if snapshot else ""
        return f"{when} · {status[1]}" if when else status[1]

    def _status_dot(self, row):
        status = self.state.switch_status.get(row["id"])
        if not status:
            return None
        return "success" if status[0] == "ok" else "danger"

    def refresh(self):
        self.refresh_switches()
        self.refresh_results()

    def refresh_switches(self):
        switches = self.state.switches
        self.switch_model.set_rows(switches)
        self.switch_stack.setCurrentIndex(1 if switches else 0)
        for widget in (self.select_all_button, self.edit_button, self.remove_button, self.lldp_option):
            widget.setVisible(bool(switches))
        self._update_buttons()

    def refresh_results(self):
        by_id = {switch["id"]: switch for switch in self.state.switches}
        rows = []
        for switch_id, snapshot in self.state.switch_snapshots.items():
            switch = by_id.get(switch_id)
            if not switch:
                continue
            for interface in snapshot["interfaces"]:
                rows.append(dict(interface, switch=switch["name"], switch_id=switch_id))
        self.result_model.set_rows(rows)
        self.result_stack.setCurrentIndex(1 if rows else 0)
        current = self.switch_filter.currentData()
        self.switch_filter.blockSignals(True)
        self.switch_filter.clear()
        self.switch_filter.addItem("Alle Switche", None)
        for switch_id in self.state.switch_snapshots:
            if switch_id in by_id:
                self.switch_filter.addItem(by_id[switch_id]["name"], switch_id)
        self.switch_filter.setCurrentIndex(max(0, self.switch_filter.findData(current)))
        self.switch_filter.blockSignals(False)
        self._apply_result_filters()

    def _apply_result_filters(self, *_args):
        switch_id = self.switch_filter.currentData()
        self.result_proxy.set_predicate("switch", (lambda row: row["switch_id"] == switch_id) if switch_id else None)
        self.result_proxy.set_predicate("neighbor", (lambda row: bool(row.get("neighbor")))
                                        if self.only_neighbors.isChecked() else None)
        self.result_proxy.set_predicate("up", (lambda row: row.get("connected"))
                                        if self.only_up.isChecked() else None)
        rows = self.result_proxy.visible_rows()
        neighbors = sum(1 for row in rows if row.get("neighbor"))
        up = sum(1 for row in rows if row.get("connected"))
        self.result_summary.setText(f"{len(rows)} Schnittstellen · {up} aktiv · {neighbors} mit Nachbar")
        self._update_buttons()

    def _checked(self):
        return self.switch_model.checked_rows()

    def _selected(self):
        rows = self.switch_table.selectionModel().selectedRows()
        return [self.switch_proxy.row_at(index) for index in rows]

    def _targets(self):
        """Checked switches, or the selected rows when nothing is checked."""
        return self._checked() or self._selected()

    def _update_buttons(self, *_args):
        busy = self.tasks.busy
        targets = self._targets()
        with_data = [switch for switch in targets if switch["id"] in self.state.switch_snapshots]
        widgets.set_enabled(self.discover_button, bool(targets) and not busy,
                            "Bitte Switche anhaken." if not targets else "Bitte warten …")
        self.discover_button.setText(f"{len(targets)} auslesen" if targets else "Auslesen")
        widgets.set_enabled(self.sync_button, bool(with_data) and not busy,
                            "Bitte zuerst Switche auslesen." if not with_data else "Bitte warten …")
        offline = self.state.offline
        if offline:
            self.sync_button.setText(f"{len(with_data)} exportieren …" if with_data else "Exportieren …")
        else:
            self.sync_button.setText(f"{len(with_data)} nach NetBox übertragen …" if with_data
                                     else "Nach NetBox übertragen …")
        self.cables_option.setVisible(not offline)
        widgets.set_enabled(self.raw_button, bool(self.state.switch_snapshots), "Noch nichts ausgelesen.")
        widgets.set_enabled(self.edit_button, len(self._selected()) == 1, "Bitte genau einen Switch markieren.")
        widgets.set_enabled(self.remove_button, bool(targets), "Bitte Switche anhaken oder markieren.")
        self.add_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)

    def set_busy(self, busy):
        self._update_buttons()

    def select_all(self):
        rows = self.switch_model.rows
        all_checked = len(self.switch_model.checked) == len(rows)
        self.switch_model.set_checked({row["id"] for row in rows}, not all_checked)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def add_switches(self):
        if self.state.profile is None:
            return

        def open_dialog(devices):
            existing = {switch.get("device_id") for switch in self.state.switches}
            dialog = AddSwitchesDialog(self.window, devices, existing)
            if dialog.exec() and dialog.result:
                self.state.switches.extend(dialog.result)
                self.state.touch()
                self.refresh_switches()
                self.switch_model.set_checked({switch["id"] for switch in dialog.result})
                self.window.toast(f"{len(dialog.result)} Switch(e) hinzugefügt – jetzt „Auslesen“ wählen")

        if self.state.offline:
            dialog = SwitchDialog(self.window, {"id": uuid.uuid4().hex, "platform": "auto", "port": 22,
                                                "enabled": True})
            if dialog.exec() and dialog.result:
                self.state.switches.append(dialog.result)
                self.state.touch()
                self.refresh_switches()
                self.switch_model.set_checked({dialog.result["id"]})
                self.window.toast(f"{dialog.result['name']} hinzugefügt – jetzt „Auslesen“ wählen")
        elif self.state.netbox_devices is not None:
            open_dialog(self.state.netbox_devices)
        else:
            self.actions.load_devices(open_dialog)

    def edit_switch(self):
        selected = self._selected()
        if len(selected) != 1:
            return
        current = selected[0]
        dialog = SwitchDialog(self.window, current, self.state.netbox_devices)
        if dialog.exec() and dialog.result:
            index = self.state.switches.index(current)
            self.state.switches[index] = dialog.result
            if dialog.result.get("device_id") != current.get("device_id"):
                self.state.switch_snapshots.pop(current["id"], None)
                self.state.switch_status.pop(current["id"], None)
            self.state.touch()
            self.refresh()

    def remove_switches(self):
        targets = self._targets()
        if not targets:
            return
        names = ", ".join(switch["name"] for switch in targets[:5]) + (" …" if len(targets) > 5 else "")
        if not widgets.confirm(self.window, "Switche entfernen",
                               f"{len(targets)} Switch-Zugang/Zugänge aus dem Profil entfernen?\n{names}\n\n"
                               "Die Geräte in NetBox bleiben unverändert.", ok_text="Entfernen", destructive=True):
            return
        ids = {switch["id"] for switch in targets}
        self.state.profile["switches"] = [switch for switch in self.state.switches if switch["id"] not in ids]
        for switch_id in ids:
            self.state.switch_snapshots.pop(switch_id, None)
            self.state.switch_status.pop(switch_id, None)
        self.state.touch()
        self.refresh()

    def discover(self):
        targets = self._targets()
        if not targets:
            return
        if self.lldp_option.isChecked() and not widgets.confirm(
                self.window, "Switch-Konfiguration ändern?",
                "„LLDP bei Bedarf aktivieren“ ist eingeschaltet. Ist LLDP auf einem Switch deaktiviert, wird es per "
                "Konfigurationsbefehl aktiviert (ohne „write memory“).", ok_text="Auslesen", destructive=True):
            return
        self.actions.discover_switches(targets)

    def load_file(self):
        self.actions.import_switches_file()

    def sync(self):
        if self.state.offline:
            pairs = [(switch, self.state.switch_snapshots[switch["id"]]) for switch in self._targets()
                     if switch["id"] in self.state.switch_snapshots]
            if pairs:
                self.actions.export_file(switches=pairs)
            return
        targets = [switch["id"] for switch in self._targets() if switch["id"] in self.state.switch_snapshots]
        self.actions.sync_switches(targets, self.cables_option.isChecked())

    def show_raw(self):
        by_id = {switch["id"]: switch for switch in self.state.switches}
        items = [(by_id[key], snapshot) for key, snapshot in self.state.switch_snapshots.items() if key in by_id]
        if not items:
            return
        selected = self._selected()
        RawDataDialog(self.window, items, selected[0]["id"] if selected else None).exec()

    def _context_menu(self, position):
        if not self._selected():
            return
        muted = theme.color("text_muted")
        menu = QMenu(self)
        menu.addAction(icon("play", muted), "Auslesen", self.discover)
        menu.addAction(icon("pencil", muted), "Bearbeiten …", self.edit_switch)
        if any(switch["id"] in self.state.switch_snapshots for switch in self._selected()):
            menu.addAction(icon("terminal", muted), "SSH-Rohdaten …", self.show_raw)
        menu.addSeparator()
        menu.addAction(icon("trash", theme.color("danger")), "Entfernen", self.remove_switches)
        menu.exec(self.switch_table.viewport().mapToGlobal(position))
