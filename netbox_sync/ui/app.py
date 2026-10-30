"""Application entry point."""

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTranslator
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from netbox_sync import APP_NAME, APP_USER_MODEL_ID, APP_VERSION
from netbox_sync.core.config import log_dir


def setup_logging():
    from netbox_sync.ui.tasks import QtLogHandler, log_bus

    root = logging.getLogger("netbox_sync")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(folder / "netbox-sync.log", maxBytes=2_000_000, backupCount=5,
                                           encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
        root.addHandler(file_handler)
    except OSError:
        pass
    root.addHandler(QtLogHandler(log_bus))
    # Third-party libraries only report problems.
    for name in ("paramiko", "urllib3", "pyVmomi"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("netbox_sync.app").info("%s %s gestartet", APP_NAME, APP_VERSION)


def install_excepthook(window_getter):
    def hook(exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("netbox_sync.app").error("Unerwarteter Fehler: %s", exc, exc_info=(exc_type, exc, tb))
        from netbox_sync.ui import widgets
        try:
            widgets.error(window_getter(), "Unerwarteter Fehler",
                          f"{exc}\n\nDie Anwendung läuft weiter. Details stehen im Protokoll.", details)
        except Exception:  # noqa: BLE001
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("NetBox Synchronizer")
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.German), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)

    from netbox_sync.ui.resources import resource_path
    from netbox_sync.ui.theme import base_font, theme

    app.setFont(base_font())
    app.setWindowIcon(QIcon(str(resource_path("app.ico"))))
    setup_logging()
    theme.apply("system")

    from netbox_sync.ui.main_window import MainWindow

    window = MainWindow()
    window.apply_theme()
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _scheme: theme.mode == "system" and theme.apply())
    install_excepthook(lambda: window)
    window.show()
    window.updates.check_on_start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
