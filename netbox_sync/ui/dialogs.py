"""Modal dialogs for editing connections and reviewing changes."""

import re
import uuid

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QColor, QGuiApplication
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from netbox_sync.core.config import known_hosts_path
from netbox_sync.core.switches import MATCH_METHODS, PLATFORMS, VLAN_METHODS, forget_host_key
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
        self.devices = {item["id"]: item for item in devices or []}
        # Without NetBox (offline mode) a switch is identified by its name only.
        self.manual = not self.devices and not switch.get("device_id")
        super().__init__(parent, "Switch bearbeiten" if switch.get("name") else "Switch hinzufügen",
                         "SSH-Zugang zu einem Switch. Der Name muss dem späteren NetBox-Gerät entsprechen."
                         if self.manual else "SSH-Zugang zu einem bestehenden NetBox-Gerät.")
        self.switch = switch
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
            if self.manual:
                self.name = QLineEdit(switch.get("name", ""))
                self.name.setPlaceholderText("Gerätename wie in NetBox")
                form.addRow("Name", self.name)
            else:
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
        fields = (self.host, self.username, self.password) + ((self.name,) if self.manual else ())
        for widget in fields:
            _mark_invalid(widget, not widget.text().strip())
        device_id = self.device.valid_data() if self.device else self.switch.get("device_id")
        if self.device:
            _mark_invalid(self.device, device_id is None)
        if (device_id is None and not self.manual) or any(not widget.text().strip() for widget in fields):
            self.show_error("Bitte Gerät und alle Zugangsdaten angeben.")
            return
        result = dict(self.switch)
        result.update({
            "device_id": device_id,
            "name": (self.name.text().strip() if self.manual else
                     self.devices[device_id]["name"] if self.devices else self.switch.get("name", "")),
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
        self.rows = [dict(device, added=device.get("stack_master", device["id"]) in existing_device_ids)
                     for device in devices]
        self.by_id = {row["id"]: row for row in self.rows}

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
            Column("added", "", 170, value=lambda row: "bereits im Profil" if row["added"] else (
                f"Stack von {row['stack_of']}" if row.get("stack_of") else ""), color=lambda row: "text_faint"),
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
        # a stack is read once through its master; every member is mapped in the review
        masters = {}
        for row in chosen:
            master = self.by_id.get(row.get("stack_master"), row)
            if not master["added"]:
                masters.setdefault(master["id"], master)
        chosen = list(masters.values())
        if not chosen:
            self.show_error("Die ausgewählten Stack-Mitglieder sind über ihren Stack bereits im Profil.")
            return
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
        for key, text in (("vrfs_created", "VRF(s) neu"), ("prefixes_created", "Prefix(e) neu"),
                          ("vlans_created", "VLAN(s) neu")):
            if totals.get(key):
                items.append((f"{totals[key]} {text}", "info"))
        if create_cables:
            items.append((f"{totals.get('cables_created', 0)} Kabel", "info"))
            if totals.get("cables_updated"):
                items.append((f"{totals['cables_updated']} Kabel angepasst", "error"))
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
            for note in report.get("notes", []):
                child = QTreeWidgetItem([note])
                child.setIcon(0, icon("info", theme.color("warning")))
                top.addChild(child)
            members = {}
            for change in changes[:2000]:
                # stack member ports read "Interface X (Y) auf <member>: …" – group them under the member
                found = re.match(r"(Interface .+?) auf ([^:]+): (.*)$", change)
                if not found:
                    top.addChild(QTreeWidgetItem([change]))
                    continue
                if found.group(2) not in members:
                    members[found.group(2)] = QTreeWidgetItem([found.group(2)])
                    members[found.group(2)].setIcon(0, icon("network", theme.color("text_muted")))
                members[found.group(2)].addChild(QTreeWidgetItem([f"{found.group(1)}: {found.group(3)}"]))
            for member in members.values():
                member.setText(0, f"{member.text(0)} — {member.childCount()} Änderung(en)")
                top.addChild(member)
                member.setExpanded(True)
            tree.addTopLevelItem(top)
            top.setExpanded(len(reports) == 1 or bool(report["errors"]))
        self.root.addWidget(tree, 1)
        if total_changes == 0:
            self.root.addWidget(Banner("NetBox ist bereits auf dem aktuellen Stand – es gibt nichts zu übertragen.", "success"))
        buttons, ok = form_dialog_buttons(self, f"{total_changes} Änderung(en) übernehmen")
        ok.setEnabled(total_changes > 0)
        self.root.addWidget(buttons)


class SwitchMappingDialog(_Dialog):
    """Review interface mapping and neighbor devices before computing changes.

    ``plans`` is a list of ``(switch, plan)`` from ``plan_switch_sync``. After
    acceptance ``result`` maps switch IDs to ``interface_map`` and
    ``neighbor_devices`` to be stored in the profile.
    """

    NEW = "\x00new"

    def __init__(self, parent, plans, create_cables):
        super().__init__(parent, "Zuordnung prüfen",
                         "Schnittstellen, Nachbarn, Netze und VLANs prüfen und bei Bedarf korrigieren. Neue Objekte "
                         "erhalten immer den Mandanten des Switches – die Auswahl wird im Profil gespeichert.",
                         width=820)
        self.resize(1120, 680)
        self.plans = plans
        self.result = None
        # Profile-wide VLAN decisions; every switch config carries the same map.
        self.previous_vlan_map = dict((plans[0][0].get("vlan_map") if plans else None) or {})
        self.vlan_map = dict(self.previous_vlan_map)
        self.previous_network_map = dict((plans[0][0].get("network_map") if plans else None) or {})
        self.network_map = dict(self.previous_network_map)
        self._new_vrf_names = []
        tabs = QTabWidget()

        # Stack ---------------------------------------------------------------
        self.stack_rows = []
        stacks = [(switch, plan) for switch, plan in plans if plan.get("stack")]
        if stacks:
            stack_page = QWidget()
            stack_layout = QVBoxLayout(stack_page)
            stack_layout.setContentsMargins(0, 12, 0, 0)
            stack_layout.addWidget(label(
                "Der Switch ist ein Stack. Jedes Mitglied wird einem eigenen NetBox-Gerät zugeordnet, die Ports "
                "kommen an das Gerät ihres Mitglieds (2/1/5 an das Gerät von Mitglied 2). LAGs und VLAN-Interfaces "
                "bleiben am Gerät des Switch-Eintrags. Vorschlag: Positionen im Virtual Chassis von NetBox.",
                muted=True, wrap=True))
            self.stack_table = QTableWidget(0, 5)
            self.stack_table.setHorizontalHeaderLabels(["Switch", "Mitglied", "Modell", "Ports", "NetBox-Gerät"])
            for switch, plan in stacks:
                items = [("– Ports überspringen –", 0)] + [(device["name"], device["id"]) for device in plan["devices"]]
                for entry in plan["stack"]:
                    row = self.stack_table.rowCount()
                    self.stack_table.insertRow(row)
                    texts = (switch["name"], str(entry["member"]), entry["model"], str(entry["ports"]))
                    for column, text in enumerate(texts):
                        self.stack_table.setItem(row, column, QTableWidgetItem(text))
                    combo = SearchableComboBox("Gerät suchen …")
                    combo.set_items(items, entry["device_id"] or 0)
                    self.stack_table.setCellWidget(row, 4, combo)
                    self.stack_rows.append((switch, entry, combo))
            self._prepare_table(self.stack_table, (160, 80, 120, 70, 320))
            stack_layout.addWidget(self.stack_table, 1)
            open_members = sum(1 for _switch, entry, _combo in self.stack_rows if not entry["device_id"])
            tabs.addTab(stack_page, f"Stack ({open_members} ohne Gerät)" if open_members else "Stack")

        # Interfaces --------------------------------------------------------
        interface_page = QWidget()
        interface_layout = QVBoxLayout(interface_page)
        interface_layout.setContentsMargins(0, 12, 0, 0)
        self.only_unclear = checkbox("Nur Schnittstellen ohne Namensgleichheit anzeigen", True)
        interface_layout.addWidget(self.only_unclear)
        self.interface_table = QTableWidget(0, 5)
        self.interface_table.setHorizontalHeaderLabels(["Switch", "Schnittstelle (Switch)", "VLANs",
                                                        "NetBox-Schnittstelle", "Zuordnung"])
        self.interface_rows = []
        for switch, plan in plans:
            vlans = {item["name"]: item["vlan_label"] for item in plan["interfaces"]}
            for item in plan["interfaces"]:
                if not item["physical"] and item["method"] in ("name", "new"):
                    continue
                # In a stack every member has its own NetBox device and interfaces.
                names = plan.get("owner_interfaces", {}).get(str(item.get("owner_id")), plan["netbox_interfaces"])
                choices = [("＋ neu anlegen", self.NEW)] + [(name, name) for name in names]
                row = self.interface_table.rowCount()
                self.interface_table.insertRow(row)
                combo = QComboBox()
                for text, data in choices:
                    combo.addItem(text, data)
                combo.setCurrentIndex(max(0, combo.findData(item["netbox_name"] or self.NEW)))
                for column, text in enumerate((switch["name"], item["name"], vlans.get(item["name"], ""))):
                    self.interface_table.setItem(row, column, QTableWidgetItem(text))
                self.interface_table.setCellWidget(row, 3, combo)
                method = QTableWidgetItem(MATCH_METHODS.get(item["method"], item["method"]))
                if item["method"] in ("number", "order"):
                    method.setForeground(QColor(theme.color("warning")))
                self.interface_table.setItem(row, 4, method)
                self.interface_rows.append((row, switch, item, combo))
        self._prepare_table(self.interface_table, (140, 170, 110, 220, 150))
        interface_layout.addWidget(self.interface_table, 1)
        unclear = sum(1 for _row, _switch, item, _combo in self.interface_rows if item["method"] != "name")
        tabs.addTab(interface_page, f"Schnittstellen ({unclear} zu prüfen)")
        self.only_unclear.toggled.connect(self._filter_interfaces)
        self._filter_interfaces(True)

        # Neighbors -----------------------------------------------------------
        self.neighbor_rows = []
        if create_cables:
            neighbor_page = QWidget()
            neighbor_layout = QVBoxLayout(neighbor_page)
            neighbor_layout.setContentsMargins(0, 12, 0, 0)
            scopes = sorted({plan["device_scope"] for _switch, plan in plans})
            neighbor_layout.addWidget(label(
                "Für jeden Nachbarn kann ein bestehendes NetBox-Gerät gewählt werden (Auswahl aus: "
                f"{', '.join(scopes)}). Die Gegenstelle wird über den Portnamen gesucht; „kein Kabel“ "
                "überspringt den Nachbarn.", muted=True, wrap=True))
            self.neighbor_table = QTableWidget(0, 5)
            self.neighbor_table.setHorizontalHeaderLabels(["Switch", "Lokaler Port", "Nachbar (LLDP/CDP)",
                                                           "Nachbar-Port", "NetBox-Gerät"])
            for switch, plan in plans:
                for neighbor in plan["neighbors"]:
                    row = self.neighbor_table.rowCount()
                    self.neighbor_table.insertRow(row)
                    combo = SearchableComboBox("Gerät suchen …")
                    combo.set_items([("– kein Kabel –", 0)] + [(device["name"], device["id"]) for device in plan["devices"]],
                                    neighbor.get("device_id") or 0)
                    texts = (switch["name"], neighbor["local_port"], neighbor.get("system_name") or
                             neighbor.get("management_ip") or "", neighbor.get("remote_port") or "")
                    for column, text in enumerate(texts):
                        self.neighbor_table.setItem(row, column, QTableWidgetItem(text))
                    self.neighbor_table.setCellWidget(row, 4, combo)
                    self.neighbor_rows.append((switch, neighbor, combo))
            self._prepare_table(self.neighbor_table, (140, 110, 220, 150, 260))
            neighbor_layout.addWidget(self.neighbor_table, 1)
            found = sum(1 for _switch, neighbor, _combo in self.neighbor_rows if neighbor.get("device_id"))
            tabs.addTab(neighbor_page, f"Nachbarn ({found} von {len(self.neighbor_rows)} zugeordnet)")

        # Networks --------------------------------------------------------------
        # Network decisions apply to all switches of the profile: one row per network.
        self.network_rows = []
        networks = {}
        for switch, plan in plans:
            for item in plan["networks"]:
                entry = networks.setdefault(item["network"], {"item": item, "switches": [], "tenants": [], "vrfs": {}})
                entry["switches"].append(switch["name"])
                if plan["tenant"] not in entry["tenants"]:
                    entry["tenants"].append(plan["tenant"])
                entry["vrfs"].update({vrf["id"]: vrf["label"] for vrf in plan["vrfs"]})
        if networks:
            network_page = QWidget()
            network_layout = QVBoxLayout(network_page)
            network_layout.setContentsMargins(0, 12, 0, 0)
            network_layout.addWidget(label(
                "Netze der Switch-IP-Adressen. Die VRF gilt für Prefix und IP-Adressen; zur Auswahl stehen die "
                "globale Tabelle und die VRFs der Mandantengruppe. Fehlende Prefixe werden nur angelegt, wenn sie "
                "angehakt sind – mit dem Mandanten des Switches. Die Auswahl gilt für alle Switche des Profils.",
                muted=True, wrap=True))
            settled = {network: entry["item"]["stored"] or bool(entry["item"]["existing"])
                       for network, entry in networks.items()}
            self.open_network_table = self._network_table()
            self.settled_network_table = self._network_table()
            for network, entry in networks.items():
                table = self.settled_network_table if settled[network] else self.open_network_table
                self._add_network_row(table, entry)
            for table in (self.open_network_table, self.settled_network_table):
                self._prepare_table(table, (160, 150, 150, 300, 200))
            open_count = sum(1 for value in settled.values() if not value)
            self.settled_network_toggle = self._collapsible(network_layout, self.open_network_table,
                                                            self.settled_network_table, open_count,
                                                            len(networks) - open_count, "Netze")
            missing = sum(1 for entry in networks.values() if not entry["item"]["existing"])
            tabs.addTab(network_page, f"Netze ({open_count} zu prüfen, {missing} Prefix(e) fehlen)")

        # VLANs -----------------------------------------------------------------
        # VLAN decisions apply to all switches of the profile: one row per VID.
        self.vlan_rows = []
        merged = {}
        for switch, plan in plans:
            for item in plan["vlans"]:
                entry = merged.setdefault(item["vid"], {"item": item, "switches": [], "candidates": {}, "tenants": {}})
                entry["switches"].append(switch["name"])
                entry["candidates"].update({candidate["id"]: candidate["label"] for candidate in item["candidates"]})
                entry["tenants"].update({tenant["id"]: tenant["name"] for tenant in plan["tenants"]})
        if merged:
            vlan_page = QWidget()
            vlan_layout = QVBoxLayout(vlan_page)
            vlan_layout.setContentsMargins(0, 12, 0, 0)
            vlan_layout.addWidget(label(
                "Gesucht wird in der Mandantengruppe über die VLAN-ID; bei mehreren Treffern entscheiden Mandant, "
                "Standort und Name. Neue VLANs erhalten den eingetragenen Namen und Mandanten. Ignorierte VLANs "
                "werden weder angelegt noch zugeordnet und fehlen im Label. Die Auswahl gilt für alle Switche "
                "des Profils und wird gespeichert.", muted=True, wrap=True))
            settled = {vid: entry["item"]["stored"] or entry["item"]["method"] in ("vid", "name")
                       for vid, entry in merged.items()}
            self.open_vlan_table = self._vlan_table()
            self.settled_vlan_table = self._vlan_table()
            for vid in sorted(merged):
                self._add_vlan_row(self.settled_vlan_table if settled[vid] else self.open_vlan_table, merged[vid])
            for table in (self.open_vlan_table, self.settled_vlan_table):
                self._prepare_table(table, (130, 60, 150, 280, 190, 170, 110))
            open_count = sum(1 for value in settled.values() if not value)
            self.settled_toggle = self._collapsible(vlan_layout, self.open_vlan_table, self.settled_vlan_table,
                                                    open_count, len(merged) - open_count, "VLANs")
            missing = sum(1 for entry in merged.values() if entry["item"]["method"] == "new")
            tabs.addTab(vlan_page, f"VLANs ({open_count} zu prüfen, {missing} fehlen)")

        self.root.addWidget(tabs, 1)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Weiter zur Vorschau")
        self.root.addWidget(buttons)

    @staticmethod
    def _prepare_table(table, widths):
        # Rows must fit the styled combo boxes, otherwise their text is clipped.
        height = max((table.cellWidget(row, column).sizeHint().height()
                      for row in range(min(table.rowCount(), 1)) for column in range(table.columnCount())
                      if table.cellWidget(row, column)), default=0)
        table.verticalHeader().setDefaultSectionSize(max(height + 10, table.verticalHeader().defaultSectionSize()))
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)

    IGNORE = "\x00ignore"

    @staticmethod
    def _vlan_table():
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(["Switch(e)", "VLAN", "Name (Switch)", "NetBox-VLAN",
                                         "Name des neuen VLANs", "Mandant (neu)", "Zuordnung"])
        return table

    def _add_vlan_row(self, table, entry):
        item = entry["item"]
        row = table.rowCount()
        table.insertRow(row)
        switches = ", ".join(entry["switches"])
        for column, text in enumerate((switches, str(item["vid"]), item["name"])):
            table.setItem(row, column, QTableWidgetItem(text))
        combo = QComboBox()
        for candidate_id, text in sorted(entry["candidates"].items(), key=lambda pair: pair[1].casefold()):
            combo.addItem(text, candidate_id)
        combo.addItem("＋ neu anlegen", self.NEW)
        combo.addItem("– nicht zuordnen –", 0)
        combo.addItem("⊘ ignorieren (auf allen Switchen)", self.IGNORE)
        current = {"skip": 0, "ignore": self.IGNORE}.get(item["method"], item["netbox_id"] or self.NEW)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        name = QLineEdit(item["new_name"])
        name.setMaxLength(64)
        name.setPlaceholderText(item["name"])
        tenant = QComboBox()
        for tenant_id, text in sorted(entry["tenants"].items(), key=lambda pair: pair[1].casefold()):
            tenant.addItem(text, tenant_id)
        tenant.setCurrentIndex(max(0, tenant.findData(item["new_tenant"])))
        method = QTableWidgetItem(VLAN_METHODS.get(item["method"], item["method"])
                                  + (" · gespeichert" if item["stored"] else ""))
        if not item["stored"] and item["method"] in ("ambiguous", "new"):
            method.setForeground(QColor(theme.color("warning")))
        table.setCellWidget(row, 3, combo)
        table.setCellWidget(row, 4, name)
        table.setCellWidget(row, 5, tenant)
        table.setItem(row, 6, method)

        def toggle(_index=None):
            for widget in (name, tenant):
                widget.setEnabled(combo.currentData() == self.NEW)
        combo.currentIndexChanged.connect(toggle)
        toggle()
        self.vlan_rows.append((item, combo, name, tenant))

    @staticmethod
    def _collapsible(layout, open_table, settled_table, open_count, settled_count, noun):
        """Open items on top, decided ones in a collapsed section that can be expanded."""
        layout.addWidget(label(f"<b>Zu prüfen ({open_count})</b>" if open_count else
                               f"Alle {noun} sind zugeordnet oder gespeichert.", wrap=True))
        layout.addWidget(open_table, 1)
        open_table.setVisible(bool(open_count))
        toggle = button("", variant="link")
        toggle.setCheckable(True)

        def update(expanded):
            arrow = "▾" if expanded else "▸"
            toggle.setText(f"{arrow} Gespeicherte und eindeutig zugeordnete {noun} ({settled_count})"
                           + ("" if expanded else " – zum Bearbeiten ausklappen"))
            settled_table.setVisible(expanded and settled_count > 0)
        toggle.toggled.connect(update)
        update(False)
        toggle.setVisible(settled_count > 0)
        layout.addWidget(toggle, 0, Qt.AlignLeft)
        layout.addWidget(settled_table, 1)
        return toggle

    @staticmethod
    def _network_table():
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Switch(e)", "Mandant", "Netz", "VRF", "Prefix in NetBox"])
        return table

    def _add_network_row(self, table, entry):
        item = entry["item"]
        row = table.rowCount()
        table.insertRow(row)
        texts = (", ".join(entry["switches"]), ", ".join(entry["tenants"]), item["network"])
        for column, text in enumerate(texts):
            table.setItem(row, column, QTableWidgetItem(text))
        combo = QComboBox()
        combo.addItem("Global (keine VRF)", 0)
        for vrf_id, text in sorted(entry["vrfs"].items(), key=lambda pair: pair[1].casefold()):
            combo.addItem(text, vrf_id)
        new_names = list(self._new_vrf_names)
        if item["vrf_name"] and item["vrf_name"] not in new_names:
            new_names.append(item["vrf_name"])
        for name in new_names:
            combo.addItem(f"{name} (neu)", f"new:{name}")
        combo.addItem("＋ Neue VRF …", self.NEW)
        current = f"new:{item['vrf_name']}" if item["vrf_name"] else item["vrf_id"]
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.setProperty("previous", combo.currentIndex())
        table.setCellWidget(row, 3, combo)
        status = QTableWidgetItem()
        table.setItem(row, 4, status)
        row_entry = (item, combo, status)
        self.network_rows.append(row_entry)
        combo.activated.connect(lambda _index, row_entry=row_entry: self._vrf_chosen(row_entry))
        self._update_network_row(row_entry, item["create"])

    def _vrf_chosen(self, entry):
        _item, combo, _status = entry
        if combo.currentData() == self.NEW:
            name = ask_text(self, "Neue VRF", "Name der neuen VRF. Sie wird beim Übertragen mit dem Mandanten "
                                              "des Switches angelegt.")
            if not name:
                combo.setCurrentIndex(combo.property("previous"))
                return
            self._new_vrf_names.append(name)
            # Offer the new VRF in every network row.
            for _other_item, other, _other_status in self.network_rows:
                if other.findData(f"new:{name}") < 0:
                    other.insertItem(other.count() - 1, f"{name} (neu)", f"new:{name}")
            combo.setCurrentIndex(combo.findData(f"new:{name}"))
        combo.setProperty("previous", combo.currentIndex())
        self._update_network_row(entry)

    def _update_network_row(self, entry, create=None):
        item, combo, status = entry
        if combo.currentData() in item["existing"]:
            status.setText("vorhanden")
            status.setFlags(Qt.ItemIsEnabled)
            status.setData(Qt.CheckStateRole, None)
            status.setForeground(QColor(theme.color("text_muted")))
            return
        status.setText("fehlt – anlegen")
        status.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        if create is not None or status.data(Qt.CheckStateRole) is None:
            status.setCheckState(Qt.Checked if create is not False else Qt.Unchecked)
        status.setForeground(QColor(theme.color("warning")))

    def _filter_interfaces(self, only_unclear):
        for row, _switch, item, _combo in self.interface_rows:
            self.interface_table.setRowHidden(row, only_unclear and item["method"] == "name")

    def accept(self):
        result = {switch["id"]: {"interface_map": dict(switch.get("interface_map") or {}),
                                 "neighbor_devices": dict(switch.get("neighbor_devices") or {})}
                  for switch, _plan in self.plans}
        # Members moved to another device: their port choices refer to the old
        # device and are dropped, so the ports are matched again automatically.
        moved = set()
        for switch, entry, combo in self.stack_rows:
            chosen = int(combo.valid_data() or 0)
            result[switch["id"]].setdefault("stack_devices", {})[str(entry["member"])] = chosen
            if chosen != (entry["device_id"] or 0) and entry["device_id"]:
                moved.add((switch["id"], entry["device_id"]))
        used = {}
        for _row, switch, item, combo in self.interface_rows:
            if (switch["id"], item.get("owner_id")) in moved:
                result[switch["id"]]["interface_map"].pop(item["name"], None)
                continue
            chosen = combo.currentData()
            target = "" if chosen == self.NEW else chosen
            mapping = result[switch["id"]]["interface_map"]
            # Stack members have their own devices, so equal names are only a clash on the same device.
            key = (switch["id"], item.get("owner_id"), target)
            if target and key in used:
                self.show_error(f"{switch['name']}: „{target}“ ist mehreren Schnittstellen zugeordnet "
                                f"({used[key]}, {item['name']}).")
                return
            if target:
                used[key] = item["name"]
            # Plain name matches stay automatic; everything else is remembered.
            if item["method"] == "name" and target == item["netbox_name"]:
                mapping.pop(item["name"], None)
            else:
                mapping[item["name"]] = target
        for switch, neighbor, combo in self.neighbor_rows:
            chosen = combo.valid_data()
            chosen = int(chosen or 0)
            stored = result[switch["id"]]["neighbor_devices"]
            if neighbor["method"] == "auto" and chosen == (neighbor.get("device_id") or 0):
                stored.pop(neighbor["local_port"], None)
            else:
                stored[neighbor["local_port"]] = chosen
        network_map = dict(self.previous_network_map)
        for item, combo, status in self.network_rows:
            chosen = combo.currentData()
            new_name = chosen[4:] if isinstance(chosen, str) and chosen.startswith("new:") else ""
            network_map[item["network"]] = {
                "vrf_id": chosen if isinstance(chosen, int) and chosen else None,
                "vrf_name": new_name,
                "create": status.data(Qt.CheckStateRole) is not None and status.checkState() == Qt.Checked,
            }
        self.network_map = network_map
        vlan_map = dict(self.previous_vlan_map)
        for item, combo, name, tenant in self.vlan_rows:
            chosen = combo.currentData()
            if chosen == self.NEW:
                if not name.text().strip():
                    self.show_error(f"Bitte einen Namen für das neue VLAN {item['vid']} eingeben.")
                    return
                choice = {"action": "new", "name": name.text().strip(), "tenant": tenant.currentData()}
            elif chosen == self.IGNORE:
                choice = {"action": "ignore"}
            elif chosen:
                choice = {"action": "map", "id": chosen}
            else:
                choice = {"action": "skip"}
            vlan_map[str(item["vid"])] = choice
        self.vlan_map = vlan_map
        self.result = result
        super().accept()


class RawDataDialog(_Dialog):
    SECTIONS = {
        "interfaces": "Schnittstellen", "ipv4_interfaces": "IPv4", "ipv6_interfaces": "IPv6",
        "neighbors": "LLDP", "cdp_neighbors": "CDP", "vlan_config": "VLAN-Konfiguration", "lldp_status": "LLDP-Status",
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
