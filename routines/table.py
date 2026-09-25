"""The step list as a table of typed editors.

A step is one atomic action now (Move / Display / Wait / Puff), not a
composite row bundling all of them — the **Kind** column picks which, and
the **Details**/**Length**/**Unit**/**Settle** columns render "—" and refuse
editing on a row whose kind doesn't use them (Length/Unit are Wait-only,
Settle is Move-only, Details is Move/Display-only) — the same "sentinel, not
blank" rule the old Stage/Pattern cells already used for "NA". Every
editable cell still edits through a widget that can only produce a legal
value: the value lives in `UserRole` while the text is a rendering of it,
usually — **Details** renders as something OTHER than its raw value the same
two ways Stage/Pattern always have:

- **Move**'s Details is X, Y (and Z, on a rig with a focus axis) together,
  `(x, y)` or `(x, y, z)` — one place, not several cells. Filled from a saved
  FOV (double-click, or right-click -> Fill from FOV…) it shows the FOV's
  name in front of the numbers instead. Typing a new number (double-click ->
  Set position…) detaches the name.
- **Display**'s Details shows a thumbnail once a pattern is set, a picture
  being the whole point of an image path; an ROI set rasterises its shapes
  over their own bounding box instead (`_roi_icon`).

Both are non-editable cells (no delegate) rather than something typed
directly into — Move takes a small dialog for the same reason Display takes
a file dialog: the value isn't free text.

Rows are **dragged to reorder**, Ctrl+Up/Down do the same, both through
`move_row` — one implementation of "what reordering means." Reordering does
NOT rewrite `Group` ranges (index-based) — a step dragged out of a group
leaves the range pointing at whatever is now at that position; this is a
pre-existing limitation, not something this table tries to fix.

Selection is **contiguous, not single**: a repeat group (`Group`) is a
start/end RANGE, so shift-click/shift-arrow extending a block is the one
extra thing selection needs to express — `selected_range()` reads it back for
the panel's "Group selected" control. A recording is a `record` STEP, not a range.
`set_groups()` tells the table which rows are in one; Record rows are read
from the steps. It can tint them (blended where a row is both grouped and recording) and badge the group's
first row with its repeat count / a recording's row with a marker, rather
than either only being visible in a separate list below the table.

Most non-reordering actions live on a right-click menu (`contextMenuEvent`),
gated by the row's kind — Set pattern/ROI/Clear only for Display rows, Set
position/Fill from FOV only for Move rows — the table still owns none of
their dialogs, it only emits a signal per action and the panel does the rest.

Split from `panel.py`: what it edits, `routines/settings.py` owns.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QHeaderView, QInputDialog,
    QMenu, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
)

from acqApp.routines.settings import (KINDS, TIMED_KINDS, UNITS, Group, Step,
                                      pattern_label)

# The Pattern cell's thumbnail — big enough to recognise a stripe set or a
# grating by eye, small enough that a dozen rows still fit on screen.
_THUMB = QSize(28, 28)

# A grouped row's tint — faint enough to read as "part of something" without
# fighting the running-step bold/selection highlight painted over it. A warm
# accent (matches the "N step(s) emit light" summary text), not one of
# style.HEX's per-subsystem colors — public so `routines/timeline.py` can
# paint the same bracket in the same color there.
GROUP_TINT = QColor(208, 135, 112, 40)
# A recording bracket's tint — a different hue (red, "on air") so a row that
# is both grouped and recording reads as neither tint alone; see `_tint_for`.
REC_TINT = QColor(196, 60, 60, 55)

VALUE = Qt.ItemDataRole.UserRole

KIND_LABELS: dict[str, str] = {
    "move": "Move", "display": "Display", "wait": "Wait", "record": "Record",
    "puff": "Puff",
    "trigger": "Trigger",
}

# Columns, in order: (title, field, tooltip). "details" is a synthetic field
# — what it shows and edits depends on the row's kind, painted in
# `_paint_details` rather than through the generic `_render`.
COLS = (
    ("Comment", "comment", "A short note for this step, shown as a title "
                          "here. Double-click to read or write the full "
                          "text."),
    ("Kind",    "kind",  "What this step does: Move the stage, start "
                         "Displaying a pattern, Wait, Puff, or wait for an "
                         "external Trigger on the camera's line."),
    ("Details", "details", "Move: where to send the stage, as (X, Y, Z). "
                         "Double-click, or right-click -> Set position…, to "
                         "type numbers; Delete clears every axis back to "
                         "\"NA\". Filled from a saved FOV, the cell "
                         "names it in front of the numbers instead.\nDisplay: the "
                         "DMD pattern, shown as a thumbnail once one is set. "
                         "Double-click to choose one, Delete to stop "
                         "displaying."),
    ("Length",  "length", "How long to wait (Wait steps only)."),
    ("Unit",    "unit",   "Frames or seconds — never converted between "
                         "them, so a step means what it says (Wait steps "
                         "only)."),
    ("Settle",  "settle_s", "Wait this long after the stage arrives, before "
                         "the step ends (Move steps only)."),
)
FIELDS = [f for _t, f, _tip in COLS]

# What an axis a Move step doesn't send reads as. A word, not a blank cell:
# blank used to mean both "leave this axis alone" and "I haven't typed it
# yet", and "leave" on its own didn't say leave WHAT.
NO_CHANGE = "NA"

# The row header of the step the engine is on. The row is bold as well; the
# marker is what survives a table the operator has scrolled.
RUNNING = "▶"

# Fields only meaningful for one kind — "—" and non-editable on any other row.
_KIND_OF_FIELD = {"length": TIMED_KINDS, "unit": TIMED_KINDS,
                  "settle_s": ("move",)}


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
    """A numeric cell, range-clamped."""

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
    panel owns the routine, this owns how it's edited.
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
        # Contiguous, not Extended: a Group/Recording is a RANGE — see module
        # docstring. ctrl-click (disjoint rows) stays unavailable.
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
        # Kind/Length/Unit/Settle are a word or a number — fit to content
        # instead of an equal Stretch share, which left them mostly empty
        # space. Comment/Details are the two columns worth the room that
        # frees up.
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for f in ("comment", "details"):
            header.setSectionResizeMode(FIELDS.index(f),
                                        QHeaderView.ResizeMode.Stretch)
        # One click on a selected cell opens its editor: with combo boxes and
        # spin boxes, needing a double-click to see the choices hides them.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)

        # Details has no delegate — like before this redesign, it's set
        # through a dialog (Set position…/Fill from FOV…, or a pattern/ROI
        # file dialog), not typed into a cell.
        self.setItemDelegateForColumn(
            FIELDS.index("kind"),
            _ChoiceDelegate(tuple((KIND_LABELS[k], k) for k in KINDS),
                            parent=self))
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
        # The row header is the step's name tag: double-click to set the label that used to be
        # its own ("Step") column — one that sat empty far more often than not.
        self.verticalHeader().sectionDoubleClicked.connect(
            self._on_header_double_clicked)
        self.verticalHeader().setToolTip("Double-click to name a step.")
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
        in one — a separate list below the table isn't "at a glance" once
        you're scrolled past it. Repaints; call after any group edit."""
        self._groups = list(groups)
        self._repaint_all()

    def _repaint_all(self) -> None:
        # Signals off, as in reload(): this only re-renders existing step
        # data, and an itemChanged here would read it straight back into the
        # routine as a spurious edit.
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

    def _recording_at(self, row: int) -> Step | None:
        """The row's step if it is a Record step; read live, so a kind edit
        retints without anyone telling the table."""
        if 0 <= row < len(self._steps) and self._steps[row].kind == "record":
            return self._steps[row]
        return None

    def _tint_for(self, row: int) -> QColor | None:
        """The row's background: blended if it's both grouped AND inside a
        recording, so neither reads as the other's plain tint."""
        g, r = self._group_at(row) is not None, self._recording_at(row) is not None
        if g and r:
            return QColor((GROUP_TINT.red() + REC_TINT.red()) // 2,
                         (GROUP_TINT.green() + REC_TINT.green()) // 2,
                         (GROUP_TINT.blue() + REC_TINT.blue()) // 2,
                         max(GROUP_TINT.alpha(), REC_TINT.alpha()))
        if g:
            return GROUP_TINT
        if r:
            return REC_TINT
        return None

    def _paint_numbers(self) -> None:
        """The row header is the step's place in the order, and carries the
        running marker, a group's repeat count on its FIRST row, a recording
        marker on EVERY row it covers (no count to show like a group's ×N),
        and the step's own name if it has one (double-click to set) —
        "which step is this, is it repeated, is it being recorded, what is
        it called" answered in one glance."""
        labels = []
        for r in range(self.rowCount()):
            n = RUNNING if r == self._running else str(r + 1)
            g = self._group_at(r)
            if g is not None and g.start == r:
                n = f"{n} ×{g.repeats}"
            if self._recording_at(r) is not None:
                n = f"{n} ⏺"
            if r < len(self._steps) and self._steps[r].label:
                n = f"{n}: {self._steps[r].label}"
            labels.append(n)
        self.setVerticalHeaderLabels(labels)

    def _paint_row(self, row: int, s: Step) -> None:
        tint = self._tint_for(row) or self.palette().base()
        for col, field in enumerate(FIELDS):
            item = self.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, col, item)
            if field == "comment":
                item.setData(VALUE, s.comment)
                item.setText(_comment_preview(s.comment))
                item.setToolTip(s.comment)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            elif field == "kind":
                item.setData(VALUE, s.kind)
                item.setText(KIND_LABELS.get(s.kind, s.kind))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            elif field == "details":
                self._paint_details(item, s)
            else:
                active = s.kind in _KIND_OF_FIELD[field]
                value = getattr(s, field)
                item.setData(VALUE, value)
                item.setText(_render(field, value) if active else "—")
                self._set_editable(item, active)
            item.setBackground(tint)

    def _paint_details(self, item: QTableWidgetItem, s: Step) -> None:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if s.kind == "move":
            # Not typed into — see Details' entry in COLS. The triple is
            # still the value of record (VALUE, above) and still what the
            # engine drives to; only the rendering changes for a named spot.
            item.setData(VALUE, (s.x_um, s.y_um, s.z_um))
            item.setIcon(QIcon())
            item.setText(_xyz_text(s.x_um, s.y_um, s.z_um, s.fov))
            z_part = f", {s.z_um:g} um" if s.z_um is not None else ""
            item.setToolTip(
                f"{s.x_um:g} um, {s.y_um:g} um{z_part} — from saved FOV "
                f"{s.fov!r}. Double-click to type new numbers, which "
                f"detaches the name." if s.fov else
                "Double-click to set a position, or right-click -> Fill "
                "from FOV…")
        elif s.kind == "display":
            item.setData(VALUE, s.pattern)
            item.setToolTip(s.pattern or "no pattern set — double-click to "
                                        "choose one.")
            item.setIcon(_pattern_icon(s.pattern))
            item.setText(pattern_label(s.pattern) if s.pattern
                        else "stop displaying")
        elif s.kind == "trigger":
            item.setData(VALUE, None)
            item.setIcon(QIcon())
            item.setText("wait for camera trigger")
            item.setToolTip(
                "Holds here until an external edge arrives on the camera's "
                "trigger line. Nothing to set — the length of what follows is "
                "what decides how long the recording lasts.\nPut the "
                "Record step AFTER this one, so the file starts on the edge.")
        else:
            item.setData(VALUE, None)
            item.setIcon(QIcon())
            item.setText("—")
            item.setToolTip("")

    @staticmethod
    def _set_editable(item: QTableWidgetItem, active: bool) -> None:
        flags = item.flags()
        item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if active
                     else flags & ~Qt.ItemFlag.ItemIsEditable)

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
        if field == "kind":
            value = item.data(VALUE)
            if value in KINDS:
                s.kind = value
        elif field == "details":
            return                          # only a dialog sets this
        elif field in _KIND_OF_FIELD:
            if s.kind not in _KIND_OF_FIELD[field]:
                return                      # cell wasn't editable; ignore
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
        if field == "comment":
            self._edit_comment(row)
            return
        if field != "details":
            return
        kind = self._steps[row].kind
        if kind not in ("move", "display"):
            return
        self.select_row(row)
        if kind == "move":
            self.position_requested.emit()
        else:
            self.pattern_requested.emit()

    # The one cell with no delegate at all — set through a dialog, so Delete
    # is the only way to empty it: Move back to "NA" on both axes,
    # Display back to "stop displaying".
    _CLEARABLE = ("details",)

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
        """Empty the Details cell: Move back to "NA", Display back to
        "stop displaying"."""
        if not (0 <= row < len(self._steps)):
            return
        s = self._steps[row]
        if field == "details":
            if s.kind == "move":
                s.x_um = s.y_um = s.z_um = None
                s.fov = ""       # no longer a full pair, so no longer that spot
            elif s.kind == "display":
                s.pattern = ""
        self._repaint_row(row)
        self.changed.emit()

    # ── reordering ───────────────────────────────────────────────────────────
    def move_row(self, src: int, dest: int) -> bool:
        """Move one step to `dest`, its FINAL index. The one implementation.

        The arrows, Ctrl+Up/Down and a drop all land here, so reordering can't
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
        # row for what's always a contiguous shift of the rows between them;
        # drag-drop and Ctrl+Up/Down both land here.
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

    def startDrag(self, supported_actions) -> None:
        """Offer Copy only. Qt's own startDrag deletes the source rows
        whenever the drag ends as a Move — wherever it lands, including a
        drop outside this table — and `move_row` already does the whole move
        itself, so a Move outcome can only lose the step."""
        indexes = self.selectedIndexes()
        if not indexes:
            return
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(indexes))
        drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)

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
        # CopyAction, not MoveAction: `move_row` below already does the whole
        # move (list splice + repaint). Accepting MoveAction here makes Qt's
        # own startDrag() ALSO delete the source row afterward, on top of the
        # one move_row already performed — the step then vanishes until the
        # next full reload() repaints over the damage.
        ev.setDropAction(Qt.DropAction.CopyAction)
        ev.accept()
        # An insertion point past the source collapses by one once it's lifted.
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
    def _selected_rows(self) -> set[int]:
        return {i.row() for i in self.selectedIndexes()}

    def selected_row(self) -> int:
        rows = self._selected_rows()
        return min(rows) if rows else -1

    def select_row(self, row: int) -> None:
        if 0 <= row < self.rowCount():
            self.selectRow(row)

    def selected_range(self) -> tuple[int, int] | None:
        """(first, last) rows of the current selection, inclusive — or None
        with fewer than 2 rows selected. ContiguousSelection guarantees no
        gaps, so min/max is the whole selection, not just its ends.

        Two rows is right for a repeat GROUP — one step repeated in place is
        what a Wait's own length already says.
        """
        rows = self._selected_rows()
        if len(rows) < 2:
            return None
        return min(rows), max(rows)

    def _on_header_double_clicked(self, row: int) -> None:
        """Name a step. Its label used to be its own ("Step") column, which
        sat empty far more often than not; the header already shows the
        step's number, so it shows the name too."""
        if not (0 <= row < len(self._steps)):
            return
        text, ok = QInputDialog.getText(
            self, "Step name", "Label for this step:",
            text=self._steps[row].label)
        if ok:
            self._steps[row].label = text.strip()
            self._paint_numbers()
            self.changed.emit()

    def _edit_comment(self, row: int) -> None:
        """The Comment cell shows a title; double-click opens the full text
        — the cell itself is too narrow for more than that."""
        if not (0 <= row < len(self._steps)):
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "Comment for this step", "Note:", self._steps[row].comment)
        if ok:
            self._steps[row].comment = text.strip()
            self._repaint_row(row)
            self.changed.emit()

    # ── context menu ─────────────────────────────────────────────────────────
    def contextMenuEvent(self, event) -> None:
        """One place for the actions that used to be a row of buttons under
        the table, gated by the row's kind — Set pattern/ROI/Clear only make
        sense on a Display row, Set position/Fill from FOV only on a Move
        row. Right-click on a row already part of a multi-row selection keeps
        that selection (so "Group selected" is on offer); right-click
        elsewhere collapses to just that row, like any other list. Recording
        isn't here at all — click the row header instead."""
        idx = self.indexAt(event.pos())
        if idx.isValid() and idx.row() not in self._selected_rows():
            self.select_row(idx.row())
        row = self.selected_row()
        span = self.selected_range()

        menu = QMenu(self)
        if row >= 0:
            kind = self._steps[row].kind
            act = menu.addAction("Duplicate step")
            act.triggered.connect(self.duplicate_requested.emit)
            act = menu.addAction("Remove step")
            act.triggered.connect(self.remove_requested.emit)
            if kind == "display":
                menu.addSeparator()
                act = menu.addAction("Set pattern…")
                act.triggered.connect(self.pattern_requested.emit)
                act = menu.addAction("Set ROI set…")
                act.triggered.connect(self.roi_requested.emit)
                act = menu.addAction("Clear pattern")
                act.setEnabled(bool(self._steps[row].pattern))
                act.triggered.connect(self.clear_pattern_requested.emit)
            elif kind == "move":
                menu.addSeparator()
                act = menu.addAction("Set position…")
                act.triggered.connect(self.position_requested.emit)
                act = menu.addAction("Fill Stage X/Y/Z from FOV…")
                act.triggered.connect(self.fov_requested.emit)
        if span is not None:
            if row >= 0:
                menu.addSeparator()
            act = menu.addAction(
                f"Group selected steps {span[0] + 1}-{span[1] + 1}…")
            act.triggered.connect(self.group_requested.emit)
        if not menu.isEmpty():
            menu.exec(event.globalPos())


