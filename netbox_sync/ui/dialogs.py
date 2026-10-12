"""Modal dialogs for editing connections and reviewing changes."""

import uuid

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from netbox_sync.core.config import known_hosts_path
from netbox_sync.core.switches import PLATFORMS, forget_host_key
from netbox_sync.core.virtualization import test_source
from netbox_sync.ui.icons import icon
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.tasks import Task
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import (
    Badge,
    Banner,
    PasswordField,
    SearchField,
    SearchableComboBox,
    Table,
    button,
    checkbox,
    form_dialog_buttons,
    label,
    repolish,
)


def _mark_invalid(widget, invalid):
    widget.setProperty("invalid", bool(invalid))
    repolish(widget)


class _Dialog(QDialog):
    def __init__(self, parent, title, subtitle=None, width=560):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 20)
        self.root.setSpacing(16)
        header = QVBoxLayout()
        header.setSpacing(4)
        header.addWidget(label(title, object_name="PageTitle"))
        if subtitle:
            header.addWidget(label(subtitle, muted=True, wrap=True))
        self.root.addLayout(header)
        self.error_banner = Banner("", "error")
        self.error_banner.hide()

    def show_error(self, text):
        self.error_banner.set(text, "error")
        self.error_banner.show()


def ask_text(parent, title, text, default="", validator=None, ok_text="OK"):
    dialog = _Dialog(parent, title, text, width=440)
    field = QLineEdit(default)
    field.selectAll()
    dialog.root.addWidget(field)
    dialog.root.addWidget(dialog.error_banner)
    buttons, _ok = form_dialog_buttons(dialog, ok_text)
    dialog.root.addWidget(buttons)
    result = {}

    def accept():
        value = field.text().strip()
        problem = "Bitte einen Namen eingeben." if not value else (validator(value) if validator else None)
        if problem:
            _mark_invalid(field, True)
            dialog.show_error(problem)
            return
        result["value"] = value
        QDialog.accept(dialog)

    dialog.accept = accept
    return result.get("value") if dialog.exec() == QDialog.Accepted else None


# --------------------------------------------------------------------------
# Virtualization source
# --------------------------------------------------------------------------

