import html
import os
import time

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QPlainTextEdit

from netbox_sync.core.config import log_dir
from netbox_sync.ui.pages import Page
from netbox_sync.ui.tasks import AREAS, log_bus
from netbox_sync.ui.theme import theme
from netbox_sync.ui.widgets import Card, SearchField, button

MAX_ENTRIES = 10000
LEVEL_RANK = {"debug": 0, "info": 1, "success": 1, "warning": 2, "error": 3}
LEVEL_TEXT = {"info": "INFO", "success": "OK", "warning": "WARNUNG", "error": "FEHLER", "debug": "DEBUG"}


class LogPage(Page):
    title = "Protokoll"
    subtitle = "Alle Aktivitäten dieser Sitzung. Zusätzlich wird ein Logfile im Benutzerprofil geschrieben."

    def __init__(self, window):
        super().__init__(window)
        self.entries = []
        card = Card(padding=0, spacing=0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(16, 14, 16, 12)
        toolbar.setSpacing(8)
        self.search = SearchField("Protokoll durchsuchen …")
        self.level = QComboBox()
        for text, value in (("Alle Meldungen", 0), ("Nur Warnungen und Fehler", 2), ("Nur Fehler", 3)):
            self.level.addItem(text, value)
        self.area = QComboBox()
        self.area.addItem("Alle Bereiche", None)
        for key, text in AREAS.items():
            self.area.addItem(text, key)
        toolbar.addWidget(self.search, 2)
        toolbar.addWidget(self.level)
        toolbar.addWidget(self.area)
        toolbar.addStretch(1)
        toolbar.addWidget(button("Kopieren", "copy", on_click=self.copy))
        toolbar.addWidget(button("Exportieren …", "download", on_click=self.export))
        toolbar.addWidget(button("Log-Ordner", "folder", on_click=self.open_folder))
        toolbar.addWidget(button("Leeren", "trash", "ghost", on_click=self.clear))
        card.add(toolbar)
        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_ENTRIES)
        card.add(self.view, 1)
        self.body.addWidget(card, 1)

        self.search.textChanged.connect(self.render)
        self.level.currentIndexChanged.connect(self.render)
        self.area.currentIndexChanged.connect(self.render)
        log_bus.entry.connect(self.add)
        theme.changed.connect(self.render)

    def _accepts(self, entry):
        if LEVEL_RANK.get(entry.level, 1) < self.level.currentData():
            return False
        area = self.area.currentData()
        if area and entry.area != area:
            return False
        query = self.search.text().strip().casefold()
        return not query or query in entry.text.casefold()

    def _html(self, entry):
        colors = {"warning": theme.color("warning"), "error": theme.color("danger"), "success": theme.color("success")}
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        level = LEVEL_TEXT.get(entry.level, entry.level.upper())
        color = colors.get(entry.level, theme.color("text"))
        muted = theme.color("text_faint")
        area = AREAS.get(entry.area, entry.area)
        return (f'<span style="color:{muted}">{stamp}</span>&nbsp;&nbsp;'
                f'<span style="color:{color}; font-weight:600">{level:<8}</span>&nbsp;'
                f'<span style="color:{muted}">[{html.escape(area)}]</span>&nbsp;'
                f'<span style="color:{color}">{html.escape(entry.text)}</span>')

    def add(self, entry):
        self.entries.append(entry)
        if len(self.entries) > MAX_ENTRIES:
            del self.entries[: len(self.entries) - MAX_ENTRIES]
        if self._accepts(entry):
            self.view.appendHtml(self._html(entry))

    def render(self, *_args):
        self.view.clear()
        self.view.appendHtml("<br>".join(self._html(entry) for entry in self.entries if self._accepts(entry)))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _plain(self):
        lines = []
        for entry in self.entries:
            if self._accepts(entry):
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))
                lines.append(f"{stamp}  {LEVEL_TEXT.get(entry.level, entry.level):<8} [{AREAS.get(entry.area, entry.area)}] "
                             f"{entry.text}")
        return "\n".join(lines)

    def copy(self):
        QGuiApplication.clipboard().setText(self._plain())
        self.window.toast("Protokoll in die Zwischenablage kopiert", "info")

    def export(self):
        name = time.strftime("netbox-sync-protokoll-%Y%m%d-%H%M.txt")
        path, _filter = QFileDialog.getSaveFileName(self, "Protokoll exportieren", name, "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(self._plain())
            self.window.toast("Protokoll gespeichert")

    def open_folder(self):
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606 - Windows Explorer

    def clear(self):
        self.entries.clear()
        self.view.clear()
