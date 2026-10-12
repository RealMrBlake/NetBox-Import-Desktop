"""Light and dark design tokens plus the application style sheet."""

from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from netbox_sync.ui.icons import svg_file

LIGHT = {
    "bg": "#F3F5F9",
    "surface": "#FFFFFF",
    "surface_alt": "#F8FAFC",
    "hover": "#EEF2F7",
    "border": "#E2E8F0",
    "border_strong": "#CBD5E1",
    "text": "#0F172A",
    "text_muted": "#64748B",
    "text_faint": "#94A3B8",
    "accent": "#0A6CF0",
    "accent_hover": "#085DCF",
    "accent_pressed": "#074EAD",
    "accent_soft": "#E7F0FE",
    "accent_text": "#FFFFFF",
    "success": "#15803D",
    "success_soft": "#DCFCE7",
    "warning": "#B45309",
    "warning_soft": "#FEF3C7",
    "danger": "#DC2626",
    "danger_soft": "#FEE2E2",
    "sidebar": "#0B1F3A",
    "sidebar_text": "#B6C3D6",
    "sidebar_hover": "#132B4D",
    "sidebar_active": "#1B3A66",
    "sidebar_accent": "#22D3EE",
    "selection": "#DBE8FD",
    "log_bg": "#FBFCFE",
}

DARK = {
    "bg": "#0E1521",
    "surface": "#151E2C",
    "surface_alt": "#192334",
    "hover": "#1E2A3D",
    "border": "#243044",
    "border_strong": "#334155",
    "text": "#E2E8F0",
    "text_muted": "#94A3B8",
    "text_faint": "#64748B",
    "accent": "#3B8BFF",
    "accent_hover": "#5A9DFF",
    "accent_pressed": "#2F7BEA",
    "accent_soft": "#16294A",
    "accent_text": "#FFFFFF",
    "success": "#4ADE80",
    "success_soft": "#13301F",
    "warning": "#FBBF24",
    "warning_soft": "#33280F",
    "danger": "#F87171",
    "danger_soft": "#3A1717",
    "sidebar": "#09111D",
    "sidebar_text": "#94A3B8",
    "sidebar_hover": "#121E31",
    "sidebar_active": "#18294A",
    "sidebar_accent": "#22D3EE",
    "selection": "#1D3357",
    "log_bg": "#111926",
}

