"""Experiment routines — the step table and the run controls.

The panel edits a `Routine` and emits it; it decides nothing. Two rules it does
enforce, because they belong next to the button:

- **Start is refused with the problems listed**, not greyed out with no reason.
  `validate()` returns sentences, and an operator who cannot start needs to
  read which step is wrong.
- **Arming is not persisted**, as in `closed_loop/`: the step list is saved,
  the fact that a routine is *running* never is. A restored "running" would
  drive the stage at launch.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.routines.engine import Phase
from acqApp.routines.settings import SAVE_MODES, UNITS, Routine, Step

# Columns of the step table, in order.
COLS = ("Label", "X (um)", "Y (um)", "Pattern", "Light", "Length", "Unit",
        "Settle (s)")


def _num(text: str) -> float | None:
    """A cell the operator may leave blank — blank means "leave this axis"."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class SettingsPanel(QWidget):
    """The routine, plus Start / Pause / Resume / Skip / Abort."""

    settings_changed = pyqtSignal(object)      # emits Routine (persisted)
    start_requested  = pyqtSignal()
    pause_requested  = pyqtSignal()
    resume_requested = pyqtSignal()
    skip_requested   = pyqtSignal()
    abort_requested  = pyqtSignal()

    def __init__(self, routine: Routine | None = None, parent=None) -> None:
        super().__init__(parent)
        self._r = routine or Routine()
        self._loading = False
        self._painted: str | None = None      # last phase actually painted
        self._painted_text: str | None = None
        self._build()
        self._reload_table()
        self._set_phase(Phase.IDLE, "")

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("Protocol")
        lay = QVBoxLayout(grp)
        lay.setSpacing(4)

        form = QFormLayout()
        form.setSpacing(4)
        self._txt_name = QLineEdit(self._r.name)
        form.addRow("Name:", self._txt_name)

        self._spn_cycles = QSpinBox()
        self._spn_cycles.setRange(1, 9999)
        self._spn_cycles.setValue(max(1, self._r.cycles))
        self._spn_cycles.setToolTip("How many times the whole step list runs.")
        form.addRow("Repeat the list:", self._spn_cycles)

        self._cmb_save = QComboBox()
        for key, label in SAVE_MODES.items():
            self._cmb_save.addItem(label, key)
        idx = self._cmb_save.findData(self._r.save_mode)
        self._cmb_save.setCurrentIndex(max(0, idx))
        form.addRow("Save as:", self._cmb_save)
        lay.addLayout(form)

        self._tbl = QTableWidget(0, len(COLS))
        self._tbl.setHorizontalHeaderLabels(list(COLS))
        self._tbl.verticalHeader().setDefaultSectionSize(22)
        self._tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._tbl.setMinimumHeight(160)
        lay.addWidget(self._tbl, 1)

        btns = QHBoxLayout()
        for text, slot, tip in (
                ("+ Step", self._add_step, "Append a step to the list."),
                ("Duplicate", self._dup_step,
                 "Copy the selected step — a grid is one step edited N times."),
                ("Remove", self._del_step, "Delete the selected step."),
                ("Pattern…", self._pick_pattern,
                 "Set the selected step's DMD pattern file.")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        root.addWidget(grp, 1)

        # ── running ──────────────────────────────────────────────────────────
        rgrp = QGroupBox("Run")
        rlay = QVBoxLayout(rgrp)
        rlay.setSpacing(4)

        self._btn_start = QPushButton("▶ Start routine")
        self._btn_start.setStyleSheet(style.toggle_btn("routines"))
        self._btn_start.clicked.connect(self.start_requested)
        rlay.addWidget(self._btn_start)

        row = QHBoxLayout()
        self._btn_pause = QPushButton("Pause")
        self._btn_resume = QPushButton("Resume (repeats the step)")
        self._btn_skip = QPushButton("Skip step")
        self._btn_abort = QPushButton("Abort")
        for b, sig, tip in (
                (self._btn_pause, self.pause_requested,
                 "Stop motion and blank the light. Capture keeps running."),
                (self._btn_resume, self.resume_requested,
                 "Run the paused step again from its start, as a fresh "
                 "attempt. The interrupted one stays in the file, marked."),
                (self._btn_skip, self.skip_requested,
                 "Give up on the paused step and go on to the next one."),
                (self._btn_abort, self.abort_requested,
                 "End the routine. Recording is still yours to stop.")):
            b.setToolTip(tip)
            b.clicked.connect(sig)
            row.addWidget(b)
        rlay.addLayout(row)

        self._lbl_state = QLabel("—")
        f = self._lbl_state.font()
        f.setBold(True)
        self._lbl_state.setFont(f)
        self._lbl_state.setWordWrap(True)
        rlay.addWidget(self._lbl_state)

        note = QLabel("A routine moves the stage and projects light on its own. "
                      "It runs only while a session does, and it is never "
                      "remembered as running.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        rlay.addWidget(note)
        root.addWidget(rgrp)

        self._txt_name.editingFinished.connect(self._emit)
        self._spn_cycles.valueChanged.connect(self._emit)
        self._cmb_save.currentIndexChanged.connect(self._emit)
        self._tbl.itemChanged.connect(self._on_cell_changed)

    # ── the step table ───────────────────────────────────────────────────────
    def _reload_table(self) -> None:
        """Repaint the table from `self._r`. Signals off — an itemChanged here
        would read half-built rows back into the routine."""
        self._loading = True
        try:
            self._tbl.setRowCount(len(self._r.steps))
            for row, s in enumerate(self._r.steps):
                self._set_row(row, s)
        finally:
            self._loading = False

    def _set_row(self, row: int, s: Step) -> None:
        for col, text in enumerate((
                s.label,
                "" if s.x_um is None else f"{s.x_um:g}",
                "" if s.y_um is None else f"{s.y_um:g}",
                Path(s.pattern).name if s.pattern else "",
                "yes" if s.project else "no",
                f"{s.length:g}",
                s.unit,
                f"{s.settle_s:g}")):
            item = QTableWidgetItem(text)
            if col == 3:                        # the pattern is set by browsing
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(s.pattern or "no pattern — the DMD keeps what "
                                             "it has")
            self._tbl.setItem(row, col, item)

    def _on_cell_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._r.steps)):
            return
        s = self._r.steps[row]
        text = item.text().strip()
        if col == 0:
            s.label = text
        elif col == 1:
            s.x_um = _num(text)
        elif col == 2:
            s.y_um = _num(text)
        elif col == 4:
            s.project = text.lower() in ("yes", "y", "true", "1", "on")
        elif col == 5:
            s.length = _num(text) if _num(text) is not None else s.length
        elif col == 6:
            s.unit = text.lower() if text.lower() in UNITS else s.unit
        elif col == 7:
            v = _num(text)
            s.settle_s = s.settle_s if v is None else v
        self._loading = True                    # rewrite the cell canonically
        try:
            self._set_row(row, s)
        finally:
            self._loading = False
        self._emit()

    def _selected(self) -> int:
        rows = {i.row() for i in self._tbl.selectedIndexes()}
        return min(rows) if rows else -1

    def _add_step(self) -> None:
        self._r.steps.append(Step(label=f"step {len(self._r.steps) + 1}"))
        self._reload_table()
        self._emit()

    def _dup_step(self) -> None:
        row = self._selected()
        if row < 0:
            return
        from dataclasses import replace
        self._r.steps.insert(row + 1, replace(self._r.steps[row]))
        self._reload_table()
        self._emit()

    def _del_step(self) -> None:
        row = self._selected()
        if row < 0:
            return
        del self._r.steps[row]
        self._reload_table()
        self._emit()

    def _pick_pattern(self) -> None:
        row = self._selected()
        if row < 0:
            return
        start = str(Path(self._r.steps[row].pattern).parent) \
            if self._r.steps[row].pattern else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pattern for this step", start,
            "Images (*.png *.bmp *.tif);;All files (*)")
        if path:                    # empty = cancelled, which must not clear it
            self._r.steps[row].pattern = path
            self._reload_table()
            self._emit()

    # ── run state ────────────────────────────────────────────────────────────
    def set_state(self, phase: str, text: str) -> None:
        """Called from the adapter's display tick."""
        self._set_phase(phase, text)

    def _set_phase(self, phase: str, text: str) -> None:
        """Repaint only what changed.

        The adapter calls this every display tick, and `setStyleSheet` repolishes
        the widget against the window's whole cascade — measured at 26 us a call
        and **53 % of the shared 30 Hz tick** with eight modules loaded, to
        re-apply the identical string. The tick was never in trouble (0.05 ms of
        a 33 ms budget); this half of it was simply free to remove.
        """
        if phase != self._painted:
            running = phase in (Phase.SETTLE, Phase.CAPTURE)
            paused = phase == Phase.PAUSED
            self._btn_start.setEnabled(not running and not paused)
            self._btn_pause.setEnabled(running)
            for b in (self._btn_resume, self._btn_skip):
                b.setEnabled(paused)
            self._btn_abort.setEnabled(running or paused)
            # The step list must not be edited out from under a running engine:
            # it holds an index into it.
            self._tbl.setEnabled(not running and not paused)
            self._lbl_state.setStyleSheet(
                "color:#d08770;" if paused else
                (f"color:{style.HEX['routines']};" if running else ""))
            self._painted = phase
        # The text moves within a phase (progress, step number); the styling
        # does not.
        if text != self._painted_text:
            self._lbl_state.setText(text or "—")
            self._painted_text = text

    def show_problems(self, problems: list[str]) -> None:
        """Why Start did nothing — every reason, not the first one."""
        self._lbl_state.setText("Cannot start:\n• " + "\n• ".join(problems))
        self._lbl_state.setStyleSheet("color:#d08770;")
        # Written out of band, so the next _set_phase must repaint even if the
        # phase has not moved.
        self._painted = self._painted_text = None

    # ── settings ─────────────────────────────────────────────────────────────
    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> Routine:
        self._r.name = self._txt_name.text().strip() or "routine"
        self._r.cycles = self._spn_cycles.value()
        self._r.save_mode = self._cmb_save.currentData() or "single"
        return self._r
