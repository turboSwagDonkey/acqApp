"""The step list as a table of typed editors.

Every field used to be free text in a cell — "yes"/"no", "frames"/"seconds", a
blank cell for an axis to leave alone. That parses, and a typo reads back as
the old value with nothing said. Now each cell edits through a widget that can
only produce a legal value, and **the value lives in `UserRole`** while the
text is a rendering of it (`250 um`, `no change`, `1.5 s`).

Split from `panel.py`: eight fields of five kinds is a job on its own. What it
edits, `routines/settings.py` owns.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHeaderView, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem,
)

from acqApp.routines.settings import UNITS, Step

VALUE = Qt.ItemDataRole.UserRole

# Columns, in order: (title, field, tooltip).
COLS = (
    ("Step",       "label",    "Your name for this step. It goes into the file."),
    ("Stage X",    "x_um",     "Where to send the stage's X axis for this "
                               "step.\n\"no change\" means this step leaves X "
                               "wherever the step before it put the stage — so "
                               "a column of Y positions at one X does not "
                               "repeat the X in every row.\nOne step below the "
                               "lowest value, or press Delete on the cell."),
    ("Stage Y",    "y_um",     "Where to send the stage's Y axis for this "
                               "step.\n\"no change\" means this step leaves Y "
                               "wherever the step before it put the stage.\n"
                               "One step below the lowest value, or press "
                               "Delete on the cell."),
    ("Pattern",    "pattern",  "The DMD pattern for this step. Double-click "
                               "to choose one, Delete to clear it; with none, "
                               "the DMD keeps whatever it already has."),
    ("Light",      "project",  "Whether the DMD emits light for the length of "
                               "this step."),
    ("Capture",    "length",   "How much to capture, once the step has settled."),
    ("Unit",       "unit",     "Frames or seconds — never converted between "
                               "them, so a step means what it says."),
    ("Settle",     "settle_s", "Wait this long after the move and the pattern, "
                               "before capture starts."),
)
FIELDS = [f for _t, f, _tip in COLS]

# What an axis this step does not move reads as. A word, not a blank cell:
# blank used to mean both "leave this axis alone" and "I have not typed it
# yet", and "leave" on its own did not say leave WHAT.
NO_CHANGE = "no change"


class _ChoiceDelegate(QStyledItemDelegate):
    """A cell with a fixed vocabulary. `choices` is ((label, value), …)."""

    def __init__(self, choices, parent=None) -> None:
        super().__init__(parent)
        self._choices = tuple(choices)

    def createEditor(self, parent, _opt, _index):
        cb = QComboBox(parent)
        for label, value in self._choices:
            cb.addItem(label, value)
        return cb

    def setEditorData(self, editor, index) -> None:
        i = editor.findData(index.data(VALUE))
        editor.setCurrentIndex(max(0, i))
        editor.showPopup()          # one click to the list, not two

    def setModelData(self, editor, model, index) -> None:
        # Only the value: the table renders the text from it, under its own
        # signal guard, so one edit is one change rather than two.
        model.setData(index, editor.currentData(), VALUE)


class _NumberDelegate(QStyledItemDelegate):
    """A numeric cell. `optional` adds a "no change" state under the range.

    Qt spells "may hold nothing" as `specialValueText` on the **minimum**, so
    the sentinel must be ONE step below the lowest real value. Parked further
    down (-1e6 under a range starting at -1e5) the state is real but
    unreachable — nine thousand arrow presses — so only a magic number sets it.
    """

    def __init__(self, lo: float, hi: float, decimals: int, suffix: str,
                 step: float = 1.0, optional: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._lo, self._hi = lo, hi
        self._decimals, self._suffix, self._step = decimals, suffix, step
        self._optional = optional
        self._blank = lo - step

    def createEditor(self, parent, _opt, _index):
        sb = QDoubleSpinBox(parent)
        sb.setDecimals(self._decimals)
        sb.setSingleStep(self._step)
        sb.setSuffix(self._suffix)
        sb.setRange(self._blank if self._optional else self._lo, self._hi)
        if self._optional:
            sb.setSpecialValueText(NO_CHANGE)
            sb.setToolTip(f"One step below {self._lo:g} is \"{NO_CHANGE}\": this "
                          f"step leaves the axis where it is.")
        sb.setKeyboardTracking(False)
        return sb

    def setEditorData(self, editor, index) -> None:
        v = index.data(VALUE)
        editor.setValue(self._blank if v is None else float(v))

    def setModelData(self, editor, model, index) -> None:
        v = editor.value()
        if self._optional and v <= self._blank:
            model.setData(index, None, VALUE)
            return
        model.setData(index, float(min(max(v, self._lo), self._hi)), VALUE)


class StepTable(QTableWidget):
    """The routine's steps, edited in place. Emits `changed` on any edit.

    Holds a reference to the caller's list of `Step`s and writes into it — the
    panel owns the routine, this owns how it is edited.
    """

    changed = pyqtSignal()
    pattern_requested = pyqtSignal(int)     # row; the panel owns the file dialog

    def __init__(self, steps: list[Step], parent=None) -> None:
        super().__init__(0, len(COLS), parent)
        self._steps = steps
        self._loading = False

        self.setHorizontalHeaderLabels([t for t, _f, _tip in COLS])
        for col, (_t, _f, tip) in enumerate(COLS):
            self.horizontalHeaderItem(col).setToolTip(tip)
        self.verticalHeader().setDefaultSectionSize(24)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        # One click on a selected cell opens its editor: with combo boxes and
        # spin boxes, needing a double-click to see the choices hides them.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)

        self.setItemDelegateForColumn(
            FIELDS.index("x_um"),
            _NumberDelegate(-1e5, 1e5, 0, " um", 100.0, optional=True, parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("y_um"),
            _NumberDelegate(-1e5, 1e5, 0, " um", 100.0, optional=True, parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("project"),
            _ChoiceDelegate((("no", False), ("yes", True)), parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("length"),
            _NumberDelegate(0.01, 1e6, 2, "", 10.0, parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("unit"),
            _ChoiceDelegate(tuple((u, u) for u in UNITS), parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("settle_s"),
            _NumberDelegate(0.0, 120.0, 2, " s", 0.05, parent=self))

        self.itemChanged.connect(self._on_item_changed)
        self.cellDoubleClicked.connect(self._on_double_click)
        self.reload()

    # ── painting ─────────────────────────────────────────────────────────────
    def reload(self) -> None:
        """Repaint from the step list. Signals off — an itemChanged here would
        read half-built rows back into the routine."""
        self._loading = True
        try:
            self.setRowCount(len(self._steps))
            for row, s in enumerate(self._steps):
                self._paint_row(row, s)
        finally:
            self._loading = False

    def _paint_row(self, row: int, s: Step) -> None:
        for col, field in enumerate(FIELDS):
            value = getattr(s, field)
            item = self.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, col, item)
            item.setData(VALUE, value)
            item.setText(_render(field, value))
            if field == "project":
                # The one column that decides whether light is emitted. It is
                # coloured rather than shouted about, and only when it is on.
                item.setForeground(QColor("#d08770") if value
                                   else self.palette().text())
            if field == "pattern":
                # Chosen with a file dialog, so the cell is not typed into.
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(s.pattern or "no pattern — the DMD keeps what "
                                             "it has. Double-click to choose one.")

    def mark_running(self, row: int | None) -> None:
        """Show which step the engine is on. -1/None clears it."""
        for r in range(self.rowCount()):
            for c in range(self.columnCount()):
                item = self.item(r, c)
                if item is None:
                    continue
                f = item.font()
                f.setBold(r == row)
                item.setFont(f)

    # ── editing ──────────────────────────────────────────────────────────────
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._steps)):
            return
        s = self._steps[row]
        field = FIELDS[col]
        if field == "label":
            s.label = item.text().strip()
        elif field == "pattern":
            return                          # only the file dialog sets this
        else:
            value = item.data(VALUE)
            if field in ("x_um", "y_um"):
                setattr(s, field, value)    # None is legal here: "leave"
            elif field == "project":
                s.project = bool(value)
            elif field == "unit":
                s.unit = value if value in UNITS else s.unit
            elif field == "length":
                # A step measured in frames is a whole number of them; the
                # validator refuses the alternative, so round here rather than
                # refuse at the Start button for a rounding the panel could fix.
                s.length = (round(float(value)) if s.unit == "frames"
                            else float(value))
            elif field == "settle_s":
                s.settle_s = float(value)
        self._repaint_row(row)
        self.changed.emit()

    def _repaint_row(self, row: int) -> None:
        """Re-render one row from the step, without re-entering the handler."""
        self._loading = True
        try:
            self._paint_row(row, self._steps[row])
        finally:
            self._loading = False

    def _on_double_click(self, row: int, col: int) -> None:
        if FIELDS[col] == "pattern":
            self.pattern_requested.emit(row)

    # The two cells that can hold nothing are the two that are hard to empty
    # through their editor — an axis has to be stepped under its range, and a
    # pattern is chosen with a file dialog, which cannot express "none".
    _CLEARABLE = ("x_um", "y_um", "pattern")

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            item = self.currentItem()
            if item is not None and FIELDS[item.column()] in self._CLEARABLE:
                self.clear_cell(item.row(), FIELDS[item.column()])
                return
        super().keyPressEvent(ev)

    def clear_cell(self, row: int, field: str) -> None:
        """Empty one cell: an axis back to "no change", a pattern to none."""
        if not (0 <= row < len(self._steps)):
            return
        setattr(self._steps[row], field, "" if field == "pattern" else None)
        self._repaint_row(row)
        self.changed.emit()

    # ── selection ────────────────────────────────────────────────────────────
    def selected_row(self) -> int:
        rows = {i.row() for i in self.selectedIndexes()}
        return min(rows) if rows else -1

    def select_row(self, row: int) -> None:
        if 0 <= row < self.rowCount():
            self.selectRow(row)


def _render(field: str, value) -> str:
    """One value as the operator reads it. The parse is the delegate's job."""
    if field in ("x_um", "y_um"):
        return NO_CHANGE if value is None else f"{value:g} um"
    if field == "pattern":
        return Path(value).name if value else "—"
    if field == "project":
        return "yes" if value else "no"
    if field == "length":
        return f"{value:g}"
    if field == "settle_s":
        return f"{value:g} s"
    return str(value)
