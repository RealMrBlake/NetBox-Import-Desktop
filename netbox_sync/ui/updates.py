"""Update check on start and on demand, download and installation."""

import tempfile
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool, QTimer

from netbox_sync import APP_VERSION
from netbox_sync.core import updater
from netbox_sync.ui import widgets
from netbox_sync.ui.tasks import Task, logger


class UpdateManager(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.state = window.state
        self.tasks = window.tasks
        self._check = None

    def check_on_start(self):
        updater.remove_leftovers()
        if self.state.settings.get("auto_update_check", True):
            # A moment after start, so the window is usable right away.
            QTimer.singleShot(4000, lambda: self.check(silent=True))

    def check(self, silent=False):
        """Ask Gitea for a newer release without blocking other tasks."""
        if self._check is not None:
            return
        task = Task(lambda _ctx: updater.check_for_update(), "app", "Nach Updates suchen")
        self._check = task

        def done(release):
            if release is None:
                logger("app").info("Keine neuere Version gefunden (installiert: %s)", APP_VERSION)
                if not silent:
                    self.window.toast(f"{APP_VERSION} ist die aktuelle Version.", "info")
                return
            if silent and self.state.settings.get("skipped_version") == release["version"]:
                return
            self._offer(release)

        def failed(error):
            logger("app").warning("Update-Prüfung fehlgeschlagen: %s", error)
            if not silent:
                widgets.message(self.window, "Update-Prüfung fehlgeschlagen", str(error), "warning")

        task.signals.succeeded.connect(done)
        task.signals.failed.connect(failed)
        task.signals.finished.connect(lambda: setattr(self, "_check", None))
        QThreadPool.globalInstance().start(task)

    def _offer(self, release):
        kind = updater.install_kind()
        asset = updater.pick_asset(release, kind) if kind else None
        text = (f"Version {release['version']} ist verfügbar (installiert: {APP_VERSION}).\n\n"
                + ("Das Update wird heruntergeladen und installiert; die Anwendung startet danach neu."
                   if asset else "Für diese Installation gibt es keine passende Datei – bitte die Release-Seite "
                                 "öffnen."))
        buttons = ("Später", "Diese Version überspringen", "Jetzt aktualisieren" if asset else "Release-Seite öffnen")
        choice = widgets.message(self.window, "Update verfügbar", text, "info", release["notes"] or None,
                                 buttons=buttons, default=2)
        if choice == 1:
            self.state.update_settings(skipped_version=release["version"])
        elif choice == 2 and asset:
            self._install(release, kind, *asset)
        elif choice == 2 and release["page"]:
            webbrowser.open(release["page"])

    def _install(self, release, kind, name, asset):
        if self.tasks.busy:
            widgets.message(self.window, "Bitte warten", f"„{self.tasks.current.title}“ läuft noch. Das Update "
                            "kann danach über Einstellungen › Updates gestartet werden.", "info")
            return
        if kind == "portable":
            target = updater.portable_download_path()
        else:
            target = Path(tempfile.mkdtemp(prefix="netbox-sync-update-")) / name

        def work(ctx):
            ctx.log(f"Lade {name} ({release['version']}) herunter …")

            def progress(done, total):
                ctx.progress(done // 1024, max(total // 1024, 1), f"{done / 1_048_576:.1f} MB")
            return updater.download(asset, target, progress, lambda: ctx.cancelled)

        def downloaded(path):
            try:
                if kind == "portable":
                    updater.apply_portable(path)
                else:
                    updater.apply_installer(path)
            except OSError as error:
                widgets.error(self.window, "Update fehlgeschlagen", f"Das Update konnte nicht gestartet werden: "
                                                                     f"{error}")
                return
            logger("app").log(25, "Update auf %s wird installiert – Anwendung wird beendet", release["version"])
            # After the task has finished, so closing does not ask about a running task.
            QTimer.singleShot(0, self.window.close)

        self.tasks.run(work, title=f"Update {release['version']} herunterladen", area="app",
                       on_success=downloaded,
                       on_error=lambda error: widgets.error(self.window, "Update fehlgeschlagen", str(error)))
