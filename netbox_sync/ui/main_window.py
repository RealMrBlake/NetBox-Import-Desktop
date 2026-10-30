"""Main window: sidebar navigation, profile switcher, pages and status bar."""

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from netbox_sync import APP_NAME, APP_VERSION
from netbox_sync.ui import widgets
from netbox_sync.ui.actions import Actions
from netbox_sync.ui.dialogs import ask_text
from netbox_sync.ui.icons import icon
from netbox_sync.ui.pages.clients import ClientsPage
from netbox_sync.ui.pages.connections import ConnectionsPage
from netbox_sync.ui.pages.ipam import IpamPage
from netbox_sync.ui.pages.log import LogPage
from netbox_sync.ui.pages.overview import OverviewPage
from netbox_sync.ui.pages.scanner import ScannerPage
from netbox_sync.ui.pages.settings import SettingsPage
from netbox_sync.ui.pages.switches import SwitchesPage
from netbox_sync.ui.pages.vms import VMsPage
from netbox_sync.ui.resources import resource_path
from netbox_sync.ui.state import AppState
from netbox_sync.ui.tasks import TaskManager, log_bus
from netbox_sync.ui.theme import theme
from netbox_sync.ui.updates import UpdateManager
from netbox_sync.ui.widgets import Badge, Toast, button, label

NAVIGATION = (
    ("overview", "Übersicht", "dashboard"),
    ("connections", "Verbindungen", "plug"),
    ("vms", "Virtuelle Maschinen", "server"),
    ("switches", "Switche", "network"),
    ("clients", "Windows-Clients", "monitor"),
    ("ipam", "VRFs && Netze", "globe"),
    ("scanner", "Netzwerk-Scanner", "search"),
    ("log", "Protokoll", "list"),
)


