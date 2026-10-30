"""Dialogs for transferring scanned Windows clients to NetBox."""

from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem

from netbox_sync.ui.dialogs import _Dialog, _mark_invalid
from netbox_sync.ui.icons import icon
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Badge, Banner, SearchableComboBox, checkbox, form_dialog_buttons, label

ROLE_HINTS = ("client", "arbeitsplatz", "workstation", "pc", "notebook")


class ClientTargetDialog(_Dialog):
    """Choose site, role and tenant for the new devices."""

    def __init__(self, parent, engine, options, count):
        super().__init__(parent, "Nach NetBox übertragen",
                         f"{count} Client(s) werden als Geräte angelegt bzw. aktualisiert. Hersteller, Gerätetypen "
                         "und Plattformen werden bei Bedarf automatisch erstellt.", width=560)
        self.result = None
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self.site = SearchableComboBox("Standort wählen …")
        self.site.set_items(engine.sites, options.get("netbox_site"))
        form.addRow("Standort *", self.site)
        self.role = SearchableComboBox("Rolle wählen oder neuen Namen eingeben …")
        self.role.set_items(engine.roles)
        remembered = options.get("netbox_role") or "Client"
        if self.role.findText(remembered) >= 0:
            self.role.setCurrentIndex(self.role.findText(remembered))
        else:
            match = next((name for name, _id in engine.roles if name.casefold() in ROLE_HINTS), None)
            if match:
                self.role.setCurrentIndex(self.role.findText(match))
            else:
                self.role.setEditText(remembered)
        form.addRow("Geräterolle *", self.role)
        self.tenant = SearchableComboBox("Mandant wählen …")
        self.tenant.set_items(list(engine.tenants), options.get("netbox_tenant"))
        form.addRow("Mandant *", self.tenant)
        self.root.addLayout(form)
        self.interfaces = checkbox("Netzwerkkarten, MAC- und IP-Adressen übernehmen",
                                   options.get("sync_interfaces", True),
                                   tooltip="Legt fehlende Schnittstellen an und ordnet IP-Adressen zu (globale "
                                           "Tabelle, ohne VRF). Bereits anderweitig zugeordnete IPs bleiben "
                                           "unverändert.")
        self.update_existing = checkbox("Vorhandene Geräte aktualisieren (Modell, Plattform, Seriennummer)",
                                        options.get("update_existing", True))
        self.root.addWidget(self.interfaces)
        self.root.addWidget(self.update_existing)
        self.root.addWidget(label("Zuordnung über den Gerätenamen (ohne Groß-/Kleinschreibung). Standort und Rolle "
                                  "vorhandener Geräte bleiben unverändert.", muted=True, wrap=True))
        self.root.addWidget(self.error_banner)
        buttons, _ok = form_dialog_buttons(self, "Änderungen berechnen")
        self.root.addWidget(buttons)

    def accept(self):
        site = self.site.valid_data()
        role_text = self.role.currentText().strip()
        role = self.role.valid_data()
        _mark_invalid(self.site, site is None)
        tenant = self.tenant.valid_data()
        _mark_invalid(self.role, not role_text)
        _mark_invalid(self.tenant, tenant is None)
        if site is None:
            self.show_error("Bitte einen vorhandenen Standort auswählen.")
            return
        if not role_text:
            self.show_error("Bitte eine Geräterolle auswählen oder einen Namen für eine neue Rolle eingeben.")
            return
        if tenant is None:
            self.show_error("Bitte einen Mandanten auswählen – alle Objekte in NetBox brauchen einen Mandanten.")
            return
        self.result = {
            "site_id": site, "role_id": role, "role_name": role_text,
            "tenant_id": tenant,
            "interfaces": self.interfaces.isChecked(), "update_existing": self.update_existing.isChecked(),
        }
        super().accept()


class ClientPreviewDialog(_Dialog):
    def __init__(self, parent, report, new_role=None):
        super().__init__(parent, "Änderungen prüfen",
                         "Diese Änderungen werden in NetBox vorgenommen. Es wurde noch nichts geschrieben.",
                         width=760)
        self.resize(860, 620)
        summary = QHBoxLayout()
        summary.setSpacing(8)
        items = [
            (f"{report['devices_created']} Geräte neu", "info"),
            (f"{report['devices_updated']} geändert", "info"),
            (f"{report['devices_unchanged']} unverändert", "neutral"),
            (f"{report['interfaces_created']} Schnittstellen", "info"),
            (f"{report['ips_created']} IP-Adressen", "info"),
        ]
        if report["errors"]:
            items.append((f"{len(report['errors'])} Probleme", "error"))
        for text, kind in items:
            summary.addWidget(Badge(text, kind))
        summary.addStretch(1)
        self.root.addLayout(summary)

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        general = list(report["changes"])
        if new_role:
            general.insert(0, f"Geräterolle anlegen: {new_role}")
        if general or report["errors"]:
            top = QTreeWidgetItem([f"Allgemein — {len(general)} Objekt(e)"])
            top.setIcon(0, icon("database", theme.color("accent")))
            for error in report["errors"]:
                child = QTreeWidgetItem([error])
                child.setIcon(0, icon("warning", theme.color("danger")))
                top.addChild(child)
            for change in general:
                top.addChild(QTreeWidgetItem([change]))
            tree.addTopLevelItem(top)
            top.setExpanded(True)
        total = len(general)
        for name, changes in report["items"]:
            real = [change for change in changes if not change.startswith("Vorhanden –")]
            total += len(real)
            top = QTreeWidgetItem([f"{name} — {len(real)} Änderung(en)" if real else f"{name} — unverändert"])
            top.setIcon(0, icon("monitor", theme.color("accent" if real else "text_faint")))
            for change in changes:
                top.addChild(QTreeWidgetItem([change]))
            tree.addTopLevelItem(top)
        self.root.addWidget(tree, 1)
        if total == 0:
            self.root.addWidget(Banner("NetBox ist bereits auf dem aktuellen Stand – es gibt nichts zu übertragen.",
                                       "success"))
        buttons, ok = form_dialog_buttons(self, f"{total} Änderung(en) übernehmen")
        ok.setEnabled(total > 0)
        self.root.addWidget(buttons)
