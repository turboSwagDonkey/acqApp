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
from PyQt6.QtCore import pyqtSignal

from acqApp import style
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

# (field, label, min, max, step, decimals)
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
# Counted in shared-clock ticks (10 Hz by default — acq/sync.py), not DAQ
# pulses: see control.py's on_tick.
_TRIGGER_FIELDS = [
    ("WaitTrigger",   "Wait ticks (prime)", 0, 100000, 1, 0),
    ("TriggersBlank", "Ticks per blank",    0, 100000, 1, 0),
    ("TriggersStim",  "Ticks per stim",     0, 100000, 1, 0),
]
# Shared by every region-grid trial type (map/tuning/contrast/size).
_REGION_FIELDS = [
    ("RegionIgnoredColumn", "Ignored column (0-3)", 0, 3, 1, 0),
]
# Only meaningful when Trial type = Map (see regions.py / control.py).
_MAP_FIELDS = [
    ("MapTicksPerRegion", "Ticks per region",       1, 100000, 1, 0),
    ("MapTicksPerFlip",   "Ticks per flip",         1, 100000, 1, 0),
    ("MapRepeats",        "Repeats (full passes)",  1, 1000, 1, 0),
]
# Only meaningful when Trial type = Tuning (see tuning.py / control.py).
_TUNING_FIELDS = [
    ("TuningRegion",             "Region (1-9)",         1, 9, 1, 0),
    ("TuningTicksPerPretrial",   "Ticks per pretrial",   1, 100000, 1, 0),
    ("TuningTicksPerOrientation", "Ticks per orientation", 1, 100000, 1, 0),
    ("TuningRepeats",            "Repeats (full sweeps)", 1, 1000, 1, 0),
]
# Only meaningful when Trial type = Contrast (see contrast.py / control.py).
_CONTRAST_FIELDS = [
    ("ContrastRegion",           "Region (1-9)",          1, 9, 1, 0),
    ("ContrastTicksPerPretrial", "Ticks per pretrial",    1, 100000, 1, 0),
    ("ContrastTicksPerLevel",    "Ticks per level",       1, 100000, 1, 0),
    ("ContrastRepeats",          "Repeats (full sweeps)", 1, 1000, 1, 0),
]
# Carried over from the MATLAB defaults for config parity — as in the current
# .m code, none of these are read by the renderer yet (see settings.py).
_LEGACY_FIELDS = [
    ("BarWidth",           "Bar width",            0, 5000, 1, 2),
    ("RotationPeriodInHz", "Rotation period (Hz)", -200, 200, 0.1, 3),
    ("FlashPeriodInHz",    "Flash period (Hz)",    -200, 200, 0.1, 3),
    ("LUTStart",           "LUT start",             0, 1_000_000, 1, 0),
    ("LUTEnd",              "LUT end",              0, 1_000_000, 1, 0),
    ("DoubleStim",           "Double stim",          0, 1, 1, 0),
    ("FlashType",            "Flash type",           0, 100, 1, 0),
    ("ModulationType",       "Modulation type",      0, 100, 1, 0),
    ("WaveType",             "Wave type",            0, 100, 1, 0),
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
        root.addWidget(self._field_group("Stimulus geometry", _GEOMETRY_FIELDS))
        root.addWidget(self._field_group("Grating & timing", _GRATING_FIELDS))
        root.addWidget(self._field_group("Trial timing (shared clock ticks)",
                                         _TRIGGER_FIELDS))
        root.addWidget(self._field_group(
            "Region grid — used by Map/Tuning/Contrast/Size", _REGION_FIELDS))
        root.addWidget(self._field_group(
            "Map trial — used when Trial type = Map", _MAP_FIELDS))
        root.addWidget(self._field_group(
            "Tuning trial — used when Trial type = Tuning", _TUNING_FIELDS))
        root.addWidget(self._field_group(
            "Contrast trial — used when Trial type = Contrast",
            _CONTRAST_FIELDS))
        root.addWidget(self._field_group("Flash / LUT (not yet rendered)",
                                         _LEGACY_FIELDS))
        root.addWidget(self._loop_group())
        root.addWidget(self._display_group())
        root.addWidget(self._run_group())
        root.addStretch()

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
        lay.addRow("Type:", self._cmb_trial)
        return grp

    def _field_group(self, title: str, fields) -> QGroupBox:
        grp = QGroupBox(title)
        lay = QFormLayout(grp)
        lay.setSpacing(4)
        for name, label, lo, hi, step, dec in fields:
            spin = QDoubleSpinBox()
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
        p = StimParams(**{name: spin.value()
                          for name, spin in self._spins.items()})
        return VisStimSettings(
            trial_type=self._cmb_trial.currentData() or TRIAL_GRATING,
            screen_index=max(0, self._cmb_screen.currentIndex()),
            stretch_to_screen=self._chk_stretch.isChecked(),
            params=p,
            loops=dict(self._s.loops),
        )