class SourceDialog(_Dialog):
    TEXTS = {
        "vmware": {
            "endpoint": ("Host / vCenter", "vcenter.example.de oder 192.0.2.10"),
            "identity": ("Benutzer", "administrator@vsphere.local"),
            "secret": ("Passwort", ""),
            "help": "Ein Benutzer mit Leserechten auf alle VMs genügt.",
        },
        "proxmox": {
            "endpoint": ("Proxmox-URL", "https://pve.example.de:8006"),
            "identity": ("API-Token-ID", "benutzer@pve!netbox"),
            "secret": ("API-Token-Secret", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
            "help": "Dem Benutzer und dem API-Token mindestens die Rolle <b>PVEAuditor</b> auf <code>/vms</code> zuweisen.",
        },
    }

    def __init__(self, parent, source=None):
        source = source or {}
        super().__init__(parent, "Quelle bearbeiten" if source else "Quelle hinzufügen",
                         "Von hier werden virtuelle Maschinen gelesen. Zugangsdaten werden verschlüsselt gespeichert.")
        self.source_id = source.get("id", uuid.uuid4().hex)
        self.result = None
        self._task = None

        type_row = QHBoxLayout()
        type_row.setSpacing(8)
        self.type_group = QButtonGroup(self)
        self.type_buttons = {}
        for key, text in (("vmware", "VMware vSphere"), ("proxmox", "Proxmox VE")):
            widget = QPushButton(text)
            widget.setCheckable(True)
            widget.setCursor(Qt.PointingHandCursor)
            widget.setMinimumHeight(38)
            self.type_group.addButton(widget)
            self.type_buttons[key] = widget
            type_row.addWidget(widget)
            widget.toggled.connect(self._update_type)
        self.root.addLayout(type_row)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.name = QLineEdit(source.get("name", ""))
        self.name.setPlaceholderText("z. B. vCenter Rechenzentrum")
        self.endpoint = QLineEdit(source.get("host", source.get("url", "")))
        self.identity = QLineEdit(source.get("username", source.get("token_id", "")))
        self.secret = PasswordField()
        self.secret.setText(source.get("password", source.get("token_secret", "")))
        self.endpoint_label = label()
        self.identity_label = label()
        self.secret_label = label()
        form.addRow("Anzeigename", self.name)
        form.addRow(self.endpoint_label, self.endpoint)
        form.addRow(self.identity_label, self.identity)
        form.addRow(self.secret_label, self.secret)
        self.root.addLayout(form)

        self.help = label("", muted=True, wrap=True)
        self.help.setTextFormat(Qt.RichText)
        self.root.addWidget(self.help)

        options = QVBoxLayout()
        options.setSpacing(6)
        self.enabled = checkbox("Quelle beim Laden berücksichtigen", source.get("enabled", True))
        self.ignore_ssl = checkbox("Selbstsigniertes Zertifikat akzeptieren (TLS-Prüfung deaktivieren)",
                                   source.get("ignore_ssl", False))
        self.include_lxc = checkbox("LXC-Container mit einlesen", source.get("include_lxc", True))
        for widget in (self.enabled, self.ignore_ssl, self.include_lxc):
            options.addWidget(widget)
        self.root.addLayout(options)

        test_row = QHBoxLayout()
        self.test_button = button("Verbindung testen", "zap", on_click=self.run_test)
        self.test_result = Badge()
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_result)
        test_row.addStretch(1)
        self.root.addLayout(test_row)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Speichern")
        self.root.addWidget(buttons)

        self.type_buttons[source.get("type", "vmware")].setChecked(True)
        self._update_type()
        (self.endpoint if self.name.text() else self.name).setFocus()

    @property
    def source_type(self):
        return "proxmox" if self.type_buttons["proxmox"].isChecked() else "vmware"

    def _update_type(self, *_args):
        texts = self.TEXTS[self.source_type]
        for key, widget, field in (("endpoint", self.endpoint_label, self.endpoint),
                                   ("identity", self.identity_label, self.identity),
                                   ("secret", self.secret_label, self.secret)):
            widget.setText(texts[key][0])
            field.setPlaceholderText(texts[key][1])
        self.help.setText(texts["help"])
        self.include_lxc.setVisible(self.source_type == "proxmox")
        for key, widget in self.type_buttons.items():
            widget.setProperty("variant", "primary" if key == self.source_type else None)
            repolish(widget)
        self.test_result.set("")

    def collect(self):
        fields = {"name": self.name, "endpoint": self.endpoint, "identity": self.identity, "secret": self.secret}
        missing = [widget for widget in fields.values() if not widget.text().strip()]
        for widget in fields.values():
            _mark_invalid(widget, widget in missing)
        if missing:
            self.show_error("Bitte die markierten Felder ausfüllen.")
            missing[0].setFocus()
            return None
        result = {
            "id": self.source_id,
            "type": self.source_type,
            "name": self.name.text().strip(),
            "enabled": self.enabled.isChecked(),
            "ignore_ssl": self.ignore_ssl.isChecked(),
        }
        endpoint = self.endpoint.text().strip()
        if self.source_type == "vmware":
            result.update({"host": endpoint, "username": self.identity.text().strip(), "password": self.secret.text()})
        else:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = f"https://{endpoint}"
            result.update({
                "url": endpoint.rstrip("/"), "token_id": self.identity.text().strip(),
                "token_secret": self.secret.text().strip(), "include_lxc": self.include_lxc.isChecked(),
            })
        return result

    def run_test(self):
        source = self.collect()
        if not source:
            return
        self.error_banner.hide()
        self.test_button.setEnabled(False)
        self.test_result.set("Teste …", "neutral")
        self._task = Task(lambda _ctx: test_source(source), "vm", "Verbindungstest")
        self._task.signals.succeeded.connect(lambda text: self.test_result.set(text, "success"))
        self._task.signals.failed.connect(self._test_failed)
        self._task.signals.finished.connect(lambda: self.test_button.setEnabled(True))
        QThreadPool.globalInstance().start(self._task)

    def _test_failed(self, error):
        self.test_result.set("Fehlgeschlagen", "error")
        self.show_error(f"Verbindung fehlgeschlagen: {error}")

    def accept(self):
        self.result = self.collect()
        if self.result:
            super().accept()


# --------------------------------------------------------------------------
# Switches
# --------------------------------------------------------------------------

def _platform_combo(current="auto", include_per_device=False):
    combo = QComboBox()
    if include_per_device:
        combo.addItem("Je Gerät vorschlagen (empfohlen)", None)
    for key, text in PLATFORMS.items():
        combo.addItem(text, key)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    return combo


