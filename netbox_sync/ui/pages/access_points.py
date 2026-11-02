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

STATE_TEXT = {"new": "neu", "known": "in NetBox", "check": "prüfen", "netbox_only": "nur in NetBox",
              "ignored": "ignoriert"}
STATE_DOT = {"new": "accent", "known": "success", "check": "warning", "netbox_only": "text_faint",
             "ignored": "text_faint"}
ROLE_HINTS = ("access point", "accesspoint", "access-point", "wlan", "wifi", "ap")


def placement_value(value):
    """Stored choice as ``{"site": ID or None, "tenant": ID or None}`` (4.9.0 stored only a site ID)."""
    if isinstance(value, dict):
        return {"site": value.get("site") or None, "tenant": value.get("tenant") or None}
    return {"site": value or None, "tenant": None}


class SiteMappingDialog(_Dialog):
    """Assign an optional NetBox site and the mandatory tenant to Sophos sites
    and to APs without a Sophos site."""

    def __init__(self, parent, rows, sites, tenants):
        super().__init__(parent, "Standorte & Mandanten zuordnen",
                         "Der Standort ist optional – APs ohne Zuordnung kommen an den Standard-Standort, weil NetBox "
                         "für jedes Gerät einen Standort verlangt. Der Mandant ist Pflicht und wird bewusst gewählt. Zeilen ohne "
                         "Standort und ohne Mandant werden ignoriert.",
                         width=860)
        self.resize(980, 620)
        site_items = [("– kein Standort (Standard-Standort) –", 0)] + [
            (site["name"] + (f" ({site['tenant']})" if site["tenant"] else ""), site["id"]) for site in sites]
        tenant_items = [(tenant["name"], tenant["id"]) for tenant in tenants]
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Sophos-Standort / Access Point", "APs", "NetBox-Standort (optional)",
                                              "Mandant *"])
        self.rows = []
        for text, count, key, current in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(text))
            self.table.setItem(row, 1, QTableWidgetItem(str(count)))
            site = SearchableComboBox("Standort suchen …")
            site.set_items(site_items, current["site"] or 0)
            tenant = SearchableComboBox("Mandant wählen …")
            # only a stored choice is shown – never a suggestion
            tenant.set_items(tenant_items, current["tenant"])
            self.table.setCellWidget(row, 2, site)
            self.table.setCellWidget(row, 3, tenant)
            self.rows.append((key, site, tenant))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((300, 50, 330, 240)):
            self.table.setColumnWidth(column, width)
        self.root.addWidget(self.table, 1)
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Übernehmen")
        self.root.addWidget(buttons)
        self.result = {}

    def accept(self):
        result = {}
        for key, site, tenant in self.rows:
            if tenant.valid_data() is None and site.valid_data():
                self.show_error(f"Bitte für „{key[1]}“ einen Mandanten wählen.")
                return
            result[key] = {"site": site.valid_data() or None, "tenant": tenant.valid_data()}
        self.result = result
        super().accept()


