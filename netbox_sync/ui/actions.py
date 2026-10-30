"""Long-running operations started from the UI. Everything here runs in background tasks."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from netbox_sync.core import exchange, netbox
from netbox_sync.core.client_sync import ClientSyncEngine
from netbox_sync.core.clients import ClientScanError, expand_targets, new_row, query_ad, scan_clients
from netbox_sync.core.config import known_hosts_path
from netbox_sync.core.netbox import NetBoxError, related_name
from netbox_sync.core.switches import (
    PLATFORMS,
    describe_snapshot,
    discover_switch,
    plan_switch_sync,
    suggest_platform,
    sync_snapshot_to_netbox,
)
from netbox_sync.core.virtualization import load_source
from netbox_sync.core.vm_sync import VMSyncEngine
from netbox_sync.ui import widgets
from netbox_sync.ui.client_dialogs import ClientPreviewDialog, ClientTargetDialog
from netbox_sync.ui.dialogs import SwitchMappingDialog, SyncPreviewDialog
from netbox_sync.ui.tasks import area_log, logger


def _device_row(device):
    primary = getattr(device, "primary_ip", None) or getattr(device, "primary_ip4", None)
    address = ""
    if primary is not None:
        address = str(getattr(primary, "address", None) or (primary.get("address") if isinstance(primary, dict) else ""))
    device_type = getattr(device, "device_type", None)
    model = related_name(device, "device_type") or getattr(device_type, "model", "") or ""
    manufacturer = related_name(device_type, "manufacturer") if device_type is not None else ""
    platform = related_name(device, "platform")
    site = related_name(device, "site")
    tenant = related_name(device, "tenant")
    role = related_name(device, "role") or related_name(device, "device_role")
    name = str(device.name)
    return {
        "id": device.id,
        "name": name,
        "site": site,
        "tenant": tenant,
        "role": role,
        "model": model,
        "manufacturer": manufacturer,
        "host": address.split("/")[0] if address else "",
        "platform": suggest_platform(platform, manufacturer, model),
        "label": " · ".join(part for part in (name, site, tenant) if part),
    }


class Actions:
    def __init__(self, window):
        self.window = window
        self.state = window.state
        self.tasks = window.tasks

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _netbox_params(self):
        profile = self.state.profile or {}
        return profile.get("netbox_url", ""), profile.get("netbox_token", ""), profile.get("netbox_ignore_ssl", False)

    def _connector(self):
        """Return a function for worker threads that yields a connected API object."""
        existing = self.state.nb
        url, token, ignore_ssl = self._netbox_params()

        def get(ctx):
            if existing is not None:
                return existing
            ctx.log(f"Verbinde zu NetBox ({url}) …")
            nb = netbox.connect(url, token, ignore_ssl)
            area_log("netbox")(f"Verbunden mit NetBox {nb.netbox_version}", "success")
            return nb
        return get

    def _netbox_failed(self, error, title="NetBox-Verbindung fehlgeschlagen"):
        if isinstance(error, NetBoxError):
            self.state.set_netbox(status="error", message=str(error))
        widgets.error(self.window, title, str(error), getattr(error, "traceback_text", None)
                      if not isinstance(error, NetBoxError) else None)

    def require_netbox_config(self):
        if self.state.netbox_configured():
            return True
        if widgets.confirm(self.window, "NetBox ist noch nicht eingerichtet",
                           "Bitte zuerst URL und API-Token der NetBox-Instanz hinterlegen.",
                           ok_text="Verbindungen öffnen"):
            self.window.navigate("connections")
        return False

    # ------------------------------------------------------------------
    # NetBox
    # ------------------------------------------------------------------

    def test_netbox(self, on_done=None):
        url, token, ignore_ssl = self._netbox_params()

        def work(ctx):
            ctx.log(f"Teste Verbindung zu {url} …")
            return netbox.connect(url, token, ignore_ssl)

        def success(nb):
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")
            logger("netbox").log(25, "Verbindung zu NetBox %s erfolgreich", nb.netbox_version)
            self.window.toast(f"Verbindung zu NetBox {nb.netbox_version} erfolgreich")
            if on_done:
                on_done(True)

        def failed(error):
            self.state.set_netbox(status="error", message=str(error))
            logger("netbox").error("%s", error)
            if on_done:
                on_done(False)

        self.tasks.run(work, title="NetBox-Verbindungstest", area="netbox", on_success=success, on_error=failed)

    def load_targets(self, on_done=None):
        """Connect and load clusters, tenants and VRFs (needed by the IPAM tools)."""
        if not self.require_netbox_config():
            return
        connect = self._connector()

        def work(ctx):
            nb = connect(ctx)
            engine = VMSyncEngine(nb, log=area_log("vm"))
            engine.load_targets()
            return nb, engine

        def success(result):
            nb, engine = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}", engine)
            if on_done:
                on_done()

        self.tasks.run(work, title="NetBox-Daten laden", area="netbox", on_success=success,
                       on_error=self._netbox_failed)

    # ------------------------------------------------------------------
    # Virtual machines
    # ------------------------------------------------------------------

    def load_vms(self):
        offline = self.state.offline
        if not offline and not self.require_netbox_config():
            return
        sources = [dict(source) for source in self.state.sources if source.get("enabled", True)]
        if not sources:
            if widgets.confirm(self.window, "Keine aktive Quelle",
                               "Es ist noch keine aktive VMware- oder Proxmox-Quelle hinterlegt.",
                               ok_text="Quelle hinzufügen"):
                self.window.navigate("connections")
                self.window.pages["connections"].add_source()
            return
        connect = None if offline else self._connector()

        def work(ctx):
            nb = engine = None
            if connect is not None:
                nb = connect(ctx)
                engine = VMSyncEngine(nb, log=area_log("vm"))
                engine.load_targets()
                ctx.check_cancelled()
            vms, failures = [], []
            ctx.progress(0, len(sources), "Lese Quellen …")
            with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
                futures = {pool.submit(load_source, source, ctx.log): source for source in sources}
                for done, future in enumerate(as_completed(futures), start=1):
                    source = futures[future]
                    try:
                        loaded = future.result()
                        for index, vm in enumerate(loaded):
                            vm["id"] = f"{source['id']}:{index}:{vm['name']}"
                            vm["source_id"] = source["id"]
                        vms.extend(loaded)
                        ctx.log(f"{source['name']}: {len(loaded)} System(e) geladen", "success")
                    except Exception as error:  # noqa: BLE001
                        failures.append(f"{source['name']}: {error}")
                        ctx.log(f"{source['name']}: {error}", "error")
                    ctx.progress(done, len(sources), source["name"])
            vms.sort(key=lambda item: item["name"].casefold())
            return nb, engine, vms, failures

        def success(result):
            nb, engine, vms, failures = result
            if nb is not None:
                self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}", engine)
            self.state.vms = vms
            self.state.vms_loaded_at = time.time()
            self.state.vms_changed.emit()
            if failures and not vms:
                widgets.error(self.window, "Keine VMs geladen", "Keine Quelle konnte gelesen werden.",
                              "\n".join(failures))
            elif failures:
                widgets.message(self.window, "Teilweise geladen",
                                f"{len(vms)} System(e) geladen, {len(failures)} Quelle(n) nicht erreichbar.",
                                "warning", "\n".join(failures))
            else:
                self.window.toast(f"{len(vms)} System(e) aus {len(sources)} Quelle(n) geladen")

        self.tasks.run(work, title="VMs laden", area="vm", on_success=success, on_error=self._netbox_failed)

    # ------------------------------------------------------------------
    # Offline exchange files
    # ------------------------------------------------------------------

    def _default_export_name(self, kind):
        profile = (self.state.profile or {}).get("name", "export")
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in profile)
        return f"{safe}-{kind}-{time.strftime('%Y%m%d-%H%M')}{exchange.FILE_SUFFIX}"

    def export_file(self, vms=(), switches=(), clients=()):
        """Write VMs, clients and/or ``(switch, snapshot)`` pairs to a JSON exchange file."""
        kind = "vms" if vms else ("clients" if clients else "switche")
        path, _filter = QFileDialog.getSaveFileName(
            self.window, "Für NetBox exportieren", self._default_export_name(kind),
            f"NetBox-Synchronizer-Export (*{exchange.FILE_SUFFIX});;JSON (*.json)")
        if not path:
            return
        data = exchange.build_export(vms, switches, (self.state.profile or {}).get("name", ""), clients)
        try:
            exchange.write_export(path, data)
        except OSError as error:
            widgets.error(self.window, "Export fehlgeschlagen", str(error))
            return
        parts = [f"{len(data['vms'])} VM(s)"] if data["vms"] else []
        parts += [f"{len(data['switches'])} Switch(e)"] if data["switches"] else []
        parts += [f"{len(data['clients'])} Client(s)"] if data["clients"] else []
        logger({"vms": "vm", "clients": "client"}.get(kind, "switch")).log(25, "Exportiert: %s → %s", ", ".join(parts), path)
        self.window.toast(f"{' und '.join(parts)} exportiert")

    def _open_export(self):
        path, _filter = QFileDialog.getOpenFileName(
            self.window, "Exportdatei laden", "",
            f"NetBox-Synchronizer-Export (*{exchange.FILE_SUFFIX} *.json);;Alle Dateien (*)")
        if not path:
            return None
        try:
            return exchange.read_export(path)
        except (OSError, exchange.ExchangeError) as error:
            widgets.error(self.window, "Datei kann nicht geladen werden", str(error))
            return None

    def _after_file_import(self):
        """In online mode connect to NetBox so the loaded data can be imported right away."""
        if not self.state.offline and self.state.engine is None and self.state.netbox_configured():
            self.load_targets(on_done=self.state.vms_changed.emit)

    def import_vms_file(self):
        data = self._open_export()
        if data is None:
            return
        if not data["vms"]:
            widgets.message(self.window, "Keine VMs enthalten", "Die Datei enthält keine virtuellen Maschinen.", "info")
            return
        self.state.vms = sorted(exchange.prepare_vms(data["vms"]), key=lambda item: item["name"].casefold())
        self.state.vms_loaded_at = time.time()
        self.state.vms_changed.emit()
        origin = f" (Profil „{data['profile']}“, {data.get('created', '')[:10]})" if data.get("profile") else ""
        logger("vm").log(25, "%d VM(s) aus Datei geladen%s", len(self.state.vms), origin)
        self.window.toast(f"{len(self.state.vms)} VM(s) aus Datei geladen")
        self._after_file_import()

    def import_clients_file(self):
        data = self._open_export()
        if data is None:
            return
        if not data["clients"]:
            widgets.message(self.window, "Keine Clients enthalten", "Die Datei enthält keine Windows-Clients.", "info")
            return
        self.state.clients = sorted(data["clients"], key=lambda item: item["name"].casefold())
        self.state.clients_loaded_at = time.time()
        self.state.clients_changed.emit()
        logger("client").log(25, "%d Client(s) aus Datei geladen", len(self.state.clients))
        self.window.toast(f"{len(self.state.clients)} Client(s) aus Datei geladen")
        if not self.state.offline and self.state.netbox_configured():
            self.load_client_devices()

    def import_switches_file(self):
        if self.state.profile is None:
            return
        data = self._open_export()
        if data is None:
            return
        if not data["switches"]:
            widgets.message(self.window, "Keine Switche enthalten", "Die Datei enthält keine Switch-Daten.", "info")
            return
        snapshots, added = exchange.merge_switches(self.state.switches, data["switches"], lambda: uuid.uuid4().hex)
        for switch_id, snapshot in snapshots.items():
            self.state.switch_snapshots[switch_id] = snapshot
            self.state.switch_status[switch_id] = ("ok", "aus Datei · " + describe_snapshot(snapshot))
        if added:
            self.state.touch()
        self.state.switches_changed.emit()
        text = f"{len(snapshots)} Switch(e) aus Datei geladen" + (f", {added} neu im Profil" if added else "")
        logger("switch").log(25, "%s", text)
        self.window.toast(text)

    # ------------------------------------------------------------------
    # Switches
    # ------------------------------------------------------------------

    def load_devices(self, on_done=None):
        if not self.require_netbox_config():
            return
        connect = self._connector()

        def work(ctx):
            nb = connect(ctx)
            ctx.log("Lade NetBox-Geräte …")
            devices = list(nb.dcim.devices.all())
            rows = [_device_row(device) for device in devices if getattr(device, "name", None)]
            skipped = len(devices) - len(rows)
            ctx.log(f"{len(rows)} Geräte geladen" + (f" ({skipped} ohne Namen übersprungen)" if skipped else ""),
                    "success")
            return nb, sorted(rows, key=lambda row: row["name"].casefold())

        def success(result):
            nb, rows = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")
            self.state.netbox_devices = rows
            if on_done:
                on_done(rows)

        self.tasks.run(work, title="NetBox-Geräte laden", area="switch", on_success=success,
                       on_error=self._netbox_failed)

    def discover_switches(self, switches):
        switches = [dict(switch) for switch in switches if switch.get("enabled", True)]
        if not switches:
            widgets.message(self.window, "Keine aktiven Switche", "Die ausgewählten Switche sind deaktiviert.", "info")
            return
        enable_lldp = bool(self.state.settings.get("enable_lldp"))
        parallel = max(1, int(self.state.settings.get("parallel_ssh", 4)))
        known_hosts = known_hosts_path()

        def work(ctx):
            snapshots, failures = {}, {}
            ctx.progress(0, len(switches), "Verbinde …")
            with ThreadPoolExecutor(max_workers=min(parallel, len(switches))) as pool:
                futures = {}
                for switch in switches:
                    ctx.log(f"{switch['name']}: lese {switch['host']} aus …")
                    futures[pool.submit(discover_switch, switch, enable_lldp, known_hosts, ctx.log)] = switch
                for done, future in enumerate(as_completed(futures), start=1):
                    switch = futures[future]
                    try:
                        snapshot = future.result()
                        snapshots[switch["id"]] = snapshot
                        ctx.log(f"{switch['name']}: {describe_snapshot(snapshot)}", "success")
                        if snapshot.get("lldp_enabled_automatically"):
                            ctx.log(f"{switch['name']}: LLDP war deaktiviert und wurde aktiviert", "warning")
                        elif snapshot.get("lldp_disabled"):
                            ctx.log(f"{switch['name']}: LLDP ist global deaktiviert – es werden keine Nachbarn "
                                    "erkannt. Bei Bedarf in den Einstellungen „LLDP aktivieren“ erlauben.", "warning")
                        if not snapshot["interfaces"]:
                            ctx.log(f"{switch['name']}: keine Schnittstellen erkannt – bitte SSH-Rohdaten prüfen",
                                    "warning")
                    except Exception as error:  # noqa: BLE001
                        failures[switch["id"]] = str(error)
                        ctx.log(f"{switch['name']}: {error}", "error")
                    ctx.progress(done, len(switches), switch["name"])
                    if ctx.cancelled:
                        for pending in futures:
                            pending.cancel()
            return snapshots, failures

        def success(result):
            snapshots, failures = result
            by_id = {switch["id"]: switch for switch in self.state.switches}
            platform_updates = 0
            for switch_id, snapshot in snapshots.items():
                self.state.switch_snapshots[switch_id] = snapshot
                self.state.switch_status[switch_id] = ("ok", describe_snapshot(snapshot))
                stored = by_id.get(switch_id)
                if stored and stored.get("platform", "auto") == "auto" and snapshot.get("platform") in PLATFORMS:
                    stored["platform"] = snapshot["platform"]
                    platform_updates += 1
            for switch_id, problem in failures.items():
                self.state.switch_status[switch_id] = ("error", problem)
            if platform_updates:
                self.state.touch()
            self.state.switches_changed.emit()
            if failures:
                names = {switch["id"]: switch["name"] for switch in switches}
                widgets.message(self.window, "Nicht alle Switche ausgelesen",
                                f"{len(snapshots)} von {len(switches)} Switch(en) erfolgreich ausgelesen.", "warning",
                                "\n".join(f"{names.get(key, key)}: {value}" for key, value in failures.items()))
            else:
                self.window.toast(f"{len(snapshots)} Switch(e) ausgelesen")

        self.tasks.run(work, title="Switche auslesen", area="switch", on_success=success)

    def sync_switches(self, switch_ids, create_cables):
        if not self.require_netbox_config():
            return
        by_id = {switch["id"]: switch for switch in self.state.switches}
        items = [(dict(by_id[switch_id]), self.state.switch_snapshots[switch_id])
                 for switch_id in switch_ids if switch_id in by_id and switch_id in self.state.switch_snapshots]
        if not items:
            widgets.message(self.window, "Keine Daten", "Bitte die Switche zuerst auslesen.", "info")
            return
        connect = self._connector()
        log = area_log("switch")

        def plan(ctx):
            nb = connect(ctx)
            plans = []
            for index, (switch, snapshot) in enumerate(items):
                ctx.progress(index, len(items), switch["name"])
                ctx.log(f"{switch['name']}: ordne Schnittstellen und Nachbarn zu …")
                plans.append((switch, plan_switch_sync(nb, switch, snapshot)))
            return nb, plans

        def review(result):
            nb, plans = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")
            reviewable = [(switch, item) for switch, item in plans if not item["error"]]
            needs_review = any(
                any(entry["method"] not in ("name", "new") or (entry["method"] == "new" and entry["physical"])
                    for entry in item["interfaces"]) or (create_cables and item["neighbors"])
                or item["networks"] or any(not vlan["netbox_name"] for vlan in item["vlans"])
                for _switch, item in reviewable)

            def ask():
                # Started after the planning task has finished, so the next
                # task is not rejected as "still busy".
                if needs_review:
                    dialog = SwitchMappingDialog(self.window, reviewable, create_cables)
                    if not dialog.exec():
                        return
                    stored = {switch["id"]: switch for switch in self.state.switches}
                    for switch, _snapshot in items:
                        choices = dialog.result.get(switch["id"])
                        if choices:
                            switch.update(choices)
                            if switch["id"] in stored:
                                stored[switch["id"]].update(choices)
                    self.state.touch()
                self.tasks.run(lambda ctx: preview(ctx, nb), title="Änderungen berechnen", area="switch",
                               on_success=show_preview, on_error=self._netbox_failed)
            QTimer.singleShot(0, ask)

        def preview(ctx, nb):
            reports = []
            for index, (switch, snapshot) in enumerate(items):
                ctx.progress(index, len(items), switch["name"])
                ctx.log(f"{switch['name']}: berechne Änderungen …")
                reports.append((switch, sync_snapshot_to_netbox(nb, switch, snapshot, create_cables, dry_run=True)))
            return nb, reports

        def show_preview(result):
            nb, reports = result

            def ask():
                # Opened after the preview task has fully finished, so the
                # follow-up task is not rejected as "still busy".
                dialog = SyncPreviewDialog(self.window, reports, create_cables)
                if dialog.exec():
                    self._apply_switch_sync(nb, items, create_cables, log)
            QTimer.singleShot(0, ask)

        self.tasks.run(plan, title="Zuordnung ermitteln", area="switch", on_success=review,
                       on_error=self._netbox_failed)

    def _apply_switch_sync(self, nb, items, create_cables, log):
        def work(ctx):
            reports = []
            for index, (switch, snapshot) in enumerate(items):
                ctx.check_cancelled()
                ctx.progress(index, len(items), switch["name"])
                ctx.log(f"{switch['name']}: übertrage nach NetBox …")
                report = sync_snapshot_to_netbox(nb, switch, snapshot, create_cables, dry_run=False)
                for error in report["errors"]:
                    ctx.log(f"{switch['name']}: {error}", "error")
                reports.append((switch, report))
            return reports

        def success(reports):
            totals = {}
            for _switch, report in reports:
                for key, value in report.items():
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
            text = (f"{totals.get('interfaces_created', 0)} Interfaces angelegt, "
                    f"{totals.get('interfaces_updated', 0)} geändert, "
                    f"{totals.get('ips_created', 0) + totals.get('ips_updated', 0)} IP-Adressen, "
                    f"{totals.get('cables_created', 0)} Kabel")
            log(f"Switch-Synchronisierung: {text}", "success")
            errors = [f"{switch['name']}: {error}" for switch, report in reports for error in report["errors"]]
            if errors:
                widgets.message(self.window, "Mit Problemen abgeschlossen", text + ".", "warning", "\n".join(errors))
            else:
                self.window.toast(f"NetBox aktualisiert: {text}")

        self.tasks.run(work, title="Switche synchronisieren", area="switch", on_success=success)

    # ------------------------------------------------------------------
    # Windows clients
    # ------------------------------------------------------------------

    def _client_credential(self):
        if self.state.client_options.get("use_current_user"):
            return None
        return self.state.client_credential

    def load_client_devices(self):
        """Fetch the NetBox device names so the client table can mark new clients."""
        connect = self._connector()

        def work(ctx):
            nb = connect(ctx)
            engine = ClientSyncEngine(nb, area_log("client"))
            engine.load_existing()
            return nb, engine

        def success(result):
            nb, engine = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")
            self.state.client_engine = engine
            self.state.clients_changed.emit()

        self.tasks.run(work, title="NetBox-Geräte laden", area="client", on_success=success,
                       on_error=self._netbox_failed)

    def scan_clients(self, rows=None):
        """Scan all targets from AD or the host list, or only the given result rows."""
        options = dict(self.state.client_options)
        credential = self._client_credential()
        parallel = max(1, int(self.state.settings.get("parallel_wmi", 8)))
        mode = options.get("mode", "ad")
        hosts = []
        if rows is None and mode == "hosts":
            try:
                hosts = expand_targets(options.get("hosts", ""))
            except ClientScanError as error:
                widgets.error(self.window, "Ungültige Eingabe", str(error))
                return
            if not hosts:
                widgets.message(self.window, "Keine Ziele", "Bitte Hostnamen, IP-Adressen oder Bereiche eintragen.",
                                "info")
                return
        connect = self._connector() if not self.state.offline and self.state.netbox_configured() else None
        targets_given = [dict(row) for row in rows] if rows is not None else None

        def work(ctx):
            nb = engine = None
            if connect is not None:
                try:
                    nb = connect(ctx)
                    engine = ClientSyncEngine(nb, ctx.log)
                    engine.load_existing()
                except Exception as error:  # noqa: BLE001 - the scan itself does not need NetBox
                    ctx.log(f"NetBox nicht erreichbar, Abgleich übersprungen: {error}", "warning")
                    nb = engine = None
            if targets_given is not None:
                targets = targets_given
            elif mode == "ad":
                targets = query_ad(options, credential, ctx.log, lambda: ctx.cancelled)
            else:
                targets = [new_row(host) for host in hosts]
            ctx.check_cancelled()
            lock = threading.Lock()
            done = [0]

            def on_result(row):
                with lock:
                    done[0] += 1
                    ctx.progress(done[0], len(targets), row["name"])

            ctx.progress(0, len(targets), "")
            results = scan_clients(targets, credential, bool(options.get("use_ssl")), parallel, log=ctx.log,
                                   on_result=on_result, cancelled=lambda: ctx.cancelled)
            ctx.check_cancelled()
            by_host = {row["host"]: row for row in results}
            return nb, engine, [by_host.get(row["host"], row) for row in targets]

        def success(result):
            nb, engine, scanned = result
            if nb is not None:
                self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")
                self.state.client_engine = engine
            rows_now = scanned
            if targets_given is not None:
                by_host = {row["host"]: row for row in scanned}
                rows_now = [by_host.get(row["host"], row) for row in self.state.clients]
            unique = {}
            for row in rows_now:
                current = unique.get(row["id"])
                if current is None or (current["status"] != "ok" and row["status"] == "ok"):
                    unique[row["id"]] = row
            self.state.clients = sorted(unique.values(), key=lambda item: item["name"].casefold())
            self.state.clients_loaded_at = time.time()
            self.state.clients_changed.emit()
            counts = {status: sum(1 for row in scanned if row["status"] == status)
                      for status in ("ok", "offline", "error")}
            text = (f"{counts['ok']} Client(s) ausgelesen, {counts['offline']} nicht erreichbar, "
                    f"{counts['error']} mit Fehler")
            logger("client").log(25 if counts["ok"] else 30, "%s", text)
            denied = sum(1 for row in scanned if row["status"] == "error" and "Zugriff verweigert" in row["message"])
            if denied and not counts["ok"]:
                widgets.message(self.window, "Zugriff verweigert",
                                "Kein Client hat die Abfrage erlaubt. Das verwendete Konto braucht lokale "
                                "Administratorrechte auf den Clients – bitte mit einem Domänen-Admin anmelden.",
                                "warning")
            else:
                self.window.toast(text, "success" if counts["ok"] else "warning")

        def failed(error):
            widgets.error(self.window, "Abfrage fehlgeschlagen", str(error),
                          getattr(error, "traceback_text", None) if not isinstance(error, ClientScanError) else None)

        self.tasks.run(work, title="Windows-Clients abfragen", area="client", on_success=success, on_error=failed)

    def sync_clients(self, rows):
        if not self.require_netbox_config():
            return
        rows = [dict(row) for row in rows if row.get("status") == "ok"]
        if not rows:
            widgets.message(self.window, "Keine abgefragten Clients",
                            "Nur erfolgreich abgefragte Clients (Status „OK“) können übertragen werden.", "info")
            return
        connect = self._connector()

        def load(ctx):
            nb = connect(ctx)
            engine = ClientSyncEngine(nb, area_log("client"))
            engine.load_targets()
            return nb, engine

        def loaded(result):
            nb, engine = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")

            def ask():
                if not engine.sites:
                    widgets.message(self.window, "Kein Standort vorhanden",
                                    "In NetBox ist noch kein Standort (Site) angelegt. Geräte brauchen einen Standort.",
                                    "warning")
                    return
                dialog = ClientTargetDialog(self.window, engine, self.state.client_options, len(rows))
                if not dialog.exec():
                    return
                choice = dialog.result
                self.state.update_client_options(
                    netbox_site=choice["site_id"], netbox_role=choice["role_name"], netbox_tenant=choice["tenant_id"],
                    sync_interfaces=choice["interfaces"], update_existing=choice["update_existing"])
                self._preview_clients(engine, rows, choice)
            QTimer.singleShot(0, ask)

        self.tasks.run(load, title="NetBox-Daten laden", area="client", on_success=loaded,
                       on_error=self._netbox_failed)

    @staticmethod
    def _run_client_sync(engine, rows, choice, ctx, dry_run, role_id=None):
        return engine.sync(rows, choice["site_id"], role_id or choice["role_id"], choice["tenant_id"],
                           choice["interfaces"], choice["update_existing"], dry_run=dry_run,
                           progress=ctx.progress, cancelled=lambda: ctx.cancelled)

    def _preview_clients(self, engine, rows, choice):
        def show(report):
            def ask():
                new_role = None if choice["role_id"] else choice["role_name"]
                if ClientPreviewDialog(self.window, report, new_role).exec():
                    self._apply_clients(engine, rows, choice)
            QTimer.singleShot(0, ask)

        self.tasks.run(lambda ctx: self._run_client_sync(engine, rows, choice, ctx, True),
                       title="Änderungen berechnen", area="client", on_success=show, on_error=self._netbox_failed)

    def _apply_clients(self, engine, rows, choice):
        def work(ctx):
            role_id = choice["role_id"] or engine.create_role(choice["role_name"])
            report = self._run_client_sync(engine, rows, choice, ctx, False, role_id)
            engine.load_existing()
            return report

        def success(report):
            self.state.client_engine = engine
            self.state.clients_changed.emit()
            text = (f"{report['devices_created']} Geräte angelegt, {report['devices_updated']} aktualisiert, "
                    f"{report['interfaces_created']} Schnittstellen, {report['ips_created']} IP-Adressen")
            area_log("client")(f"Client-Übertragung: {text}", "success")
            if report["errors"]:
                widgets.message(self.window, "Mit Problemen abgeschlossen", text + ".", "warning",
                                "\n".join(report["errors"]))
            else:
                self.window.toast(f"NetBox aktualisiert: {text}")

        self.tasks.run(work, title="Clients übertragen", area="client", on_success=success,
                       on_error=self._netbox_failed)