QSS = Template("""
* { outline: 0; }
QWidget { color: $text; font-size: 10pt; }
QMainWindow, QDialog, #PageRoot, QStackedWidget#Pages { background: $bg; }
QDialog { background: $bg; }
QToolTip { background: $text; color: $surface; border: none; padding: 6px 8px; border-radius: 6px; }

/* ---------- Sidebar ---------- */
#Sidebar { background: $sidebar; }
#Sidebar QLabel { color: $sidebar_text; }
#Sidebar #AppTitle { color: #FFFFFF; font-size: 12pt; font-weight: 600; }
#Sidebar #AppSubtitle { color: $sidebar_text; font-size: 8.5pt; }
#Sidebar #SectionLabel { color: #6B7FA0; font-size: 8pt; font-weight: 600; }
#Sidebar QPushButton[nav="true"] {
    color: $sidebar_text; background: transparent; border: none; border-radius: 8px;
    padding: 9px 12px; text-align: left; font-size: 10pt;
}
#Sidebar QPushButton[nav="true"]:hover { background: $sidebar_hover; color: #FFFFFF; }
#Sidebar QPushButton[nav="true"]:checked { background: $sidebar_active; color: #FFFFFF; font-weight: 600; }
#Sidebar QPushButton[nav="true"]:disabled { color: #3E5272; }
#Sidebar QComboBox {
    background: $sidebar_hover; color: #FFFFFF; border: 1px solid #22385A; border-radius: 8px; padding: 6px 10px;
}
#Sidebar QComboBox::drop-down { border: none; width: 24px; }
#Sidebar QComboBox::down-arrow { image: url($chevron_light); width: 14px; height: 14px; }
#Sidebar QToolButton { background: $sidebar_hover; border: 1px solid #22385A; border-radius: 8px; padding: 5px; }
#Sidebar QToolButton:hover { background: $sidebar_active; }
#Sidebar QToolButton::menu-indicator { image: none; }
#Sidebar #VersionLabel { color: #4F6385; font-size: 8pt; }
#Sidebar QLabel[badge="true"] { background: $danger; color: #FFFFFF; }

/* ---------- Typography ---------- */
#PageTitle { font-size: 17pt; font-weight: 600; color: $text; }
#PageSubtitle { color: $text_muted; font-size: 10pt; }
#CardTitle { font-size: 11pt; font-weight: 600; }
#CardSubtitle, QLabel[muted="true"] { color: $text_muted; }
QLabel[faint="true"] { color: $text_faint; }
#StatValue { font-size: 15pt; font-weight: 600; }
#StatLabel { color: $text_muted; font-size: 9pt; }
#EmptyTitle { font-size: 13pt; font-weight: 600; }
#WizardStep { color: $text_faint; font-weight: 600; }
#WizardStep[active="true"] { color: $accent; }
#WizardStep[done="true"] { color: $success; }
#SummaryKey { color: $text_muted; }
#SummaryValue { font-weight: 600; }

/* ---------- Cards ---------- */
#Card { background: $surface; border: 1px solid $border; border-radius: 12px; }
#Card[flat="true"] { background: transparent; border: none; }
#Banner { border-radius: 10px; border: 1px solid $border; background: $surface_alt; }
#Banner[kind="info"] { background: $accent_soft; border-color: $accent_soft; }
#Banner[kind="warning"] { background: $warning_soft; border-color: $warning_soft; }
#Banner[kind="error"] { background: $danger_soft; border-color: $danger_soft; }
#Banner[kind="success"] { background: $success_soft; border-color: $success_soft; }
#Banner QLabel { background: transparent; }
#Separator { background: $border; max-height: 1px; min-height: 1px; }

/* ---------- Badges ---------- */
QLabel[badge="true"] { border-radius: 9px; padding: 2px 9px; font-size: 8.5pt; font-weight: 600; }
QLabel[badge="true"][kind="neutral"] { background: $hover; color: $text_muted; }
QLabel[badge="true"][kind="info"] { background: $accent_soft; color: $accent; }
QLabel[badge="true"][kind="success"] { background: $success_soft; color: $success; }
QLabel[badge="true"][kind="warning"] { background: $warning_soft; color: $warning; }
QLabel[badge="true"][kind="error"] { background: $danger_soft; color: $danger; }

/* ---------- Buttons ---------- */
QPushButton {
    background: $surface; border: 1px solid $border_strong; border-radius: 8px;
    padding: 7px 14px; color: $text; font-weight: 500;
}
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $border; }
QPushButton:disabled { color: $text_faint; border-color: $border; background: $surface_alt; }
QPushButton[variant="primary"] { background: $accent; border-color: $accent; color: $accent_text; font-weight: 600; }
QPushButton[variant="primary"]:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton[variant="primary"]:pressed { background: $accent_pressed; }
QPushButton[variant="primary"]:disabled { background: $border; border-color: $border; color: $text_faint; }
QPushButton[variant="danger"] { color: $danger; }
QPushButton[variant="danger"]:hover { background: $danger_soft; border-color: $danger; }
QPushButton[variant="ghost"] { background: transparent; border-color: transparent; color: $text_muted; }
QPushButton[variant="ghost"]:hover { background: $hover; color: $text; }
QPushButton[variant="link"] { background: transparent; border: none; color: $accent; padding: 2px 4px; }
QPushButton::menu-indicator { image: url($chevron); subcontrol-origin: padding; subcontrol-position: right center; width: 12px; right: 8px; }
QToolButton { background: transparent; border: none; border-radius: 6px; padding: 4px; }
QToolButton:hover { background: $hover; }

/* ---------- Inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {
    background: $surface; border: 1px solid $border_strong; border-radius: 8px;
    padding: 6px 9px; selection-background-color: $accent; selection-color: #FFFFFF;
}
QPlainTextEdit, QTextEdit { padding: 6px; }
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QPlainTextEdit:hover { border-color: $text_faint; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border: 1px solid $accent; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: $surface_alt; color: $text_faint; }
QLineEdit[invalid="true"] { border-color: $danger; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow { image: url($chevron); width: 14px; height: 14px; }
QComboBox QAbstractItemView {
    background: $surface; border: 1px solid $border_strong; border-radius: 8px; padding: 4px;
    selection-background-color: $accent_soft; selection-color: $text; outline: 0;
}
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url($chevron_up); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url($chevron); width: 10px; height: 10px; }

QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator, QTableView::indicator {
    width: 16px; height: 16px; border: 1px solid $border_strong; background: $surface;
}
QCheckBox::indicator, QTableView::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QTableView::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked, QTableView::indicator:checked { background: $accent; border-color: $accent; image: url($check); }
QCheckBox::indicator:indeterminate { background: $accent; border-color: $accent; }
QRadioButton::indicator:checked { background: $accent; border-color: $accent; image: url($radio_dot); }
QCheckBox:disabled { color: $text_faint; }

/* ---------- Tables ---------- */
QTableView, QTreeView, QListView {
    background: $surface; alternate-background-color: $surface_alt; border: none;
    gridline-color: transparent; selection-background-color: $selection; selection-color: $text;
}
QTableView::item, QTreeView::item { padding: 4px 6px; border: none; }
QTableView::item:hover, QTreeView::item:hover { background: $hover; }
QTableView::item:selected, QTreeView::item:selected { background: $selection; color: $text; }
QHeaderView { background: $surface; }
QHeaderView::section {
    background: $surface; color: $text_muted; border: none; border-bottom: 1px solid $border;
    padding: 8px 6px; font-weight: 600; font-size: 9pt;
}
QHeaderView::section:hover { color: $text; }
QHeaderView::down-arrow { image: url($chevron); width: 10px; height: 10px; }
QHeaderView::up-arrow { image: url($chevron_up); width: 10px; height: 10px; }
QTableCornerButton::section { background: $surface; border: none; }

/* ---------- Misc ---------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: $border_strong; border-radius: 3px; min-height: 30px; min-width: 30px; }
QScrollBar::handle:hover { background: $text_faint; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QSplitter::handle { background: transparent; }
QSplitter::handle:vertical { height: 10px; }
QSplitter::handle:horizontal { width: 10px; }
QProgressBar {
    background: $border; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent;
    max-height: 8px;
}
QProgressBar::chunk { background: $accent; border-radius: 4px; }
QMenu { background: $surface; border: 1px solid $border_strong; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 6px; }
QMenu::item:selected { background: $accent_soft; color: $text; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
QMenu::icon { padding-left: 8px; }
QTabWidget::pane { border: 1px solid $border; border-radius: 10px; background: $surface; top: -1px; }
QTabBar::tab {
    background: transparent; color: $text_muted; padding: 8px 14px; border: none;
    border-bottom: 2px solid transparent; margin-right: 4px;
}
QTabBar::tab:selected { color: $accent; border-bottom: 2px solid $accent; font-weight: 600; }
QTabBar::tab:hover { color: $text; }
#StatusBar { background: $surface; border-top: 1px solid $border; }
#StatusBar QLabel { color: $text_muted; font-size: 9pt; }
#Toast { background: $text; border-radius: 10px; }
#Toast QLabel { color: $surface; background: transparent; }
#LogView { background: $log_bg; border: none; font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt; }
#StepCircle { border-radius: 14px; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
              background: $hover; color: $text_muted; font-weight: 600; }
#StepCircle[done="true"] { background: $success_soft; color: $success; }
""")


