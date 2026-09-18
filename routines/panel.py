"""Experiment routines — the protocol, the run controls, and one Start button.

The panel edits a `Routine` and emits it; decides nothing. Four rules it enforces

- **Start starts everything it needs.** Opens recording itself (through
  `ModuleHost.set_recording`, like DMD calibration opens live view
  via `set_live`) rather than refusing until operator has found
  Record button in another part of the window. 
- **Start is refused with the problems listed**, not greyed out with no reason.
  `validate()` returns sentences, and an operator who cannot start needs to
  read which step is wrong.
- **Arming is not persisted**, as in `closed_loop/`: the step list is saved,
  the fact that a routine is *running* never is. Restored "running" would
  drive the stage at launch.
- **Templates are files, not another key in the config.** `routines/templates.py`
  owns the folder; this owns the four buttons over it. The routine being edited
  persists — loading a template overwrites it, saving one copies it out.

Two readouts, because "is it working" and "how long is this" are different
questions: **progress bar** is whole routine, current step included,
and **summary line** is what protocol costs before it starts
(`routines/estimate.py`). Both are floors — nothing times a stage move.

The step *table* is `routines/table.py` — a step is one atomic action (Move /
Display / Wait / Puff) now, and every cell edits through a widget that can
only produce a legal value, which is why nothing parses "yes". Per-step
actions (Duplicate, Remove, Pattern/ROI/Clear pattern, Set position, Fill
from FOV) live on table's right-click menu, gated by row's kind,
rather than as buttons, panel keeps only +Step (with kind picker) 
and reordering visible, since those are used on every step.

A **repeat group** (`routines/settings.py`'s `Group`) comes from selecting a
range in table, not typing row numbers — `_on_selection_changed` mirrors
the table's selection into the "Repeat groups" control, and nests range
of steps inside `cycles`. A **recording** (`Recording`) is not a selection at
all, it is a sticker on step, toggled by clicking that step's row number
in table (`StepTable.recording_toggled`) — "the camera is capturing for
this step," independent of what step does and independent of Groups (a
recording may sit inside, outside, or straddling a group's edge freely). For
now a recording covers exactly the one step it's stuck to; it does not carry
over into step after. A step inside a repeated Group gets a fresh
recording file each time it repeats — `routines/engine.py` opens a new one
per `(cycle, serial)` automatically, nothing here has to ask again.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.routines import templates
from acqApp.routines.engine import Phase
from acqApp.routines.estimate import estimate
from acqApp.routines.settings import (KINDS, SAVE_MODES, START_TRIGGERS,
                                      Group, Recording, Routine, Step)
from acqApp.routines.table import KIND_LABELS, NO_CHANGE, StepTable


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
        self._hz: float | None = None         # frame rate the estimate uses
        self._painted_pct: int | None = None
        self._painted_note: str | None = None
        self._build()
        self.refresh_templates()
        self._reload_table()
        self._set_phase(Phase.IDLE, "")

    # ── construction ─────────────────────────────────────────────────────────
    @staticmethod
    def _add_buttons(layout, specs) -> None:
        """Add one QPushButton per (text, slot, tip) spec, in order."""
        for text, slot, tip in specs:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            layout.addWidget(b)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("Protocol")
        lay = QVBoxLayout(grp)
        lay.setSpacing(4)

        # A protocol worth running twice is worth keeping — a folder of
        # files copies to rig machine with repo.
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Template:"))
        self._cmb_tpl = QComboBox()
        self._cmb_tpl.setToolTip("Saved protocols. Loading one replaces step list below.")
        trow.addWidget(self._cmb_tpl, 1)
        self._add_buttons(trow, (
            ("Load", self._on_load_template,
             "Replace protocol below with selected template."),
            ("Save as…", self._on_save_template,
             "Save protocol below as template, under chosen name."),
            ("Delete", self._on_delete_template,
             "Delete selected template. Protocol below is untouched.")))
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

        self._cmb_trigger = QComboBox()
        for key, label in START_TRIGGERS.items():
            self._cmb_trigger.addItem(label, key)
        idx = self._cmb_trigger.findData(self._r.start_trigger)
        self._cmb_trigger.setCurrentIndex(max(0, idx))
        self._cmb_trigger.setToolTip(
            "Manual: Start begins the routine right away.\nTTL: Start puts "
            "the camera in External edge mode itself, opens the recording "
            "and arms the routine, then waits for a frame the camera did "
            "not have yet — which only happens on a real pulse.")
        form.addRow("Start trigger:", self._cmb_trigger)
        lay.addLayout(form)

        self._tbl = StepTable(self._r.steps)
        self._tbl.setMinimumHeight(160)
        self._tbl.changed.connect(self._emit)
        self._tbl.pattern_requested.connect(self._pick_pattern)
        self._tbl.roi_requested.connect(self._pick_roi)
        self._tbl.fov_requested.connect(self._pick_fov)
        self._tbl.position_requested.connect(self._set_position)
        self._tbl.duplicate_requested.connect(self._dup_step)
        self._tbl.remove_requested.connect(self._del_step)
        self._tbl.clear_pattern_requested.connect(self._clear_pattern)
        self._tbl.group_requested.connect(self._group_selected)
        self._tbl.recording_toggled.connect(self._toggle_recording)
        self._tbl.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._tbl, 1)

        # Add-step/reorder stay buttons (used constantly); everything else
        # (Duplicate/Remove/Pattern/ROI/Clear/FOV/Group/Mark as recording)
        # moved to table's right-click menu — three crowded button rows was
        # this panel's single biggest complaint.
        btns = QHBoxLayout()
        btns.addWidget(QLabel("+ Step:"))
        self._cmb_new_kind = QComboBox()
        for kind in KINDS:
            self._cmb_new_kind.addItem(KIND_LABELS[kind], kind)
        self._cmb_new_kind.setCurrentIndex(KINDS.index("wait"))
        self._cmb_new_kind.setToolTip("What kind of step + Step appends.")
        btns.addWidget(self._cmb_new_kind)
        self._add_buttons(btns, (
            ("+ Step", self._add_step, "Append a step of the chosen kind."),
            ("↑", self._move_up,
             "Move the selected step earlier. Dragging the row and "
             "Ctrl+Up do the same."),
            ("↓", self._move_down,
             "Move the selected step later. Dragging the row and "
             "Ctrl+Down do the same."),
            ("Timeline…", self._show_timeline,
             "See one cycle drawn to scale — repeat groups as a bracket, "
             "recordings as separate bars (one per repeat, never merged).")))
        btns.addStretch(1)
        hint = QLabel("Right-click a step for Duplicate, Remove, Pattern, "
                      "ROI set, Position, and Group selected. Click a step's "
                      "number to toggle recording for it.")
        hint.setStyleSheet("color:#9aa0a6; font-size: 9pt;")
        btns.addWidget(hint)
        lay.addLayout(btns)

        # ── repeat groups ────────────────────────────────────────────────
        # A contiguous range, repeated as unit, nested inside `cycles` (which
        # repeats WHOLE list). Select range in table instead of typing row
        # numbers — repeat count is only thing left to ask. Stays editable
        # (double-click group below), so "how many times" isn't
        # delete-and-regroup.
        ggrp = QGroupBox("Repeat groups")
        gl = QVBoxLayout(ggrp)
        gl.setSpacing(4)

        grow = QHBoxLayout()
        self._lbl_g_selection = QLabel("Select 2+ steps in the table to group them")
        self._lbl_g_selection.setStyleSheet("color:#9aa0a6;")
        grow.addWidget(self._lbl_g_selection, 1)
        grow.addWidget(QLabel("×"))
        self._spn_g_repeats = QSpinBox()
        self._spn_g_repeats.setRange(2, 999)
        self._spn_g_repeats.setValue(2)
        self._spn_g_repeats.setToolTip("How many times the selected steps "
                                       "repeat before the routine moves on.")
        grow.addWidget(self._spn_g_repeats)
        self._btn_g_add = QPushButton("Group selected")
        self._btn_g_add.setEnabled(False)
        self._btn_g_add.setToolTip("Group the steps selected in the table "
                                   "above to repeat × times, nested inside "
                                   "\"Repeat the list\" above.")
        self._btn_g_add.clicked.connect(self._group_selected)
        grow.addWidget(self._btn_g_add)
        gl.addLayout(grow)

        self._lst_groups = QListWidget()
        self._lst_groups.setMaximumHeight(70)
        self._lst_groups.setToolTip("Double-click a group to change its "
                                    "repeat count.")
        self._lst_groups.itemDoubleClicked.connect(self._edit_group_repeats)
        gl.addWidget(self._lst_groups)

        btn_g_del = QPushButton("Remove selected")
        btn_g_del.clicked.connect(self._del_group)
        gl.addWidget(btn_g_del)
        lay.addWidget(ggrp)

        # What's about to happen, in one line — a step list longer than a
        # screen can't be totalled up by eye.
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
        # solid_btn, not toggle_btn: Start is panel's primary action, not a
        # toggle — disabled for whole run, which solid style renders honestly.
        self._btn_start.setStyleSheet(style.solid_btn("routines"))
        self._btn_start.setToolTip(
            "Check the protocol, start recording if it is not already running, "
            "and run the steps.\nA recording this button started is stopped "
            "again when the routine ends; one you started yourself is left "
            "alone.\nWith a TTL start trigger, this ARMS the routine instead "
            "of moving right away — the steps begin on the camera's next "
            "externally-triggered frame.")
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

        # Where routine is, as a bar — a line of text is counted, not seen.
        # Running step is also marked in table, which answers "which step".
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
        self._cmb_trigger.currentIndexChanged.connect(self._emit)

    # ── step list ────────────────────────────────────────────────────────
    # Table edits `self._r.steps` in place; these are operations on list
    # itself, which a table cell can't express.
    def _reload_table(self) -> None:
        self._tbl.reload()
        self._reload_groups()
        self._reload_recordings()
        self._refresh_summary()

    # ── repeat groups ────────────────────────────────────────────────────────
    def _reload_groups(self) -> None:
        self._lst_groups.clear()
        for g in self._r.groups:
            self._lst_groups.addItem(
                f"steps {g.start + 1}-{g.end + 1} × {g.repeats}")
        self._tbl.set_groups(self._r.groups)

    def _on_selection_changed(self) -> None:
        """Repeat-groups control tracks table's selection rather than asking
        for row numbers again — a group needs 2+ contiguous rows, see
        `StepTable.selected_range`. Recording has no selection to track: it
        toggles straight off a header click."""
        span = self._tbl.selected_range()
        selected = f"Steps {span[0] + 1}-{span[1] + 1} selected" if span else None
        self._btn_g_add.setEnabled(span is not None)
        self._lbl_g_selection.setText(
            selected or "Select 2+ steps in the table to group them")

    def _group_selected(self) -> None:
        span = self._tbl.selected_range()
        if span is None:
            return
        start, end = span
        self._r.groups.append(Group(start=start, end=end,
                                    repeats=self._spn_g_repeats.value()))
        self._reload_groups()
        self._emit()

    def _del_group(self) -> None:
        row = self._lst_groups.currentRow()
        if 0 <= row < len(self._r.groups):
            del self._r.groups[row]
            self._reload_groups()
            self._emit()

    # ── recordings ───────────────────────────────────────────────────────────
    def _reload_recordings(self) -> None:
        self._tbl.set_recordings(self._r.recordings)

    def _toggle_recording(self, row: int) -> None:
        """Step's sticker: on if nothing covers `row`, off (removing whatever
        range does) otherwise. Always adds a one-step `Recording` — see module
        docstring for why recording is never a selection."""
        if not (0 <= row < len(self._r.steps)):
            return
        existing = next((r for r in self._r.recordings
                         if r.start <= row <= r.end), None)
        if existing is not None:
            self._r.recordings.remove(existing)
        else:
            self._r.recordings.append(Recording(start=row, end=row))
        self._reload_recordings()
        self._emit()

    def _edit_group_repeats(self, item) -> None:
        """Repeat count is only thing worth changing on an existing group
        without redoing selection — everything else means picking a
        different range and regrouping."""
        row = self._lst_groups.row(item)
        if not (0 <= row < len(self._r.groups)):
            return
        g = self._r.groups[row]
        n, ok = QInputDialog.getInt(
            self, "Repeat count",
            f"Steps {g.start + 1}-{g.end + 1} repeat:", g.repeats, 1, 999)
        if ok and n != g.repeats:
            g.repeats = n
            self._reload_groups()
            self._emit()

    def _selected(self) -> int:
        return self._tbl.selected_row()

    def _add_step(self) -> None:
        kind = self._cmb_new_kind.currentData() or "wait"
        self._r.steps.append(Step(kind=kind))
        self._reload_table()
        self._tbl.select_row(len(self._r.steps) - 1)
        self._emit()

    def _dup_step(self) -> None:
        row = self._selected()
        if row < 0:
            return
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
        """One step earlier or later. Table owns what reordering means —
        arrows, Ctrl+Up/Down, and a dropped row are same operation."""
        row = self._selected()
        if row >= 0:
            self._tbl.move_row(row, row + delta)

    def _show_timeline(self) -> None:
        """One cycle of routine being edited, drawn to scale — see
        `routines/timeline.py`. Reads live step/group/recording lists
        directly; nothing here editable, so nothing to sync back."""
        from acqApp.routines.timeline import TimelineDialog

        TimelineDialog(self._r, self._hz, self).exec()

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

    def _pick_roi(self) -> None:
        self._pick_roi_for(self._selected())

    def _pick_roi_for(self, row: int) -> None:
        if not (0 <= row < len(self._r.steps)):
            return
        from acqApp.devices.dmd.roi_picker import RoiSetPicker

        dlg = RoiSetPicker(self)
        if dlg.exec() and dlg.path is not None:
            self._r.steps[row].pattern = str(dlg.path)
            self._reload_table()
            self._emit()

    # Blank cell below lowest real position — same sentinel shape as
    # table.py's _NumberDelegate, so "NA" is a state spin boxes reach,
    # not a magic number.
    _POS_LO, _POS_HI, _POS_STEP = -1e5, 1e5, 100.0

    def _position_spin(self, value: float | None) -> QDoubleSpinBox:
        sb = QDoubleSpinBox()
        sb.setDecimals(0)
        sb.setSuffix(" um")
        sb.setRange(self._POS_LO - self._POS_STEP, self._POS_HI)
        sb.setSingleStep(self._POS_STEP)
        sb.setSpecialValueText(NO_CHANGE)
        sb.setKeyboardTracking(False)
        sb.setValue(self._POS_LO - self._POS_STEP if value is None else value)
        return sb

    def _set_position(self) -> None:
        self._set_position_for(self._selected())

    def _set_position_for(self, row: int) -> None:
        """Move step's Details is X and Y together (table.py's "details"),
        not two cells — typing a number takes small dialog instead of inline
        spin box, same way Display's pattern always has (file dialog, not
        typed cell)."""
        if not (0 <= row < len(self._r.steps)):
            return
        step = self._r.steps[row]
        dlg = QDialog(self)
        dlg.setWindowTitle("Stage position for this step")
        form = QFormLayout(dlg)
        x_spin = self._position_spin(step.x_um)
        y_spin = self._position_spin(step.y_um)
        z_spin = self._position_spin(step.z_um)
        form.addRow("X:", x_spin)
        form.addRow("Y:", y_spin)
        form.addRow("Z:", z_spin)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if not dlg.exec():
            return

        def val(sb: QDoubleSpinBox) -> float | None:
            blank = self._POS_LO - self._POS_STEP
            return None if sb.value() <= blank + 1e-9 else sb.value()

        step.x_um, step.y_um, step.z_um = val(x_spin), val(y_spin), val(z_spin)
        step.fov = ""            # typed — no longer necessarily a saved spot
        self._reload_table()
        self._emit()

    def _pick_fov(self) -> None:
        self._pick_fov_for(self._selected())

    def _pick_fov_for(self, row: int) -> None:
        if not (0 <= row < len(self._r.steps)):
            return
        from acqApp.devices.stage.fov_picker import FovPicker

        dlg = FovPicker(self)
        dlg.exec()
        if dlg.fov is not None:
            self._r.steps[row].x_um = dlg.fov.x_um
            self._r.steps[row].y_um = dlg.fov.y_um
            self._r.steps[row].z_um = dlg.fov.z_um
            self._r.steps[row].fov = dlg.fov.name
            self._reload_table()
            self._emit()

    def _clear_pattern(self) -> None:
        """Back to "stop displaying". File dialog can't express this —
        cancelling means "changed my mind", not "no pattern". Delete key on
        cell does same, through same call."""
        row = self._selected()
        if row >= 0 and self._r.steps[row].pattern:
            self._tbl.clear_cell(row, "details")

    def _refresh_summary(self) -> None:
        """One line: how long this is, and whether any of it emits light."""
        r = self._r
        if not r.steps:
            self._lbl_summary.setText("No steps yet — add one.")
            return
        est = estimate(r, self._hz)
        bits = [f"{r.total_steps()} run(s): {len(r.steps)} step(s)"
                + (f" x {r.cycles} cycles" if r.cycles > 1 else "")
                + (f", {len(r.groups)} repeat group(s)" if r.groups else "")]
        # "about", not a promise: nothing times a stage move, so every total
        # is a floor. Frames become seconds only once camera reports rate —
        # otherwise reported as frames, not guessed.
        bits.append(est.text() + (f" (at {est.hz:g} Hz)" if est.hz and
                                  any(x.unit == "frames" for x in r.steps)
                                  else ""))
        if est.moves:
            bits.append("moves the stage")
        if est.lit:
            # Coloured, not capitalised: the one line saying light will be
            # emitted — shouting reads as decoration.
            bits.append(f"<span style='color:#d08770'>{est.lit} step(s) emit "
                        f"light</span>")
        self._lbl_summary.setText(" · ".join(bits))

    @property
    def frame_rate(self) -> float | None:
        """The rate the estimate is using, or None if no camera has said."""
        return self._hz

    def set_frame_rate(self, hz: float | None) -> None:
        """Camera's rate, for estimate only — a step measured in frames is
        never converted where it's *recorded* (settings.py)."""
        hz = float(hz) if hz and hz > 0 else None
        if hz != self._hz:
            self._hz = hz
            self._refresh_summary()

    # ── run state ────────────────────────────────────────────────────────────
    def set_state(self, phase: str, text: str, row: int | None = None) -> None:
        """Called from the adapter's display tick. `row` is the step running."""
        self._set_phase(phase, text)
        if row != self._marked:
            self._tbl.mark_running(row)
            self._marked = row

    def set_progress(self, fraction: float | None, note: str = "") -> None:
        """Where routine is, 0..1, and one grey line under bar.

        `None` puts both away — before a run there's nothing to be part-way
        through, and empty bar reads as stalled. Repaints only on change, same
        reason as `_set_phase`: called 30x/s.
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

        Adapter calls this every display tick; `setStyleSheet` repolishes
        widget against window's whole cascade — measured at 26 us/call, 53% of
        shared 30 Hz tick with eight modules loaded, to reapply identical
        string. Tick was never in trouble (0.05 ms of 33 ms budget); this half
        was simply free to remove.
        """
        if phase != self._painted:
            running = phase == Phase.RUNNING
            armed = phase == Phase.ARMED
            waiting = phase == Phase.WAITING
            paused = phase == Phase.PAUSED
            held = running or armed or waiting or paused
            self._btn_start.setEnabled(not held)
            # Pause offered while WAITING too: a trigger step can sit there
            # for minutes, and operator must be able to take rig back without
            # waiting for an edge that may never come.
            self._btn_pause.setEnabled(running or waiting)
            for b in (self._btn_resume, self._btn_skip):
                b.setEnabled(paused)
            self._btn_abort.setEnabled(held)
            # Step list must not be edited out from under running engine: it
            # holds an index into it. Armed counts too — recording it opened
            # is already running, one TTL pulse from step 1.
            self._tbl.setEnabled(not held)
            self._lbl_state.setStyleSheet(
                "color:#d08770;" if paused else
                (f"color:{style.HEX['routines']};"
                 if (running or armed or waiting) else ""))
            self._painted = phase
        # Text moves within a phase (progress, step number); styling doesn't.
        if text != self._painted_text:
            self._lbl_state.setText(text or "—")
            self._painted_text = text

    def show_problems(self, problems: list[str]) -> None:
        """Why Start did nothing — every reason, not the first one."""
        self._lbl_state.setText("Cannot start:\n• " + "\n• ".join(problems))
        self._lbl_state.setStyleSheet("color:#d08770;")
        # Written out of band, so next _set_phase must repaint even if phase
        # hasn't moved.
        self._painted = self._painted_text = None

    # ── templates ────────────────────────────────────────────────────────
    # Four buttons are thin on purpose: folder, naming, reading back are
    # `routines/templates.py`, which has no Qt.
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
        """Replace protocol being edited. A template isn't a second live
        routine — there's one, and this is what it now says."""
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
        """Adopt whole routine, keeping step LIST table holds.

        Table was handed `self._r.steps` and writes into it, so list object
        must survive — rebinding it leaves table editing a routine nothing
        else can see.
        """
        self._loading = True
        try:
            self._r.name, self._r.cycles = r.name, max(1, r.cycles)
            self._r.save_mode = r.save_mode
            self._r.start_trigger = r.start_trigger
            self._r.steps[:] = r.steps
            self._r.groups[:] = r.groups
            self._r.recordings[:] = r.recordings
            self._txt_name.setText(self._r.name)
            self._spn_cycles.setValue(self._r.cycles)
            self._cmb_save.setCurrentIndex(
                max(0, self._cmb_save.findData(self._r.save_mode)))
            self._cmb_trigger.setCurrentIndex(
                max(0, self._cmb_trigger.findData(self._r.start_trigger)))
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
        self._r.start_trigger = self._cmb_trigger.currentData() or "manual"
        return self._r

