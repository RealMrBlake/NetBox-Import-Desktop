"""Long-running operations started from the UI. Everything here runs in background tasks."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QTimer

from netbox_sync.core import netbox
from netbox_sync.core.config import known_hosts_path
from netbox_sync.core.netbox import NetBoxError, related_name
from netbox_sync.core.switches import (
    PLATFORMS,
    describe_snapshot,
    discover_switch,
    suggest_platform,
    sync_snapshot_to_netbox,
)
from netbox_sync.core.virtualization import load_source
from netbox_sync.core.vm_sync import VMSyncEngine
from netbox_sync.ui import widgets
from netbox_sync.ui.dialogs import SyncPreviewDialog
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
        if not self.require_netbox_config():
            return
        sources = [dict(source) for source in self.state.sources if source.get("enabled", True)]
        if not sources:
            if widgets.confirm(self.window, "Keine aktive Quelle",
                               "Es ist noch keine aktive VMware- oder Proxmox-Quelle hinterlegt.",
                               ok_text="Quelle hinzufügen"):
                self.window.navigate("connections")
                self.window.pages["connections"].add_source()
            return
        connect = self._connector()

        def work(ctx):
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

        def preview(ctx):
            nb = connect(ctx)
            reports = []
            for index, (switch, snapshot) in enumerate(items):
                ctx.progress(index, len(items), switch["name"])
                ctx.log(f"{switch['name']}: berechne Änderungen …")
                reports.append((switch, sync_snapshot_to_netbox(nb, switch, snapshot, create_cables, dry_run=True)))
            return nb, reports

        def show_preview(result):
            nb, reports = result
            self.state.set_netbox(nb, "ok", f"Verbunden · NetBox {nb.netbox_version}")

            def ask():
                # Opened after the preview task has fully finished, so the
                # follow-up task is not rejected as "still busy".
                dialog = SyncPreviewDialog(self.window, reports, create_cables)
                if dialog.exec():
                    self._apply_switch_sync(nb, items, create_cables, log)
            QTimer.singleShot(0, ask)

        self.tasks.run(preview, title="Änderungen berechnen", area="switch", on_success=show_preview,
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
