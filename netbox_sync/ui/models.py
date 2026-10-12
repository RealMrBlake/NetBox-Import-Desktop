"""Table models with check boxes, natural sorting and multi-criteria filtering."""

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor

from netbox_sync.ui.icons import icon
from netbox_sync.ui.theme import theme

SORT_ROLE = Qt.UserRole + 1
ROW_ROLE = Qt.UserRole + 2


def natural_key(value):
    """Sort key Qt can compare: numbers stay numbers, text gets zero-padded digits (1/1/2 < 1/1/10)."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    return re.sub(r"\d+", lambda match: match.group(0).zfill(12), str(value).casefold())


@dataclass
class Column:
    key: str
    title: str
    width: int = 120
    align: str = "left"
    value: Callable[[dict], Any] = None
    color: Callable[[dict], str] = None        # theme token
    dot: Callable[[dict], str] = None          # theme token for a status dot
    tooltip: Callable[[dict], str] = None
    sort: Callable[[dict], Any] = None
    extra: dict = field(default_factory=dict)

    def display(self, row):
        return self.value(row) if self.value else row.get(self.key, "")


class RowTableModel(QAbstractTableModel):
    """List-of-dicts model. With ``checkable`` the first column gets a check box."""

    checked_changed = Signal()

    def __init__(self, columns, checkable=False, row_id=lambda row: id(row)):
        super().__init__()
        self.columns = columns
        self.checkable = checkable
        self.row_id = row_id
        self.rows = []
        self.checked = set()
        theme.changed.connect(self._repaint)

    def _repaint(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))

    def set_rows(self, rows, keep_checked=True):
        self.beginResetModel()
        self.rows = list(rows)
        valid = {self.row_id(row) for row in self.rows}
        self.checked = (self.checked & valid) if keep_checked else set()
        self.endResetModel()
        self.checked_changed.emit()

    def refresh(self):
        self._repaint()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        column = self.columns[section]
        if role == Qt.DisplayRole:
            return column.title
        if role == Qt.TextAlignmentRole:
            return (Qt.AlignRight if column.align == "right" else Qt.AlignLeft) | Qt.AlignVCenter
        return None

    def flags(self, index):
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self.checkable and index.column() == 0:
            flags |= Qt.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = self.columns[index.column()]
        if role == Qt.DisplayRole:
            value = column.display(row)
            return "" if value is None else str(value)
        if role == SORT_ROLE:
            return natural_key(column.sort(row) if column.sort else column.display(row))
        if role == ROW_ROLE:
            return row
        if role == Qt.CheckStateRole and self.checkable and index.column() == 0:
            return Qt.Checked if self.row_id(row) in self.checked else Qt.Unchecked
        if role == Qt.TextAlignmentRole:
            return (Qt.AlignRight if column.align == "right" else Qt.AlignLeft) | Qt.AlignVCenter
        if role == Qt.ForegroundRole and column.color:
            token = column.color(row)
            return QColor(theme.color(token)) if token else None
        if role == Qt.DecorationRole and column.dot:
            token = column.dot(row)
            return icon("dot", theme.color(token)) if token else None
        if role == Qt.ToolTipRole and column.tooltip:
            return column.tooltip(row)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and self.checkable and index.column() == 0:
            row_id = self.row_id(self.rows[index.row()])
            if Qt.CheckState(value) == Qt.Checked:
                self.checked.add(row_id)
            else:
                self.checked.discard(row_id)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            self.checked_changed.emit()
            return True
        return False

    def set_checked(self, row_ids, checked=True):
        row_ids = set(row_ids)
        if checked:
            self.checked |= row_ids
        else:
            self.checked -= row_ids
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0), [Qt.CheckStateRole])
        self.checked_changed.emit()

    def checked_rows(self):
        return [row for row in self.rows if self.row_id(row) in self.checked]


class FilterProxy(QSortFilterProxyModel):
    """Free-text search across all columns plus arbitrary row predicates."""

    def __init__(self, source):
        super().__init__()
        self.setSourceModel(source)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.text = ""
        self.predicates = {}

    def set_text(self, text):
        self.text = text.strip().casefold()
        self.invalidateFilter()

    def set_predicate(self, name, predicate):
        if predicate is None:
            self.predicates.pop(name, None)
        else:
            self.predicates[name] = predicate
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        row = model.rows[source_row]
        if any(not predicate(row) for predicate in self.predicates.values()):
            return False
        if not self.text:
            return True
        haystack = " ".join(str(column.display(row) or "") for column in model.columns).casefold()
        return all(term in haystack for term in self.text.split())

    def visible_rows(self):
        model = self.sourceModel()
        return [model.rows[self.mapToSource(self.index(row, 0)).row()] for row in range(self.rowCount())]

    def row_at(self, proxy_index):
        return self.sourceModel().rows[self.mapToSource(proxy_index).row()]
