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
from typing import Callable

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QMenu, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
    QWidget,
)

from acqApp import style
from acqApp.devices.stage.fov_store import SavedFov
from acqApp.widgets import ElidedLabel, button_row, compact, spin
from acqApp.routines import pairs, templates
from acqApp.routines.engine import Phase
from acqApp.routines.estimate import estimate
from acqApp.routines.settings import (DEFAULT_REPEATS, KINDS, UNITS, Group,
                                      Routine, Step)
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
        self._routine = routine or Routine()
        # One pass, a file per recording run, held at each .dcimg roll: the
        # only shapes the rig uses, so none is offered and a template's own
        # values are not adopted.
        self._routine.cycles, self._routine.save_mode = 1, "per_repeat"
        self._routine.wait_for_camera = True
        self._loading = False
        self._painted: str | None = None      # last phase actually painted
        self._painted_text: str | None = None
        self._marked: int | None = None       # step row shown as running
        self._hz: float | None = None         # frame rate the estimate uses
        self._painted_pct: int | None = None
        self._painted_note: str | None = None
        # FOV -> the ROI set drawn for it, or None; set by whoever owns a DMD.
        self.draw_roi: Callable[[SavedFov], Path | None] | None = None
        self._build()
        self.refresh_templates()
        self._reload_table()
        self._set_phase(Phase.IDLE, "")

    # ── construction ─────────────────────────────────────────────────────────
    @staticmethod
    def _make_buttons(specs) -> list[QPushButton]:
        """One QPushButton per (text, slot, tip) spec, in order."""
        out = []
        for text, slot, tip in specs:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            out.append(b)
        return out

    def _build(self) -> None:
        """`self.view`, the always-shown page (the steps and Run), and this
        panel, the setup behind it (templates, name, repeat groups)."""
        self.view = QWidget()
        vroot = QVBoxLayout(self.view)
        vroot.setContentsMargins(4, 4, 4, 4)
        vroot.setSpacing(6)
        # Every protocol edit; locked while a routine runs.
        self._protocol = QWidget()
        lay = QVBoxLayout(self._protocol)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._setup = QWidget()
        setup = QVBoxLayout(self._setup)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.setSpacing(6)
        root.addWidget(self._setup)

        self._cmb_tpl = compact(QComboBox())
        self._cmb_tpl.setToolTip("Saved protocols. Loading one replaces the steps.")
        # Long template names must not set the panel's width.
        self._cmb_tpl.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._cmb_tpl.setMinimumContentsLength(16)
        self._txt_name = compact(QLineEdit(self._routine.name), chars=24)
        form = QFormLayout()
        form.setSpacing(4)
        form.addRow("Template:", button_row(
            self._cmb_tpl, *self._make_buttons((
                ("Load", self._on_load_template,
                 "Replace the steps with the selected template."),
                ("Save as…", self._on_save_template,
                 "Save the steps as a template, under a chosen name."),
                ("Delete", self._on_delete_template,
                 "Delete the selected template. The steps are untouched.")))))
        form.addRow("Name:", self._txt_name)
        setup.addLayout(form)

        self._tbl = StepTable(self._routine.steps)
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
        # ── what a new FOV step starts with ──────────────────────────────
        r = self._routine
        self._spn_capture = spin(0.01, 1e6, r.capture_length, decimals=2,
                                 step=10.0, track=False,
                                 tooltip="How long each FOV step records.")
        self._cmb_unit = compact(QComboBox())
        self._cmb_unit.addItems(UNITS)
        self._cmb_unit.setCurrentText(r.capture_unit)
        self._cmb_unit.setToolTip("Frames or seconds, never converted between "
                                  "them, so a capture means what it says.")
        self._spn_wait = spin(0.0, 1e5, r.wait_s, decimals=1, suffix=" s",
                              track=False,
                              tooltip="Unlit pause after each FOV step's "
                                      "capture.")
        self._general = (r.capture_length, r.capture_unit, r.wait_s)
        for w in (self._spn_capture, self._spn_wait):
            w.editingFinished.connect(self._on_general_changed)
        self._cmb_unit.currentTextChanged.connect(self._on_general_changed)
        lay.addLayout(button_row(QLabel("Capture:"), self._spn_capture,
                                 self._cmb_unit, None, QLabel("Wait after:"),
                                 self._spn_wait))
        lay.addWidget(self._tbl, 1)

        # Per-step actions live on the table's right-click menu.
        add, other, up, down, timeline = self._make_buttons((
            ("+ FOV step…", self._add_fov_step,
             "Pick a saved FOV: move there, show its ROI set, wait for the "
             "camera's trigger, capture, light off, wait."),
            ("Other ▾", lambda: None, "Add a Wait, Puff or Trigger step."),
            ("↑", self._move_up,
             "Move the selected step earlier. Dragging the row and "
             "Ctrl+Up do the same."),
            ("↓", self._move_down,
             "Move the selected step later. Dragging the row and "
             "Ctrl+Down do the same."),
            ("Timeline…", self._show_timeline,
             "See one cycle drawn to scale — repeat groups as a bracket, "
             "Record steps as separate bars (one per repeat, never merged).")))
        add.setStyleSheet(style.solid_btn("routines"))
        menu = QMenu(other)
        for kind in KINDS[1:]:
            menu.addAction(KIND_LABELS[kind],
                           lambda k=kind: self._add_step(k))
        other.setMenu(menu)
        btns = button_row(add, other, None, up, down, right=(timeline,))
        self._lbl_hint = QLabel("Right-click a step for more.")
        self._lbl_hint.setToolTip("Duplicate, Remove, Change FOV, ROI set, No "
                                  "light and Group selected.")
        self._lbl_hint.setStyleSheet("color:#9aa0a6; font-size: 9pt;")
        self._lbl_hint.setWordWrap(True)
        lay.addLayout(btns)
        lay.addWidget(self._lbl_hint)

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
            2, 999, DEFAULT_REPEATS,
            tooltip="How many times the selected steps repeat before the "
                    "routine moves on.")
        grow.addWidget(self._spn_g_repeats)
        self._btn_g_add = QPushButton("Group selected")
        self._btn_g_add.setEnabled(False)
        self._btn_g_add.setToolTip("Group the steps selected in the table "
                                   "to repeat × times.")
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
        gl.addLayout(button_row(btn_g_del))
        setup.addWidget(ggrp)

        self._lbl_summary = QLabel()
        self._lbl_summary.setWordWrap(True)
        self._lbl_summary.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(self._lbl_summary)
        vroot.addWidget(self._protocol, 1)

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
            "frame.\nThe recording and the camera stop when the routine "
            "ends or is aborted.\nA routine moves the stage and projects "
            "light on its own; it is never remembered as running.")
        self._btn_start.clicked.connect(self.start_requested)

        self._btn_pause = QPushButton("Pause")
        self._btn_resume = QPushButton("Resume")
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
                 "End the routine now. Motion stops, the light goes off, and "
                 "the recording stops with it.")):
            b.setToolTip(tip)
            b.clicked.connect(sig)
        rlay.addLayout(button_row(self._btn_start, self._btn_pause,
                                  self._btn_resume, self._btn_skip,
                                  right=(self._btn_abort,)))

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

        self._lbl_saved = ElidedLabel()
        self._lbl_saved.setStyleSheet("color:#9aa0a6; font-size:10px;")
        self._lbl_saved.hide()
        rlay.addWidget(self._lbl_saved)
        vroot.addWidget(rgrp)
        root.addStretch(1)

        self._txt_name.editingFinished.connect(self._emit)

    # ── step list (the table edits cells; these edit the list) ───────────
    def _reload_table(self) -> None:
        self._reload_groups()           # set_groups repaints the whole table
        self._refresh_summary()

    # ── repeat groups ────────────────────────────────────────────────────────
    def _reload_groups(self) -> None:
        self._lst_groups.clear()
        for g in self._routine.groups:
            self._lst_groups.addItem(
                f"steps {g.start + 1}-{g.end + 1} × {g.repeats}")
        self._tbl.set_groups(self._routine.groups)

    def _on_selection_changed(self) -> None:
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
        self._routine.groups.append(Group(start=start, end=end,
                                    repeats=self._spn_g_repeats.value()))
        self._reload_groups()
        self._emit()

    def _del_group(self) -> None:
        row = self._lst_groups.currentRow()
        if 0 <= row < len(self._routine.groups):
            del self._routine.groups[row]
            self._reload_groups()
            self._emit()

    def _edit_group_repeats(self, item) -> None:
        row = self._lst_groups.row(item)
        if not (0 <= row < len(self._routine.groups)):
            return
        g = self._routine.groups[row]
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

    def _add_step(self, kind: str) -> None:
        """A Wait lasts the general wait (1 s if that's none)."""
        r = self._routine
        step = Step(kind=kind)
        if kind == "wait":
            step.length, step.unit = r.wait_s or 1.0, "seconds"
        r.steps.append(step)
        self._list_edited(len(r.steps) - 1)

    def _add_fov_step(self) -> None:
        """Pick a saved FOV -> a new FOV step with the general capture/wait
        and the FOV's ROI set (drawn now if it has none)."""
        r = self._routine
        r.steps.append(self._new_fov_step())
        row = len(r.steps) - 1
        if not self._pick_fov_for(row):
            del r.steps[row]

    def add_fov_step(self, fov: SavedFov, roi: Path) -> int:
        """A new FOV step for a saved FOV and its ROI set -> its row."""
        step = self._new_fov_step()
        self._fill_from_fov(step, fov)
        step.pattern = str(roi)
        self._routine.steps.append(step)
        row = len(self._routine.steps) - 1
        self._list_edited(row)
        return row

    def _new_fov_step(self) -> Step:
        r = self._routine
        return Step(kind="fov", length=r.capture_length, unit=r.capture_unit,
                    wait_s=r.wait_s, repeats=DEFAULT_REPEATS, trigger=True)

    def _on_general_changed(self, *_a) -> None:
        """Steps still on the old general value follow; hand-edited ones keep
        theirs. A unit change moves every FOV step: one unit per routine."""
        r = self._routine
        old_len, old_unit, old_wait = self._general
        r.capture_length = self._spn_capture.value()
        r.capture_unit = self._cmb_unit.currentText()
        r.wait_s = self._spn_wait.value()
        if r.capture_unit == "frames":
            r.capture_length = round(r.capture_length)
        new = (r.capture_length, r.capture_unit, r.wait_s)
        if new == self._general:
            return
        self._general = new
        for s in r.steps:
            if s.kind != "fov":
                continue
            if s.length == old_len and s.unit == old_unit:
                s.length = r.capture_length
            s.unit = r.capture_unit
            if s.unit == "frames":
                s.length = round(s.length)
            if s.wait_s == old_wait:
                s.wait_s = r.wait_s
        self._reload_table()
        self._emit()

    def _dup_step(self) -> None:
        row = self._selected()
        if row >= 0:
            self._insert_step(row + 1, replace(self._routine.steps[row]), owner=row)
            self._list_edited(row + 1)

    def _del_step(self) -> None:
        row = self._selected()
        if row >= 0:
            self._remove_step(row)
            self._list_edited(min(row, len(self._routine.steps) - 1))

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

        TimelineDialog(self._routine, self._hz, self).exec()

    def _pick_pattern(self) -> None:
        self._pick_pattern_for(self._selected())

    def _pick_pattern_for(self, row: int) -> None:
        if not (0 <= row < len(self._routine.steps)):
            return
        cur = self._routine.steps[row].pattern
        start = str(Path(cur).parent) if cur else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pattern for this step", start,
            "Images (*.png *.bmp *.tif);;All files (*)")
        if path:                    # empty = cancelled, which must not clear it
            self._routine.steps[row].pattern = path
            self._reload_table()
            self._emit()

    def _pick_roi(self) -> None:
        self._pick_roi_for(self._selected())

    def _pick_roi_for(self, row: int) -> None:
        if not (0 <= row < len(self._routine.steps)):
            return
        from acqApp.devices.dmd.roi_picker import RoiSetPicker

        dlg = RoiSetPicker(self)
        if dlg.exec() and dlg.path is not None:
            self._routine.steps[row].pattern = str(dlg.path)
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
        if not (0 <= row < len(self._routine.steps)):
            return
        step = self._routine.steps[row]
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

    def _pick_fov_for(self, row: int) -> bool:
        """Pick a saved FOV for step `row`; an FOV step takes its ROI set too.
        -> whether one was picked."""
        if not (0 <= row < len(self._routine.steps)):
            return False
        from acqApp.devices.stage.fov_picker import FovPicker

        dlg = FovPicker(self)
        dlg.exec()
        fov = dlg.fov
        if fov is None:
            return False
        step = self._routine.steps[row]
        self._fill_from_fov(step, fov)
        if step.kind == "fov":
            roi = pairs.roi_for_fov(fov) or self._draw_roi(fov)
            step.pattern = str(roi) if roi is not None else ""
            if roi is None:
                self.status_message.emit(
                    f'FOV "{fov.name}" has no ROI set: step {row + 1} '
                    f"captures without light")
        self._list_edited(row)
        return True

    def _draw_roi(self, fov: SavedFov) -> Path | None:
        """Offer to draw `fov`'s ROI set now, on its snapshot."""
        if self.draw_roi is None:
            return None
        ask = QMessageBox.question(
            self, "No ROI set",
            f'FOV "{fov.name}" has no ROI set yet. Draw one on its snapshot '
            f"now?\n(No: this step captures without light.)")
        if ask != QMessageBox.StandardButton.Yes:
            return None
        return self.draw_roi(fov)

    @staticmethod
    def _fill_from_fov(step: Step, fov: SavedFov) -> None:
        step.x_um, step.y_um, step.z_um = fov.x_um, fov.y_um, fov.z_um
        step.fov = fov.name

    def _insert_step(self, at: int, step: Step, owner: int) -> None:
        """Insert `step` at `at`, belonging with step `owner` (pre-insert
        index): groups holding `owner` take it in, later groups shift."""
        self._routine.steps.insert(at, step)
        for g in self._routine.groups:
            if g.start <= owner <= g.end:
                g.end += 1
            elif g.start >= at:
                g.start += 1
                g.end += 1

    def _remove_step(self, row: int) -> None:
        """Delete step `row`: groups holding it shrink (an emptied one goes),
        later groups shift, so each still repeats the steps it did."""
        del self._routine.steps[row]
        for g in self._routine.groups:
            if g.start > row:
                g.start -= 1
            if g.end >= row:
                g.end -= 1
        self._routine.groups[:] = [g for g in self._routine.groups if g.start <= g.end]

    def _clear_pattern(self) -> None:
        """Back to no light (cancelling the file dialog doesn't)."""
        row = self._selected()
        if row >= 0 and self._routine.steps[row].pattern:
            self._tbl.clear_cell(row, "roi")

    def _refresh_summary(self) -> None:
        """One line: how long this is, and whether any of it emits light."""
        r = self._routine
        self._lbl_hint.setVisible(bool(r.steps))
        if not r.steps:
            self._lbl_summary.setText("No steps yet: save an FOV + ROIs on "
                                      "Stage, or use + FOV step.")
            return
        est = estimate(r, self._hz)
        bits = [f"{r.total_steps()} run(s): {len(r.steps)} step(s)"
                + (f" x {r.cycles} cycles" if r.cycles > 1 else "")
                + (f", {len(r.groups)} repeat group(s)" if r.groups else "")]
        bits.append(est.text() + (f" (at {est.hz:g} Hz)" if est.hz and any(
            x.unit == "frames" and x.kind in ("fov", "wait", "record")
            for x in r.steps) else ""))
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
            # Every edit, not just the table's: templates, ±Step, groups.
            self._protocol.setEnabled(not held)
            self._setup.setEnabled(not held)
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
        with QSignalBlocker(self._cmb_tpl):
            self._cmb_tpl.clear()
            self._cmb_tpl.addItems(names)
            if select in names:
                self._cmb_tpl.setCurrentIndex(names.index(select))
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
        try:
            templates.delete(name)
        except OSError as e:
            self.status_message.emit(f"could not delete {name!r}: {e}")
            return
        self.refresh_templates()
        self.status_message.emit(f"template {name!r} deleted")

    def set_routine(self, r: Routine) -> None:
        """Adopt `r` in place: the table holds `self._routine.steps` itself, so
        rebinding the list would orphan it."""
        self._loading = True
        try:
            self._routine.name = r.name
            self._routine.steps[:] = r.steps
            self._routine.groups[:] = r.groups
            self._txt_name.setText(self._routine.name)
            rt = self._routine
            rt.capture_length, rt.capture_unit, rt.wait_s = self._general = (
                r.capture_length, r.capture_unit, r.wait_s)
            with QSignalBlocker(self._cmb_unit):
                self._cmb_unit.setCurrentText(r.capture_unit)
            self._spn_capture.setValue(r.capture_length)
            self._spn_wait.setValue(r.wait_s)
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
        self._routine.name = self._txt_name.text().strip() or "routine"
        return self._routine

