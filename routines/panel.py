"""Experiment routines — the protocol editor and run controls. Edits a
`Routine` and emits it; decides nothing.

- Start opens the recording it needs; refusals list every problem.
- Running is never persisted: restoring it would drive the stage at launch.
- Templates are files (`routines/templates.py`).
- Estimates are floors: nothing times a stage move.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.widgets import ElidedLabel, compact, spin
from acqApp.routines import templates
from acqApp.routines.engine import Phase
from acqApp.routines.estimate import estimate
from acqApp.routines.settings import KINDS, SAVE_MODES, Group, Routine, Step
from acqApp.routines.table import KIND_LABELS, NO_CHANGE, StepTable


class SettingsPanel(QWidget):
    """The routine, plus Start / Pause / Resume / Skip / Abort."""

    settings_changed = pyqtSignal(object)      # emits Routine (persisted)
    status_message   = pyqtSignal(str)         # one line for the status bar
    state_shown      = pyqtSignal(str, str)    # (phase, text), on change
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

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Template:"))
        self._cmb_tpl = compact(QComboBox())
        self._cmb_tpl.setToolTip("Saved protocols. Loading one replaces step list below.")
        trow.addWidget(self._cmb_tpl)
        self._add_buttons(trow, (
            ("Load", self._on_load_template,
             "Replace protocol below with selected template."),
            ("Save as…", self._on_save_template,
             "Save protocol below as template, under chosen name."),
            ("Delete", self._on_delete_template,
             "Delete selected template. Protocol below is untouched.")))
        trow.addStretch()
        lay.addLayout(trow)

        form = QFormLayout()
        form.setSpacing(4)
        self._txt_name = compact(QLineEdit(self._r.name), chars=24)
        form.addRow("Name:", self._txt_name)

        self._spn_cycles = spin(
            1, 9999, max(1, self._r.cycles),
            tooltip="How many times the whole step list runs.")
        form.addRow("Repeat the list:", self._spn_cycles)

        self._cmb_save = compact(QComboBox())
        for key, label in SAVE_MODES.items():
            self._cmb_save.addItem(label, key)
        idx = self._cmb_save.findData(self._r.save_mode)
        self._cmb_save.setCurrentIndex(max(0, idx))
        form.addRow("Save as:", self._cmb_save)

        self._chk_wait_cam = QCheckBox("Hold at a file roll until frames resume")
        self._chk_wait_cam.setChecked(self._r.wait_for_camera)
        self._chk_wait_cam.setToolTip(
            "Only bites when ORCA format is DCIMG and the save mode rolls "
            "files.\nDCAM rebinds its recorder to a STOPPED camera, so a roll "
            "costs about\n0.9 s with no frames. On: the routine waits it out "
            "and restarts the\nstep's clock, so a 5 s step records 5 s of "
            "frames and the trial just\ntakes longer. Off: the step counts "
            "down through the gap and the\ntrial comes up short.")
        form.addRow("", self._chk_wait_cam)
        # Long item text must not set the panel's width.
        for cmb in (self._cmb_save, self._cmb_tpl):
            cmb.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cmb.setMinimumContentsLength(16)
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
        self._tbl.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._tbl, 1)

        # Per-step actions live on the table's right-click menu.
        btns = QHBoxLayout()
        btns.addWidget(QLabel("+ Step:"))
        self._cmb_new_kind = compact(QComboBox())
        for kind in KINDS:
            self._cmb_new_kind.addItem(KIND_LABELS[kind], kind)
        self._cmb_new_kind.setCurrentIndex(KINDS.index("wait"))
        self._cmb_new_kind.setToolTip(
            "What kind of step + Step appends. Follows the selected row, so "
            "adding several of the same kind in a row doesn't need "
            "reselecting it each time — still yours to override.")
        btns.addWidget(self._cmb_new_kind)
        self._add_buttons(btns, (
            ("+ Step", self._add_step, "Append a step of the chosen kind."),
            ("+ Trigger→Record", self._add_trigger_record_pair,
             "Append a Trigger step followed by a Record step — the "
             "\"one recording per edge\" pattern, built in the order it has "
             "to run in rather than by hand."),
            ("↑", self._move_up,
             "Move the selected step earlier. Dragging the row and "
             "Ctrl+Up do the same."),
            ("↓", self._move_down,
             "Move the selected step later. Dragging the row and "
             "Ctrl+Down do the same."),
            ("Timeline…", self._show_timeline,
             "See one cycle drawn to scale — repeat groups as a bracket, "
             "Record steps as separate bars (one per repeat, never merged).")))
        btns.addStretch(1)
        hint = QLabel("Right-click a step for Duplicate, Remove, Pattern, "
                      "ROI set, Position, and Group selected. Add a Record "
                      "step to record.")
        hint.setStyleSheet("color:#9aa0a6; font-size: 9pt;")
        hint.setWordWrap(True)
        lay.addLayout(btns)
        lay.addWidget(hint)

        # ── repeat groups (a selected range, nested inside cycles) ───────
        ggrp = QGroupBox("Repeat groups")
        gl = QVBoxLayout(ggrp)
        gl.setSpacing(4)

        grow = QHBoxLayout()
        self._lbl_g_selection = QLabel("Select 2+ steps in the table to group them")
        self._lbl_g_selection.setStyleSheet("color:#9aa0a6;")
        self._lbl_g_selection.setWordWrap(True)
        grow.addWidget(self._lbl_g_selection, 1)
        grow.addWidget(QLabel("×"))
        self._spn_g_repeats = spin(
            2, 999, 2, tooltip="How many times the selected steps repeat "
                               "before the routine moves on.")
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
        self._btn_start.setStyleSheet(style.solid_btn("routines"))
        self._btn_start.setToolTip(
            "Check the protocol, put the camera in External edge mode, start "
            "recording if it is not already running, and run. With Trigger "
            "steps, each one waits for its own edge, from trial 1; without, "
            "the routine is ARMED and step 1 begins on the first triggered "
            "frame.\nA recording this button started is stopped again when "
            "the routine ends; one you started yourself is left alone.")
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

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)          # tenths of a percent
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

        self._lbl_saved = ElidedLabel()
        self._lbl_saved.setStyleSheet("color:#9aa0a6; font-size:10px;")
        self._lbl_saved.hide()
        rlay.addWidget(self._lbl_saved)
        root.addWidget(rgrp)

        self._txt_name.editingFinished.connect(self._emit)
        self._spn_cycles.valueChanged.connect(self._emit)
        self._cmb_save.currentIndexChanged.connect(self._emit)
        self._chk_wait_cam.toggled.connect(self._emit)

    # ── step list (the table edits cells; these edit the list) ───────────
    def _reload_table(self) -> None:
        self._reload_groups()           # set_groups repaints the whole table
        self._refresh_summary()

    # ── repeat groups ────────────────────────────────────────────────────────
    def _reload_groups(self) -> None:
        self._lst_groups.clear()
        for g in self._r.groups:
            self._lst_groups.addItem(
                f"steps {g.start + 1}-{g.end + 1} × {g.repeats}")
        self._tbl.set_groups(self._r.groups)

    def _on_selection_changed(self) -> None:
        span = self._tbl.selected_range()
        selected = f"Steps {span[0] + 1}-{span[1] + 1} selected" if span else None
        self._btn_g_add.setEnabled(span is not None)
        self._lbl_g_selection.setText(
            selected or "Select 2+ steps in the table to group them")
        self._sync_new_kind_to_selection()

    def _sync_new_kind_to_selection(self) -> None:
        """"+ Step" defaults to the selected row's kind."""
        row = self._tbl.selected_row()
        if row < 0 or row >= len(self._r.steps):
            return
        i = self._cmb_new_kind.findData(self._r.steps[row].kind)
        if i >= 0:
            self._cmb_new_kind.setCurrentIndex(i)

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

    def _edit_group_repeats(self, item) -> None:
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

    def _list_edited(self, select: int) -> None:
        self._reload_table()
        self._tbl.select_row(select)
        self._emit()

    def _add_step(self) -> None:
        kind = self._cmb_new_kind.currentData() or "wait"
        self._r.steps.append(Step(kind=kind))
        self._list_edited(len(self._r.steps) - 1)

    def _add_trigger_record_pair(self) -> None:
        """[trigger, record 2 s]: one recording per edge, in the right order."""
        self._r.steps.append(Step(kind="trigger"))
        self._r.steps.append(Step(kind="record", length=2.0, unit="seconds"))
        self._list_edited(len(self._r.steps) - 1)

    def _dup_step(self) -> None:
        row = self._selected()
        if row >= 0:
            self._r.steps.insert(row + 1, replace(self._r.steps[row]))
            self._list_edited(row + 1)

    def _del_step(self) -> None:
        row = self._selected()
        if row >= 0:
            del self._r.steps[row]
            self._list_edited(min(row, len(self._r.steps) - 1))

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(+1)

    def _move(self, delta: int) -> None:
        row = self._selected()
        if row >= 0:
            self._tbl.move_row(row, row + delta)

    def _show_timeline(self) -> None:
        from acqApp.routines.timeline import TimelineDialog

        TimelineDialog(self._r, self._hz, self).exec()

    def _pick_pattern(self) -> None:
        self._pick_pattern_for(self._selected())

    def _pick_pattern_for(self, row: int) -> None:
        if not (0 <= row < len(self._r.steps)):
            return
        cur = self._r.steps[row].pattern
        start = str(Path(cur).parent) if cur else ""
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

    _POS_LO, _POS_HI, _POS_STEP = -1e5, 1e5, 100.0
    _POS_BLANK = _POS_LO - _POS_STEP      # "leave this axis alone" (NA)

    def _position_spin(self, value: float | None) -> QDoubleSpinBox:
        blank = self._POS_BLANK
        sb = spin(blank, self._POS_HI, blank if value is None else value,
                  decimals=0, step=self._POS_STEP, suffix=" um", track=False)
        sb.setSpecialValueText(NO_CHANGE)
        return sb

    def _set_position(self) -> None:
        self._set_position_for(self._selected())

    def _set_position_for(self, row: int) -> None:
        if not (0 <= row < len(self._r.steps)):
            return
        step = self._r.steps[row]
        dlg = QDialog(self)
        dlg.setWindowTitle("Stage position for this step")
        form = QFormLayout(dlg)
        spins = [self._position_spin(v)
                 for v in (step.x_um, step.y_um, step.z_um)]
        xyz = QHBoxLayout()           # X, Y, Z on one row
        for axis, sb in zip("XYZ", spins):
            if axis != "X":
                xyz.addWidget(QLabel(axis))
            xyz.addWidget(sb)
        xyz.addStretch()
        form.addRow("X:", xyz)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)
        if not dlg.exec():
            return

        step.x_um, step.y_um, step.z_um = (
            None if sb.value() <= self._POS_BLANK + 1e-9 else sb.value()
            for sb in spins)
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
        fov = dlg.fov
        if fov is not None:
            s = self._r.steps[row]
            s.x_um, s.y_um, s.z_um = fov.x_um, fov.y_um, fov.z_um
            s.fov = fov.name
            self._reload_table()
            self._emit()

    def _clear_pattern(self) -> None:
        """Back to "stop displaying" (cancelling the file dialog doesn't)."""
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
        bits.append(est.text() + (f" (at {est.hz:g} Hz)" if est.hz and
                                  any(x.unit == "frames" for x in r.steps)
                                  else ""))
        if est.moves:
            bits.append("moves the stage")
        if est.lit:
            bits.append(f"<span style='color:#d08770'>{est.lit} step(s) emit "
                        f"light</span>")
        self._lbl_summary.setText(" · ".join(bits))

    @property
    def frame_rate(self) -> float | None:
        return self._hz

    def set_frame_rate(self, hz: float | None) -> None:
        """For the estimate only."""
        hz = float(hz) if hz and hz > 0 else None
        if hz != self._hz:
            self._hz = hz
            self._refresh_summary()

    # ── run state ────────────────────────────────────────────────────────────
    def set_save_location(self, text: str) -> None:
        """Where the routine's files go (or the one being written); empty
        hides the line."""
        self._lbl_saved.set_full_text(text)
        self._lbl_saved.setVisible(bool(text))

    def set_state(self, phase: str, text: str, row: int | None = None) -> None:
        """Called from the adapter's display tick. `row` is the step running."""
        self._set_phase(phase, text)
        if row != self._marked:
            self._tbl.mark_running(row)
            self._marked = row

    def set_progress(self, fraction: float | None, note: str = "") -> None:
        """0..1 and a note; None hides both. Repaints only on change (30x/s)."""
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
        """Repaint only what changed: setStyleSheet costs ~26 us per call."""
        if phase != self._painted:
            paused = phase == Phase.PAUSED
            active = phase in (Phase.RUNNING, Phase.ARMED, Phase.WAITING)
            held = active or paused
            self._btn_start.setEnabled(not held)
            self._btn_pause.setEnabled(phase in (Phase.RUNNING,
                                                 Phase.WAITING))
            for b in (self._btn_resume, self._btn_skip):
                b.setEnabled(paused)
            self._btn_abort.setEnabled(held)
            # The engine holds an index into the step list.
            self._tbl.setEnabled(not held)
            self._lbl_state.setStyleSheet(
                "color:#d08770;" if paused else
                f"color:{style.HEX['routines']};" if active else "")
            self._painted = phase
        if text != self._painted_text:
            self._lbl_state.setText(text or "—")
            self._painted_text = text
            self.state_shown.emit(phase, text)

    def show_problems(self, problems: list[str]) -> None:
        """Why Start did nothing — every reason, not the first one."""
        self._lbl_state.setText("Cannot start:\n• " + "\n• ".join(problems))
        self._lbl_state.setStyleSheet("color:#d08770;")
        self._painted = self._painted_text = None   # force the next repaint

    # ── templates ────────────────────────────────────────────────────────
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
        """Replace the protocol being edited."""
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
        """Adopt `r` in place: the table holds `self._r.steps` itself, so
        rebinding the list would orphan it."""
        self._loading = True
        try:
            self._r.name, self._r.cycles = r.name, max(1, r.cycles)
            self._r.save_mode = r.save_mode
            self._r.wait_for_camera = r.wait_for_camera
            self._r.steps[:] = r.steps
            self._r.groups[:] = r.groups
            self._txt_name.setText(self._r.name)
            self._spn_cycles.setValue(self._r.cycles)
            self._cmb_save.setCurrentIndex(
                max(0, self._cmb_save.findData(self._r.save_mode)))
            self._chk_wait_cam.setChecked(self._r.wait_for_camera)
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
        self._r.wait_for_camera = self._chk_wait_cam.isChecked()
        return self._r