class SwitchDialog(_Dialog):
    """Edit one switch access."""

    def __init__(self, parent, switch, devices=None):
        super().__init__(parent, "Switch bearbeiten", "SSH-Zugang zu einem bestehenden NetBox-Gerät.")
        self.switch = switch
        self.devices = {item["id"]: item for item in devices or []}
        self.result = None

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        if self.devices:
            self.device = SearchableComboBox("Gerät suchen …")
            self.device.set_items(
                [(item["label"], item["id"]) for item in sorted(self.devices.values(), key=lambda d: d["name"].casefold())],
                switch.get("device_id"),
            )
            form.addRow("NetBox-Gerät", self.device)
        else:
            self.device = None
            form.addRow("NetBox-Gerät", label(f"<b>{switch.get('name', '')}</b>"))
        self.platform = _platform_combo(switch.get("platform", "auto"))
        self.host = QLineEdit(switch.get("host", ""))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(switch.get("port", 22)))
        self.username = QLineEdit(switch.get("username", ""))
        self.password = PasswordField()
        self.password.setText(switch.get("password", ""))
        form.addRow("Plattform", self.platform)
        form.addRow("Host / IP", self.host)
        form.addRow("SSH-Port", self.port)
        form.addRow("Benutzer", self.username)
        form.addRow("Passwort", self.password)
        self.root.addLayout(form)
        self.enabled = checkbox("Switch aktiv", switch.get("enabled", True))
        self.root.addWidget(self.enabled)
        forget = button("Gespeicherten SSH-Hostschlüssel vergessen", "shield", "ghost", on_click=self.forget_key,
                        tooltip="Nötig, wenn ein Switch ausgetauscht wurde und sich sein Hostschlüssel geändert hat.")
        self.root.addWidget(forget, 0, Qt.AlignLeft)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self)
        self.root.addWidget(buttons)

    def forget_key(self):
        removed = forget_host_key(known_hosts_path(), self.host.text().strip(), self.port.value())
        text = "Hostschlüssel entfernt. Beim nächsten Auslesen wird der neue Schlüssel gespeichert." if removed \
            else "Für diesen Host war kein Schlüssel gespeichert."
        self.error_banner.set(text, "success" if removed else "info")
        self.error_banner.show()

    def accept(self):
        fields = (self.host, self.username, self.password)
        for widget in fields:
            _mark_invalid(widget, not widget.text().strip())
        device_id = self.device.valid_data() if self.device else self.switch.get("device_id")
        if self.device:
            _mark_invalid(self.device, device_id is None)
        if device_id is None or any(not widget.text().strip() for widget in fields):
            self.show_error("Bitte Gerät und alle Zugangsdaten angeben.")
            return
        result = dict(self.switch)
        result.update({
            "device_id": device_id,
            "name": self.devices[device_id]["name"] if self.devices else self.switch.get("name", ""),
            "platform": self.platform.currentData(),
            "host": self.host.text().strip(),
            "port": self.port.value(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "enabled": self.enabled.isChecked(),
        })
        self.result = result
        super().accept()


class AddSwitchesDialog(_Dialog):
    """Pick several NetBox devices at once and give them shared SSH credentials."""

    def __init__(self, parent, devices, existing_device_ids):
        super().__init__(parent, "Switche hinzufügen",
                         "Wähle vorhandene NetBox-Geräte aus. Host und Plattform werden aus NetBox vorgeschlagen.",
                         width=980)
        self.resize(1080, 720)
        self.result = []
        self.rows = [dict(device, added=device["id"] in existing_device_ids) for device in devices]

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.search = SearchField("Name, Standort, Modell, IP …")
        self.tenant = QComboBox()
        self.site = QComboBox()
        self.role = QComboBox()
        for combo, key, everything in ((self.tenant, "tenant", "Alle Mandanten"), (self.site, "site", "Alle Standorte"),
                                       (self.role, "role", "Alle Rollen")):
            combo.addItem(everything, None)
            for value in sorted({row[key] or "—" for row in self.rows}, key=str.casefold):
                combo.addItem(value, value)
            combo.setMinimumWidth(160)
        self.only_switches = checkbox("Nur Switch-Rollen")
        has_switch_role = any("switch" in (row["role"] or "").casefold() for row in self.rows)
        self.only_switches.setChecked(has_switch_role)
        self.only_switches.setVisible(has_switch_role)
        filters.addWidget(self.search, 2)
        filters.addWidget(self.tenant, 1)
        filters.addWidget(self.site, 1)
        filters.addWidget(self.role, 1)
        filters.addWidget(self.only_switches)
        self.root.addLayout(filters)

        columns = [
            Column("name", "Gerät", 200),
            Column("site", "Standort", 130),
            Column("tenant", "Mandant", 130),
            Column("role", "Rolle", 120),
            Column("model", "Modell", 170),
            Column("host", "SSH-Host", 130),
            Column("platform", "Plattform", 160, value=lambda row: PLATFORMS[row["platform"]],
                   color=lambda row: "text_faint" if row["platform"] == "auto" else None),
            Column("added", "", 110, value=lambda row: "bereits im Profil" if row["added"] else "",
                   color=lambda row: "text_faint"),
        ]
        self.model = RowTableModel(columns, checkable=True, row_id=lambda row: row["id"])
        self.model.set_rows(self.rows)
        self.proxy = FilterProxy(self.model)
        self.table = Table(stretch_column=None)
        self.table.setModel(self.proxy)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.table.doubleClicked.connect(self._toggle_row)
        self.root.addWidget(self.table, 1)

        selection_row = QHBoxLayout()
        selection_row.addWidget(button("Alle sichtbaren auswählen", variant="ghost", on_click=self.select_visible))
        selection_row.addWidget(button("Auswahl aufheben", variant="ghost", on_click=lambda: self.model.set_checked(
            set(self.model.checked), False)))
        selection_row.addStretch(1)
        self.selection_label = label("", muted=True)
        selection_row.addWidget(self.selection_label)
        self.root.addLayout(selection_row)

        credentials = QWidget()
        grid = QGridLayout(credentials)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.username = QLineEdit()
        self.username.setPlaceholderText("SSH-Benutzer")
        self.password = PasswordField("Passwort")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.platform = _platform_combo(include_per_device=True)
        self.platform.setCurrentIndex(0)
        for column, (title, widget) in enumerate((("Benutzer", self.username), ("Passwort", self.password),
                                                  ("SSH-Port", self.port), ("Plattform", self.platform))):
            grid.addWidget(label(title, muted=True), 0, column)
            grid.addWidget(widget, 1, column)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 2)
        self.root.addWidget(label("Gemeinsame Zugangsdaten", object_name="CardTitle"))
        self.root.addWidget(credentials)
        self.root.addWidget(self.error_banner)

        buttons, self.ok_button = form_dialog_buttons(self, "Hinzufügen")
        self.root.addWidget(buttons)

        self.search.textChanged.connect(self.proxy.set_text)
        for combo, key in ((self.tenant, "tenant"), (self.site, "site"), (self.role, "role")):
            combo.currentIndexChanged.connect(lambda _i, c=combo, k=key: self.proxy.set_predicate(
                k, None if c.currentData() is None else (lambda row, v=c.currentData(), k=k: (row[k] or "—") == v)))
        self.only_switches.toggled.connect(self._apply_switch_filter)
        self._apply_switch_filter(self.only_switches.isChecked())
        self.model.checked_changed.connect(self._update_selection)
        self._update_selection()
        self.search.setFocus()

    def _apply_switch_filter(self, enabled):
        self.proxy.set_predicate("switch_role", (lambda row: "switch" in (row["role"] or "").casefold()) if enabled else None)

    def _toggle_row(self, index):
        row = self.proxy.row_at(index)
        self.model.set_checked({row["id"]}, row["id"] not in self.model.checked)

    def select_visible(self):
        self.model.set_checked({row["id"] for row in self.proxy.visible_rows() if not row["added"]})

    def _update_selection(self):
        count = len(self.model.checked)
        self.selection_label.setText(f"{count} Gerät(e) ausgewählt")
        self.ok_button.setText(f"{count} Switch(e) hinzufügen" if count else "Hinzufügen")
        self.ok_button.setEnabled(count > 0)

    def accept(self):
        chosen = self.model.checked_rows()
        username = self.username.text().strip()
        password = self.password.text()
        _mark_invalid(self.username, not username)
        _mark_invalid(self.password, not password)
        if not chosen:
            self.show_error("Bitte mindestens ein Gerät auswählen.")
            return
        if not username or not password:
            self.show_error("Bitte Benutzer und Passwort für den SSH-Zugang angeben.")
            return
        platform = self.platform.currentData()
        self.result = [{
            "id": uuid.uuid4().hex,
            "name": row["name"],
            "device_id": row["id"],
            "platform": platform or row["platform"],
            "host": row["host"] or row["name"],
            "port": self.port.value(),
            "username": username,
            "password": password,
            "enabled": True,
        } for row in chosen]
        super().accept()