class AccessPointsPage(Page):
    title = "Access Points"
    subtitle = ("Sophos-APs aus einem Export von Sophos Central laden, mit NetBox abgleichen (global über "
                "Seriennummer, MAC und Name) und fehlende anlegen.")

    def __init__(self, window):
        super().__init__(window)
        self.aps = []
        self.only_netbox = []
        self.engine = None
        self.roles = []
        self.file_name = ""

        source = Card("Sophos-Export", "In Sophos Central: Wireless › Access Points › Exportieren (CSV).")
        self.load_button = source.add_action(button("CSV laden …", "folder", on_click=self.load_file))
        self.sync_button = source.add_action(button("Nach NetBox übertragen …", "upload", "primary",
                                                    on_click=self.sync))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        self.role = SearchableComboBox("Geräterolle wählen …")
        self.role.setMinimumWidth(320)
        form.addRow("Geräterolle", self.role)
        site_row = QHBoxLayout()
        self.default_site = SearchableComboBox("Standort wählen …")
        self.default_site.setMinimumWidth(320)
        site_row.addWidget(self.default_site)
        site_row.addWidget(label("Für neue APs ohne zugeordneten Standort – NetBox verlangt für jedes Gerät einen.",
                                 muted=True), 1)
        form.addRow("Standard-Standort", site_row)
        self.rename = checkbox("Namen vorhandener Geräte aus Sophos übernehmen", True)
        form.addRow("", self.rename)
        source.add(form)
        self.body.addWidget(source)

        result = Card("Abgleich")
        self.sites_button = result.add_action(button("Standorte && Mandanten …", "globe", on_click=self.map_sites))
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
                   color=lambda row: "warning" if row["state"] == "new" and not row.get("site_id")
                   else ("text_muted" if str(row.get("netbox_site", "")).startswith("Standard:") else None)),
            Column("tenant", "Mandant", 150),
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
        self.role.activated.connect(lambda _index: self._store(role_id=self.role.valid_data()))
        self.default_site.activated.connect(lambda _index: (self._store(default_site=self.default_site.valid_data()),
                                                            self._refresh()))
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
        self.aps, self.only_netbox, self.engine, self.roles = [], [], None, []
        self.role.set_items([])
        self.default_site.set_items([])
        self.rename.blockSignals(True)
        self.rename.setChecked(self._settings().get("rename", True))
        self.rename.blockSignals(False)
        self._refresh()

    def on_show(self):
        if not self.roles and self._netbox_ready() and not self.tasks.busy:
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
            ctx.log("Lade Geräterollen …")
            return sorted(((item.name, item.id) for item in nb.dcim.device_roles.all()), key=lambda x: x[0].casefold())

        def done(roles):
            self.roles = roles
            settings = self._settings()
            role_id = settings.get("role_id") or next(
                (role_id for name, role_id in roles if name.casefold() in ROLE_HINTS
                 or "access point" in name.casefold()), None)
            self.role.set_items(roles, role_id)
            if then_compare:
                QTimer.singleShot(0, self.compare)

        self.tasks.run(work, title="Geräterollen laden", area="app", on_success=done,
                       on_error=self.actions._netbox_failed)

    def compare(self):
        if not self.aps or self.tasks.busy:
            return
        connect = self.actions._connector()
        aps = [dict(ap, issues=[issue for issue in ap["issues"] if not issue.startswith(("NetBox-Gerät gehört",
                                                                                           "Seriennummer in NetBox"))])
               for ap in self.aps]

        def work(ctx):
            engine = APSyncEngine(connect(ctx), ctx.log)
            ctx.log("Lade Standorte, Mandanten und vorhandene Sophos-Geräte …")
            engine.load_context()
            ctx.log(f"Suche {len(aps)} Access Points in NetBox (Seriennummer, MAC, Name) …")
            only_netbox = engine.match(aps)
            return engine, aps, only_netbox

        def done(result):
            self.engine, self.aps, self.only_netbox = result
            self.default_site.set_items([(site["name"] + (f" ({site['tenant']})" if site["tenant"] else ""),
                                          site["id"]) for site in self.engine.ap_sites],
                                        self._settings().get("default_site"))
            found = sum(1 for ap in self.aps if ap.get("device"))
            logger("app").log(25, "Abgleich: %s von %s APs in NetBox, %s nur in NetBox", found, len(self.aps),
                              len(self.only_netbox))
            self._refresh()

        self.tasks.run(work, title="Access Points abgleichen", area="app", on_success=done,
                       on_error=self.actions._netbox_failed)

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _placement(self, ap):
        """Site (optional) and tenant for a new AP: stored choice, else the suggestion."""
        settings = self._settings()
        stored = (settings.get("ap_sites") or {}).get(ap_key(ap)) or (settings.get("site_map") or {}).get(
            ap["site_hint"])
        if stored:
            value = placement_value(stored)
            # neither site nor tenant: the user chose to leave these APs out
            return dict(value, stored=True, ignored=not value["site"] and not value["tenant"])
        site = self.engine.suggest_site(ap["site_hint"]) if self.engine else None
        return {"site": site, "tenant": None, "stored": False, "ignored": False}

    def _tenant_for(self, placement):
        return placement.get("tenant")

    def _site_name(self, site_id):
        sites = self.engine.ap_sites if self.engine else []
        return next((site["name"] for site in sites if site["id"] == site_id), "")

    def _tenant_name(self, tenant_id):
        tenants = self.engine.group_tenants if self.engine else []
        return next((tenant["name"] for tenant in tenants if tenant["id"] == tenant_id), "")

    def _refresh(self):
        rows = []
        default_site = self.default_site.valid_data()
        for ap in self.aps:
            device = ap.get("device")
            site_id = tenant_name = None
            if device:
                site = getattr(device, "site", None)
                site_name = getattr(site, "name", "") or self._site_name(getattr(site, "id", site))
                tenant_name = getattr(getattr(device, "tenant", None), "name", "") or self._tenant_name(
                    related_id(device, "tenant"))
            elif self.engine and self._placement(ap)["ignored"]:
                site_name = ""
            elif self.engine:
                placement = self._placement(ap)
                site_id = placement["site"] or default_site
                site_name = (self._site_name(placement["site"]) if placement["site"] else
                             f"Standard: {self._site_name(default_site)}" if default_site else "– Standard-Standort fehlt –")
                tenant_name = self._tenant_name(self._tenant_for(placement))
            else:
                site_name = ""
            state = ("ignored" if self.engine and self._placement(ap)["ignored"] else "new") if not device                 else ("check" if ap["issues"] else "known")
            rows.append(dict(ap, key=ap_key(ap), state=state if self.engine else "", site_id=site_id,
                             netbox_site=site_name, tenant=tenant_name or "",
                             netbox_device=device.name if device else "", method=ap.get("method", "")))
        # NetBox APs missing in the export – only for tenants this export is about
        tenants = {related_id(ap["device"], "tenant") for ap in self.aps if ap.get("device")}
        tenants |= {self._tenant_for(self._placement(ap)) for ap in self.aps if not ap.get("device")}
        tenants.discard(None)
        for device in (device for device in self.only_netbox if related_id(device, "tenant") in tenants):
            rows.append({"key": f"netbox-{device.id}", "state": "netbox_only", "name": device.name,
                         "model": getattr(getattr(device, "device_type", None), "model", "") or "",
                         "serial": getattr(device, "serial", "") or "", "mac": "",
                         "ip": str(getattr(getattr(device, "primary_ip4", None), "address", "") or "").split("/")[0],
                         "site_hint": "", "netbox_site": getattr(getattr(device, "site", None), "name", "")
                         or self._site_name(related_id(device, "site")),
                         "tenant": self._tenant_name(related_id(device, "tenant")),
                         "netbox_device": device.name, "method": "",
                         "issues": ["In NetBox, aber nicht im Sophos-Export"]})
        self.model.set_rows(rows)
        self.stack.setCurrentIndex(1 if rows else 0)
        counts = {key: sum(1 for row in rows if row["state"] == key) for key in STATE_TEXT}
        parts = [f"{len(self.aps)} Access Points" + (f" aus {self.file_name}" if self.file_name else "")]
        if self.engine:
            parts += [f"{counts['known']} in NetBox", f"{counts['new']} neu", f"{counts['check']} zu prüfen",
                      f"{counts['netbox_only']} nur in NetBox"]
            if counts["ignored"]:
                parts.append(f"{counts['ignored']} ignoriert")
            missing_sites = sum(1 for row in rows if row["state"] == "new" and not row.get("site_id"))
            if missing_sites:
                parts.append(f"{missing_sites} ohne Standort – Standard-Standort wählen")
        elif self.aps:
            parts.append("Abgleich mit NetBox läuft …" if self._netbox_ready()
                         else "kein NetBox-Abgleich (kein Zugang im Profil oder Offline-Modus)")
        self.summary.setText(" · ".join(parts) if self.aps or self.only_netbox else "")
        self._update_buttons()

    def _update_buttons(self):
        busy = self.tasks.busy
        widgets.set_enabled(self.sync_button, bool(self.engine) and not busy,
                            "Bitte einen Export laden." if not self.engine else "Bitte warten …")
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
        groups = {}
        for ap in new:
            if ap["site_hint"]:
                groups.setdefault(ap["site_hint"], []).append(ap)
        rows = [(hint, len(members), ("site", hint), self._placement(members[0]))
                for hint, members in sorted(groups.items(), key=lambda pair: pair[0].casefold())]
        rows += [(f"{ap['name']} (ohne Sophos-Standort)", 1, ("ap", ap_key(ap)), self._placement(ap))
                 for ap in new if not ap["site_hint"]]
        if not rows:
            self.window.toast("Alle Access Points sind bereits in NetBox.", "info")
            return False
        dialog = SiteMappingDialog(self.window, rows, self.engine.ap_sites, self.engine.group_tenants)
        if not dialog.exec():
            return False
        settings = self._settings()
        site_map, ap_sites = dict(settings.get("site_map") or {}), dict(settings.get("ap_sites") or {})
        for (kind, key), value in dialog.result.items():
            (site_map if kind == "site" else ap_sites)[key] = value
        self._store(site_map=site_map, ap_sites=ap_sites)
        self._refresh()
        return True

    def sync(self):
        if not self.engine or self.tasks.busy:
            return
        new = [ap for ap in self._new_aps() if not self._placement(ap)["ignored"]]
        role_id = self.role.valid_data()
        if new and not role_id:
            widgets.message(self.window, "Geräterolle fehlt", "Für neue Access Points bitte eine Geräterolle wählen.",
                            "info")
            return
        if any(not self._placement(ap)["stored"] for ap in new):
            if not self.map_sites():
                return
            new = [ap for ap in new if not self._placement(ap)["ignored"]]
        default_site = self.default_site.valid_data()
        placements = {}
        for ap in new:
            placement = self._placement(ap)
            placements[ap_key(ap)] = {"site": placement["site"], "tenant": self._tenant_for(placement)}
        homeless = [ap["name"] for ap in new if not placements[ap_key(ap)]["site"]]
        if homeless and not default_site:
            widgets.message(self.window, "Standard-Standort fehlt",
                            f"{len(homeless)} neue Access Points haben keinen Standort. NetBox verlangt für jedes "
                            "Gerät einen Standort – bitte oben einen Standard-Standort wählen.", "info",
                            "\n".join(homeless))
            return
        engine, rename = self.engine, self.rename.isChecked()
        aps = [ap for ap in self.aps if ap.get("device") or ap_key(ap) in placements]

        def run(dry_run):
            return lambda ctx: engine.sync(aps, placements, role_id, rename, dry_run=dry_run, default_site=default_site)

        def show(report):
            def ask():
                if ClientPreviewDialog(self.window, report).exec():
                    self.tasks.run(run(False), title="Access Points übertragen", area="app", on_success=applied,
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

        self.tasks.run(run(True), title="Änderungen berechnen", area="app", on_success=show,
                       on_error=self.actions._netbox_failed)
