import copy
import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QMenu, QStackedWidget

from netbox_sync.core.virtualization import SOURCE_TYPES, source_endpoint
from netbox_sync.ui import widgets
from netbox_sync.ui.dialogs import SourceDialog
from netbox_sync.ui.icons import icon
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Badge, Card, EmptyState, PasswordField, Table, button, checkbox, label


class ConnectionsPage(Page):
    title = "Verbindungen"
    subtitle = "NetBox-Ziel und Virtualisierungsquellen des Kundenprofils. Änderungen werden automatisch gespeichert."

    def __init__(self, window):
        super().__init__(window)
        self._loading = False

        # NetBox ----------------------------------------------------------
        netbox_card = Card("NetBox", "Ziel aller Synchronisierungen. Benötigt einen API-Token mit Schreibrechten.")
        self.status_badge = Badge()
        netbox_card.add_action(self.status_badge)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://netbox.example.de")
        self.token = PasswordField("API-Token")
        self.ignore_ssl = checkbox("Selbstsigniertes Zertifikat akzeptieren (TLS-Prüfung deaktivieren)")
        form.addRow("URL", self.url)
        form.addRow("API-Token", self.token)
        form.addRow("", self.ignore_ssl)
        netbox_card.add(form)
        test_row = QHBoxLayout()
        self.test_button = button("Verbindung testen", "zap", on_click=self.test_netbox)
        self.status_text = label("", muted=True, wrap=True)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.status_text, 1)
        netbox_card.add(test_row)
        self.body.addWidget(netbox_card)

        # Sources ---------------------------------------------------------
        sources_card = Card("Virtualisierungsquellen", "VMware vCenter/ESXi und Proxmox VE – beliebig viele pro Profil.")
        self.add_button = sources_card.add_action(button("Quelle hinzufügen", "plus", "primary", on_click=self.add_source))
        self.sources_card = sources_card
        columns = [
            Column("enabled", "Aktiv", 70, value=lambda row: ""),
            Column("name", "Name", 220),
            Column("type", "Typ", 140, value=lambda row: SOURCE_TYPES.get(row.get("type"), row.get("type"))),
            Column("endpoint", "Endpunkt", 320, value=source_endpoint),
            Column("tls", "TLS", 140, value=lambda row: "nicht geprüft" if row.get("ignore_ssl") else "geprüft",
                   color=lambda row: "warning" if row.get("ignore_ssl") else "text_muted"),
        ]
        self.model = RowTableModel(columns, checkable=True, row_id=lambda row: row["id"])
        self.model.checked_changed.connect(self._enabled_changed)
        self.proxy = FilterProxy(self.model)
        self.table = Table(stretch_column=3)
        self.table.setModel(self.proxy)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.table.doubleClicked.connect(lambda _index: self.edit_source())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)

        self.sources_stack = QStackedWidget()
        self.sources_empty = EmptyState("server", "Noch keine Quelle",
                                        "Füge einen vCenter-, ESXi- oder Proxmox-Zugang hinzu, um VMs zu laden.",
                                        "Quelle hinzufügen", self.add_source)
        self.sources_stack.addWidget(self.sources_empty)
        self.sources_stack.addWidget(self.table)
        sources_card.add(self.sources_stack, 1)
        toolbar = QHBoxLayout()
        self.edit_button = button("Bearbeiten", "pencil", on_click=self.edit_source)
        self.duplicate_button = button("Duplizieren", "copy", on_click=self.duplicate_source)
        self.remove_button = button("Entfernen", "trash", "danger", on_click=self.remove_source)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.duplicate_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.remove_button)
        self.source_toolbar = toolbar
        sources_card.add(toolbar)
        self.body.addWidget(sources_card, 1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._store_netbox)
        self.url.textEdited.connect(self._save_timer.start)
        self.token.textEdited.connect(self._save_timer.start)
        self.ignore_ssl.toggled.connect(self._store_netbox)
        self.url.editingFinished.connect(self._normalize_url)

        self.state.profile_switched.connect(self.load_profile)
        self.state.netbox_changed.connect(self.update_status)
        self.load_profile()

    # ------------------------------------------------------------------

    def load_profile(self):
        profile = self.state.profile
        self._loading = True
        self.setEnabled(profile is not None)
        self.url.setText(profile.get("netbox_url", "") if profile else "")
        self.token.setText(profile.get("netbox_token", "") if profile else "")
        self.ignore_ssl.setChecked(bool(profile.get("netbox_ignore_ssl")) if profile else False)
        self._loading = False
        self.refresh_sources()
        self.update_status()

    def _normalize_url(self):
        text = self.url.text().strip().rstrip("/")
        if text and not text.startswith(("http://", "https://")):
            text = f"https://{text}"
        if text.endswith("/api"):
            text = text[:-4]
        if text != self.url.text():
            self.url.setText(text)
            self._store_netbox()

    def _store_netbox(self, *_args):
        if self._loading:
            return
        self._save_timer.stop()
        self.state.edit_profile(
            netbox_url=self.url.text().strip(),
            netbox_token=self.token.text().strip(),
            netbox_ignore_ssl=self.ignore_ssl.isChecked(),
        )

    def update_status(self):
        status = self.state.netbox_status
        configured = self.state.netbox_configured()
        if not configured:
            self.status_badge.set("Nicht eingerichtet", "warning")
            self.status_text.setText("")
        elif status == "ok":
            self.status_badge.set(f"Verbunden · {self.state.netbox_version}", "success")
            self.status_text.setText("")
        elif status == "error":
            self.status_badge.set("Fehler", "error")
            self.status_text.setText(self.state.netbox_message)
        else:
            self.status_badge.set("Nicht getestet", "neutral")
            self.status_text.setText("")
        self.status_text.setProperty("muted", status != "error")
        self.status_text.setStyleSheet(f"color: {theme.color('danger')};" if status == "error" else "")

    def test_netbox(self):
        self._store_netbox()
        if not self.state.netbox_configured():
            self.status_text.setText("Bitte URL und API-Token eintragen.")
            return
        self.status_badge.set("Teste …", "neutral")
        self.actions.test_netbox()

    def set_busy(self, busy):
        self.test_button.setEnabled(not busy)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def refresh_sources(self):
        sources = self.state.sources
        self.model.checked = {source["id"] for source in sources if source.get("enabled", True)}
        self.model.set_rows(sources)
        has_sources = bool(sources)
        self.sources_stack.setCurrentIndex(1 if has_sources else 0)
        self.add_button.setVisible(has_sources)
        for widget in (self.edit_button, self.duplicate_button, self.remove_button):
            widget.setVisible(has_sources)
        self._update_buttons()

    def _enabled_changed(self):
        changed = False
        for source in self.state.sources:
            enabled = source["id"] in self.model.checked
            if source.get("enabled", True) != enabled:
                source["enabled"] = enabled
                changed = True
        if changed:
            self.state.touch()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.proxy.row_at(rows[0]) if rows else None

    def _update_buttons(self, *_args):
        selected = self._selected() is not None
        for widget in (self.edit_button, self.duplicate_button, self.remove_button):
            widgets.set_enabled(widget, selected, "Bitte zuerst eine Quelle in der Tabelle auswählen.")

    def add_source(self):
        if self.state.profile is None:
            return
        dialog = SourceDialog(self.window)
        if dialog.exec() and dialog.result:
            self.state.sources.append(dialog.result)
            self.state.touch()
            self.refresh_sources()
            self.window.toast(f"Quelle „{dialog.result['name']}“ hinzugefügt")

    def edit_source(self):
        source = self._selected()
        if not source:
            return
        dialog = SourceDialog(self.window, source)
        if dialog.exec() and dialog.result:
            index = self.state.sources.index(source)
            self.state.sources[index] = dialog.result
            self.state.touch()
            self.refresh_sources()

    def duplicate_source(self):
        source = self._selected()
        if not source:
            return
        clone = copy.deepcopy(source)
        clone["id"] = uuid.uuid4().hex
        clone["name"] = f"{source['name']} (Kopie)"
        dialog = SourceDialog(self.window, clone)
        if dialog.exec() and dialog.result:
            self.state.sources.append(dialog.result)
            self.state.touch()
            self.refresh_sources()

    def remove_source(self):
        source = self._selected()
        if not source:
            return
        if widgets.confirm(self.window, "Quelle entfernen", f"Quelle „{source['name']}“ aus dem Profil entfernen?",
                           ok_text="Entfernen", destructive=True):
            self.state.sources.remove(source)
            self.state.touch()
            self.refresh_sources()

    def _context_menu(self, position):
        if not self._selected():
            return
        menu = QMenu(self)
        menu.addAction(icon("pencil", theme.color("text_muted")), "Bearbeiten …", self.edit_source)
        menu.addAction(icon("copy", theme.color("text_muted")), "Duplizieren …", self.duplicate_source)
        menu.addSeparator()
        menu.addAction(icon("trash", theme.color("danger")), "Entfernen", self.remove_source)
        menu.exec(self.table.viewport().mapToGlobal(position))
