"""Main window pages."""

from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from netbox_sync.ui.widgets import PageHeader


class Page(QWidget):
    """Common page frame: header plus content area, optionally scrollable."""

    title = ""
    subtitle = ""

    def __init__(self, window, scrollable=False):
        super().__init__()
        self.window = window
        self.state = window.state
        self.tasks = window.tasks
        self.actions = window.actions
        self.setObjectName("PageRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        if scrollable:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            inner = QWidget()
            inner.setObjectName("PageRoot")
            scroll.setWidget(inner)
            outer.addWidget(scroll)
            host = inner
        else:
            host = self
        self.body = QVBoxLayout() if host is self else QVBoxLayout(host)
        if host is self:
            outer.addLayout(self.body)
        self.body.setContentsMargins(32, 26, 32, 24)
        self.body.setSpacing(18)
        self.header = PageHeader(self.title, self.subtitle)
        self.body.addWidget(self.header)

    def on_show(self):
        """Called whenever the page becomes visible."""

    def set_busy(self, busy):
        """Disable actions that would conflict with a running task."""