class ThemeManager(QObject):
    """Applies a theme and tells widgets when colours change."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.mode = "system"
        self.tokens = dict(LIGHT)
        self.dark = False

    def color(self, name):
        return self.tokens[name]

    def resolve_dark(self, mode):
        if mode == "dark":
            return True
        if mode == "light":
            return False
        hints = QApplication.styleHints()
        return hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark

    def apply(self, mode=None):
        app = QApplication.instance()
        if mode:
            self.mode = mode
        self.dark = self.resolve_dark(self.mode)
        self.tokens = dict(DARK if self.dark else LIGHT)
        tokens = self.tokens

        palette = QPalette()
        roles = {
            QPalette.Window: tokens["bg"], QPalette.WindowText: tokens["text"],
            QPalette.Base: tokens["surface"], QPalette.AlternateBase: tokens["surface_alt"],
            QPalette.Text: tokens["text"], QPalette.Button: tokens["surface"],
            QPalette.ButtonText: tokens["text"], QPalette.Highlight: tokens["accent"],
            QPalette.HighlightedText: "#FFFFFF", QPalette.ToolTipBase: tokens["text"],
            QPalette.ToolTipText: tokens["surface"], QPalette.PlaceholderText: tokens["text_faint"],
            QPalette.Link: tokens["accent"], QPalette.Mid: tokens["border_strong"],
            QPalette.Midlight: tokens["border"], QPalette.Light: tokens["surface"],
            QPalette.Dark: tokens["border_strong"],
        }
        for role, value in roles.items():
            palette.setColor(role, QColor(value))
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            palette.setColor(QPalette.Disabled, role, QColor(tokens["text_faint"]))
        app.setPalette(palette)

        style = QSS.substitute(
            tokens,
            chevron=svg_file("chevron-down", tokens["text_muted"], 2.2),
            chevron_up=svg_file("chevron-up", tokens["text_muted"], 2.2),
            chevron_light=svg_file("chevron-down", "#B6C3D6", 2.2),
            check=svg_file("check", "#FFFFFF", 3.0),
            radio_dot=svg_file("radio-dot", "#FFFFFF"),
        )
        app.setStyleSheet(style)
        self.changed.emit()


def base_font():
    font = QFont("Segoe UI")
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


theme = ThemeManager()