class NavButton(QPushButton):
    def __init__(self, text, icon_name):
        super().__init__(text)
        self.setProperty("nav", True)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(18, 18))
        self.icon_name = icon_name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.addStretch(1)
        self.badge = Badge("", "error")
        self.badge.hide()
        layout.addWidget(self.badge)
        self.toggled.connect(self.update_icon)
        self.update_icon()

    def update_icon(self, *_args):
        color = "#FFFFFF" if self.isChecked() else theme.color("sidebar_text")
        self.setIcon(icon(self.icon_name, color))

    def set_badge(self, count):
        self.badge.set(str(count) if count else "", "error")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1080, 700)
        self.state = AppState()
        self.tasks = TaskManager()
        self.actions = Actions(self)
        self.updates = UpdateManager(self)
        self.pages = {}
        self.nav_buttons = {}
        self.unseen_errors = 0

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        self.stack.setObjectName("Pages")
        right.addWidget(self.stack, 1)
        right.addWidget(self._build_status_bar())
        root.addLayout(right, 1)
        self.setCentralWidget(central)
        self.toast_widget = Toast(central)

        for key, page_class in (("overview", OverviewPage), ("connections", ConnectionsPage), ("vms", VMsPage),
                                ("switches", SwitchesPage), ("clients", ClientsPage), ("ipam", IpamPage), ("scanner", ScannerPage),
                                ("log", LogPage),
                                ("settings", SettingsPage)):
            page = page_class(self)
            self.pages[key] = page
            self.stack.addWidget(page)

        self.tasks.busy_changed.connect(self._busy_changed)
        self.tasks.progress.connect(self._progress)
        self.tasks.started.connect(lambda title: self.task_label.setText(title + " …"))
        log_bus.entry.connect(self._log_entry)
        self.state.profiles_changed.connect(self._refresh_profiles)
        self.state.profile_switched.connect(self._refresh_profiles)
        self.state.netbox_changed.connect(self._refresh_netbox_pill)
        self.state.profile_edited.connect(self._refresh_netbox_pill)
        self.state.settings_changed.connect(self._settings_changed)
        self.state.save_failed.connect(lambda text: self.toast(f"Speichern fehlgeschlagen: {text}", "error", 8000))
        theme.changed.connect(self._theme_changed)

        for index, (key, _title, _icon) in enumerate((*NAVIGATION, ("settings", "", ""))):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self, activated=lambda k=key: self.navigate(k))
        QShortcut(QKeySequence("F5"), self, activated=self._reload_current)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)

        self._restore_geometry()
        self.state.load()
        self.navigate("overview" if self.state.profile is None
                      or not (self.state.offline or self.state.netbox_configured()) else "vms")
        self._busy_changed(False)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(250)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(resource_path("app.png"))).scaled(
            34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setStyleSheet("background: #FFFFFF; border-radius: 9px; padding: 3px;")
        logo.setFixedSize(40, 40)
        logo.setAlignment(Qt.AlignCenter)
        brand.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("NetBox Synchronizer", object_name="AppTitle"))
        titles.addWidget(label("VMs, Switche & Clients → NetBox", object_name="AppSubtitle"))
        brand.addLayout(titles, 1)
        layout.addLayout(brand)
        layout.addSpacing(22)

        layout.addWidget(label("KUNDENPROFIL", object_name="SectionLabel"))
        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumHeight(34)
        self.profile_combo.currentTextChanged.connect(self._profile_selected)
        profile_row.addWidget(self.profile_combo, 1)
        self.profile_menu_button = QToolButton()
        self.profile_menu_button.setIcon(icon("more", "#B6C3D6"))
        self.profile_menu_button.setToolTip("Profil verwalten")
        self.profile_menu_button.setFixedSize(34, 34)
        self.profile_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.profile_menu_button.setCursor(Qt.PointingHandCursor)
        menu = QMenu(self)
        self.profile_actions = {
            "new": QAction("Neues Profil …", self, triggered=self.new_profile),
            "duplicate": QAction("Duplizieren …", self, triggered=self.duplicate_profile),
            "rename": QAction("Umbenennen …", self, triggered=self.rename_profile),
            "delete": QAction("Löschen …", self, triggered=self.delete_profile),
        }
        menu.addAction(self.profile_actions["new"])
        menu.addAction(self.profile_actions["duplicate"])
        menu.addAction(self.profile_actions["rename"])
        menu.addSeparator()
        menu.addAction(self.profile_actions["delete"])
        self.profile_menu = menu
        self.profile_menu_button.setMenu(menu)
        profile_row.addWidget(self.profile_menu_button)
        layout.addLayout(profile_row)
        layout.addSpacing(8)
        self.offline_check = widgets.checkbox(
            "Offline-Modus (ohne NetBox)",
            tooltip="VMs und Switche nur auslesen und als JSON-Datei exportieren. Die Datei lässt sich später auf "
                    "einem Rechner mit NetBox-Zugang über „Aus Datei laden …“ importieren.")
        self.offline_check.toggled.connect(lambda value: self.state.update_settings(offline_mode=value))
        layout.addWidget(self.offline_check)
        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, text, icon_name in NAVIGATION:
            nav = NavButton(text, icon_name)
            nav.clicked.connect(lambda _checked=False, k=key: self.navigate(k))
            self.nav_group.addButton(nav)
            self.nav_buttons[key] = nav
            layout.addWidget(nav)
        layout.addStretch(1)
        settings = NavButton("Einstellungen", "settings")
        settings.clicked.connect(lambda: self.navigate("settings"))
        self.nav_group.addButton(settings)
        self.nav_buttons["settings"] = settings
        layout.addWidget(settings)
        layout.addSpacing(6)
        layout.addWidget(label(f"Version {APP_VERSION}", object_name="VersionLabel"))
        return sidebar

    def _build_status_bar(self):
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(38)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 14, 0)
        layout.setSpacing(10)
        self.status_icon = QLabel()
        self.status_icon.setFixedSize(14, 14)
        layout.addWidget(self.status_icon)
        self.status_message = QLabel("Bereit")
        self.status_message.setMinimumWidth(200)
        layout.addWidget(self.status_message, 1)
        self.task_label = QLabel("")
        layout.addWidget(self.task_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(180)
        layout.addWidget(self.progress_bar)
        self.cancel_button = button("Abbrechen", "stop", "ghost", on_click=self._cancel_task)
        layout.addWidget(self.cancel_button)
        self.netbox_pill = Badge("", "neutral")
        self.netbox_pill.setCursor(Qt.PointingHandCursor)
        self.netbox_pill.mousePressEvent = lambda _event: self.navigate("connections")
        layout.addWidget(self.netbox_pill, 0, Qt.AlignVCenter)
        return bar

    # ------------------------------------------------------------------
    # Navigation & feedback
    # ------------------------------------------------------------------

    def navigate(self, key):
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        self.nav_buttons[key].setChecked(True)
        for nav in self.nav_buttons.values():
            nav.update_icon()
        if key == "log":
            self.unseen_errors = 0
            self.nav_buttons["log"].set_badge(0)
        page.on_show()

    def current_page(self):
        return self.stack.currentWidget()

    def toast(self, text, kind="success", duration=4500):
        self.toast_widget.show_message(text, kind, duration)

    def _reload_current(self):
        page = self.current_page()
        if page is self.pages["vms"] and not self.tasks.busy:
            page.load()
        elif page is self.pages["switches"] and not self.tasks.busy:
            page.discover()
        elif page is self.pages["clients"] and not self.tasks.busy:
            page.scan()
        elif page is self.pages["scanner"] and not self.tasks.busy:
            page.scan()

    def _focus_search(self):
        search = getattr(self.current_page(), "search", None)
        if search is not None:
            search.setFocus()
            search.selectAll()

    def _log_entry(self, entry):
        if entry.level == "debug":
            return
        tokens = {"error": "danger", "warning": "warning", "success": "success"}
        self.status_icon.setPixmap(icon({"error": "error", "warning": "warning", "success": "success"}.get(
            entry.level, "info"), theme.color(tokens.get(entry.level, "text_faint"))).pixmap(14, 14))
        metrics = self.status_message.fontMetrics()
        self.status_message.setText(metrics.elidedText(entry.text, Qt.ElideRight, max(200, self.status_message.width())))
        self.status_message.setToolTip(entry.text)
        if entry.level == "error" and self.current_page() is not self.pages["log"]:
            self.unseen_errors += 1
            self.nav_buttons["log"].set_badge(self.unseen_errors)

    def _busy_changed(self, busy):
        self.progress_bar.setVisible(busy)
        self.cancel_button.setVisible(busy and getattr(self.tasks.current, "cancellable", False))
        self.cancel_button.setEnabled(True)
        self.task_label.setVisible(busy)
        if busy:
            self.progress_bar.setRange(0, 0)
        for page in self.pages.values():
            page.set_busy(busy)
        for action in self.profile_actions.values():
            action.setEnabled(not busy)
        self.profile_combo.setEnabled(not busy)
        self.offline_check.setEnabled(not busy)

    def _progress(self, value, maximum, text):
        if maximum:
            self.progress_bar.setRange(0, maximum)
            self.progress_bar.setValue(value)
        if self.tasks.current:
            suffix = f" ({value}/{maximum})" if maximum else ""
            self.task_label.setText(f"{self.tasks.current.title}{suffix}")

    def _cancel_task(self):
        self.tasks.cancel()
        self.cancel_button.setEnabled(False)
        self.task_label.setText("Wird abgebrochen …")

    def _settings_changed(self):
        self.offline_check.blockSignals(True)
        self.offline_check.setChecked(self.state.offline)
        self.offline_check.blockSignals(False)
        self._refresh_netbox_pill()

    def _refresh_netbox_pill(self):
        self.netbox_pill.setToolTip("")
        if self.state.offline:
            self.netbox_pill.set("Offline-Modus · Export per JSON", "neutral")
        elif self.state.profile is None or not self.state.netbox_configured():
            self.netbox_pill.set("NetBox: nicht eingerichtet", "warning")
        elif self.state.netbox_status == "ok":
            self.netbox_pill.set(f"NetBox {self.state.netbox_version} · verbunden", "success")
        elif self.state.netbox_status == "error":
            self.netbox_pill.set("NetBox: Fehler", "error")
            self.netbox_pill.setToolTip(self.state.netbox_message)
        else:
            self.netbox_pill.set("NetBox: nicht verbunden", "neutral")

    def apply_theme(self):
        theme.apply(self.state.settings.get("theme", "system"))

    def _theme_changed(self):
        for nav in self.nav_buttons.values():
            nav.update_icon()

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def _refresh_profiles(self):
        names = self.state.profile_names()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        if not names:
            self.profile_combo.setPlaceholderText("Kein Profil")
        self.profile_combo.setCurrentText(self.state.active)
        self.profile_combo.blockSignals(False)
        has_profile = self.state.profile is not None
        for key in ("duplicate", "rename", "delete"):
            self.profile_actions[key].setEnabled(has_profile)
        for key in ("connections", "vms", "switches", "clients", "ipam"):
            self.nav_buttons[key].setEnabled(has_profile)
        self.setWindowTitle(f"{self.state.active} – {APP_NAME}" if has_profile else APP_NAME)
        self._refresh_netbox_pill()

    def _profile_selected(self, name):
        if name and name != self.state.active:
            self.state.switch_profile(name)
            self.toast(f"Profil „{name}“ geöffnet", "info", 2500)

    def _validate_name(self, current=None):
        def check(value):
            if value != current and value in self.state.profiles:
                return "Ein Profil mit diesem Namen existiert bereits."
            return None
        return check

    def new_profile(self):
        name = ask_text(self, "Neues Kundenprofil", "Name des Kunden oder der Umgebung, z. B. „Muster GmbH“.",
                        validator=self._validate_name(), ok_text="Anlegen")
        if name:
            self.state.create_profile(name)
            self.navigate("connections")
            self.toast(f"Profil „{name}“ angelegt – jetzt NetBox-Zugang eintragen", "info")

    def duplicate_profile(self):
        source = self.state.active
        if not source:
            return
        name = ask_text(self, "Profil duplizieren", f"Kopie von „{source}“ inklusive aller Zugänge anlegen.",
                        f"{source} (Kopie)", validator=self._validate_name(), ok_text="Duplizieren")
        if name:
            self.state.create_profile(name, copy_from=source)

    def rename_profile(self):
        current = self.state.active
        if not current:
            return
        name = ask_text(self, "Profil umbenennen", "Neuer Name für das Kundenprofil.", current,
                        validator=self._validate_name(current), ok_text="Umbenennen")
        if name and name != current:
            self.state.rename_profile(current, name)

    def delete_profile(self):
        name = self.state.active
        if name and widgets.confirm(self, "Profil löschen",
                                    f"Kundenprofil „{name}“ mit allen gespeicherten Zugängen endgültig löschen?",
                                    ok_text="Löschen", destructive=True):
            self.state.delete_profile(name)
            self.toast(f"Profil „{name}“ gelöscht", "info")

    # ------------------------------------------------------------------
    # Window state
    # ------------------------------------------------------------------

    def _restore_geometry(self):
        settings = QSettings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1320, 860)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast_widget.isVisible():
            self.toast_widget.reposition()

    def closeEvent(self, event):
        if self.tasks.busy and not widgets.confirm(
                self, "Vorgang läuft noch", f"„{self.tasks.current.title}“ ist noch nicht abgeschlossen. "
                "Trotzdem beenden?", ok_text="Beenden", destructive=True):
            event.ignore()
            return
        self.tasks.cancel()
        self.state.save_now()
        QSettings().setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)
