"""Visual stim — the settings panel.

Groups guiVisStimDAQ.m's four panels (Parameters, Loop Variables, Actions,
Hardware Status) into acqApp-style QFormLayout sections rather than the .m
code's flat "click a listbox row, edit one text box" UI — each parameter gets
its own labeled spinbox, matching every other module's panel (see
devices/dmd/panel.py). Loop variables stay a QListWidget + Add/Delete, since
that part of the .m UI is inherently dynamic. Group boxes are made
collapsible centrally by dialogs.py (widgets.collapsible_groups), same as
every other panel — this file does not call it itself.

No DAQ channel/status here: unlike the .m code, blank/stim gating rides the
shared session clock's own tick (control.py's `on_tick`), not a hardware line
this module reads itself — so there is no per-module connection state to show.

The Trial type combo picks the paradigm for the whole run; entries not yet
built (settings.IMPLEMENTED_TRIAL_TYPES) are listed but disabled, so the
roadmap is visible without being selectable.
"""
from __future__ import annotations

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from acqApp import style
from acqApp.acq.sync import DEFAULT_TICK_MS
from .settings import (IMPLEMENTED_TRIAL_TYPES, TRIAL_CONTRAST, TRIAL_GRATING,
                       TRIAL_MAP, TRIAL_SIZE, TRIAL_TUNING, TRIAL_TYPES,
                       TRIAL_VISUOMOTOR, LoopVar, StimParams, VisStimSettings,
                       parse_values)

_TRIAL_TYPE_LABELS = {
    TRIAL_GRATING:    "Grating (drifting)",
    TRIAL_MAP:        "Map (region flash)",
    TRIAL_TUNING:     "Tuning",
    TRIAL_CONTRAST:   "Contrast",
    TRIAL_SIZE:       "Size",
    TRIAL_VISUOMOTOR: "Visuomotor",
}

# The shared session clock ticks at DEFAULT_TICK_MS (main.py constructs its
# one SyncController with it) — StimParams still stores these as raw tick
# counts (control.py's on_tick counts them directly, and the field names
# match the original MATLAB code for config parity), but the operator
# shouldn't have to think in ticks, so the panel shows/edits them as seconds.
# `_TICK_FIELDS` names which fields get that conversion; MapRepeats/
# TuningRepeats/ContrastRepeats are sweep counts, not durations, so they're
# left alone.
_TICK_HZ = 1000.0 / DEFAULT_TICK_MS
_TICK_FIELDS = frozenset({
    "WaitTrigger", "TriggersBlank", "TriggersStim",
    "MapTicksPerRegion", "MapTicksPerFlip",
    "TuningTicksPerPretrial", "TuningTicksPerOrientation",
    "ContrastTicksPerPretrial", "ContrastTicksPerLevel",
    "SizeTicksPerPretrial", "SizeTicksPerLevel",
    "VisuomotorDurationTicks",
})