# --------------------------------------------------------------------------
# Sync preview and raw data
# --------------------------------------------------------------------------

class SyncPreviewDialog(_Dialog):
    def __init__(self, parent, reports, create_cables):
        super().__init__(parent, "Änderungen prüfen",
                         "Diese Änderungen werden in NetBox vorgenommen. Es wurde noch nichts geschrieben.",
                         width=760)
        self.resize(860, 620)
        totals = {}
        for _switch, report in reports:
            for key, value in report.items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        summary = QHBoxLayout()
        summary.setSpacing(8)
        items = [
            (f"{totals.get('interfaces_created', 0)} Interfaces neu", "info"),
            (f"{totals.get('interfaces_updated', 0)} Interfaces geändert", "info"),
            (f"{totals.get('ips_created', 0) + totals.get('ips_updated', 0)} IP-Adressen", "info"),
        ]
        if create_cables:
            items.append((f"{totals.get('cables_created', 0)} Kabel", "info"))
        errors = sum(len(report["errors"]) for _switch, report in reports)
        if errors:
            items.append((f"{errors} Probleme", "error"))
        for text, kind in items:
            summary.addWidget(Badge(text, kind))
        summary.addStretch(1)
        self.root.addLayout(summary)

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        total_changes = 0
        for switch, report in reports:
            changes = report["changes"]
            total_changes += len(changes)
            unchanged = report["interfaces_unchanged"]
            top = QTreeWidgetItem([f"{switch['name']} — {len(changes)} Änderung(en), {unchanged} Interface(s) unverändert"])
            top.setIcon(0, icon("network", theme.color("accent")))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for error in report["errors"]:
                child = QTreeWidgetItem([error])
                child.setIcon(0, icon("warning", theme.color("danger")))
                top.addChild(child)
            for change in changes[:2000]:
                top.addChild(QTreeWidgetItem([change]))
            tree.addTopLevelItem(top)
            top.setExpanded(len(reports) == 1 or bool(report["errors"]))
        self.root.addWidget(tree, 1)
        if total_changes == 0:
            self.root.addWidget(Banner("NetBox ist bereits auf dem aktuellen Stand – es gibt nichts zu übertragen.", "success"))
        buttons, ok = form_dialog_buttons(self, f"{total_changes} Änderung(en) übernehmen")
        ok.setEnabled(total_changes > 0)
        self.root.addWidget(buttons)


