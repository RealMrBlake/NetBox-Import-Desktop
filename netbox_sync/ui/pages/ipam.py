import ipaddress

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from netbox_sync.core.vm_sync import vrf_label
from netbox_sync.ui import widgets
from netbox_sync.ui.pages import Page
from netbox_sync.ui.widgets import Banner, Card, EmptyState, SearchableComboBox, button, label

GLOBAL = "__global__"


class IpamPage(Page):
    title = "VRFs & Netze"
    subtitle = "Mehrere VRFs oder Präfixe für einen Mandanten in einem Schritt in NetBox anlegen."

    def __init__(self, window):
        super().__init__(window)
        self.stack = QStackedWidget()
        self.empty = EmptyState("globe", "NetBox-Daten werden benötigt",
                                "Für dieses Werkzeug werden Mandanten und VRFs aus NetBox geladen.",
                                "NetBox-Daten laden", lambda: self.actions.load_targets())
        self.stack.addWidget(self.empty)

        content = QWidget()
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        tenant_row = QFormLayout()
        self.tenant = SearchableComboBox("Mandant suchen …")
        self.tenant.setMinimumWidth(320)
        tenant_row.addRow("Mandant", self.tenant)

        vrf_card = Card("VRFs anlegen", "Ein Name pro Zeile. Bereits vorhandene VRFs des Mandanten werden übersprungen.")
        self.vrf_names = QPlainTextEdit()
        self.vrf_names.setPlaceholderText("Kunde - Produktion\nKunde - DMZ\nKunde - Management")
        vrf_card.add(self.vrf_names, 1)
        self.vrf_button = button("VRFs anlegen", "plus", "primary", on_click=self.create_vrfs)
        vrf_card.add(self.vrf_button)

        prefix_card = Card("Präfixe anlegen", "Ein Netz pro Zeile, z. B. 10.10.100.0/24 oder 2001:db8:10::/64.")
        form = QFormLayout()
        self.vrf = QComboBox()
        form.addRow("Ziel-VRF", self.vrf)
        prefix_card.add(form)
        self.prefixes = QPlainTextEdit()
        self.prefixes.setPlaceholderText("10.10.100.0/24\n10.10.101.0/24")
        prefix_card.add(self.prefixes, 1)
        self.prefix_hint = label("", muted=True, wrap=True)
        prefix_card.add(self.prefix_hint)
        self.prefix_button = button("Präfixe anlegen", "plus", "primary", on_click=self.create_prefixes)
        prefix_card.add(self.prefix_button)

        layout.addWidget(vrf_card, 1)
        layout.addWidget(prefix_card, 1)
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(16)
        outer_layout.addLayout(tenant_row)
        self.result = Banner("", "success", closable=True)
        self.result.hide()
        outer_layout.addWidget(self.result)
        outer_layout.addWidget(content, 1)
        self.stack.addWidget(outer)
        self.body.addWidget(self.stack, 1)

        self.tenant.currentIndexChanged.connect(self.refresh_vrfs)
        self.prefixes.textChanged.connect(self._validate_prefixes)
        self.state.netbox_changed.connect(self.refresh)
        self.state.profile_switched.connect(self.refresh)
        self.refresh()

    def on_show(self):
        if self.state.engine is None and self.state.netbox_configured() and not self.tasks.busy:
            self.actions.load_targets()

    def refresh(self):
        engine = self.state.engine
        self.stack.setCurrentIndex(1 if engine else 0)
        if not engine:
            return
        current = self.tenant.valid_data()
        self.tenant.set_items(sorted(engine.tenants, key=str.casefold), current)
        self.refresh_vrfs()

    def refresh_vrfs(self, *_args):
        engine = self.state.engine
        tenant = self.tenant.valid_data()
        current = self.vrf.currentData()
        self.vrf.clear()
        if engine and tenant:
            for vrf in engine.tenant_vrfs(engine.tenants[tenant]):
                self.vrf.addItem(vrf_label(vrf), vrf.id)
        self.vrf.addItem("Global (ohne VRF)", GLOBAL)
        self.vrf.setCurrentIndex(max(0, self.vrf.findData(current)))

    @staticmethod
    def _lines(widget):
        return list(dict.fromkeys(line.strip() for line in widget.toPlainText().splitlines() if line.strip()))

    def _validate_prefixes(self):
        invalid, adjusted = [], []
        for line in self._lines(self.prefixes):
            try:
                network = ipaddress.ip_network(line, strict=False)
                if str(network) != line:
                    adjusted.append(f"{line} → {network}")
            except ValueError:
                invalid.append(line)
        parts = []
        if invalid:
            parts.append("Ungültig: " + ", ".join(invalid[:5]))
        if adjusted:
            parts.append("Wird korrigiert: " + ", ".join(adjusted[:5]))
        self.prefix_hint.setText(" · ".join(parts))

    def set_busy(self, busy):
        self.vrf_button.setEnabled(not busy)
        self.prefix_button.setEnabled(not busy)

    def _tenant_or_warn(self):
        tenant = self.tenant.valid_data()
        if not tenant:
            widgets.message(self.window, "Mandant fehlt", "Bitte zuerst einen Mandanten auswählen.", "info")
        return tenant

    def create_vrfs(self):
        tenant = self._tenant_or_warn()
        names = self._lines(self.vrf_names)
        if not tenant or not names:
            return
        engine = self.state.engine

        def done(result):
            created, reused, errors = result
            self.refresh_vrfs()
            text = f"VRFs für {tenant}: {created} angelegt, {reused} bereits vorhanden."
            if errors:
                self.result.set(text + "<br>" + "<br>".join(errors), "warning")
            else:
                self.vrf_names.clear()
                self.result.set(text, "success")
            self.result.show()

        self.tasks.run(lambda _ctx: engine.create_vrfs_batch(tenant, names), title="VRFs anlegen", area="ipam",
                       on_success=done)

    def create_prefixes(self):
        tenant = self._tenant_or_warn()
        networks = self._lines(self.prefixes)
        if not tenant or not networks:
            return
        vrf_data = self.vrf.currentData()
        vrf_id = None if vrf_data == GLOBAL else vrf_data
        vrf_name = self.vrf.currentText()
        if not widgets.confirm(self.window, "Präfixe anlegen",
                               f"{len(networks)} Präfix(e) für {tenant} in „{vrf_name}“ anlegen?", ok_text="Anlegen"):
            return
        engine = self.state.engine

        def done(result):
            created, reused, canonical, errors = result
            text = f"Präfixe: {created} angelegt, {reused} bereits vorhanden (Mandant aktualisiert)."
            if errors:
                self.prefixes.setPlainText("\n".join(error.split(":")[0] for error in errors))
                self.result.set(text + "<br>Fehler:<br>" + "<br>".join(errors), "warning")
            else:
                self.prefixes.clear()
                self.result.set(text, "success")
            self.result.show()

        self.tasks.run(lambda _ctx: engine.create_prefixes_batch(tenant, vrf_id, networks), title="Präfixe anlegen",
                       area="ipam", on_success=done)