# (field, label, min, max, step, decimals) — min/max/step are in ticks for
# any field in _TICK_FIELDS; _field_group converts them to seconds.
_GEOMETRY_FIELDS = [
    ("StimDiameter",  "Diameter (px)",     0, 20000, 10, 0),
    ("StimXPosition", "X position (px)", -10000, 10000, 5, 0),
    ("StimYPosition", "Y position (px)", -10000, 10000, 5, 0),
    ("Orientation",   "Orientation (deg)", -3600, 3600, 1, 1),
]
_GRATING_FIELDS = [
    ("WaveSpPeriod",       "Spatial period (px)", 0.1, 5000, 1, 2),
    ("WaveTempPeriodInHz", "Temporal freq (Hz)",  0, 200, 0.1, 3),
    ("Contrast",           "Contrast",             0, 1, 0.01, 3),
    ("Phase",               "Phase (px)",          -5000, 5000, 1, 2),
    ("Mean",                 "Mean luminance",       0, 1, 0.01, 3),
    ("BKGColor",             "Background level",     0, 1, 0.01, 3),
    ("PeriodsToShow",        "Periods to show",      0, 1_000_000, 1, 0),
]
_TRIGGER_FIELDS = [
    ("WaitTrigger",   "Prime wait",     0, 100000, 1, 0),
    ("TriggersBlank", "Blank duration", 0, 100000, 1, 0),
    ("TriggersStim",  "Stim duration",  0, 100000, 1, 0),
]
# Only meaningful when Trial type = Map (see regions.py / control.py).
_MAP_FIELDS = [
    ("MapTicksPerRegion", "Region duration",       1, 100000, 1, 0),
    ("MapTicksPerFlip",   "Flip duration",         1, 100000, 1, 0),
    ("MapRepeats",        "Repeats (full passes)", 1, 1000, 1, 0),
]
# Only meaningful when Trial type = Tuning (see tuning.py / control.py).
_TUNING_FIELDS = [
    ("TuningRegion",             "Region (1-9)",          1, 9, 1, 0),
    ("TuningTicksPerPretrial",   "Pretrial duration",     1, 100000, 1, 0),
    ("TuningTicksPerOrientation", "Orientation duration", 1, 100000, 1, 0),
    ("TuningRepeats",            "Repeats (full sweeps)", 1, 1000, 1, 0),
]
# Only meaningful when Trial type = Contrast (see contrast.py / control.py).
_CONTRAST_FIELDS = [
    ("ContrastRegion",           "Region (1-9)",          1, 9, 1, 0),
    ("ContrastTicksPerPretrial", "Pretrial duration",     1, 100000, 1, 0),
    ("ContrastTicksPerLevel",    "Level duration",        1, 100000, 1, 0),
    ("ContrastRepeats",          "Repeats (full sweeps)", 1, 1000, 1, 0),
]
# Only meaningful when Trial type = Size (see size.py / control.py). Sweeps
# fractions of the region's own width (size.SIZE_FRACTIONS), not a field.
_SIZE_FIELDS = [
    ("SizeRegion",           "Region (1-9)",          1, 9, 1, 0),
    ("SizeTicksPerPretrial", "Pretrial duration",     1, 100000, 1, 0),
    ("SizeTicksPerLevel",    "Size step duration",    1, 100000, 1, 0),
    ("SizeRepeats",          "Repeats (full sweeps)", 1, 1000, 1, 0),
]
# Only meaningful when Trial type = Visuomotor (see control.py's
# _begin_visuomotor_trial/_visuomotor_frame). Everything else the grating
# needs (geometry, spatial period, contrast, ...) is shared with Grating —
# only the drift source and trial length differ.
_VISUOMOTOR_FIELDS = [
    ("VisuomotorGain", "Gain (px drift / wheel unit)", -100, 100, 0.1, 3),
    ("VisuomotorDurationTicks", "Trial duration", 1, 100000, 1, 0),
]


