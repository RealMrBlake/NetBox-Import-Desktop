import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QFormLayout, QHBoxLayout, QRadioButton, QSpinBox

from netbox_sync import APP_NAME, APP_PUBLISHER, APP_VERSION
from netbox_sync.core.config import config_dir, config_path, known_hosts_path, log_dir
from netbox_sync.ui import widgets
from netbox_sync.ui.pages import Page
from netbox_sync.ui.widgets import Card, button, checkbox, label


class SettingsPage(Page):
    title = "Einstellungen"
    subtitle = "Gelten für alle Kundenprofile."

    def __init__(self, window):
        super().__init__(window, scrollable=True)

        appearance = Card("Darstellung")
        row = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        self.theme_buttons = {}
        for key, text in (("system", "Wie Windows"), ("light", "Hell"), ("dark", "Dunkel")):
            radio = QRadioButton(text)
            radio.setCursor(Qt.PointingHandCursor)
            self.theme_group.addButton(radio)
            self.theme_buttons[key] = radio
            row.addWidget(radio)
            radio.toggled.connect(lambda checked, k=key: checked and self._set_theme(k))
        row.addStretch(1)
        appearance.add(row)
        self.body.addWidget(appearance)

        switches = Card("Switche", "Verhalten beim Auslesen per SSH.")
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 16)
        self.parallel.setSuffix(" gleichzeitig")
        self.parallel.setFixedWidth(160)
        form.addRow("SSH-Sitzungen", self.parallel)
        switches.add(form)
        self.enable_lldp = checkbox("LLDP automatisch aktivieren, wenn es auf einem Switch deaktiviert ist",
                                    tooltip="Ändert die laufende Konfiguration des Switches.")
        switches.add(self.enable_lldp)
        switches.add(label("Ohne diese Option wird die Switch-Konfiguration nie verändert; es werden nur "
                           "„show“-/„display“-Befehle ausgeführt.", muted=True, wrap=True))
        keys = QHBoxLayout()
        keys.addWidget(label("SSH-Hostschlüssel werden beim ersten Kontakt gespeichert und danach geprüft.",
                             muted=True, wrap=True), 1)
        keys.addWidget(button("Alle vergessen", "trash", "danger", on_click=self.forget_keys))
        switches.add(keys)
        self.body.addWidget(switches)

        clients = Card("Windows-Clients", "Abfrage per WMI/CIM über WinRM.")
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        self.parallel_wmi = QSpinBox()
        self.parallel_wmi.setRange(1, 32)
        self.parallel_wmi.setSuffix(" gleichzeitig")
        self.parallel_wmi.setFixedWidth(160)
        form.addRow("WinRM-Sitzungen", self.parallel_wmi)
        clients.add(form)
        clients.add(label("Kennwörter aus dem Windows-Anmeldefenster werden nie gespeichert, sondern nur bis zum "
                          "Beenden des Programms im Arbeitsspeicher gehalten.", muted=True, wrap=True))
        self.body.addWidget(clients)

        data = Card("Daten & Dateien")
        for caption, path, opener in (
            ("Konfiguration", config_path(), config_dir()),
            ("Protokolle", log_dir(), log_dir()),
        ):
            line = QHBoxLayout()
            line.addWidget(label(caption), 0)
            line.addSpacing(12)
            line.addWidget(label(str(path), muted=True, selectable=True), 1)
            line.addWidget(button("Öffnen", "folder", on_click=lambda _c=False, p=opener: self._open(p)))
            data.add(line)
        data.add(label("Passwörter und Tokens sind mit Windows DPAPI an dieses Benutzerkonto gebunden und können "
                       "nicht auf einem anderen Rechner entschlüsselt werden.", muted=True, wrap=True))
        self.body.addWidget(data)

        about = Card("Über")
        about.add(label(f"<b>{APP_NAME}</b> Version {APP_VERSION}"))
        about.add(label(f"© {APP_PUBLISHER}", muted=True))
        self.body.addWidget(about)
        self.body.addStretch(1)

        self.parallel.valueChanged.connect(lambda value: self.state.update_settings(parallel_ssh=value))
        self.enable_lldp.toggled.connect(lambda value: self.state.update_settings(enable_lldp=value))
        self.parallel_wmi.valueChanged.connect(lambda value: self.state.update_settings(parallel_wmi=value))
        self.state.settings_changed.connect(self.load)
        self.load()

    def load(self):
        settings = self.state.settings
        for widget in (self.parallel, self.parallel_wmi, self.enable_lldp, *self.theme_buttons.values()):
            widget.blockSignals(True)
        self.parallel.setValue(int(settings.get("parallel_ssh", 4)))
        self.parallel_wmi.setValue(int(settings.get("parallel_wmi", 8)))
        self.enable_lldp.setChecked(bool(settings.get("enable_lldp")))
        self.theme_buttons.get(settings.get("theme", "system"), self.theme_buttons["system"]).setChecked(True)
        for widget in (self.parallel, self.parallel_wmi, self.enable_lldp, *self.theme_buttons.values()):
            widget.blockSignals(False)

    def _set_theme(self, mode):
        if self.state.settings.get("theme") != mode:
            self.state.update_settings(theme=mode)
            self.window.apply_theme()

    @staticmethod
    def _open(path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606 - Windows Explorer

    def forget_keys(self):
        path = known_hosts_path()
        if not path.exists():
            self.window.toast("Es sind keine Hostschlüssel gespeichert.", "info")
            return
        if widgets.confirm(self.window, "Hostschlüssel vergessen",
                           "Alle gespeicherten SSH-Hostschlüssel löschen? Beim nächsten Auslesen werden sie ohne "
                           "Prüfung neu übernommen.", ok_text="Löschen", destructive=True):
            path.unlink()
            self.window.toast("Gespeicherte Hostschlüssel gelöscht")
