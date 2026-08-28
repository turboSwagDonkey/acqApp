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
- **Templates are files, not another key in the config.** `routines/templates.py`
  owns the folder; this owns the four buttons over it. The routine being edited
  is still the one that persists — loading a template overwrites it, saving one
  copies it out.

Two readouts, because "is it working" and "how long is this" are different
questions: the **progress bar** is the whole routine, current step included,
and the **summary line** is what the protocol costs before it starts
(`routines/estimate.py`). Both are floors — nothing times a stage move.

The step *table* is `routines/table.py` — every cell edits through a widget
that can only produce a legal value, which is why nothing here parses "yes".
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.routines import templates
from acqApp.routines.engine import Phase
from acqApp.routines.estimate import clock, estimate
from acqApp.routines.settings import SAVE_MODES, Routine, Step
from acqApp.routines.table import StepTable


class SettingsPanel(QWidget):
    """The routine, plus Start / Pause / Resume / Skip / Abort."""

    settings_changed = pyqtSignal(object)      # emits Routine (persisted)
    status_message   = pyqtSignal(str)         # one line for the status bar
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
        self._fps: float | None = None        # frame rate the estimate uses
        self._painted_pct: int | None = None
        self._painted_note: str | None = None
        self._build()
        self.refresh_templates()
        self._reload_table()
        self._set_phase(Phase.IDLE, "")

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("Protocol")
        lay = QVBoxLayout(grp)
        lay.setSpacing(4)

        # A protocol worth running twice is worth keeping. The library is a
        # folder of files, so it copies to the rig machine with the repo.
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Template:"))
        self._cmb_tpl = QComboBox()
        self._cmb_tpl.setToolTip("Saved protocols. Loading one replaces the "
                                 "step list below.")
        trow.addWidget(self._cmb_tpl, 1)
        for text, slot, tip in (
                ("Load", self._on_load_template,
                 "Replace the protocol below with the selected template."),
                ("Save as…", self._on_save_template,
                 "Save the protocol below as a template, under a name you "
                 "choose."),
                ("Delete", self._on_delete_template,
                 "Delete the selected template. The protocol below is not "
                 "touched.")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            trow.addWidget(b)
        lay.addLayout(trow)

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
                ("↑", self._move_up,
                 "Move the selected step earlier. Dragging the row and "
                 "Ctrl+Up do the same."),
                ("↓", self._move_down,
                 "Move the selected step later. Dragging the row and "
                 "Ctrl+Down do the same.")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)

        # A secondary row, not folded into the one above: both of these
        # duplicate an interaction already on the cell itself (their own
        # tooltips say so) — they read as pattern shortcuts, not list
        # operations, and crowded the primary row before this split.
        pat_btns = QHBoxLayout()
        for text, slot, tip in (
                ("Pattern…", self._pick_pattern,
                 "Set the selected step's DMD pattern file. Double-clicking "
                 "the Pattern cell does the same."),
                ("No pattern", self._clear_pattern,
                 "Leave the DMD showing whatever it already has for this "
                 "step. Delete on the cell does the same — as it does on a "
                 "Stage X or Y cell, which sets it back to \"no change\".")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            pat_btns.addWidget(b)
        pat_btns.addStretch(1)
        lay.addLayout(pat_btns)

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

        # Where the routine is, as a bar: a twelve-step protocol read off a
        # line of text is counted, not seen. The running step is also marked
        # in the table, which is where "which step" is actually answered.
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)          # tenths of a percent: 40 steps move it
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        self._bar.setToolTip("Progress through the whole routine, the step "
                             "now running included.")
        self._bar.hide()
        rlay.addWidget(self._bar)

        self._lbl_eta = QLabel()
        self._lbl_eta.setWordWrap(True)
        self._lbl_eta.setStyleSheet("color:#9aa0a6;")
        self._lbl_eta.hide()
        rlay.addWidget(self._lbl_eta)

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
        """One step earlier or later. The table owns what reordering means —
        the arrows, Ctrl+Up/Down and a dropped row are the same operation."""
        row = self._selected()
        if row >= 0:
            self._tbl.move_row(row, row + delta)

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
        — cancelling it means "changed my mind", not "no pattern". The Delete
        key on the cell does the same, through the same call."""
        row = self._selected()
        if row >= 0 and self._r.steps[row].pattern:
            self._tbl.clear_cell(row, "pattern")

    def _refresh_summary(self) -> None:
        """One line: how long this is, and whether any of it emits light."""
        r = self._r
        if not r.steps:
            self._lbl_summary.setText("No steps yet — add one.")
            return
        est = estimate(r, self._fps)
        bits = [f"{r.total_steps()} run(s): {len(r.steps)} step(s)"
                + (f" x {r.cycles} cycles" if r.cycles > 1 else "")]
        # "about", not a promise: nothing times a stage move, so every total
        # here is a floor. Frames become seconds only when a camera has told us
        # its rate — otherwise they are reported as frames rather than guessed.
        bits.append(est.text() + (f" (at {est.fps:g} fps)" if est.fps and
                                  any(x.unit == "frames" for x in r.steps)
                                  else ""))
        if est.moves:
            bits.append("moves the stage")
        if est.lit:
            # Coloured, not capitalised: it is the one line that says light
            # will be emitted, and shouting reads as decoration.
            bits.append(f"<span style='color:#d08770'>{est.lit} step(s) emit "
                        f"light</span>")
        self._lbl_summary.setText(" · ".join(bits))

    @property
    def frame_rate(self) -> float | None:
        """The rate the estimate is using, or None if no camera has said."""
        return self._fps

    def set_frame_rate(self, fps: float | None) -> None:
        """The camera's rate, for the estimate only — a step measured in frames
        is still never converted where it is *recorded* (settings.py)."""
        fps = float(fps) if fps and fps > 0 else None
        if fps != self._fps:
            self._fps = fps
            self._refresh_summary()

    # ── run state ────────────────────────────────────────────────────────────
    def set_state(self, phase: str, text: str, row: int | None = None) -> None:
        """Called from the adapter's display tick. `row` is the step running."""
        self._set_phase(phase, text)
        if row != self._marked:
            self._tbl.mark_running(row)
            self._marked = row

    def set_progress(self, fraction: float | None, note: str = "") -> None:
        """Where the routine is, 0..1, and one grey line under the bar.

        `None` puts both away — before a run there is nothing to be part-way
        through, and an empty bar reads as a stalled one. Repaints only on a
        change, for the same reason `_set_phase` does: this is called 30x/s.
        """
        if fraction is None:
            if self._painted_pct is not None:
                self._bar.hide()
                self._lbl_eta.hide()
                self._painted_pct = self._painted_note = None
            return
        pct = int(round(max(0.0, min(1.0, fraction)) * 1000))
        if pct != self._painted_pct:
            if self._painted_pct is None:
                self._bar.show()
                self._lbl_eta.show()
            self._bar.setValue(pct)
            self._painted_pct = pct
        if note != self._painted_note:
            self._lbl_eta.setText(note)
            self._painted_note = note

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

    # ── templates ────────────────────────────────────────────────────────────
    # The four buttons are thin on purpose: the folder, the naming and the
    # reading back are `routines/templates.py`, which has no Qt in it.
    def refresh_templates(self, select: str = "") -> None:
        names = templates.names()
        self._cmb_tpl.blockSignals(True)
        try:
            self._cmb_tpl.clear()
            self._cmb_tpl.addItems(names)
            if select in names:
                self._cmb_tpl.setCurrentIndex(names.index(select))
        finally:
            self._cmb_tpl.blockSignals(False)
        self._cmb_tpl.setEnabled(bool(names))
        if not names:
            self._cmb_tpl.setPlaceholderText("no saved templates")

    def _on_save_template(self) -> None:
        name, ok = QInputDialog.getText(self, "Save as template",
                                        "Template name:",
                                        text=self.settings.name)
        if ok and name.strip():
            self.save_template(name.strip())

    def save_template(self, name: str) -> None:
        try:
            path = templates.save(self.settings, name)
        except OSError as e:
            self.status_message.emit(f"could not save the template: {e}")
            return
        self.refresh_templates(templates.safe_name(name))
        self.status_message.emit(f"template saved as {path.name}")

    def _on_load_template(self) -> None:
        name = self._cmb_tpl.currentText()
        if name:
            self.load_template(name)

    def load_template(self, name: str) -> None:
        """Replace the protocol being edited. A template is not a second live
        routine — there is one, and this is what it now says."""
        try:
            loaded = templates.load(name)
        except (OSError, ValueError) as e:
            self.status_message.emit(f"could not load {name!r}: {e}")
            return
        self.set_routine(loaded)
        self.status_message.emit(
            f"loaded template {name!r} — {len(loaded.steps)} step(s)")

    def _on_delete_template(self) -> None:
        name = self._cmb_tpl.currentText()
        if not name:
            return
        templates.delete(name)
        self.refresh_templates()
        self.status_message.emit(f"template {name!r} deleted")

    def set_routine(self, r: Routine) -> None:
        """Adopt a whole routine, keeping the step LIST the table holds.

        The table was handed `self._r.steps` and writes into it, so the list
        object has to survive — rebinding it would leave the table editing a
        routine nothing else can see.
        """
        self._loading = True
        try:
            self._r.name, self._r.cycles = r.name, max(1, r.cycles)
            self._r.save_mode = r.save_mode
            self._r.steps[:] = r.steps
            self._txt_name.setText(self._r.name)
            self._spn_cycles.setValue(self._r.cycles)
            self._cmb_save.setCurrentIndex(
                max(0, self._cmb_save.findData(self._r.save_mode)))
        finally:
            self._loading = False
        self._reload_table()
        self._tbl.select_row(0)
        self._emit()

    # ── settings ─────────────────────────────────────────────────────────────
    def _emit(self, *_a) -> None:
        if self._loading:               # set_routine moves every widget at once
            return
        self._refresh_summary()
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> Routine:
        self._r.name = self._txt_name.text().strip() or "routine"
        self._r.cycles = self._spn_cycles.value()
        self._r.save_mode = self._cmb_save.currentData() or "single"
        return self._r