class SettingsPanel(QWidget):
    settings_changed    = pyqtSignal(object)   # emits VisStimSettings
    run_requested        = pyqtSignal()
    stop_requested        = pyqtSignal()

    def __init__(self, settings: VisStimSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or VisStimSettings()
        self._spins: dict[str, QDoubleSpinBox] = {}
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._trial_type_group())
        self._grp_geometry = self._field_group("Stimulus geometry",
                                                _GEOMETRY_FIELDS)
        root.addWidget(self._grp_geometry)
        self._grp_grating = self._field_group("Grating & timing", _GRATING_FIELDS)
        root.addWidget(self._grp_grating)
        self._grp_trigger = self._field_group(
            "Trial timing (shared clock ticks)", _TRIGGER_FIELDS)
        root.addWidget(self._grp_trigger)
        self._grp_map = self._field_group("Map trial", _MAP_FIELDS)
        root.addWidget(self._grp_map)
        self._grp_tuning = self._field_group("Tuning trial", _TUNING_FIELDS)
        root.addWidget(self._grp_tuning)
        self._grp_contrast = self._field_group("Contrast trial",
                                               _CONTRAST_FIELDS)
        root.addWidget(self._grp_contrast)
        self._grp_size = self._field_group("Size trial", _SIZE_FIELDS)
        root.addWidget(self._grp_size)
        self._grp_visuomotor = self._field_group("Visuomotor trial",
                                                  _VISUOMOTOR_FIELDS)
        root.addWidget(self._grp_visuomotor)
        root.addWidget(self._loop_group())
        root.addWidget(self._display_group())
        root.addWidget(self._run_group())
        root.addStretch()
        self._update_group_visibility()

    # ── trial type ────────────────────────────────────────────────────────
    def _trial_type_group(self) -> QGroupBox:
        grp = QGroupBox("Trial type")
        lay = QFormLayout(grp)
        lay.setSpacing(4)
        self._cmb_trial = QComboBox()
        for t in TRIAL_TYPES:
            label = _TRIAL_TYPE_LABELS.get(t, t.title())
            implemented = t in IMPLEMENTED_TRIAL_TYPES
            if not implemented:
                label += " (coming soon)"
            self._cmb_trial.addItem(label, t)
            if not implemented:
                item = self._cmb_trial.model().item(self._cmb_trial.count() - 1)
                item.setEnabled(False)
        idx = self._cmb_trial.findData(self._s.trial_type)
        self._cmb_trial.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_trial.currentIndexChanged.connect(self._emit)
        self._cmb_trial.currentIndexChanged.connect(self._update_group_visibility)
        lay.addRow("Type:", self._cmb_trial)
        return grp

    def _update_group_visibility(self, *_a) -> None:
        """Show only what control.py actually reads for the selected Trial
        type. Map/Tuning/Contrast/Size each build their own trial in
        control.py's `_begin_map_trial`/`_begin_tuning_trial`/
        `_begin_contrast_trial`/`_begin_size_trial`; Grating and Visuomotor
        both fall through to the plain-grating code path instead
        (`_begin_grating_trial`/`_begin_visuomotor_trial`), so grating
        fields stay live for Visuomotor too — only its own duration/gain
        fields and the drift source differ (`_visuomotor_frame`).

        Within "Stimulus geometry", Map/Tuning/Size override Diameter/X/Y
        entirely (region-derived geometry) so none of it applies; Contrast
        and Size both leave Orientation live (it still rotates the grating
        drawn inside the circle) while Tuning/Map don't — see control.py's
        `_begin_*_trial` methods."""
        t = self._cmb_trial.currentData()
        region_like = t in (TRIAL_MAP, TRIAL_TUNING, TRIAL_CONTRAST, TRIAL_SIZE)
        grating_like = not region_like
        self._grp_map.setVisible(t == TRIAL_MAP)
        self._grp_tuning.setVisible(t == TRIAL_TUNING)
        self._grp_contrast.setVisible(t == TRIAL_CONTRAST)
        self._grp_size.setVisible(t == TRIAL_SIZE)
        self._grp_visuomotor.setVisible(t == TRIAL_VISUOMOTOR)

        self._grp_grating.setVisible(grating_like)
        # WaveTempPeriodInHz/PeriodsToShow drive the fixed-frequency drift
        # _begin_grating_trial uses; Visuomotor drives drift from the wheel
        # instead (_visuomotor_frame), so neither applies there.
        grating_lay = self._grp_grating.layout()
        for name in ("WaveTempPeriodInHz", "PeriodsToShow"):
            grating_lay.setRowVisible(self._spins[name], t != TRIAL_VISUOMOTOR)

        trig_lay = self._grp_trigger.layout()
        for name in ("TriggersBlank", "TriggersStim"):
            trig_lay.setRowVisible(self._spins[name], grating_like)

        geo_lay = self._grp_geometry.layout()
        show_orientation = grating_like or t in (TRIAL_CONTRAST, TRIAL_SIZE)
        for name in ("StimDiameter", "StimXPosition", "StimYPosition"):
            geo_lay.setRowVisible(self._spins[name], grating_like)
        geo_lay.setRowVisible(self._spins["Orientation"], show_orientation)
        self._grp_geometry.setVisible(grating_like or show_orientation)

    def _field_group(self, title: str, fields) -> QGroupBox:
        grp = QGroupBox(title)
        lay = QFormLayout(grp)
        lay.setSpacing(4)
        for name, label, lo, hi, step, dec in fields:
            spin = QDoubleSpinBox()
            if name in _TICK_FIELDS:
                spin.setRange(lo / _TICK_HZ, hi / _TICK_HZ)
                spin.setSingleStep(max(step / _TICK_HZ, 0.1 / _TICK_HZ))
                spin.setDecimals(max(dec, 2))
                spin.setSuffix(" s")
                spin.setValue(getattr(self._s.params, name) / _TICK_HZ)
            else:
                spin.setRange(lo, hi)
                spin.setSingleStep(step)
                spin.setDecimals(dec)
                spin.setValue(getattr(self._s.params, name))
            spin.valueChanged.connect(self._emit)
            self._spins[name] = spin
            lay.addRow(f"{label}:", spin)
        return grp

    # ── loop variables ───────────────────────────────────────────────────
    def _loop_group(self) -> QGroupBox:
        grp = QGroupBox("Loop variables")
        lay = QVBoxLayout(grp)
        self._lst_loops = QListWidget()
        self._lst_loops.setMaximumHeight(90)
        self._lst_loops.currentRowChanged.connect(self._select_loop)
        lay.addWidget(self._lst_loops)

        form = QFormLayout()
        self._edt_loop_name = QLineEdit()
        self._edt_loop_name.setPlaceholderText("e.g. Orientation")
        self._edt_loop_vals = QLineEdit()
        self._edt_loop_vals.setPlaceholderText("0,45,90,135  or  0:45:315")
        form.addRow("Field name:", self._edt_loop_name)
        form.addRow("Values:", self._edt_loop_vals)
        lay.addLayout(form)

        row = QHBoxLayout()
        btn_add = QPushButton("Add / update")
        btn_add.clicked.connect(self._add_loop)
        btn_del = QPushButton("Delete")
        btn_del.clicked.connect(self._delete_loop)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        lay.addLayout(row)
        self._refresh_loops()
        return grp

    def _refresh_loops(self) -> None:
        self._lst_loops.clear()
        for name, lv in self._s.loops.items():
            vals = ", ".join(f"{v:g}" for v in lv.values)
            self._lst_loops.addItem(f"{name} = [{vals}]")

    def _select_loop(self, row: int) -> None:
        names = list(self._s.loops)
        if 0 <= row < len(names):
            name = names[row]
            self._edt_loop_name.setText(name)
            self._edt_loop_vals.setText(
                ", ".join(f"{v:g}" for v in self._s.loops[name].values))

    def _add_loop(self) -> None:
        name = self._edt_loop_name.text().strip()
        if not name:
            return
        if name not in StimParams.__dataclass_fields__:
            QMessageBox.warning(self, "Unknown field",
                                f"'{name}' is not a stimulus parameter.")
            return
        vals = parse_values(self._edt_loop_vals.text())
        if not vals:
            QMessageBox.warning(self, "Invalid values",
                                "Enter a comma/space-separated list, or a "
                                "start:step:stop range.")
            return
        self._s.loops[name] = LoopVar(name, vals)
        self._refresh_loops()
        self._emit()

    def _delete_loop(self) -> None:
        row = self._lst_loops.currentRow()
        names = list(self._s.loops)
        if 0 <= row < len(names):
            del self._s.loops[names[row]]
            self._refresh_loops()
            self._emit()

    # ── display ───────────────────────────────────────────────────────────
    def _display_group(self) -> QGroupBox:
        grp = QGroupBox("Display")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_screen = QComboBox()
        self._refresh_screens()
        self._cmb_screen.currentIndexChanged.connect(self._emit)
        lay.addRow("Show on:", self._cmb_screen)

        self._btn_identify = QPushButton("Identify displays")
        self._btn_identify.setToolTip(
            "Briefly show each display's number/name on that monitor, so "
            "you can match it to a \"Show on:\" entry above.")
        self._btn_identify.clicked.connect(self._identify_displays)
        lay.addRow(self._btn_identify)

        self._chk_stretch = QCheckBox("Stretch to screen")
        self._chk_stretch.setChecked(self._s.stretch_to_screen)
        self._chk_stretch.toggled.connect(self._emit)
        lay.addRow(self._chk_stretch)
        return grp

    def _refresh_screens(self) -> None:
        self._cmb_screen.blockSignals(True)
        self._cmb_screen.clear()
        for i, scr in enumerate(QGuiApplication.screens()):
            g = scr.geometry()
            self._cmb_screen.addItem(
                f"{i}: {scr.name()} ({g.width()}x{g.height()})")
        if 0 <= self._s.screen_index < self._cmb_screen.count():
            self._cmb_screen.setCurrentIndex(self._s.screen_index)
        self._cmb_screen.blockSignals(False)

    def _identify_displays(self) -> None:
        """One borderless window per connected screen, each showing that
        screen's index/name — the same index/name shown in "Show on:" — so
        the operator can match a combo entry to a physical monitor without
        trial-and-error. Self-closes after a few seconds."""
        self._identify_windows: list[QWidget] = []
        for i, scr in enumerate(QGuiApplication.screens()):
            win = QWidget(None, Qt.WindowType.FramelessWindowHint
                              | Qt.WindowType.WindowStaysOnTopHint)
            win.setStyleSheet("background-color: black;")
            win.setGeometry(scr.geometry())
            lbl = QLabel(f"{i}\n{scr.name()}", win)
            lbl.setStyleSheet(
                "color: white; font-size: 96px; font-weight: bold;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            QVBoxLayout(win).addWidget(lbl)
            win.show()
            self._identify_windows.append(win)
        QTimer.singleShot(3000, self._close_identify_windows)

    def _close_identify_windows(self) -> None:
        for win in getattr(self, "_identify_windows", []):
            win.close()
            win.deleteLater()
        self._identify_windows = []

    # ── run ───────────────────────────────────────────────────────────────
    def _run_group(self) -> QGroupBox:
        grp = QGroupBox("Run")
        lay = QVBoxLayout(grp)
        self._lbl_progress = QLabel("Progress: 0%")
        lay.addWidget(self._lbl_progress)
        self._btn_run = QPushButton("RUN STIMULUS")
        self._btn_run.setStyleSheet(style.solid_btn("vis_stim"))
        self._btn_run.clicked.connect(self._on_run_clicked)
        lay.addWidget(self._btn_run)
        return grp

    def _on_run_clicked(self) -> None:
        if self._btn_run.text() == "RUN STIMULUS":
            self.run_requested.emit()
        else:
            self.stop_requested.emit()

    # ── driven by the adapter/controller ────────────────────────────────
    def set_progress(self, text: str) -> None:
        self._lbl_progress.setText(text)

    def set_run_state(self, state: str) -> None:
        if state == "IDLE":
            self._btn_run.setText("RUN STIMULUS")
            self._btn_run.setStyleSheet(style.solid_btn("vis_stim"))
        elif state == "PRIMING":
            self._btn_run.setText("STOP (WAITING...)")
            self._btn_run.setStyleSheet(style.solid_btn("puffer"))
        else:
            self._btn_run.setText("STOP (RUNNING...)")
            self._btn_run.setStyleSheet(style.solid_btn("puffer"))

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> VisStimSettings:
        p = StimParams(**{
            name: (round(spin.value() * _TICK_HZ) if name in _TICK_FIELDS
                  else spin.value())
            for name, spin in self._spins.items()})
        return VisStimSettings(
            trial_type=self._cmb_trial.currentData() or TRIAL_GRATING,
            screen_index=max(0, self._cmb_screen.currentIndex()),
            stretch_to_screen=self._chk_stretch.isChecked(),
            params=p,
            loops=dict(self._s.loops),
        )
