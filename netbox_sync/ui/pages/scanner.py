import os
import subprocess
import webbrowser

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QMenu, QStackedWidget

from netbox_sync.core import netscan
from netbox_sync.ui import widgets
from netbox_sync.ui.icons import icon
from netbox_sync.ui.models import Column, FilterProxy, RowTableModel
from netbox_sync.ui.pages import Page
from netbox_sync.ui.tasks import logger
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Card, EmptyState, SearchField, Table, button, checkbox, label

COMPARE_DOT = {"match": "success", "differs": "warning", "missing": "danger", "offline": "text_faint"}


class _Results(QObject):
    """Carries hosts from the scan threads to the UI thread."""

    host = Signal(object)


def _name(row):
    return row.get("dns_name") or row.get("netbios_name") or ""


def _netbox(row, key):
    return ", ".join(dict.fromkeys(entry[key] for entry in row.get("netbox", []) if entry[key]))


def _services(row):
    return ", ".join(f"{item['service'] or 'TCP'} ({item['port']})" for item in row.get("ports", []))


class ScannerPage(Page):
    title = "Netzwerk-Scanner"
    subtitle = ("IP-Bereiche nach erreichbaren Geräten, Namen, MAC-Adressen und offenen Diensten durchsuchen. "
                "Jede Adresse wird automatisch mit NetBox abgeglichen – es wird nichts geschrieben.")

    def __init__(self, window):
        super().__init__(window)
        self.rows = {}
        self.netbox = {}
        self.netbox_note = ""
        self.scan_task = None
        self.results = _Results()
        self.results.host.connect(self._host_found)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(250)
        self.refresh_timer.timeout.connect(self._refresh)

        target_card = Card("Scan")
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        targets = QHBoxLayout()
        self.targets = QLineEdit()
        self.targets.setPlaceholderText("z. B. 192.168.1.1-254, 10.0.0.0/24, server01")
        targets.addWidget(self.targets, 1)
        self.own_network = button("Eigenes Netz", "network", tooltip="Netze dieses Rechners übernehmen")
        self.own_network.clicked.connect(self._own_network_menu)
        targets.addWidget(self.own_network)
        form.addRow("IP-Bereiche", targets)
        self.ports = QLineEdit()
        self.ports.setPlaceholderText(netscan.DEFAULT_PORTS)
        form.addRow("Ports", self.ports)
        target_card.add(form)
        self.scan_button = target_card.add_action(button("Scannen", "play", "primary", on_click=self.scan))
        self.body.addWidget(target_card)

        result_card = Card("Ergebnisse", "Name aus DNS bzw. NetBIOS; NetBox-Spalten aus den IP-Adressen in NetBox.")
        filters = QHBoxLayout()
        self.search = SearchField("IP, Name, MAC, Dienst, NetBox-Objekt …")
        self.only_alive = checkbox("Nur erreichbare", True)
        self.only_issues = checkbox("Nur Abweichungen",
                                    tooltip="Nicht in NetBox, abweichender Name oder in NetBox, aber nicht erreichbar")
        filters.addWidget(self.search, 2)
        filters.addWidget(self.only_alive)
        filters.addWidget(self.only_issues)
        filters.addStretch(1)
        result_card.add(filters)
        columns = [
            Column("ip", "IP-Adresse", 130, dot=lambda row: "success" if row.get("alive") else "text_faint"),
            Column("name", "Name", 210, value=_name,
                   tooltip=lambda row: "\n".join(filter(None, (
                       f"DNS: {row['dns_name']}" if row.get("dns_name") else "",
                       f"NetBIOS: {row['netbios_name']}" if row.get("netbios_name") else "",
                       f"Arbeitsgruppe/Domäne: {row['workgroup']}" if row.get("workgroup") else "")))),
            Column("mac", "MAC-Adresse", 140),
            Column("services", "Dienste", 230, value=_services, tooltip=_services),
            Column("rtt", "Antwort", 70, value=lambda row: "" if row.get("rtt") is None else f"{row['rtt']} ms",
                   sort=lambda row: row.get("rtt") if row.get("rtt") is not None else 99999,
                   color=lambda row: "text_muted"),
            Column("nb_object", "NetBox-Objekt", 190, value=lambda row: _netbox(row, "object"),
                   tooltip=lambda row: "\n".join(
                       f"{entry['object_kind'] or 'IP'} {entry['object']} · {entry['vrf'] or 'global'} · "
                       f"{entry['tenant'] or 'ohne Mandant'} · {entry['status']}"
                       + (f"\n{entry['description']}" if entry["description"] else "")
                       for entry in row.get("netbox", []))),
            Column("nb_dns", "NetBox-DNS-Name", 190, value=lambda row: _netbox(row, "dns_name")),
            Column("compare", "Abgleich", 170, value=lambda row: netscan.COMPARE_TEXT.get(row.get("compare"), ""),
                   dot=lambda row: COMPARE_DOT.get(row.get("compare"))),
        ]
        self.model = RowTableModel(columns, row_id=lambda row: row["ip"])
        self.proxy = FilterProxy(self.model)
        self.table = Table()
        self.table.setModel(self.proxy)
        for index, column in enumerate(columns):
            self.table.setColumnWidth(index, column.width)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.doubleClicked.connect(lambda _index: self._open_default())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.stack = QStackedWidget()
        self.stack.addWidget(EmptyState("search", "Noch nicht gescannt",
                                        "IP-Bereiche eintragen und „Scannen“ wählen. Ist ein NetBox-Zugang "
                                        "eingerichtet, erscheinen Objekt und DNS-Name aus NetBox automatisch."))
        self.stack.addWidget(self.table)
        result_card.add(self.stack, 1)
        self.summary = label("", muted=True)
        result_card.add(self.summary)
        self.body.addWidget(result_card, 1)

        self.search.textChanged.connect(self.proxy.set_text)
        self.only_alive.toggled.connect(self._apply_filters)
        self.only_issues.toggled.connect(self._apply_filters)
        self.targets.editingFinished.connect(
            lambda: self.state.update_settings(scanner_targets=self.targets.text().strip()))
        self.ports.editingFinished.connect(lambda: self.state.update_settings(scanner_ports=self.ports.text().strip()))
        self.targets.returnPressed.connect(self.scan)
        self.targets.setText(self.state.settings.get("scanner_targets", ""))
        self.ports.setText(self.state.settings.get("scanner_ports", ""))
        self._apply_filters()

    # ------------------------------------------------------------------

    def on_show(self):
        if not self.targets.text().strip():
            networks = netscan.local_networks()
            if networks:
                self.targets.setText(networks[0])

    def set_busy(self, busy):
        scanning = busy and self.scan_task is not None and self.tasks.current is self.scan_task
        self.scan_button.setText("Stopp" if scanning else "Scannen")
        widgets.set_enabled(self.scan_button, scanning or not busy, "Bitte warten …")

    def _own_network_menu(self):
        networks = netscan.local_networks()
        if not networks:
            self.window.toast("Kein eigenes IPv4-Netz gefunden.", "info")
            return
        menu = QMenu(self)
        for network in networks:
            menu.addAction(network, lambda n=network: self.targets.setText(n))
        if len(networks) > 1:
            menu.addSeparator()
            menu.addAction("Alle", lambda: self.targets.setText(", ".join(networks)))
        menu.exec(self.own_network.mapToGlobal(self.own_network.rect().bottomLeft()))

    def _apply_filters(self, *_args):
        self.proxy.set_predicate("alive", (lambda row: row.get("alive") or row.get("compare") == "offline")
                                 if self.only_alive.isChecked() else None)
        self.proxy.set_predicate("issues", (lambda row: row.get("compare") in ("differs", "missing", "offline"))
                                 if self.only_issues.isChecked() else None)
        self._update_summary()

    def _update_summary(self):
        rows = list(self.rows.values())
        alive = sum(1 for row in rows if row.get("alive"))
        counts = {key: sum(1 for row in rows if row.get("compare") == key) for key in COMPARE_DOT}
        parts = [f"{alive} erreichbar"]
        if self.netbox_note:
            parts.append(self.netbox_note)
        else:
            parts += [f"{counts['match']} stimmen mit NetBox überein", f"{counts['differs']} abweichend",
                      f"{counts['missing']} nicht in NetBox", f"{counts['offline']} in NetBox, nicht erreichbar"]
        self.summary.setText(" · ".join(parts))

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(self):
        if self.scan_task is not None and self.tasks.current is self.scan_task:
            self.tasks.cancel()
            return
        if self.tasks.busy:
            return
        try:
            ips = netscan.expand_ips(self.targets.text())
            ports = netscan.parse_ports(self.ports.text().strip() or netscan.DEFAULT_PORTS)
        except netscan.ScanError as error:
            widgets.message(self.window, "Eingabe prüfen", str(error), "warning")
            return
        self.state.update_settings(scanner_targets=self.targets.text().strip(), scanner_ports=self.ports.text().strip())
        compare = bool(self.state.profile) and not self.state.offline and self.state.netbox_configured()
        connect = self.actions._connector() if compare else None
        self.rows, self.netbox = {}, {}
        self.netbox_note = "" if compare else "kein NetBox-Abgleich (kein Zugang im Profil oder Offline-Modus)"
        self.model.set_rows([])
        self.stack.setCurrentIndex(1)
        results = self.results

        def work(ctx):
            netbox_map, note = {}, ""
            if connect:
                try:
                    ctx.progress(0, len(ips), "NetBox-Abgleich …")
                    netbox_map = netscan.netbox_lookup(connect(ctx), ips)
                    ctx.log(f"NetBox: {sum(len(items) for items in netbox_map.values())} IP-Adressen in den "
                            "gescannten Bereichen")
                except Exception as error:  # noqa: BLE001 - the scan works without NetBox
                    note = f"NetBox-Abgleich nicht möglich: {error}"
                    ctx.log(note, "warning")
            results.host.emit({"__netbox__": netbox_map, "__note__": note})
            ctx.log(f"Scanne {len(ips)} Adressen, {len(ports)} Ports …")
            done = [0]

            def found(host):
                done[0] += 1
                ctx.progress(done[0], len(ips), host["ip"])
                results.host.emit(host)
            netscan.scan(ips, ports, found, lambda: ctx.cancelled)
            ctx.check_cancelled()
            return len(ips)

        def finished(count):
            self._finish()
            alive = sum(1 for row in self.rows.values() if row.get("alive"))
            logger("app").log(25, "Scan abgeschlossen: %s von %s Adressen erreichbar", alive, count)
            self.window.toast(f"{alive} von {count} Adressen erreichbar")

        self.scan_task = self.tasks.run(work, title=f"{len(ips)} Adressen scannen", area="app",
                                        on_success=finished, on_error=lambda error: widgets.error(
                                            self.window, "Scan fehlgeschlagen", str(error)),
                                        on_finished=self._scan_ended)
        self.set_busy(True)

    def _scan_ended(self):
        self.scan_task = None
        self._finish()
        self.set_busy(self.tasks.busy)

    def _host_found(self, host):
        if "__netbox__" in host:
            self.netbox = host["__netbox__"]
            if host["__note__"]:
                self.netbox_note = host["__note__"]
            return
        row = dict(host, netbox=self.netbox.get(host["ip"], []))
        row["compare"] = netscan.compare(row, row["netbox"]) if not self.netbox_note else ""
        if row["alive"] or row["netbox"]:
            self.rows[row["ip"]] = row
            if not self.refresh_timer.isActive():
                self.refresh_timer.start()

    def _finish(self):
        # Documented addresses that never answered are listed as "not reachable".
        for ip, entries in self.netbox.items():
            if ip not in self.rows:
                self.rows[ip] = {"ip": ip, "alive": False, "netbox": entries, "compare": "offline"}
        self._refresh()

    def _refresh(self):
        self.model.set_rows(list(self.rows.values()))
        self._update_summary()

    # ------------------------------------------------------------------
    # Actions on hosts
    # ------------------------------------------------------------------

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        return [self.proxy.row_at(index) for index in rows]

    def _netbox_url(self, entry):
        base = (self.state.profile or {}).get("netbox_url", "").rstrip("/")
        return f"{base}/ipam/ip-addresses/{entry['id']}/" if base else entry.get("url")

    def _open_default(self):
        selected = self._selected()
        if not selected:
            return
        row = selected[0]
        ports = {item["port"] for item in row.get("ports", [])}
        if ports & {443, 8443, 8006}:
            port = min(ports & {443, 8443, 8006})
            webbrowser.open(f"https://{row['ip']}" + ("" if port == 443 else f":{port}"))
        elif ports & {80, 8080}:
            port = min(ports & {80, 8080})
            webbrowser.open(f"http://{row['ip']}" + ("" if port == 80 else f":{port}"))
        elif 3389 in ports:
            self._rdp(row)
        elif 445 in ports:
            os.startfile(f"\\\\{row['ip']}")  # noqa: S606 - Windows Explorer
        elif row.get("netbox"):
            webbrowser.open(self._netbox_url(row["netbox"][0]))

    @staticmethod
    def _rdp(row):
        subprocess.Popen(["mstsc", f"/v:{row['ip']}"])  # noqa: S603, S607 - user action

    def _context_menu(self, position):
        selected = self._selected()
        if not selected:
            return
        row = selected[0]
        ports = {item["port"] for item in row.get("ports", [])}
        muted = theme.color("text_muted")
        menu = QMenu(self)
        for port, scheme in ((443, "https"), (8443, "https"), (8006, "https"), (80, "http"), (8080, "http")):
            if port in ports:
                suffix = "" if port in (80, 443) else f":{port}"
                url = f"{scheme}://{row['ip']}{suffix}"
                menu.addAction(icon("globe", muted), f"Im Browser öffnen ({url})", lambda u=url: webbrowser.open(u))
        if 3389 in ports:
            menu.addAction(icon("monitor", muted), "Remotedesktop verbinden", lambda: self._rdp(row))
        if 445 in ports or 139 in ports:
            menu.addAction(icon("folder", muted), "Freigaben öffnen",
                           lambda: os.startfile(f"\\\\{row['ip']}"))  # noqa: S606
        for entry in row.get("netbox", []):
            menu.addAction(icon("upload", muted), f"In NetBox öffnen ({entry['object'] or entry['dns_name'] or 'IP'})",
                           lambda e=entry: webbrowser.open(self._netbox_url(e)))
        if not menu.isEmpty():
            menu.addSeparator()
        clipboard = QGuiApplication.clipboard()
        menu.addAction(icon("copy", muted), "IP-Adresse kopieren",
                       lambda: clipboard.setText("\n".join(item["ip"] for item in selected)))
        if _name(row):
            menu.addAction(icon("copy", muted), "Name kopieren", lambda: clipboard.setText(_name(row)))
        if row.get("mac"):
            menu.addAction(icon("copy", muted), "MAC-Adresse kopieren", lambda: clipboard.setText(row["mac"]))
        menu.exec(self.table.viewport().mapToGlobal(position))
