"""Step-by-step VM import: target → networks → review → run."""

import ipaddress
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from netbox_sync.core.vm_sync import prefix_choices, vrf_label
from netbox_sync.ui.icons import icon
from netbox_sync.ui.tasks import log_bus
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Banner, SearchableComboBox, button, checkbox, hline, label, repolish

NEW_VRF = "__new__"
GLOBAL_VRF = "__global__"
STEPS = ("Ziel", "Netzwerke", "Prüfen", "Import")


def _vrf_items(engine, tenant_id):
    items = [(vrf_label(vrf), vrf.id) for vrf in engine.tenant_vrfs(tenant_id)]
    items.append(("Global (ohne VRF)", GLOBAL_VRF))
    items.append(("➕ Neue VRF anlegen …", NEW_VRF))
    return items


class ImportWizard(QDialog):
    def __init__(self, parent, state, tasks, vms):
        super().__init__(parent)
        self.state = state
        self.tasks = tasks
        self.engine = state.engine
        self.vms = vms
        self.missing_networks = []
        self.network_rows = []
        self.plan = None
        self.running = False
        self.finished_import = False
        self.result_summary = None
        self._vrf_chosen = False
        self.target = None
        self.setWindowTitle("VMs nach NetBox importieren")
        self.resize(980, 680)
        self.setMinimumSize(820, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("Card")
        header.setStyleSheet("#Card { border-radius: 0; border-left: none; border-right: none; border-top: none; }")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 18, 28, 18)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(label("VMs nach NetBox importieren", object_name="PageTitle"))
        new, existing = self.engine.classify_vms(vms)
        title_box.addWidget(label(f"{len(vms)} VM(s) ausgewählt · {new} neu · {existing} bereits in NetBox", muted=True))
        header_layout.addLayout(title_box, 1)
        self.step_labels = []
        for index, title in enumerate(STEPS):
            if index:
                arrow = QLabel()
                arrow.setPixmap(icon("chevron-right", theme.color("text_faint")).pixmap(14, 14))
                header_layout.addWidget(arrow)
            step = label(f"{index + 1}  {title}", object_name="WizardStep")
            self.step_labels.append(step)
            header_layout.addWidget(step)
        root.addWidget(header)

        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        body = QVBoxLayout()
        body.setContentsMargins(28, 22, 28, 12)
        body.addWidget(self.pages, 1)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(28, 12, 28, 20)
        self.back_button = button("Zurück", "arrow-left", on_click=self.go_back)
        self.cancel_button = button("Abbrechen", variant="ghost", on_click=self.reject)
        self.next_button = button("Weiter", "arrow-right", "primary", on_click=self.go_next)
        self.next_button.setLayoutDirection(Qt.RightToLeft)
        footer.addWidget(self.cancel_button)
        footer.addStretch(1)
        footer.addWidget(self.back_button)
        footer.addWidget(self.next_button)
        root.addLayout(footer)

        self.pages.addWidget(self._build_target_page())
        self.pages.addWidget(self._build_network_page())
        self.pages.addWidget(self._build_review_page())
        self.pages.addWidget(self._build_run_page())
        self._show_page(0)

    # ------------------------------------------------------------------
    # Page 1 – target
    # ------------------------------------------------------------------

    def _build_target_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(label("Wohin sollen die VMs?", object_name="CardTitle"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.cluster = SearchableComboBox("Cluster suchen …")
        self.cluster.set_items(sorted(self.engine.clusters, key=str.casefold))
        self.cluster.setMinimumWidth(420)
        form.addRow("NetBox-Cluster", self.cluster)

        self.tenant = SearchableComboBox("Mandant suchen …")
        self.tenant.set_items(sorted(self.engine.tenants, key=str.casefold))
        self.tenant_hint = label("", muted=True, wrap=True)
        tenant_box = QVBoxLayout()
        tenant_box.setSpacing(4)
        tenant_box.addWidget(self.tenant)
        tenant_box.addWidget(self.tenant_hint)
        form.addRow("Mandant", tenant_box)

        self.vrf = QComboBox()
        self.new_vrf_name = QLineEdit()
        self.new_vrf_name.setPlaceholderText("Name der neuen VRF")
        vrf_box = QVBoxLayout()
        vrf_box.setSpacing(6)
        vrf_box.addWidget(self.vrf)
        vrf_box.addWidget(self.new_vrf_name)
        form.addRow("VRF für IP-Adressen", vrf_box)
        layout.addLayout(form)

        layout.addWidget(Banner(
            "Der Mandant wird an VMs, IP-Adressen und Präfixen gesetzt. Hat der Cluster in NetBox einen Mandanten, "
            "ist dieser vorausgewählt.", "info"))
        self.target_error = Banner("", "error")
        self.target_error.hide()
        layout.addWidget(self.target_error)
        layout.addStretch(1)

        self.cluster.currentIndexChanged.connect(self._cluster_changed)
        self.tenant.currentIndexChanged.connect(self._tenant_changed)
        self.vrf.currentIndexChanged.connect(self._vrf_changed)
        self.vrf.activated.connect(lambda _index: setattr(self, "_vrf_chosen", True))
        self.cluster.lineEdit().editingFinished.connect(self._cluster_changed)
        self._tenant_changed()
        return page

    def _cluster_changed(self, *_args):
        cluster = self.cluster.valid_data()
        if not cluster:
            return
        inherited = self.engine.cluster_tenants.get(cluster)
        if inherited:
            self.tenant.select_data(inherited["name"])
            self.tenant_hint.setText(f"Vom Cluster „{cluster}“ übernommen.")
        else:
            self.tenant_hint.setText(f"Der Cluster „{cluster}“ hat keinen Mandanten – bitte auswählen.")
        self._tenant_changed()

    def _tenant_changed(self, *_args):
        tenant = self.tenant.valid_data()
        tenant_id = self.engine.tenants.get(tenant) if tenant else None
        current = self.vrf.currentData()
        self.vrf.blockSignals(True)
        self.vrf.clear()
        for text, data in _vrf_items(self.engine, tenant_id):
            self.vrf.addItem(text, data)
        index = self.vrf.findData(current) if self._vrf_chosen else -1
        self.vrf.setCurrentIndex(index if index >= 0 else 0)
        self.vrf.blockSignals(False)
        if tenant and not self.new_vrf_name.isModified():
            cluster = self.cluster.valid_data() or "VMs"
            self.new_vrf_name.setText(f"{tenant} - {cluster}")
        self._vrf_changed()

    def _vrf_changed(self, *_args):
        self.new_vrf_name.setVisible(self.vrf.currentData() == NEW_VRF)

    def _target(self):
        cluster = self.cluster.valid_data()
        tenant = self.tenant.valid_data()
        vrf = self.vrf.currentData()
        if not cluster:
            return None, "Bitte einen NetBox-Cluster auswählen."
        if not tenant:
            return None, "Bitte einen Mandanten auswählen."
        if vrf == NEW_VRF and not self.new_vrf_name.text().strip():
            return None, "Bitte einen Namen für die neue VRF eingeben."
        return {
            "cluster": cluster,
            "cluster_id": self.engine.clusters[cluster],
            "tenant": tenant,
            "tenant_id": self.engine.tenants[tenant],
            "vrf_mode": "new" if vrf == NEW_VRF else ("global" if vrf == GLOBAL_VRF else "existing"),
            "vrf_id": vrf if isinstance(vrf, int) else None,
            "vrf_name": self.new_vrf_name.text().strip() if vrf == NEW_VRF else self.vrf.currentText(),
        }, None

    # ------------------------------------------------------------------
    # Page 2 – networks
    # ------------------------------------------------------------------

    def _build_network_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(label("Neue Netzwerke", object_name="CardTitle"))
        self.network_info = label("", muted=True, wrap=True)
        layout.addWidget(self.network_info)

        bulk = QHBoxLayout()
        bulk.addWidget(label("VRF für alle markierten Netze:", muted=True))
        self.bulk_vrf = QComboBox()
        self.bulk_vrf.setMinimumWidth(260)
        bulk.addWidget(self.bulk_vrf)
        bulk.addWidget(button("Übernehmen", on_click=self._apply_bulk_vrf))
        bulk.addStretch(1)
        self.bulk_row = QWidget()
        self.bulk_row.setLayout(bulk)
        layout.addWidget(self.bulk_row)

        self.network_table = QTableWidget(0, 5)
        self.network_table.setHorizontalHeaderLabels(["Anlegen", "Netzwerk", "Größe", "VRF", "Gefunden bei"])
        self.network_table.verticalHeader().setVisible(False)
        self.network_table.verticalHeader().setDefaultSectionSize(44)
        self.network_table.setShowGrid(False)
        self.network_table.setAlternatingRowColors(True)
        self.network_table.setSelectionMode(QTableWidget.NoSelection)
        self.network_table.setFocusPolicy(Qt.NoFocus)
        header = self.network_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.network_table.setColumnWidth(0, 76)
        self.network_table.setColumnWidth(1, 190)
        self.network_table.setColumnWidth(2, 90)
        self.network_table.setColumnWidth(3, 330)
        layout.addWidget(self.network_table, 1)
        self.no_networks = Banner("Alle erkannten Netzwerke sind bereits in NetBox vorhanden – es müssen keine "
                                  "Präfixe angelegt werden.", "success")
        layout.addWidget(self.no_networks)
        self.network_error = Banner("", "error")
        self.network_error.hide()
        layout.addWidget(self.network_error)
        return page

    def _fill_networks(self, missing):
        target = self.target
        self.missing_networks = missing
        self.network_rows = []
        self.network_table.setRowCount(0)
        has_rows = bool(missing)
        self.network_table.setVisible(has_rows)
        self.bulk_row.setVisible(has_rows)
        self.no_networks.setVisible(not has_rows)
        self.network_info.setText(
            "Für diese IP-Adressen gibt es in NetBox noch kein passendes Präfix. Größe und VRF prüfen; "
            "nicht markierte Netze werden nicht angelegt." if has_rows else ""
        )
        vrf_items = _vrf_items(self.engine, target["tenant_id"])
        if target["vrf_mode"] == "new":
            vrf_items.insert(0, (f"{target['vrf_name']} (neu)", "__target_new__"))
        default = {"new": "__target_new__", "global": GLOBAL_VRF}.get(target["vrf_mode"], target["vrf_id"])
        self.bulk_vrf.clear()
        for text, data in vrf_items:
            self.bulk_vrf.addItem(text, data)
        self.bulk_vrf.setCurrentIndex(max(0, self.bulk_vrf.findData(default)))

        for row_index, item in enumerate(missing):
            self.network_table.insertRow(row_index)
            enabled = checkbox("")
            enabled.setChecked(True)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(18, 0, 0, 0)
            holder_layout.addWidget(enabled)
            self.network_table.setCellWidget(row_index, 0, holder)
            network_item = QTableWidgetItem()
            self.network_table.setItem(row_index, 1, network_item)
            size = QComboBox()
            for length in prefix_choices(item["host_address"], item["detected_prefixlen"]):
                size.addItem(f"/{length}", length)
            size.setCurrentIndex(max(0, size.findData(item["suggested_prefixlen"])))
            self.network_table.setCellWidget(row_index, 2, size)
            vrf_widget = QWidget()
            vrf_layout = QHBoxLayout(vrf_widget)
            vrf_layout.setContentsMargins(0, 3, 6, 3)
            vrf_layout.setSpacing(6)
            vrf = QComboBox()
            for text, data in vrf_items:
                vrf.addItem(text, data)
            vrf.setCurrentIndex(max(0, vrf.findData(default)))
            new_name = QLineEdit(f"{target['tenant']} - {item['suggested_network'].network_address}")
            new_name.setPlaceholderText("Name der neuen VRF")
            vrf_layout.addWidget(vrf, 1)
            vrf_layout.addWidget(new_name, 1)
            self.network_table.setCellWidget(row_index, 3, vrf_widget)
            vms = sorted(item["vms"], key=str.casefold)
            found = QTableWidgetItem(", ".join(vms[:4]) + (f" +{len(vms) - 4}" if len(vms) > 4 else ""))
            found.setToolTip("\n".join(vms))
            self.network_table.setItem(row_index, 4, found)
            row = {"item": item, "enabled": enabled, "size": size, "vrf": vrf, "new_name": new_name,
                   "network_item": network_item}
            self.network_rows.append(row)
            size.currentIndexChanged.connect(lambda _i, r=row: self._update_network_row(r))
            vrf.currentIndexChanged.connect(lambda _i, r=row: self._update_network_row(r))
            enabled.toggled.connect(lambda _c, r=row: self._update_network_row(r))
            self._update_network_row(row)

    def _update_network_row(self, row):
        network = ipaddress.ip_network(f"{row['item']['host_address']}/{row['size'].currentData()}", strict=False)
        row["network"] = str(network)
        row["network_item"].setText(str(network))
        row["new_name"].setVisible(row["vrf"].currentData() == NEW_VRF)
        active = row["enabled"].isChecked()
        for widget in (row["size"], row["vrf"], row["new_name"]):
            widget.setEnabled(active)

    def _apply_bulk_vrf(self):
        data = self.bulk_vrf.currentData()
        for row in self.network_rows:
            if row["enabled"].isChecked():
                row["vrf"].setCurrentIndex(max(0, row["vrf"].findData(data)))

    def _network_plan(self):
        decisions = {}
        for row in self.network_rows:
            if not row["enabled"].isChecked():
                continue
            data = row["vrf"].currentData()
            network = row["network"]
            if data == NEW_VRF:
                name = row["new_name"].text().strip()
                if not name:
                    return None, f"Bitte für {network} einen VRF-Namen eingeben."
                decision = {"network": network, "vrf_id": None, "create_vrf": True, "vrf_name": name}
            elif data == "__target_new__":
                decision = {"network": network, "vrf_id": None, "create_vrf": True, "vrf_name": self.target["vrf_name"]}
            elif data == GLOBAL_VRF:
                decision = {"network": network, "vrf_id": None, "create_vrf": False, "vrf_name": None}
            else:
                decision = {"network": network, "vrf_id": data, "create_vrf": False, "vrf_name": None}
            existing = decisions.get(network)
            if existing and existing != decision:
                return None, f"{network} wurde mehrfach mit unterschiedlichen VRFs ausgewählt."
            decisions[network] = decision
        return list(decisions.values()), None

    # ------------------------------------------------------------------
    # Page 3 – review
    # ------------------------------------------------------------------

    def _build_review_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label("Bitte prüfen", object_name="CardTitle"))
        layout.addWidget(label("Mit „Import starten“ werden die folgenden Änderungen in NetBox geschrieben.", muted=True))
        self.review_grid = QGridLayout()
        self.review_grid.setHorizontalSpacing(24)
        self.review_grid.setVerticalSpacing(10)
        layout.addLayout(self.review_grid)
        layout.addWidget(hline())
        self.review_networks = label("", muted=True, wrap=True, selectable=True)
        layout.addWidget(self.review_networks)
        self.tenant_warning = Banner("", "warning")
        layout.addWidget(self.tenant_warning)
        layout.addStretch(1)
        return page

    def _fill_review(self):
        while self.review_grid.count():
            item = self.review_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        target = self.target
        new, existing = self.engine.classify_vms(self.vms)
        vrf_text = {"new": f"{target['vrf_name']} (wird angelegt)", "global": "Global (ohne VRF)"}.get(
            target["vrf_mode"], target["vrf_name"])
        new_vrfs = sorted({decision["vrf_name"] for decision in self.plan if decision.get("create_vrf")}
                          - {target["vrf_name"] if target["vrf_mode"] == "new" else None})
        rows = [
            ("Cluster", target["cluster"]),
            ("Mandant", target["tenant"]),
            ("Standard-VRF", vrf_text),
            ("Virtuelle Maschinen", f"{len(self.vms)} gesamt · {new} werden angelegt · {existing} aktualisiert"),
            ("Neue Präfixe", str(len(self.plan)) if self.plan else "keine"),
        ]
        if new_vrfs:
            rows.append(("Zusätzliche neue VRFs", ", ".join(new_vrfs)))
        for index, (key, value) in enumerate(rows):
            key_label = label(key, object_name="SummaryKey")
            value_label = label(value, object_name="SummaryValue", wrap=True, selectable=True)
            self.review_grid.addWidget(key_label, index, 0, Qt.AlignTop)
            self.review_grid.addWidget(value_label, index, 1)
        self.review_grid.setColumnStretch(1, 1)
        if self.plan:
            networks = ", ".join(f"{decision['network']}" for decision in self.plan[:12])
            more = f" und {len(self.plan) - 12} weitere" if len(self.plan) > 12 else ""
            self.review_networks.setText(f"Neue Präfixe: {networks}{more}")
        else:
            self.review_networks.setText("Es werden keine neuen Präfixe angelegt.")
        inherited = self.engine.cluster_tenants.get(target["cluster"], {}).get("name")
        if inherited and inherited != target["tenant"]:
            self.tenant_warning.set(
                f"Achtung: Der Cluster gehört in NetBox zum Mandanten <b>{inherited}</b>, "
                f"importiert wird aber für <b>{target['tenant']}</b>.", "warning")
            self.tenant_warning.show()
        else:
            self.tenant_warning.hide()

    # ------------------------------------------------------------------
    # Page 4 – run
    # ------------------------------------------------------------------

    def _build_run_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.run_title = label("Import läuft …", object_name="CardTitle")
        layout.addWidget(self.run_title)
        self.run_status = label("", muted=True)
        layout.addWidget(self.run_status)
        self.run_progress = QProgressBar()
        layout.addWidget(self.run_progress)
        self.run_log = QPlainTextEdit()
        self.run_log.setObjectName("LogView")
        self.run_log.setReadOnly(True)
        layout.addWidget(self.run_log, 1)
        self.run_result = Banner("", "success")
        self.run_result.hide()
        layout.addWidget(self.run_result)
        return page

    def _append_log(self, entry):
        if not self.running or entry.area != "vm" or entry.level == "debug":
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        prefix = {"warning": "⚠ ", "error": "✖ ", "success": "✔ "}.get(entry.level, "  ")
        self.run_log.appendPlainText(f"{stamp}  {prefix}{entry.text}")

    def _start_import(self):
        target = self.target
        plan = self.plan
        vms = list(self.vms)
        engine = self.engine
        self.running = True
        log_bus.entry.connect(self._append_log)
        self.run_progress.setRange(0, len(vms))
        self.run_progress.setValue(0)

        def work(ctx):
            vrf_id = target["vrf_id"]
            if target["vrf_mode"] == "new":
                vrf_id = engine.get_or_create_tenant_vrf(target["vrf_name"], target["tenant_id"])
            for decision in plan:
                if decision.get("create_vrf") and decision["vrf_name"] == target["vrf_name"] and vrf_id:
                    decision.update(vrf_id=vrf_id, create_vrf=False)
            engine.apply_network_plan(plan, target["tenant_id"])
            results = {"created": 0, "updated": 0, "failed": []}
            for index, vm in enumerate(vms):
                if ctx.cancelled:
                    results["cancelled"] = True
                    break
                ctx.progress(index, len(vms), vm["name"])
                try:
                    outcome = engine.sync_vm(vm, target["cluster_id"], target["tenant_id"], vrf_id)
                    results[outcome] += 1
                except Exception as error:  # noqa: BLE001
                    results["failed"].append(f"{vm['name']}: {error}")
                    ctx.log(f"{vm['name']}: {error}", "error")
            ctx.progress(len(vms), len(vms), "")
            return results

        self.tasks.run(work, title="VM-Import", area="vm", on_success=self._import_done,
                       on_error=self._import_failed, on_progress=self._import_progress, on_finished=self._import_finished)

    def _import_progress(self, value, maximum, text):
        self.run_progress.setMaximum(max(1, maximum))
        self.run_progress.setValue(value)
        self.run_status.setText(f"{value} von {maximum} · {text}" if text else f"{value} von {maximum}")

    def _import_done(self, results):
        failed = results["failed"]
        summary = f"{results['created']} VM(s) angelegt, {results['updated']} aktualisiert"
        if results.get("cancelled"):
            self.run_title.setText("Import abgebrochen")
            self.run_result.set(summary + ". Der Import wurde nach der laufenden VM beendet.", "warning")
        elif failed:
            self.run_title.setText("Import mit Fehlern abgeschlossen")
            self.run_result.set(summary + f", <b>{len(failed)} fehlgeschlagen</b>:<br>" + "<br>".join(failed[:8]), "warning")
        else:
            self.run_title.setText("Import abgeschlossen")
            self.run_result.set(summary + ".", "success")
        self.run_result.show()
        self.state.vms_changed.emit()
        self.result_summary = (summary, "warning" if failed else "success")

    def _import_failed(self, error):
        self.run_title.setText("Import fehlgeschlagen")
        self.run_result.set(str(error), "error")
        self.run_result.show()

    def _import_finished(self):
        self.running = False
        self.finished_import = True
        try:
            log_bus.entry.disconnect(self._append_log)
        except (RuntimeError, TypeError):
            pass
        self.next_button.setText("Fertig")
        self.next_button.setIcon(icon("check", "#FFFFFF"))
        self.next_button.setEnabled(True)
        self.cancel_button.hide()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_page(self, index):
        self.pages.setCurrentIndex(index)
        for step, widget in enumerate(self.step_labels):
            widget.setProperty("active", step == index)
            widget.setProperty("done", step < index)
            repolish(widget)
        self.back_button.setVisible(0 < index < 3)
        self.next_button.setText({2: "Import starten", 3: "Abbrechen"}.get(index, "Weiter"))
        self.next_button.setIcon(icon({2: "play", 3: "stop"}.get(index, "arrow-right"), "#FFFFFF"))
        self.next_button.setLayoutDirection(Qt.LeftToRight if index >= 2 else Qt.RightToLeft)
        self.cancel_button.setVisible(index < 3)

    def go_back(self):
        index = self.pages.currentIndex()
        if index == 2 and not self.missing_networks:
            index = 1
        self._show_page(max(0, index - 1))

    def go_next(self):
        index = self.pages.currentIndex()
        if index == 0:
            target, problem = self._target()
            if problem:
                self.target_error.set(problem, "error")
                self.target_error.show()
                return
            self.target_error.hide()
            self.target = target
            self._detect_networks()
        elif index == 1:
            plan, problem = self._network_plan()
            if problem:
                self.network_error.set(problem, "error")
                self.network_error.show()
                return
            self.network_error.hide()
            self.plan = plan
            self._fill_review()
            self._show_page(2)
        elif index == 2:
            self._show_page(3)
            self._start_import()
        elif index == 3:
            if self.finished_import:
                self.accept()
            else:
                self.tasks.cancel()
                self.next_button.setEnabled(False)
                self.run_status.setText("Wird nach der aktuellen VM beendet …")

    def _detect_networks(self):
        target = self.target
        engine = self.engine
        vms = list(self.vms)
        # A VRF that does not exist yet cannot contain prefixes; -1 keeps global
        # prefixes from being mistaken for prefixes of the new VRF.
        default_vrf = -1 if target["vrf_mode"] == "new" else target["vrf_id"]
        self.next_button.setEnabled(False)
        self.next_button.setText("Prüfe Netzwerke …")

        def work(_ctx):
            return engine.detect_missing_networks(vms, target["tenant_id"], default_vrf)

        def done(missing):
            self._fill_networks(missing)
            if missing:
                self._show_page(1)
            else:
                self.plan = []
                self._fill_review()
                self._show_page(2)

        def failed(error):
            self.target_error.set(f"Netzwerkprüfung fehlgeschlagen: {error}", "error")
            self.target_error.show()
            self._show_page(0)

        def finished():
            self.next_button.setEnabled(True)
            if self.pages.currentIndex() == 0:
                self._show_page(0)

        self.tasks.run(work, title="Netzwerkprüfung", area="vm", on_success=done, on_error=failed,
                       on_finished=finished, cancellable=False)

    def reject(self):
        if self.running:
            return
        super().reject()

    def closeEvent(self, event):
        if self.running:
            event.ignore()
            return
        super().closeEvent(event)
