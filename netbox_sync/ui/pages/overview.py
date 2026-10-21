import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from netbox_sync.ui.icons import icon
from netbox_sync.ui.pages import Page
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Card, EmptyState, StatTile, button, hline, label


def _ago(timestamp):
    if not timestamp:
        return ""
    minutes = int((time.time() - timestamp) / 60)
    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        return f"vor {minutes} min"
    return time.strftime("um %H:%M Uhr", time.localtime(timestamp))


class Step(QWidget):
    def __init__(self, number, title, text, action_text, on_action, optional=False):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(14)
        self.circle = QLabel(str(number))
        self.circle.setObjectName("StepCircle")
        self.circle.setAlignment(Qt.AlignCenter)
        self.number = number
        layout.addWidget(self.circle, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        heading = title + ("  <span style='font-weight:400'>(optional)</span>" if optional else "")
        self.title = label(heading)
        self.title.setTextFormat(Qt.RichText)
        self.title.setStyleSheet("font-weight: 600;")
        texts.addWidget(self.title)
        self.text = label(text, muted=True, wrap=True)
        texts.addWidget(self.text)
        layout.addLayout(texts, 1)
        self.action = button(action_text, on_click=on_action)
        layout.addWidget(self.action, 0, Qt.AlignVCenter)
        self.done = False
        theme.changed.connect(self._render)

    def set_done(self, done, primary=False):
        self.done = done
        self.action.setProperty("variant", "primary" if primary and not done else None)
        self.action.style().unpolish(self.action)
        self.action.style().polish(self.action)
        self._render()

    def _render(self):
        self.circle.setProperty("done", self.done)
        if self.done:
            self.circle.setText("")
            self.circle.setPixmap(icon("check", theme.color("success"), 2.6).pixmap(16, 16))
        else:
            self.circle.clear()
            self.circle.setText(str(self.number))
        self.circle.style().unpolish(self.circle)
        self.circle.style().polish(self.circle)


class OverviewPage(Page):
    title = "Übersicht"
    subtitle = "Status des aktiven Kundenprofils und die nächsten Schritte."

    def __init__(self, window):
        super().__init__(window, scrollable=True)
        self.empty = EmptyState(
            "building", "Willkommen beim NetBox Synchronizer",
            "Lege zuerst ein Kundenprofil an. Darin werden die NetBox-Instanz, VMware-/Proxmox-Quellen und "
            "Switch-Zugänge eines Kunden gespeichert – Passwörter verschlüsselt mit Windows DPAPI.",
            "Erstes Kundenprofil anlegen", window.new_profile,
        )
        self.body.addWidget(self.empty, 1)

        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(18)
        tiles = QGridLayout()
        tiles.setSpacing(16)
        self.netbox_tile = StatTile("database", "NetBox")
        self.vm_tile = StatTile("server", "Virtualisierung")
        self.switch_tile = StatTile("network", "Switche")
        self.netbox_tile.clicked.connect(lambda: window.navigate("connections"))
        self.vm_tile.clicked.connect(lambda: window.navigate("vms"))
        self.switch_tile.clicked.connect(lambda: window.navigate("switches"))
        for column, tile in enumerate((self.netbox_tile, self.vm_tile, self.switch_tile)):
            tiles.addWidget(tile, 0, column)
            tiles.setColumnStretch(column, 1)
        content.addLayout(tiles)

        steps = Card("Erste Schritte", "In vier Schritten von der leeren NetBox zur gepflegten Dokumentation.")
        self.steps = [
            Step(1, "NetBox verbinden", "URL und API-Token hinterlegen und die Verbindung testen.",
                 "Einrichten", lambda: window.navigate("connections")),
            Step(2, "Virtualisierungsquelle hinzufügen", "vCenter, ESXi-Host oder Proxmox-Cluster anbinden.",
                 "Quelle hinzufügen", self._add_source),
            Step(3, "VMs laden und importieren",
                 "VMs auswählen, Cluster und Mandant festlegen – neue Netze werden vor dem Anlegen angezeigt.",
                 "Zu den VMs", lambda: window.navigate("vms")),
            Step(4, "Switche dokumentieren",
                 "Bestehende NetBox-Geräte per SSH auslesen: Schnittstellen, IP-Adressen und LLDP-Verkabelung.",
                 "Zu den Switchen", lambda: window.navigate("switches"), optional=True),
        ]
        for index, step in enumerate(self.steps):
            if index:
                steps.add(hline())
            steps.add(step)
        content.addWidget(steps)

        tips = Card("Gut zu wissen")
        for text in (
            "Alle Eingaben werden automatisch gespeichert – es gibt keinen Speichern-Knopf mehr.",
            "Lange Vorgänge laufen im Hintergrund. Das Fenster bleibt bedienbar, unten rechts lässt sich abbrechen.",
            "Vor jedem Schreibvorgang zeigt die Anwendung eine Zusammenfassung der Änderungen.",
            "Das Protokoll enthält alle Details und wird zusätzlich als Datei gespeichert.",
        ):
            row = QHBoxLayout()
            dot = QLabel("•")
            dot.setProperty("muted", True)
            row.addWidget(dot, 0, Qt.AlignTop)
            row.addWidget(label(text, muted=True, wrap=True), 1)
            tips.add(row)
        content.addWidget(tips)
        content.addStretch(1)
        self.body.addWidget(self.content, 1)

        for signal in (self.state.profile_switched, self.state.profile_edited, self.state.netbox_changed,
                       self.state.vms_changed, self.state.switches_changed, self.state.settings_changed):
            signal.connect(self.refresh)
        self.refresh()

    def _add_source(self):
        self.window.navigate("connections")
        self.window.pages["connections"].add_source()

    def on_show(self):
        self.refresh()

    def refresh(self):
        profile = self.state.profile
        self.empty.setVisible(profile is None)
        self.content.setVisible(profile is not None)
        if profile is None:
            self.header.subtitle.setText("Noch kein Kundenprofil vorhanden.")
            return
        self.header.subtitle.setText(f"Kundenprofil „{profile['name']}“")

        configured = self.state.netbox_configured()
        status = self.state.netbox_status
        if self.state.offline:
            self.netbox_tile.set("Offline-Modus", "Daten werden als JSON-Datei exportiert statt nach NetBox "
                                 "geschrieben.", "Offline", "neutral")
        elif not configured:
            self.netbox_tile.set("Nicht eingerichtet", "URL und API-Token fehlen.", "Einrichten", "warning")
        elif status == "ok":
            self.netbox_tile.set(f"NetBox {self.state.netbox_version}", profile["netbox_url"], "Verbunden", "success")
        elif status == "error":
            self.netbox_tile.set("Fehler", self.state.netbox_message, "Fehler", "error")
        else:
            self.netbox_tile.set("Bereit", profile["netbox_url"], "Nicht getestet", "neutral")

        sources = self.state.sources
        active = sum(1 for source in sources if source.get("enabled", True))
        if self.state.vms:
            new = sum(1 for vm in self.state.vms if self.state.engine and vm["name"] not in self.state.engine.existing_vms)
            known = "" if self.state.engine is None else f"{new} noch nicht in NetBox · "
            self.vm_tile.set(f"{len(self.state.vms)} VMs", f"{known}geladen {_ago(self.state.vms_loaded_at)}",
                             f"{active} Quelle(n)", "info")
        else:
            self.vm_tile.set(f"{len(sources)} Quelle(n)", "VMs noch nicht geladen." if sources else "Noch keine Quelle.",
                             "", "neutral")

        switches = self.state.switches
        scanned = len([key for key in self.state.switch_snapshots if any(s["id"] == key for s in switches)])
        errors = sum(1 for status_, _text in self.state.switch_status.values() if status_ == "error")
        detail = f"{scanned} ausgelesen" if scanned else ("Noch nicht ausgelesen." if switches else "Noch keine Switche.")
        self.switch_tile.set(f"{len(switches)} Switch(e)", detail,
                             f"{errors} Fehler" if errors else "", "error" if errors else "neutral")

        done = [self.state.offline or (configured and status != "error"), bool(sources), bool(self.state.vms), bool(switches)]
        first_open = next((index for index, value in enumerate(done) if not value), None)
        for index, step in enumerate(self.steps):
            step.set_done(done[index], primary=index == first_open)
