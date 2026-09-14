"""The step list as a table of typed editors.

Every field used to be free text in a cell — "yes"/"no", "frames"/"seconds", a
blank cell for an axis to leave alone. That parses, and a typo reads back as
the old value with nothing said. Now each cell edits through a widget that can
only produce a legal value: the value lives in `UserRole` while the text is a
rendering of it (`250 um`, `no change`, `1.5 s`) — usually. Two cells render as
something OTHER than their raw value when a friendlier fact is available:

- **Stage** is X and Y together, `(x, y)` — one place, not two cells that
  happen to sit next to each other. Filled from a saved FOV (double-click, or
  right-click -> Fill from FOV…) it shows the FOV's name in front of the
  numbers instead — `window1 (100 um, 200 um)` — since a step over a
  recognised spot is more useful read by name. Typing a new number (double-
  click -> Set position…) detaches the name; it may no longer be that spot.
- **Pattern** shows a thumbnail once one is set, a picture being the whole
  point of an image path. An ROI set is not an image — its shapes are
  rasterised over their own bounding box instead (`_roi_icon`; camera px, the
  space they are drawn in, so no DMD calibration is needed for *this* — it
  answers "what shapes", not "where on the DMD").

Both are non-editable cells (no delegate, like Pattern always was) rather than
something typed into directly — Stage takes a small dialog for the same reason
Pattern takes a file dialog: the value is not free text, so nothing here
parses it back out of a string.

There is no Light/LED column: both used to be per-step checkboxes, but they
duplicated information the step already carries. Light now follows whether a
step has a pattern (`engine.py`) — a step either shows something or it
doesn't — and the illumination LED simply tracks capture, on whenever the
camera is recording, on nobody's per-step say-so. One less pair of boxes to
tick, and no state that could disagree with the pattern cell right next to it.

Rows are **dragged to reorder** — a protocol is an ordered thing, and the
arrow buttons alone made moving step 9 to the top nine deliberate presses.
Ctrl+Up/Ctrl+Down does the same from the keyboard, and both go through
`move_row`, so there is one implementation of "what reordering means".

Selection is **contiguous, not single**: a repeat group (`routines/settings.
py`'s `Group`) is a start/end RANGE, so shift-click/shift-arrow extending a
block is the one extra thing selection needs to express beyond "which row is
current" — `selected_range()` reads it back for the panel's "Group selected"
control. `set_groups()` then tells the table which rows are currently grouped,
so it can tint them and badge the group's first row with its repeat count,
rather than that only being visible in a separate list below the table.

Most non-reordering actions that used to be buttons under the table (Duplicate,
Remove, Pattern/ROI/Clear pattern, Fill from FOV, Group selected) are a
right-click menu on the table now (`contextMenuEvent`) — the table still owns
none of their dialogs, it only emits a signal per action and the panel does
the rest, the same split `pattern_requested` already had.

Split from `panel.py`: seven columns of four kinds (free text, dialog-set
display, numeric spin box, dropdown) is a job on its own. What it edits,
`routines/settings.py` owns.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHeaderView, QMenu,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem,
)

from acqApp.routines.settings import UNITS, Group, Step, pattern_label

# The Pattern cell's thumbnail — big enough to recognise a stripe set or a
# grating by eye, small enough that a dozen rows still fit on screen.
_THUMB = QSize(28, 28)

# A grouped row's tint — faint enough to read as "part of something" without
# fighting the running-step bold/selection highlight painted over it.
_GROUP_TINT = QColor(208, 135, 112, 40)   # style.HEX["routines"] at low alpha

VALUE = Qt.ItemDataRole.UserRole

# Columns, in order: (title, field, tooltip).
COLS = (
    ("Step",       "label",    "Your name for this step. It goes into the file."),
    ("Stage",      "xy",       "Where to send the stage for this step, as "
                               "(X, Y).\nDouble-click, or right-click -> Set "
                               "position…, to type numbers; Delete clears "
                               "both back to \"no change\".\nFilled from a "
                               "saved FOV (right-click -> Fill from FOV…), "
                               "the cell names it in front of the numbers "
                               "instead — typing a new number detaches it."),
    ("Pattern",    "pattern",  "The DMD pattern for this step, shown as a "
                               "thumbnail once one is set. Double-click to "
                               "choose one, Delete to clear it; with none, "
                               "the DMD keeps whatever it already has. Light "
                               "follows a pattern being set — no separate "
                               "on/off to forget."),
    ("Capture",    "length",   "How much to capture, once the step has settled."),
    ("Unit",       "unit",     "Frames or seconds — never converted between "
                               "them, so a step means what it says."),
    ("Settle",     "settle_s", "Wait this long after the move and the pattern, "
                               "before capture starts."),
    ("Puff every", "puff_interval_s", "Fire an air puff at this interval during "
                               "capture, on the puffer's own configured "
                               "duration. 0 = no puffs."),
)
FIELDS = [f for _t, f, _tip in COLS]

# What an axis this step does not move reads as. A word, not a blank cell:
# blank used to mean both "leave this axis alone" and "I have not typed it
# yet", and "leave" on its own did not say leave WHAT.
NO_CHANGE = "no change"

# The row header of the step the engine is on. The row is bold as well; the
# marker is what survives a table the operator has scrolled.
RUNNING = "▶"


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
    """A numeric cell, range-clamped. (Stage used to be two of these with a
    "no change" state under the range — `_position_spin` in `panel.py` now
    owns that sentinel shape, for its dialog's spin boxes instead.)"""

    def __init__(self, lo: float, hi: float, decimals: int, suffix: str,
                 step: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self._lo, self._hi = lo, hi
        self._decimals, self._suffix, self._step = decimals, suffix, step

    def createEditor(self, parent, _opt, _index):
        sb = QDoubleSpinBox(parent)
        sb.setDecimals(self._decimals)
        sb.setSingleStep(self._step)
        sb.setSuffix(self._suffix)
        sb.setRange(self._lo, self._hi)
        sb.setKeyboardTracking(False)
        return sb

    def setEditorData(self, editor, index) -> None:
        editor.setValue(float(index.data(VALUE)))

    def setModelData(self, editor, model, index) -> None:
        v = editor.value()
        model.setData(index, float(min(max(v, self._lo), self._hi)), VALUE)


class StepTable(QTableWidget):
    """The routine's steps, edited in place. Emits `changed` on any edit.

    Holds a reference to the caller's list of `Step`s and writes into it — the
    panel owns the routine, this owns how it is edited.
    """

    changed = pyqtSignal()
    pattern_requested = pyqtSignal()        # the panel owns the file dialog
    roi_requested = pyqtSignal()            # the panel owns the ROI-set picker
    fov_requested = pyqtSignal()            # the panel owns the FOV picker
    position_requested = pyqtSignal()       # the panel owns the position dialog
    duplicate_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    clear_pattern_requested = pyqtSignal()
    group_requested = pyqtSignal()          # the panel reads selected_range()
    reordered = pyqtSignal(int)             # the moved step's new row

    def __init__(self, steps: list[Step], parent=None) -> None:
        super().__init__(0, len(COLS), parent)
        self._steps = steps
        self._loading = False
        self._running: int | None = None     # row the engine is on
        self._groups: list[Group] = []

        self.setHorizontalHeaderLabels([t for t, _f, _tip in COLS])
        for col, (_t, _f, tip) in enumerate(COLS):
            self.horizontalHeaderItem(col).setToolTip(tip)
        self.verticalHeader().setDefaultSectionSize(_THUMB.height() + 4)
        self.setIconSize(_THUMB)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Contiguous, not Extended: a repeat group is a RANGE (Group.start/end),
        # so the one extra thing multi-select needs to express is a contiguous
        # block — shift-click/shift-arrow extend it, ctrl-click (disjoint rows)
        # stays unavailable, and every single-row action still has one obvious
        # target (the anchor row) when more than one row is selected.
        self.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        # Drag a row to where it belongs. InternalMove alone would have Qt move
        # the *cells*; `dropEvent` below moves the Step instead, because the
        # list is what the engine reads.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.verticalHeader().setSectionsMovable(False)
        self.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        # One click on a selected cell opens its editor: with combo boxes and
        # spin boxes, needing a double-click to see the choices hides them.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)

        # Stage has no delegate — like Pattern, it is set through a dialog
        # (Set position…/Fill from FOV…), not typed into a cell.
        self.setItemDelegateForColumn(
            FIELDS.index("length"),
            _NumberDelegate(0.01, 1e6, 2, "", 10.0, parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("unit"),
            _ChoiceDelegate(tuple((u, u) for u in UNITS), parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("settle_s"),
            _NumberDelegate(0.0, 120.0, 2, " s", 0.05, parent=self))
        self.setItemDelegateForColumn(
            FIELDS.index("puff_interval_s"),
            _NumberDelegate(0.0, 3600.0, 2, " s", 0.5, parent=self))

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
            self._paint_numbers()
        finally:
            self._loading = False

    def set_groups(self, groups: list[Group]) -> None:
        """The routine's repeat groups, so the table can show which rows are
        in one — a separate list below the table is not "at a glance" once
        you're scrolled past it. Repaints; call after any group edit.

        Signals off, like reload()/_repaint_row(): this only re-renders
        existing step data, and an itemChanged here would read it straight
        back into the routine as a spurious edit — see move_row's comment
        for why every repaint path follows this same rule.
        """
        self._groups = list(groups)
        self._loading = True
        try:
            self._paint_numbers()
            for row in range(self.rowCount()):
                self._paint_row(row, self._steps[row])
        finally:
            self._loading = False

    def _group_at(self, row: int) -> Group | None:
        for g in self._groups:
            if g.start <= row <= g.end:
                return g
        return None

    def _paint_numbers(self) -> None:
        """The row header is the step's place in the order, and carries the
        running marker and — on a group's FIRST row — its repeat count, so
        "which step is this, and is it repeated" is answered in one glance
        without cross-referencing the Repeat groups list."""
        labels = []
        for r in range(self.rowCount()):
            n = RUNNING if r == self._running else str(r + 1)
            g = self._group_at(r)
            if g is not None and g.start == r:
                n = f"{n} ×{g.repeats}"
            labels.append(n)
        self.setVerticalHeaderLabels(labels)

    def _paint_row(self, row: int, s: Step) -> None:
        tint = self._group_at(row) is not None
        for col, field in enumerate(FIELDS):
            value = (s.x_um, s.y_um) if field == "xy" else getattr(s, field)
            item = self.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, col, item)
            item.setData(VALUE, value)
            if field == "xy":
                # Not typed into — see Stage's entry in COLS. The pair is
                # still the value of record (VALUE, above) and still what the
                # engine drives to; only the rendering changes for a named spot.
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setText(_xy_text(s.x_um, s.y_um, s.fov))
                item.setToolTip(
                    f"{s.x_um:g} um, {s.y_um:g} um — from the saved FOV "
                    f"{s.fov!r}. Double-click to type new numbers, which "
                    f"detaches the name." if s.fov else "")
            elif field == "pattern":
                # Chosen with a file dialog, so the cell is not typed into.
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(s.pattern or "no pattern — the DMD keeps what "
                                             "it has. Double-click to choose one.")
                item.setIcon(_pattern_icon(s.pattern))
                item.setText(_render(field, value))
            else:
                item.setText(_render(field, value))
            item.setBackground(_GROUP_TINT if tint else self.palette().base())

    def mark_running(self, row: int | None) -> None:
        """Show which step the engine is on. -1/None clears it.

        Signals off, like every other repaint path: `setFont()` calls
        `setData()` under the hood, so without the guard this fires
        itemChanged on every cell, every time the running row moves — read
        straight back into the routine as a spurious edit mid-run.
        """
        self._running = row if row is not None and row >= 0 else None
        self._loading = True
        try:
            for r in range(self.rowCount()):
                for c in range(self.columnCount()):
                    item = self.item(r, c)
                    if item is None:
                        continue
                    f = item.font()
                    f.setBold(r == row)
                    item.setFont(f)
            self._paint_numbers()
        finally:
            self._loading = False

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
        elif field in ("pattern", "xy"):
            return                          # only a dialog sets these
        else:
            value = item.data(VALUE)
            if field == "unit":
                s.unit = value if value in UNITS else s.unit
            elif field == "length":
                # A step measured in frames is a whole number of them; the
                # validator refuses the alternative, so round here rather than
                # refuse at the Start button for a rounding the panel could fix.
                s.length = (round(float(value)) if s.unit == "frames"
                            else float(value))
            elif field == "settle_s":
                s.settle_s = float(value)
            elif field == "puff_interval_s":
                s.puff_interval_s = float(value)
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
        field = FIELDS[col]
        if field == "pattern":
            self.select_row(row)
            self.pattern_requested.emit()
        elif field == "xy":
            self.select_row(row)
            self.position_requested.emit()

    # The two cells with no delegate at all — set through a dialog, so Delete
    # is the only way to empty them: Stage back to "no change" on both axes,
    # Pattern back to none.
    _CLEARABLE = ("xy", "pattern")

    def keyPressEvent(self, ev) -> None:
        if (ev.modifiers() & Qt.KeyboardModifier.ControlModifier
                and ev.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            row = self.selected_row()
            self.move_row(row, row + (-1 if ev.key() == Qt.Key.Key_Up else +1))
            return
        if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            item = self.currentItem()
            if item is not None and FIELDS[item.column()] in self._CLEARABLE:
                self.clear_cell(item.row(), FIELDS[item.column()])
                return
        super().keyPressEvent(ev)

    def clear_cell(self, row: int, field: str) -> None:
        """Empty one cell: Stage back to "no change" on both axes, a pattern
        to none."""
        if not (0 <= row < len(self._steps)):
            return
        s = self._steps[row]
        if field == "xy":
            s.x_um = s.y_um = None
            s.fov = ""       # no longer a full pair, so no longer that spot
        else:
            s.pattern = ""
        self._repaint_row(row)
        self.changed.emit()

    # ── reordering ───────────────────────────────────────────────────────────
    def move_row(self, src: int, dest: int) -> bool:
        """Move one step to `dest`, its FINAL index. The one implementation.

        The arrows, Ctrl+Up/Down and a drop all land here, so reordering cannot
        mean two different things depending on how it was asked for.
        """
        n = len(self._steps)
        if not (0 <= src < n):
            return False
        dest = max(0, min(dest, n - 1))
        if dest == src:
            return False
        self._steps.insert(dest, self._steps.pop(src))
        # Only src..dest actually shifted — a full reload() repainted every
        # row (7 columns x N) for what is always a contiguous shift of the
        # rows between them; drag-drop and Ctrl+Up/Down both land here.
        lo, hi = min(src, dest), max(src, dest)
        self._loading = True
        try:
            for row in range(lo, hi + 1):
                self._paint_row(row, self._steps[row])
            self._paint_numbers()
        finally:
            self._loading = False
        self.select_row(dest)          # so a second press moves the same step
        self.reordered.emit(dest)
        self.changed.emit()
        return True

    def dropEvent(self, ev) -> None:
        """A dropped row moves the Step, not the cells.

        Qt's InternalMove would shuffle the *items* and leave `self._steps` in
        the old order — the table would look right and the engine would run the
        old protocol.
        """
        if ev.source() is not self:
            ev.ignore()
            return
        src = self.selected_row()
        insert = self._drop_index(ev)
        ev.setDropAction(Qt.DropAction.MoveAction)
        ev.accept()
        # An insertion point past the source collapses by one once it is lifted.
        self.move_row(src, insert - 1 if insert > src else insert)

    def _drop_index(self, ev) -> int:
        """Where the drop points, as an insertion index in [0, len]."""
        pos = ev.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return len(self._steps)
        rect = self.visualRect(idx)
        # Below the middle of a row means after it — the drop indicator's line.
        return idx.row() + (1 if pos.y() > rect.center().y() else 0)

    # ── selection ────────────────────────────────────────────────────────────
    def selected_row(self) -> int:
        rows = {i.row() for i in self.selectedIndexes()}
        return min(rows) if rows else -1

    def select_row(self, row: int) -> None:
        if 0 <= row < self.rowCount():
            self.selectRow(row)

    def selected_range(self) -> tuple[int, int] | None:
        """(first, last) rows of the current selection, inclusive — or None
        with fewer than 2 rows selected, since a "group" of one step is not
        what the Repeat groups control is for. ContiguousSelection guarantees
        no gaps, so min/max is the whole selection, not just its ends."""
        rows = {i.row() for i in self.selectedIndexes()}
        if len(rows) < 2:
            return None
        return min(rows), max(rows)

    # ── context menu ─────────────────────────────────────────────────────────
    def contextMenuEvent(self, event) -> None:
        """One place for the actions that used to be a row of buttons under
        the table — Duplicate/Remove/Pattern/ROI/FOV/Group — so the panel
        stays legible with the table doing most of the vertical space.
        Right-click on a row already part of a multi-row selection keeps that
        selection (so "Group selected" is on offer); right-click elsewhere
        collapses to just that row, like any other list."""
        idx = self.indexAt(event.pos())
        if idx.isValid() and idx.row() not in {i.row() for i in self.selectedIndexes()}:
            self.select_row(idx.row())
        row = self.selected_row()
        span = self.selected_range()

        menu = QMenu(self)
        if row >= 0:
            act = menu.addAction("Duplicate step")
            act.triggered.connect(self.duplicate_requested.emit)
            act = menu.addAction("Remove step")
            act.triggered.connect(self.remove_requested.emit)
            menu.addSeparator()
            act = menu.addAction("Set pattern…")
            act.triggered.connect(self.pattern_requested.emit)
            act = menu.addAction("Set ROI set…")
            act.triggered.connect(self.roi_requested.emit)
            act = menu.addAction("Clear pattern")
            act.setEnabled(bool(self._steps[row].pattern))
            act.triggered.connect(self.clear_pattern_requested.emit)
            menu.addSeparator()
            act = menu.addAction("Set position…")
            act.triggered.connect(self.position_requested.emit)
            act = menu.addAction("Fill Stage X/Y from FOV…")
            act.triggered.connect(self.fov_requested.emit)
        if span is not None:
            if row >= 0:
                menu.addSeparator()
            act = menu.addAction(
                f"Group selected steps {span[0] + 1}-{span[1] + 1}…")
            act.triggered.connect(self.group_requested.emit)
        if not menu.isEmpty():
            menu.exec(event.globalPos())


def _xy_text(x: float | None, y: float | None, fov: str) -> str:
    """Stage as one fact: "(x, y)", or the saved FOV's name in front of it
    once one is filled — a recognised spot is read by name, not by the two
    numbers that happen to describe it."""
    def part(v: float | None) -> str:
        return NO_CHANGE if v is None else f"{v:g} um"
    coords = f"({part(x)}, {part(y)})"
    return f"{fov} {coords}" if fov else coords


def _pattern_icon(pattern: str) -> QIcon:
    """A thumbnail of the pattern, so a step reads as "shows this" at a
    glance instead of a filename."""
    if not pattern:
        return QIcon()
    if Path(pattern).name.endswith(".roi.json"):
        return _roi_icon(pattern)
    pix = QPixmap(pattern)
    if pix.isNull():
        return QIcon()
    return QIcon(pix.scaled(_THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation))


def _roi_icon(path: str) -> QIcon:
    """An ROI set has no image of its own — its shapes are rasterised over
    their own bounding box instead, in camera px (the space they are drawn
    in). No DMD calibration involved: this answers "what shapes", not "where
    on the DMD" — `RoiSet.dmd_frame` is the one that needs a calibration."""
    from acqApp.devices.dmd import roi_store

    try:
        rois = roi_store.load(path)
    except Exception:                # noqa: BLE001 — a bad/missing file is
        return QIcon()                # just "no thumbnail", not a crash
    shown = [r for r in rois if r.enabled] or list(rois)
    if not shown:
        return QIcon()
    pts = np.concatenate([r.boundary() for r in shown])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    pad = np.maximum(hi - lo, 1.0) * 0.15      # a sliver of margin, not edge-to-edge
    lo, hi = lo - pad, hi + pad
    n = _THUMB.width()
    xs = np.linspace(lo[0], hi[0], n)
    ys = np.linspace(lo[1], hi[1], n)
    mask = np.zeros((n, n), dtype=bool)
    for r in shown:
        mask |= r.mask_at(xs, ys)
    bits = np.where(mask, np.uint8(255), np.uint8(0))
    # QImage can reference the buffer it's built from — .copy() detaches it,
    # so the array going out of scope on return does not corrupt the icon.
    img = QImage(bits.tobytes(), n, n, n, QImage.Format.Format_Grayscale8)
    return QIcon(QPixmap.fromImage(img.copy()))


def _render(field: str, value) -> str:
    """One value as the operator reads it. The parse is the delegate's job."""
    if field == "pattern":
        return pattern_label(value) if value else "—"
    if field == "length":
        return f"{value:g}"
    if field == "settle_s":
        return f"{value:g} s"
    if field == "puff_interval_s":
        return "off" if not value else f"{value:g} s"
    return str(value)