class RawDataDialog(_Dialog):
    SECTIONS = {
        "interfaces": "Schnittstellen", "ipv4_interfaces": "IPv4", "ipv6_interfaces": "IPv6",
        "neighbors": "LLDP", "cdp_neighbors": "CDP", "lldp_status": "LLDP-Status",
    }

    def __init__(self, parent, items, selected_id=None):
        super().__init__(parent, "SSH-Rohdaten", "Unveränderte Ausgaben der Switch-CLI – hilfreich bei der Fehlersuche.",
                         width=900)
        self.resize(1040, 700)
        self.items = items
        top = QHBoxLayout()
        self.switch_combo = QComboBox()
        for switch, _snapshot in items:
            self.switch_combo.addItem(f"{switch['name']} ({switch['host']})", switch["id"])
        if selected_id:
            self.switch_combo.setCurrentIndex(max(0, self.switch_combo.findData(selected_id)))
        top.addWidget(self.switch_combo, 1)
        top.addWidget(button("Kopieren", "copy", on_click=self.copy_current))
        top.addWidget(button("Alles speichern …", "download", on_click=self.save_all))
        self.root.addLayout(top)
        self.tabs = QTabWidget()
        self.editors = {}
        for key, title in self.SECTIONS.items():
            editor = QPlainTextEdit()
            editor.setObjectName("LogView")
            editor.setReadOnly(True)
            editor.setLineWrapMode(QPlainTextEdit.NoWrap)
            self.tabs.addTab(editor, title)
            self.editors[key] = editor
        self.root.addWidget(self.tabs, 1)
        close = button("Schließen", variant="primary", on_click=self.accept)
        self.root.addWidget(close, 0, Qt.AlignRight)
        self.switch_combo.currentIndexChanged.connect(self.show_switch)
        self.show_switch()

    def _current(self):
        return self.items[max(0, self.switch_combo.currentIndex())]

    def show_switch(self, *_args):
        _switch, snapshot = self._current()
        raw = snapshot.get("raw", {})
        for key, editor in self.editors.items():
            editor.setPlainText(str(raw.get(key) or "(keine Ausgabe)"))

    def copy_current(self):
        editor = self.tabs.currentWidget()
        QGuiApplication.clipboard().setText(editor.toPlainText())

    def save_all(self):
        path, _filter = QFileDialog.getSaveFileName(self, "SSH-Rohdaten speichern", "ssh-rohdaten.txt", "Text (*.txt)")
        if not path:
            return
        sections = []
        for switch, snapshot in self.items:
            sections.append(f"{'=' * 20} {switch['name']} ({switch['host']}) {'=' * 20}")
            raw = snapshot.get("raw", {})
            for key, title in self.SECTIONS.items():
                sections.extend((f"\n--- {title.upper()} ---", str(raw.get(key) or "(keine Ausgabe)")))
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("\n".join(sections))