def _xyz_text(x: float | None, y: float | None, z: float | None,
             fov: str) -> str:
    """Move's Details as one fact: "(x, y)", or "(x, y, z)" once a step has a
    Z target (most rigs and most steps never do — Z only joins the text when
    it's actually set), or the saved FOV's name in front of it once one is
    filled — a recognised spot is read by name, not by the numbers that
    happen to describe it."""
    def part(v: float | None) -> str:
        return NO_CHANGE if v is None else f"{v:g} um"
    coords = (f"({part(x)}, {part(y)})" if z is None else
             f"({part(x)}, {part(y)}, {part(z)})")
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
    their own bounding box instead, in camera px (the space they're drawn
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
    # so the array going out of scope on return doesn't corrupt the icon.
    img = QImage(bits.tobytes(), n, n, n, QImage.Format.Format_Grayscale8)
    return QIcon(QPixmap.fromImage(img.copy()))


def _comment_preview(text: str, limit: int = 40) -> str:
    """The Comment cell's text — a title, not the note; the full thing is
    one double-click away."""
    if not text:
        return "—"
    first = text.splitlines()[0]
    return first if len(first) <= limit else first[:limit - 1] + "…"


def _render(field: str, value) -> str:
    """One value as the operator reads it. The parse is the delegate's job."""
    if field == "length":
        return f"{value:g}"
    if field == "settle_s":
        return f"{value:g} s"
    return str(value)
