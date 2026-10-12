"""Reusable widgets that give every page the same look and feel."""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from netbox_sync.ui.icons import icon
from netbox_sync.ui.theme import theme


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class ThemedIconMixin:
    """Re-render an icon whenever the theme changes."""

    def _bind_icon(self, name, color_token, setter):
        def apply():
            setter(icon(name, theme.color(color_token)))
        apply()
        theme.changed.connect(apply)


def button(text, icon_name=None, variant=None, on_click=None, tooltip=None, parent=None):
    widget = QPushButton(text, parent)
    widget.setCursor(Qt.PointingHandCursor)
    if variant:
        widget.setProperty("variant", variant)
    if icon_name:
        token = {"primary": "accent_text", "danger": "danger", "link": "accent"}.get(variant, "text_muted")

        def apply_icon():
            color = "#FFFFFF" if token == "accent_text" else theme.color(token)
            if not widget.isEnabled() and variant == "primary":
                color = theme.color("text_faint")
            widget.setIcon(icon(icon_name, color))

        apply_icon()
        theme.changed.connect(apply_icon)
        widget.setIconSize(QSize(16, 16))
        widget._apply_icon = apply_icon
    if on_click:
        widget.clicked.connect(on_click)
    if tooltip:
        widget.setToolTip(tooltip)
    return widget


def set_enabled(widget, enabled, reason=None):
    """Enable/disable a button and show why it is disabled."""
    widget.setEnabled(enabled)
    if hasattr(widget, "_default_tooltip") is False:
        widget._default_tooltip = widget.toolTip()
    widget.setToolTip(widget._default_tooltip if enabled or not reason else reason)
    if hasattr(widget, "_apply_icon"):
        widget._apply_icon()


def label(text="", muted=False, faint=False, wrap=False, object_name=None, selectable=False):
    widget = QLabel(text)
    if muted:
        widget.setProperty("muted", True)
    if faint:
        widget.setProperty("faint", True)
    if wrap:
        widget.setWordWrap(True)
    if object_name:
        widget.setObjectName(object_name)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def hline():
    line = QFrame()
    line.setObjectName("Separator")
    line.setFrameShape(QFrame.NoFrame)
    return line


class Badge(QLabel):
    def __init__(self, text="", kind="neutral"):
        super().__init__(text)
        self.setProperty("badge", True)
        self.setAlignment(Qt.AlignCenter)
        self.set(text, kind)

    def set(self, text, kind="neutral"):
        self.setText(text)
        self.setProperty("kind", kind)
        self.setVisible(bool(text))
        repolish(self)


class Card(QFrame):
    def __init__(self, title=None, subtitle=None, parent=None, padding=18, spacing=12):
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(padding, padding, padding, padding)
        self.layout_.setSpacing(spacing)
        self.header = None
        self.actions = None
        if title:
            self.header = QHBoxLayout()
            self.header.setSpacing(8)
            text_box = QVBoxLayout()
            text_box.setSpacing(2)
            self.title_label = label(title, object_name="CardTitle")
            text_box.addWidget(self.title_label)
            if subtitle:
                self.subtitle_label = label(subtitle, object_name="CardSubtitle", wrap=True)
                text_box.addWidget(self.subtitle_label)
            self.header.addLayout(text_box, 1)
            self.actions = QHBoxLayout()
            self.actions.setSpacing(8)
            self.header.addLayout(self.actions)
            self.layout_.addLayout(self.header)

    def add(self, widget, stretch=0):
        if isinstance(widget, QWidget):
            self.layout_.addWidget(widget, stretch)
        else:
            self.layout_.addLayout(widget, stretch)
        return widget

    def add_action(self, widget):
        self.actions.addWidget(widget)
        return widget


class PageHeader(QWidget):
    def __init__(self, title, subtitle=""):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.title = label(title, object_name="PageTitle")
        self.subtitle = label(subtitle, object_name="PageSubtitle", wrap=True)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        layout.addLayout(texts, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)

    def add_action(self, widget):
        self.actions.addWidget(widget)
        return widget


