from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
)

from netbox_sync.core.ap_sync import APImportError, APSyncEngine, ap_key, parse_csv
from netbox_sync.core.netbox import related_id
from netbox_sync.ui import widgets
from netbox_sync.ui.client_dialogs import ClientPreviewDialog
from netbox_sync.ui.dialogs import _Dialog
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.tasks import logger
from netbox_sync.ui.widgets import (
    Card,
    EmptyState,
    SearchableComboBox,
    SearchField,
    Table,
    button,
    checkbox,
    form_dialog_buttons,
    label,
)

STATE_TEXT = {"new": "neu", "known": "in NetBox", "check": "prüfen", "netbox_only": "nur in NetBox"}
STATE_DOT = {"new": "accent", "known": "success", "check": "warning", "netbox_only": "text_faint"}
ROLE_HINTS = ("access point", "accesspoint", "access-point", "wlan", "wifi", "ap")


class SiteMappingDialog(_Dialog):
    """Assign NetBox sites to Sophos sites and to APs without a Sophos site."""

    def __init__(self, parent, hints, homeless, sites, site_map, ap_sites):
        super().__init__(parent, "Standorte zuordnen",
                         "Neue Access Points werden am zugeordneten NetBox-Standort angelegt und erhalten dessen "
                         "Mandanten. Zur Auswahl stehen die Standorte der Mandantengruppe.", width=760)
        self.resize(860, 620)
        items = [("– nicht zugeordnet –", 0)] + [
            (f"{site['name']}" + (f" ({site['tenant']})" if site["tenant"] else ""), site["id"]) for site in sites]
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Sophos-Standort / Access Point", "APs", "NetBox-Standort"])
        self.rows = []
        for hint, count, suggestion in hints:
            self._row(hint, str(count), items, site_map.get(hint) or suggestion or 0, ("site", hint))
        for ap in homeless:
            self._row(f"{ap['name']} (ohne Sophos-Standort)", "1", items, ap_sites.get(ap_key(ap)) or 0,
                      ("ap", ap_key(ap)))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((330, 50, 360)):
            self.table.setColumnWidth(column, width)
        self.root.addWidget(self.table, 1)
        buttons, _ok = form_dialog_buttons(self, "Übernehmen")
        self.root.addWidget(buttons)
        self.site_map, self.ap_sites = dict(site_map), dict(ap_sites)

    def _row(self, text, count, items, current, key):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(text))
        self.table.setItem(row, 1, QTableWidgetItem(count))
        combo = SearchableComboBox("Standort suchen …")
        combo.set_items(items, current)
        self.table.setCellWidget(row, 2, combo)
        self.rows.append((key, combo))

    def accept(self):
        for (kind, key), combo in self.rows:
            target = self.site_map if kind == "site" else self.ap_sites
            value = combo.valid_data()
            if value:
                target[key] = value
            else:
                target.pop(key, None)
        super().accept()


