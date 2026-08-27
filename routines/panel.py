"""Experiment routines — the protocol, the run controls, and one Start button.

The panel edits a `Routine` and emits it; it decides nothing. Three rules it
does enforce, because they belong next to the button:

- **Start starts everything it needs.** It opens the recording itself (through
  `ModuleHost.set_recording`, as the DMD calibration opens the live view
  through `set_live`) rather than refusing until the operator has found the
  Record button in another part of the window. A routine that cannot record is
  a routine that cannot run, so making the operator do it by hand only moved
  the failure earlier.
- **Start is refused with the problems listed**, not greyed out with no reason.
  `validate()` returns sentences, and an operator who cannot start needs to
  read which step is wrong.
- **Arming is not persisted**, as in `closed_loop/`: the step list is saved,
  the fact that a routine is *running* never is. A restored "running" would
  drive the stage at launch.

The step *table* is `routines/table.py` — every cell edits through a widget
that can only produce a legal value, which is why nothing here parses "yes".
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.routines.engine import Phase
from acqApp.routines.settings import SAVE_MODES, Routine, Step
from acqApp.routines.table import StepTable


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
        self._marked: int | None = None       # step row shown as running
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

        self._tbl = StepTable(self._r.steps)
        self._tbl.setMinimumHeight(160)
        self._tbl.changed.connect(self._emit)
        self._tbl.pattern_requested.connect(self._pick_pattern_for)
        lay.addWidget(self._tbl, 1)

        btns = QHBoxLayout()
        for text, slot, tip in (
                ("+ Step", self._add_step, "Append a step to the list."),
                ("Duplicate", self._dup_step,
                 "Copy the selected step — a grid is one step edited N times."),
                ("Remove", self._del_step, "Delete the selected step."),
                ("↑", self._move_up, "Move the selected step earlier."),
                ("↓", self._move_down, "Move the selected step later."),
                ("Pattern…", self._pick_pattern,
                 "Set the selected step's DMD pattern file. Double-clicking "
                 "the Pattern cell does the same."),
                ("No pattern", self._clear_pattern,
                 "Leave the DMD showing whatever it already has for this "
                 "step.")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)

        # What is about to happen, in one line — a step list is not something
        # you can total up by eye once it is longer than a screen.
        self._lbl_summary = QLabel()
        self._lbl_summary.setWordWrap(True)
        self._lbl_summary.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(self._lbl_summary)
        root.addWidget(grp, 1)

        # ── running ──────────────────────────────────────────────────────────
        rgrp = QGroupBox("Run")
        rlay = QVBoxLayout(rgrp)
        rlay.setSpacing(4)

        self._btn_start = QPushButton("▶ Start routine")
        # solid_btn, not toggle_btn: Start is not a toggle, it is the panel's
        # primary action — and it is disabled for the whole of a run, which is
        # the case the solid style renders honestly.
        self._btn_start.setStyleSheet(style.solid_btn("routines"))
        self._btn_start.setToolTip(
            "Check the protocol, start recording if it is not already running, "
            "and run the steps.\nA recording this button started is stopped "
            "again when the routine ends; one you started yourself is left "
            "alone.")
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
                 "End the routine now. Motion stops and the light goes off; a "
                 "recording this panel started is stopped with it.")):
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
                      "Start opens the recording it needs; it is never "
                      "remembered as running.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        rlay.addWidget(note)
        root.addWidget(rgrp)

        self._txt_name.editingFinished.connect(self._emit)
        self._spn_cycles.valueChanged.connect(self._emit)
        self._cmb_save.currentIndexChanged.connect(self._emit)

    # ── the step list ────────────────────────────────────────────────────────
    # The table edits `self._r.steps` in place; these are the operations on the
    # list itself, which a table cell cannot express.
    def _reload_table(self) -> None:
        self._tbl.reload()
        self._refresh_summary()

    def _selected(self) -> int:
        return self._tbl.selected_row()

    def _add_step(self) -> None:
        self._r.steps.append(Step(label=f"step {len(self._r.steps) + 1}"))
        self._reload_table()
        self._tbl.select_row(len(self._r.steps) - 1)
        self._emit()

    def _dup_step(self) -> None:
        row = self._selected()
        if row < 0:
            return
        from dataclasses import replace
        self._r.steps.insert(row + 1, replace(self._r.steps[row]))
        self._reload_table()
        self._tbl.select_row(row + 1)
        self._emit()

    def _del_step(self) -> None:
        row = self._selected()
        if row < 0:
            return
        del self._r.steps[row]
        self._reload_table()
        self._tbl.select_row(min(row, len(self._r.steps) - 1))
        self._emit()

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(+1)

    def _move(self, delta: int) -> None:
        """Reorder one step. A protocol is an ordered thing, and until this
        existed the only way to reorder was to delete and retype."""
        row = self._selected()
        new = row + delta
        if row < 0 or not (0 <= new < len(self._r.steps)):
            return
        steps = self._r.steps
        steps[row], steps[new] = steps[new], steps[row]
        self._reload_table()
        self._tbl.select_row(new)
        self._emit()

    def _pick_pattern(self) -> None:
        self._pick_pattern_for(self._selected())

    def _pick_pattern_for(self, row: int) -> None:
        if not (0 <= row < len(self._r.steps)):
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

    def _clear_pattern(self) -> None:
        """Back to "whatever the DMD has". The file dialog cannot express this
        — cancelling it means "changed my mind", not "no pattern"."""
        row = self._selected()
        if row < 0 or not self._r.steps[row].pattern:
            return
        self._r.steps[row].pattern = ""
        self._reload_table()
        self._emit()

    def _refresh_summary(self) -> None:
        """One line: how much work this is, and whether any of it emits light."""
        r = self._r
        runs = r.total_steps()
        if not r.steps:
            self._lbl_summary.setText("No steps yet — add one.")
            return
        secs = sum(s.settle_s for s in r.steps) * max(1, r.cycles)
        secs += sum(s.length for s in r.steps if s.unit == "seconds") \
            * max(1, r.cycles)
        frames = sum(s.length for s in r.steps if s.unit == "frames") \
            * max(1, r.cycles)
        bits = [f"{runs} run(s): {len(r.steps)} step(s)"
                + (f" x {r.cycles} cycles" if r.cycles > 1 else "")]
        # Frames and seconds are never interconverted (that is the point of
        # `unit`), so they are reported side by side rather than as one total.
        bits.append("at least " + _clock(secs)
                    + (f" plus {frames:g} frames" if frames else ""))
        if any(s.x_um is not None or s.y_um is not None for s in r.steps):
            bits.append("moves the stage")
        lit = sum(1 for s in r.steps if s.project)
        if lit:
            # Coloured, not capitalised: it is the one line that says light
            # will be emitted, and shouting reads as decoration.
            bits.append(f"<span style='color:#d08770'>{lit} step(s) emit "
                        f"light</span>")
        self._lbl_summary.setText(" · ".join(bits))

    # ── run state ────────────────────────────────────────────────────────────
    def set_state(self, phase: str, text: str, row: int | None = None) -> None:
        """Called from the adapter's display tick. `row` is the step running."""
        self._set_phase(phase, text)
        if row != self._marked:
            self._tbl.mark_running(row)
            self._marked = row

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
        self._refresh_summary()
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> Routine:
        self._r.name = self._txt_name.text().strip() or "routine"
        self._r.cycles = self._spn_cycles.value()
        self._r.save_mode = self._cmb_save.currentData() or "single"
        return self._r


def _clock(seconds: float) -> str:
    """Seconds as the operator reads a duration. 124 s is not a duration."""
    if seconds < 90:
        return f"{seconds:g} s"
    m, sec = divmod(int(round(seconds)), 60)
    return f"{m}:{sec:02d} min"