class Banner(QFrame):
    """Inline message instead of yet another modal dialog."""

    ICONS = {"info": ("info", "accent"), "warning": ("warning", "warning"),
             "error": ("error", "danger"), "success": ("success", "success")}

    def __init__(self, text="", kind="info", action_text=None, on_action=None, closable=False):
        super().__init__()
        self.setObjectName("Banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        layout.addWidget(self.icon_label, 0, Qt.AlignTop)
        self.text_label = label(text, wrap=True)
        self.text_label.setTextFormat(Qt.RichText)
        self.text_label.setOpenExternalLinks(False)
        layout.addWidget(self.text_label, 1)
        self.action = None
        if action_text:
            self.action = button(action_text, variant="link", on_click=on_action)
            layout.addWidget(self.action, 0, Qt.AlignVCenter)
        if closable:
            close = button("", "x", "ghost", on_click=self.hide)
            close.setFixedSize(26, 26)
            layout.addWidget(close, 0, Qt.AlignTop)
        self.kind = kind
        self.set(text, kind)
        theme.changed.connect(lambda: self.set(self.text_label.text(), self.kind))

    def set(self, text, kind=None):
        if kind:
            self.kind = kind
        self.setProperty("kind", self.kind)
        name, token = self.ICONS[self.kind]
        self.icon_label.setPixmap(icon(name, theme.color(token)).pixmap(18, 18))
        self.text_label.setText(text)
        repolish(self)


class EmptyState(QWidget):
    def __init__(self, icon_name, title, text, action_text=None, on_action=None, secondary=None):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 40, 24, 40)
        layout.setSpacing(10)
        layout.addStretch(1)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        icon_name_ = icon_name

        def apply():
            self.icon_label.setPixmap(icon(icon_name_, theme.color("text_faint"), 1.5).pixmap(56, 56))
        apply()
        theme.changed.connect(apply)
        layout.addWidget(self.icon_label)
        self.title_label = label(title, object_name="EmptyTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)
        self.text_label = label(text, muted=True, wrap=True)
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setMaximumWidth(520)
        layout.addWidget(self.text_label, 0, Qt.AlignHCenter)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.action = None
        if secondary:
            buttons.addWidget(button(secondary[0], secondary[2] if len(secondary) > 2 else None,
                                     on_click=secondary[1]))
        if action_text:
            self.action = button(action_text, "arrow-right", "primary", on_click=on_action)
            buttons.addWidget(self.action)
        buttons.addStretch(1)
        layout.addSpacing(6)
        layout.addLayout(buttons)
        layout.addStretch(2)

    def set_text(self, title=None, text=None):
        if title is not None:
            self.title_label.setText(title)
        if text is not None:
            self.text_label.setText(text)


class StatTile(QFrame):
    clicked = Signal()

    def __init__(self, icon_name, caption):
        super().__init__()
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(40, 40)
        self.icon_label.setAlignment(Qt.AlignCenter)
        self._icon_name = icon_name
        layout.addWidget(self.icon_label, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.caption = label(caption, object_name="StatLabel")
        self.value = label("–", object_name="StatValue")
        self.detail = label("", muted=True, wrap=True)
        texts.addWidget(self.caption)
        texts.addWidget(self.value)
        texts.addWidget(self.detail)
        layout.addLayout(texts, 1)
        self.badge = Badge()
        layout.addWidget(self.badge, 0, Qt.AlignTop)
        self._apply()
        theme.changed.connect(self._apply)

    def _apply(self):
        self.icon_label.setStyleSheet(
            f"background: {theme.color('accent_soft')}; border-radius: 10px;"
        )
        self.icon_label.setPixmap(icon(self._icon_name, theme.color("accent")).pixmap(22, 22))

    def set(self, value, detail="", badge_text="", badge_kind="neutral"):
        self.value.setText(value)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.badge.set(badge_text, badge_kind)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class SearchField(QLineEdit):
    def __init__(self, placeholder="Suchen …"):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._action = None

        def apply():
            if self._action:
                self.removeAction(self._action)
            self._action = self.addAction(icon("search", theme.color("text_faint")), QLineEdit.LeadingPosition)
        apply()
        theme.changed.connect(apply)
        self.setMinimumWidth(220)


class PasswordField(QLineEdit):
    def __init__(self, placeholder=""):
        super().__init__()
        self.setEchoMode(QLineEdit.Password)
        self.setPlaceholderText(placeholder)
        self._toggle = self.addAction(icon("eye", theme.color("text_muted")), QLineEdit.TrailingPosition)
        self._toggle.setToolTip("Anzeigen")
        self._toggle.triggered.connect(self.toggle)
        theme.changed.connect(self._update_icon)

    def toggle(self):
        visible = self.echoMode() == QLineEdit.Password
        self.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
        self._update_icon()

    def _update_icon(self):
        hidden = self.echoMode() == QLineEdit.Password
        self._toggle.setIcon(icon("eye" if hidden else "eye-off", theme.color("text_muted")))
        self._toggle.setToolTip("Anzeigen" if hidden else "Verbergen")


class SearchableComboBox(QComboBox):
    """Editable combo box with substring completion – handy for long NetBox lists."""

    def __init__(self, placeholder=""):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.lineEdit().setPlaceholderText(placeholder)
        completer = QCompleter(self.model(), self)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(completer)
        self.setMaxVisibleItems(18)

    def set_items(self, items, current=None):
        self.blockSignals(True)
        self.clear()
        for item in items:
            if isinstance(item, tuple):
                self.addItem(item[0], item[1])
            else:
                self.addItem(item, item)
        self.completer().setModel(self.model())
        if current is not None:
            self.select_data(current)
        else:
            self.setCurrentIndex(-1)
        self.blockSignals(False)

    def select_data(self, data):
        index = self.findData(data)
        self.setCurrentIndex(index)
        if index < 0:
            self.setEditText("")

    def valid_data(self):
        """Return the item data only if the visible text matches an item."""
        index = self.findText(self.currentText(), Qt.MatchFixedString)
        return self.itemData(index) if index >= 0 else None


def configure_table(view, stretch_column=None, selection=QAbstractItemView.ExtendedSelection):
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(selection)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.setSortingEnabled(True)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(34)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(stretch_column is None)
    if stretch_column is not None:
        header.setSectionResizeMode(stretch_column, QHeaderView.Stretch)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    return view


class Table(QTableView):
    def __init__(self, stretch_column=None):
        super().__init__()
        configure_table(self, stretch_column)


class Toast(QFrame):
    """Short non-blocking confirmation in the lower right corner."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("Toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 16, 11)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        layout.addWidget(self.icon_label)
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(420)
        layout.addWidget(self.text_label)
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b"opacity", self)
        self.animation.setDuration(220)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fade_out)
        self.hide()

    def show_message(self, text, kind="success", duration=4500):
        colors = {"success": "#4ADE80", "info": "#60A5FA", "warning": "#FBBF24", "error": "#F87171"}
        names = {"success": "success", "info": "info", "warning": "warning", "error": "error"}
        self.icon_label.setPixmap(icon(names.get(kind, "info"), colors.get(kind, "#60A5FA")).pixmap(18, 18))
        self.text_label.setText(text)
        self.adjustSize()
        self.reposition()
        self.raise_()
        self.show()
        self.animation.stop()
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.start()
        self.timer.start(duration)

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            self.move(parent.width() - self.width() - 24, parent.height() - self.height() - 52)

    def fade_out(self):
        self.animation.stop()
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.finished.connect(self._hide_once)
        self.animation.start()

    def _hide_once(self):
        self.animation.finished.disconnect(self._hide_once)
        self.hide()

    def mousePressEvent(self, _event):
        self.timer.stop()
        self.hide()


class TextDialog(QDialog):
    """Read-only text viewer with copy support."""

    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        editor = QPlainTextEdit(text)
        editor.setObjectName("LogView")
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(editor)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        from PySide6.QtGui import QGuiApplication
        buttons.addWidget(button("Kopieren", "copy", on_click=lambda: QGuiApplication.clipboard().setText(text)))
        buttons.addWidget(button("Schließen", variant="primary", on_click=self.accept))
        layout.addLayout(buttons)


def message(parent, title, text, kind="info", details=None, buttons=("OK",), default=0):
    """Consistent modal message. Returns the index of the clicked button or -1."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(460)
    dialog.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(22, 20, 22, 18)
    layout.setSpacing(14)
    row = QHBoxLayout()
    row.setSpacing(14)
    icon_label = QLabel()
    token = {"info": "accent", "warning": "warning", "error": "danger", "success": "success", "question": "accent"}[kind]
    name = {"question": "info"}.get(kind, kind)
    icon_label.setPixmap(icon(name, theme.color(token)).pixmap(28, 28))
    row.addWidget(icon_label, 0, Qt.AlignTop)
    texts = QVBoxLayout()
    texts.setSpacing(6)
    texts.addWidget(label(title, object_name="CardTitle"))
    body = label(text, wrap=True, selectable=True)
    body.setMaximumWidth(560)
    texts.addWidget(body)
    row.addLayout(texts, 1)
    layout.addLayout(row)
    if details:
        detail_view = QPlainTextEdit(details)
        detail_view.setObjectName("LogView")
        detail_view.setReadOnly(True)
        detail_view.setMinimumHeight(140)
        detail_view.setMaximumHeight(260)
        layout.addWidget(detail_view)
    box = QHBoxLayout()
    box.addStretch(1)
    result = {"index": -1}
    for index, text_value in enumerate(buttons):
        variant = None
        if index == default:
            variant = "danger" if kind == "warning" and len(buttons) > 1 else "primary"
        widget = button(text_value, variant=variant)
        widget.clicked.connect(lambda _checked=False, i=index: (result.update(index=i), dialog.accept()))
        if index == default:
            widget.setDefault(True)
        box.addWidget(widget)
    layout.addLayout(box)
    dialog.exec()
    return result["index"]


def confirm(parent, title, text, ok_text="OK", cancel_text="Abbrechen", destructive=False, details=None):
    kind = "warning" if destructive else "question"
    index = message(parent, title, text, kind, details, buttons=(cancel_text, ok_text), default=1)
    return index == 1


def error(parent, title, text, details=None):
    message(parent, title, text, "error", details)


def form_dialog_buttons(dialog, ok_text="Speichern"):
    box = QDialogButtonBox()
    cancel = button("Abbrechen", on_click=dialog.reject)
    ok = button(ok_text, variant="primary", on_click=dialog.accept)
    ok.setDefault(True)
    box.addButton(cancel, QDialogButtonBox.RejectRole)
    box.addButton(ok, QDialogButtonBox.AcceptRole)
    return box, ok


def checkbox(text, checked=False, tooltip=None):
    widget = QCheckBox(text)
    widget.setChecked(checked)
    widget.setCursor(Qt.PointingHandCursor)
    if tooltip:
        widget.setToolTip(tooltip)
    return widget
