from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QStackedWidget

from netbox_sync.ui import widgets
from netbox_sync.ui.import_wizard import ImportWizard
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.widgets import Card, EmptyState, SearchField, Table, button, label


def _ips(vm):
    return ", ".join(ip.split("/")[0] for interface in vm.get("interfaces", []) for ip in interface.get("ips", []))


def _interfaces_tooltip(vm):
    lines = [f"{vm['name']} · {vm.get('guest_os') or 'unbekanntes OS'}"]
    for interface in vm.get("interfaces", []):
        ips = ", ".join(interface.get("ips", [])) or "keine IP"
        lines.append(f"  {interface['name']}  {interface.get('mac', '')}  {ips}")
    if len(lines) == 1:
        lines.append("  Keine Netzwerkschnittstellen gemeldet (VMware Tools / QEMU Agent prüfen)")
    return "\n".join(lines)


class VMsPage(Page):
    title = "Virtuelle Maschinen"
    subtitle = "VMs aus allen aktiven Quellen laden, auswählen und nach NetBox übernehmen."

    def __init__(self, window):
        super().__init__(window)
        self.file_button = self.header.add_action(button("Aus Datei laden …", "folder", on_click=self.load_file,
                                                         tooltip="VMs aus einer im Offline-Modus erstellten "
                                                                 "Exportdatei (JSON) laden."))
        self.load_button = self.header.add_action(button("VMs laden", "refresh", "primary", on_click=self.load))

        self.stack = QStackedWidget()
        self.empty = EmptyState("server", "Noch keine VMs geladen",
                                "Die Anwendung liest alle aktiven Quellen parallel aus und gleicht sie mit NetBox ab.",
                                "VMs jetzt laden", self.load,
                                secondary=("Verbindungen prüfen", lambda: window.navigate("connections")))
        self.stack.addWidget(self.empty)

        card = Card(padding=0, spacing=0)
        filters = QHBoxLayout()
        filters.setContentsMargins(16, 14, 16, 12)
        filters.setSpacing(8)
        self.search = SearchField("Name, IP-Adresse, Betriebssystem …")
        self.source_filter = QComboBox()
        self.status_filter = QComboBox()
        for text, value in (("Alle Zustände", None), ("Eingeschaltet", "active"), ("Ausgeschaltet", "offline")):
            self.status_filter.addItem(text, value)
        self.netbox_filter = QComboBox()
        for text, value in (("NetBox: alle", None), ("Nur neue", "new"), ("Nur vorhandene", "existing")):
            self.netbox_filter.addItem(text, value)
        filters.addWidget(self.search, 3)
        filters.addWidget(self.source_filter, 1)
        filters.addWidget(self.status_filter, 1)
        filters.addWidget(self.netbox_filter, 1)
        card.add(filters)

        columns = [
            Column("name", "Name", 220, tooltip=_interfaces_tooltip),
            Column("netbox", "NetBox", 95, value=self._netbox_state,
                   color=lambda row: "accent" if self._netbox_state(row) == "Neu" else "text_muted"),
            Column("source", "Quelle", 140),
            Column("cluster", "Cluster", 140, tooltip=lambda row: f"Hypervisor-Cluster: {row.get('cluster') or '–'}"),
            Column("kind", "Typ", 55),
            Column("status", "Status", 135, value=lambda row: "Eingeschaltet" if row["status"] == "active" else "Aus",
                   dot=lambda row: "success" if row["status"] == "active" else "text_faint"),
            Column("vcpus", "vCPU", 55, align="right"),
            Column("memory", "RAM", 75, align="right", value=lambda row: f"{row['memory'] / 1024:g} GB",
                   sort=lambda row: row["memory"]),
            Column("disk", "Disk", 75, align="right",
                   value=lambda row: f"{row['disk'] / 1024:.0f} GB" if row.get("disk") else "–",
                   sort=lambda row: row.get("disk") or 0),
            Column("ips", "IP-Adressen", 200, value=_ips),
            Column("guest_os", "Betriebssystem", 220),
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
        self.import_button = button("Importieren …", "upload", "primary", on_click=self.start_import)
        footer.addWidget(self.import_button)
        card.add(footer)
        self.stack.addWidget(card)
        self.body.addWidget(self.stack, 1)

        self.search.textChanged.connect(self.proxy.set_text)
        self.source_filter.currentIndexChanged.connect(self._apply_filters)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.netbox_filter.currentIndexChanged.connect(self._apply_filters)
        self.model.checked_changed.connect(self._update_selection)
        self.state.vms_changed.connect(self.refresh)
        self.state.profile_switched.connect(self.refresh)
        self.state.settings_changed.connect(self.refresh)
        self.state.netbox_changed.connect(self.refresh)
        self.refresh()

    def _netbox_state(self, vm):
        engine = self.state.engine
        if engine is None:
            return ""
        return "Vorhanden" if vm["name"] in engine.existing_vms else "Neu"

    def refresh(self):
        offline = self.state.offline
        self.table.setColumnHidden(1, offline)
        self.netbox_filter.setVisible(not offline)
        if offline and self.netbox_filter.currentIndex():
            self.netbox_filter.setCurrentIndex(0)
        vms = self.state.vms
        self.stack.setCurrentIndex(1 if vms else 0)
        current = self.source_filter.currentData()
        self.source_filter.blockSignals(True)
        self.source_filter.clear()
        self.source_filter.addItem("Alle Quellen", None)
        for name in sorted({vm["source"] for vm in vms}, key=str.casefold):
            self.source_filter.addItem(name, name)
        self.source_filter.setCurrentIndex(max(0, self.source_filter.findData(current)))
        self.source_filter.blockSignals(False)
        self.model.set_rows(vms)
        self._apply_filters()
        self.load_button.setText("Neu laden" if vms else "VMs laden")
        if not vms:
            if offline and not self.state.sources:
                self.empty.set_text(text="Offline-Modus: Unter „Verbindungen“ eine VMware- oder Proxmox-Quelle "
                                         "einrichten oder eine Exportdatei laden.")
            elif offline:
                self.empty.set_text(text="Offline-Modus: Die Quellen werden ohne NetBox ausgelesen; ausgewählte VMs "
                                         "lassen sich anschließend als JSON-Datei exportieren.")
            elif not self.state.netbox_configured():
                self.empty.set_text(text="Zuerst unter „Verbindungen“ NetBox und mindestens eine Quelle einrichten.")
            elif not self.state.sources:
                self.empty.set_text(text="Es ist noch keine VMware- oder Proxmox-Quelle hinterlegt.")
            else:
                self.empty.set_text(text="Die Anwendung liest alle aktiven Quellen parallel aus und gleicht sie "
                                         "mit NetBox ab.")

    def _apply_filters(self, *_args):
        source = self.source_filter.currentData()
        status = self.status_filter.currentData()
        netbox_state = self.netbox_filter.currentData()
        self.proxy.set_predicate("source", (lambda row: row["source"] == source) if source else None)
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
        self.model.set_checked({row["id"] for row in self.proxy.visible_rows() if self._netbox_state(row) == "Neu"})

    def clear_selection(self):
        self.model.set_checked(set(self.model.checked), False)

    def _update_selection(self):
        count = len(self.model.checked)
        visible = self.proxy.rowCount()
        total = len(self.model.rows)
        shown = f"{visible} von {total} angezeigt" if visible != total else f"{total} VMs"
        self.selection_label.setText(f"{shown} · {count} ausgewählt")
        verb = "exportieren" if self.state.offline else "importieren"
        self.import_button.setText(f"{count} VM(s) {verb} …" if count else f"{verb.capitalize()} …")
        self._update_import_button()

    def _update_import_button(self):
        count = len(self.model.checked)
        ready = self.state.offline or self.state.engine is not None
        enabled = count > 0 and not self.tasks.busy and ready
        reason = ("Bitte zuerst VMs in der Tabelle anhaken." if not count else
                  "Bitte warten …" if self.tasks.busy else "Keine Verbindung zu NetBox – „Neu laden“ wählen.")
        widgets.set_enabled(self.import_button, enabled, reason)

    def set_busy(self, busy):
        self.load_button.setEnabled(not busy)
        self.file_button.setEnabled(not busy)
        self._update_import_button()

    def load(self):
        self.actions.load_vms()

    def load_file(self):
        self.actions.import_vms_file()

    def start_import(self):
        vms = self.model.checked_rows()
        if vms and self.state.offline:
            self.actions.export_file(vms=vms)
            return
        if not vms or self.state.engine is None:
            return
        wizard = ImportWizard(self.window, self.state, self.tasks, vms)
        wizard.exec()
        if wizard.result_summary:
            text, kind = wizard.result_summary
            self.window.toast(text, kind)
            self.model.refresh()
            self._apply_filters()