class AccessPointsPage(Page):
    title = "Access Points"
    subtitle = ("Sophos-APs aus einem Export von Sophos Central laden, mit NetBox abgleichen (global über "
                "Seriennummer und MAC, im Mandanten über den Namen) und fehlende anlegen.")

    def __init__(self, window):
        super().__init__(window)
        self.aps = []
        self.only_netbox = []
        self.engine = None
        self.tenants = []
        self.file_name = ""

        source = Card("Sophos-Export", "In Sophos Central: Wireless › Access Points › Exportieren (CSV).")
        self.load_button = source.add_action(button("CSV laden …", "folder", on_click=self.load_file))
        self.sync_button = source.add_action(button("Nach NetBox übertragen …", "upload", "primary",
                                                    on_click=self.sync))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        tenant_row = QHBoxLayout()
        self.tenant = SearchableComboBox("Mandant wählen …")
        self.tenant.setMinimumWidth(320)
        tenant_row.addWidget(self.tenant)
        self.tenant_hint = label("Standorte und vorhandene APs werden in seiner Mandantengruppe gesucht.", muted=True)
        tenant_row.addWidget(self.tenant_hint, 1)
        form.addRow("Mandant *", tenant_row)
        self.role = SearchableComboBox("Geräterolle wählen …")
        self.role.setMinimumWidth(320)
        form.addRow("Geräterolle", self.role)
        self.rename = checkbox("Namen vorhandener Geräte aus Sophos übernehmen", True)
        form.addRow("", self.rename)
        source.add(form)
        self.body.addWidget(source)

        result = Card("Abgleich")
        self.sites_button = result.add_action(button("Standorte zuordnen …", "globe", on_click=self.map_sites))
        self.refresh_button = result.add_action(button("Neu abgleichen", "refresh", on_click=self.compare))
        filters = QHBoxLayout()
        self.search = SearchField("Name, S/N, MAC, IP, Standort …")
        self.only_open = checkbox("Nur neue und zu prüfende")
        filters.addWidget(self.search, 2)
        filters.addWidget(self.only_open)
        filters.addStretch(1)
        result.add(filters)
        columns = [
            Column("state", "Status", 110, value=lambda row: STATE_TEXT.get(row["state"], ""),
                   dot=lambda row: "warning" if row.get("issues") and row["state"] == "new"
                   else STATE_DOT.get(row["state"]), tooltip=lambda row: "\n".join(row.get("issues", []))),
            Column("name", "Name", 230),
            Column("model", "Modell", 80),
            Column("serial", "Seriennummer", 140),
            Column("mac", "MAC-Adresse", 135),
            Column("ip", "IP-Adresse", 110),
            Column("site_hint", "Sophos-Standort", 170),
            Column("netbox_site", "NetBox-Standort", 190,
                   color=lambda row: "warning" if row["state"] == "new" and not row.get("site_id") else None),
            Column("netbox_device", "NetBox-Gerät", 170),
            Column("method", "Gefunden über", 110, color=lambda row: "text_muted"),
            Column("issues", "Hinweise", 260, value=lambda row: "; ".join(row.get("issues", [])),
                   color=lambda row: "warning"),
        ]
        self.model = RowTableModel(columns, row_id=lambda row: row["key"])
        self.proxy = FilterProxy(self.model)
        self.table = Table()
        self.table.setModel(self.proxy)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.stack = QStackedWidget()
        self.stack.addWidget(EmptyState("upload", "Noch kein Export geladen",
                                        "Die CSV-Datei aus Sophos Central laden. Danach wird automatisch mit NetBox "
                                        "abgeglichen – geschrieben wird erst nach der Vorschau.",
                                        "CSV laden …", self.load_file))
        self.stack.addWidget(self.table)
        result.add(self.stack, 1)
        self.summary = label("", muted=True)
        result.add(self.summary)
        self.body.addWidget(result, 1)

        self.search.textChanged.connect(self.proxy.set_text)
        self.only_open.toggled.connect(lambda value: self.proxy.set_predicate(
            "open", (lambda row: row["state"] in ("new", "check")) if value else None))
        self.tenant.activated.connect(lambda _index: self._tenant_chosen())
        self.role.activated.connect(lambda _index: self._store(role_id=self.role.valid_data()))
        self.rename.toggled.connect(lambda value: self._store(rename=value))
        self.state.profile_switched.connect(self._profile_switched)
        self._profile_switched()

    # ------------------------------------------------------------------
    # Profile settings
    # ------------------------------------------------------------------

    def _settings(self):
        return self.state.profile.setdefault("access_points", {}) if self.state.profile else {}

    def _store(self, **values):
        if self.state.profile is None:
            return
        self._settings().update(values)
        self.state.touch()

    def _profile_switched(self):
        self.aps, self.only_netbox, self.engine, self.tenants = [], [], None, []
        self.tenant.set_items([])
        self.role.set_items([])
        self.rename.blockSignals(True)
        self.rename.setChecked(self._settings().get("rename", True))
        self.rename.blockSignals(False)
        self._refresh()

    def on_show(self):
        if not self.tenants and self._netbox_ready() and not self.tasks.busy:
            self._load_lists()

    def set_busy(self, busy):
        for widget in (self.load_button, self.sites_button, self.refresh_button):
            widget.setEnabled(not busy)
        self._update_buttons()

    def _netbox_ready(self):
        return bool(self.state.profile) and not self.state.offline and self.state.netbox_configured()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_file(self):
        path, _filter = QFileDialog.getOpenFileName(self.window, "Sophos-Export laden", "",
                                                    "CSV-Dateien (*.csv);;Alle Dateien (*)")
        if not path:
            return
        try:
            self.aps = parse_csv(Path(path).read_bytes())
        except (OSError, APImportError) as error:
            widgets.message(self.window, "Datei nicht lesbar", str(error), "warning")
            return
        self.file_name = Path(path).name
        self.only_netbox, self.engine = [], None
        logger("app").info("Sophos-Export %s: %s Access Points", self.file_name, len(self.aps))
        self._refresh()
        if self._netbox_ready():
            self._load_lists(then_compare=True)

    def _load_lists(self, then_compare=False):
        connect = self.actions._connector()

        def work(ctx):
            nb = connect(ctx)
            ctx.log("Lade Mandanten und Geräterollen …")
            tenants = sorted(((item.name, item.id) for item in nb.tenancy.tenants.all()), key=lambda x: x[0].casefold())
            roles = sorted(((item.name, item.id) for item in nb.dcim.device_roles.all()), key=lambda x: x[0].casefold())
            return tenants, roles

        def done(result):
            self.tenants, roles = result
            settings = self._settings()
            self.tenant.set_items(self.tenants, settings.get("tenant_id"))
            role_id = settings.get("role_id") or next(
                (role_id for name, role_id in roles if name.casefold() in ROLE_HINTS
                 or "access point" in name.casefold()), None)
            self.role.set_items(roles, role_id)
            if then_compare and self.tenant.valid_data():
                QTimer.singleShot(0, self.compare)

        self.tasks.run(work, title="Mandanten laden", area="app", on_success=done,
                       on_error=self.actions._netbox_failed)

    def _tenant_chosen(self):
        self._store(tenant_id=self.tenant.valid_data())
        if self.aps and self.tenant.valid_data():
            self.compare()

    def compare(self):
        tenant_id = self.tenant.valid_data()
        if not self.aps or not tenant_id or self.tasks.busy:
            if not tenant_id and self.aps:
                self.window.toast("Bitte zuerst den Mandanten wählen.", "info")
            return
        connect = self.actions._connector()
        aps = [dict(ap, issues=[issue for issue in ap["issues"] if not issue.startswith(("NetBox-Gerät gehört",
                                                                                           "Seriennummer in NetBox"))])
               for ap in self.aps]

        def work(ctx):
            engine = APSyncEngine(connect(ctx), ctx.log)
            ctx.log("Lade Standorte und vorhandene Access Points der Mandantengruppe …")
            engine.load_context(tenant_id)
            ctx.log(f"Suche {len(aps)} Access Points in NetBox (Seriennummer, MAC, Name) …")
            only_netbox = engine.match(aps)
            return engine, aps, only_netbox

        def done(result):
            self.engine, self.aps, self.only_netbox = result
            found = sum(1 for ap in self.aps if ap.get("device"))
            logger("app").log(25, "Abgleich: %s von %s APs in NetBox, %s nur in NetBox", found, len(self.aps),
                              len(self.only_netbox))
            self._refresh()

        self.tasks.run(work, title="Access Points abgleichen", area="app", on_success=done,
                       on_error=self.actions._netbox_failed)

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _site_for(self, ap):
        settings = self._settings()
        chosen = (settings.get("ap_sites") or {}).get(ap_key(ap)) or (settings.get("site_map") or {}).get(
            ap["site_hint"])
        if chosen:
            return chosen
        return self.engine.suggest_site(ap["site_hint"]) if self.engine else None

    def _site_name(self, site_id):
        sites = self.engine.ap_sites if self.engine else []
        return next((site["name"] for site in sites if site["id"] == site_id), "")

    def _refresh(self):
        rows = []
        for ap in self.aps:
            device = ap.get("device")
            site_id = None if device else self._site_for(ap)
            if device:
                site = getattr(device, "site", None)
                site_name = getattr(site, "name", "") or self._site_name(getattr(site, "id", site))
            elif self.engine:
                site_name = self._site_name(site_id) if site_id else "– zuordnen –"
            else:
                site_name = ""
            state = "new" if not device else ("check" if ap["issues"] else "known")
            rows.append(dict(ap, key=ap_key(ap), state=state if self.engine else "", site_id=site_id,
                             netbox_site=site_name, netbox_device=device.name if device else "",
                             method=ap.get("method", "")))
        for device in self.only_netbox:
            rows.append({"key": f"netbox-{device.id}", "state": "netbox_only", "name": device.name,
                         "model": getattr(getattr(device, "device_type", None), "model", "") or "",
                         "serial": getattr(device, "serial", "") or "", "mac": "",
                         "ip": str(getattr(getattr(device, "primary_ip4", None), "address", "") or "").split("/")[0],
                         "site_hint": "", "netbox_site": getattr(getattr(device, "site", None), "name", "")
                         or self._site_name(related_id(device, "site")),
                         "netbox_device": device.name, "method": "",
                         "issues": ["In NetBox, aber nicht im Sophos-Export"]})
        self.model.set_rows(rows)
        self.stack.setCurrentIndex(1 if rows else 0)
        counts = {key: sum(1 for row in rows if row["state"] == key) for key in STATE_TEXT}
        parts = [f"{len(self.aps)} Access Points" + (f" aus {self.file_name}" if self.file_name else "")]
        if self.engine:
            parts += [f"{counts['known']} in NetBox", f"{counts['new']} neu", f"{counts['check']} zu prüfen",
                      f"{counts['netbox_only']} nur in NetBox"]
            missing_sites = sum(1 for row in rows if row["state"] == "new" and not row.get("site_id"))
            if missing_sites:
                parts.append(f"{missing_sites} ohne NetBox-Standort")
        elif self.aps:
            parts.append("Mandant wählen, um mit NetBox abzugleichen" if self._netbox_ready()
                         else "kein NetBox-Abgleich (kein Zugang im Profil oder Offline-Modus)")
        self.summary.setText(" · ".join(parts) if self.aps or self.only_netbox else "")
        self._update_buttons()

    def _update_buttons(self):
        busy = self.tasks.busy
        widgets.set_enabled(self.sync_button, bool(self.engine) and not busy,
                            "Bitte Export laden und Mandanten wählen." if not self.engine else "Bitte warten …")
        widgets.set_enabled(self.sites_button, bool(self.engine) and not busy, "Erst mit NetBox abgleichen.")
        widgets.set_enabled(self.refresh_button, bool(self.aps) and not busy, "Erst einen Export laden.")

    # ------------------------------------------------------------------
    # Sites and transfer
    # ------------------------------------------------------------------

    def _new_aps(self):
        return [ap for ap in self.aps if not ap.get("device")]

    def map_sites(self):
        if not self.engine:
            return False
        new = self._new_aps()
        hints = {}
        for ap in new:
            if ap["site_hint"]:
                hints[ap["site_hint"]] = hints.get(ap["site_hint"], 0) + 1
        settings = self._settings()
        dialog = SiteMappingDialog(
            self.window, [(hint, count, self.engine.suggest_site(hint)) for hint, count in sorted(hints.items())],
            [ap for ap in new if not ap["site_hint"]], self.engine.ap_sites,
            settings.get("site_map") or {}, settings.get("ap_sites") or {})
        if not dialog.exec():
            return False
        self._store(site_map=dialog.site_map, ap_sites=dialog.ap_sites)
        self._refresh()
        return True

    def sync(self):
        if not self.engine or self.tasks.busy:
            return
        new = self._new_aps()
        role_id = self.role.valid_data()
        if new and not role_id:
            widgets.message(self.window, "Geräterolle fehlt", "Für neue Access Points bitte eine Geräterolle wählen.",
                            "info")
            return
        if any(not self._site_for(ap) for ap in new) and not self.map_sites():
            return
        sites = {ap_key(ap): self._site_for(ap) for ap in new}
        missing = [ap["name"] for ap in new if not sites.get(ap_key(ap))]
        if missing and not widgets.confirm(
                self.window, "Standorte fehlen", f"{len(missing)} neue Access Points haben keinen NetBox-Standort und "
                "werden übersprungen.", ok_text="Trotzdem fortfahren", details="\n".join(missing)):
            return
        engine, aps, rename = self.engine, list(self.aps), self.rename.isChecked()

        def preview(ctx):
            ctx.log("Berechne Änderungen …")
            return engine.sync(aps, sites, role_id, rename, dry_run=True)

        def show(report):
            def ask():
                if ClientPreviewDialog(self.window, report).exec():
                    self.tasks.run(lambda ctx: engine.sync(aps, sites, role_id, rename, dry_run=False),
                                   title="Access Points übertragen", area="app", on_success=applied,
                                   on_error=self.actions._netbox_failed)
            QTimer.singleShot(0, ask)

        def applied(report):
            for error in report["errors"]:
                logger("app").error("%s", error)
            text = (f"{report['devices_created']} Access Points angelegt, {report['devices_updated']} aktualisiert"
                    + (f", {len(report['errors'])} Fehler" if report["errors"] else ""))
            logger("app").log(25, "%s", text)
            self.window.toast(text, "warning" if report["errors"] else "success")
            QTimer.singleShot(0, self.compare)

        self.tasks.run(preview, title="Änderungen berechnen", area="app", on_success=show,
                       on_error=self.actions._netbox_failed)
