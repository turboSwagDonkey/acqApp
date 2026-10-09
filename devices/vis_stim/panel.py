"""Visual stim — the settings panel.

One spinbox per parameter (guiVisStimDAQ.m's listbox + text box replaced).
No DAQ status: gating rides the shared clock tick, not a hardware line.
Trial types not in settings.IMPLEMENTED_TRIAL_TYPES are listed but disabled.
"""
from __future__ import annotations

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from acqApp import style
from acqApp.widgets import (button_row, check, compact, pairs_grid,
                            sections_help, show_item, spin)
from acqApp.acq.sync import DEFAULT_TICK_MS
from .settings import (IMPLEMENTED_TRIAL_TYPES, REGION_TRIAL_TYPES,
                       TRIAL_CONTRAST, TRIAL_GRATING, TRIAL_MAP, TRIAL_SIZE,
                       TRIAL_TUNING, TRIAL_TYPES, TRIAL_VISUOMOTOR, LoopVar,
                       StimParams, VisStimSettings, parse_values)

_TRIAL_TYPE_LABELS = {
    TRIAL_GRATING:    "Grating (drifting)",
    TRIAL_MAP:        "Map (region flash)",
    TRIAL_TUNING:     "Tuning",
    TRIAL_CONTRAST:   "Contrast",
    TRIAL_SIZE:       "Size",
    TRIAL_VISUOMOTOR: "Visuomotor",
}

# Stored as shared-clock tick counts, shown in seconds. Assumes main.py's
# SyncController runs at DEFAULT_TICK_MS.
_TICK_HZ = 1000.0 / DEFAULT_TICK_MS
_TICK_FIELDS = frozenset({
    "WaitTrigger", "TriggersBlank", "TriggersStim",
    "MapTicksPerRegion", "MapTicksPerFlip",
    "TuningTicksPerPretrial", "TuningTicksPerOrientation",
    "ContrastTicksPerPretrial", "ContrastTicksPerLevel",
    "SizeTicksPerPretrial", "SizeTicksPerLevel",
    "VisuomotorDurationTicks",
})

# (field, label, min, max, step, decimals); ticks for _TICK_FIELDS.
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
_MAP_FIELDS = [
    ("MapTicksPerRegion", "Region duration",       1, 100000, 1, 0),
    ("MapTicksPerFlip",   "Flip duration",         1, 100000, 1, 0),
    ("MapRepeats",        "Repeats (full passes)", 1, 1000, 1, 0),
]
_TUNING_FIELDS = [
    ("TuningRegion",             "Region (1-9)",          1, 9, 1, 0),
    ("TuningTicksPerPretrial",   "Pretrial duration",     1, 100000, 1, 0),
    ("TuningTicksPerOrientation", "Orientation duration", 1, 100000, 1, 0),
    ("TuningRepeats",            "Repeats (full sweeps)", 1, 1000, 1, 0),
]
_CONTRAST_FIELDS = [
    ("ContrastRegion",           "Region (1-9)",          1, 9, 1, 0),
    ("ContrastTicksPerPretrial", "Pretrial duration",     1, 100000, 1, 0),
    ("ContrastTicksPerLevel",    "Level duration",        1, 100000, 1, 0),
    ("ContrastRepeats",          "Repeats (full sweeps)", 1, 1000, 1, 0),
]
_SIZE_FIELDS = [
    ("SizeRegion",           "Region (1-9)",          1, 9, 1, 0),
    ("SizeTicksPerPretrial", "Pretrial duration",     1, 100000, 1, 0),
    ("SizeTicksPerLevel",    "Size step duration",    1, 100000, 1, 0),
    ("SizeRepeats",          "Repeats (full sweeps)", 1, 1000, 1, 0),
]
_VISUOMOTOR_FIELDS = [
    ("VisuomotorGain", "Gain (px drift / wheel unit)", -100, 100, 0.1, 3),
    ("VisuomotorDurationTicks", "Trial duration", 1, 100000, 1, 0),
]


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # VisStimSettings
    run_requested    = pyqtSignal()
    stop_requested   = pyqtSignal()

    def __init__(self, settings: VisStimSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or VisStimSettings()
        self._spins: dict[str, QDoubleSpinBox] = {}
        self._rows: dict[str, QWidget] = {}    # field -> its spin box
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Every group `_update_group_visibility` touches exists before the
        # trial combo is wired to it; visual order is the addWidget calls.
        self._grp_geometry = self._field_group("Stimulus geometry",
                                                _GEOMETRY_FIELDS)
        self._grp_grating = self._field_group("Grating & timing", _GRATING_FIELDS)
        self._grp_trigger = self._field_group(
            "Trial timing (shared clock ticks)", _TRIGGER_FIELDS)
        self._grp_map = self._field_group("Map trial", _MAP_FIELDS)
        self._grp_tuning = self._field_group("Tuning trial", _TUNING_FIELDS)
        self._grp_contrast = self._field_group("Contrast trial",
                                               _CONTRAST_FIELDS)
        self._grp_size = self._field_group("Size trial", _SIZE_FIELDS)
        self._grp_visuomotor = self._field_group("Visuomotor trial",
                                                  _VISUOMOTOR_FIELDS)
        self._grp_loops = self._loop_group()
        trial_type_group = self._trial_type_group()

        root.addWidget(trial_type_group)
        root.addWidget(self._grp_geometry)
        root.addWidget(self._grp_grating)
        root.addWidget(self._grp_trigger)
        root.addWidget(self._grp_map)
        root.addWidget(self._grp_tuning)
        root.addWidget(self._grp_contrast)
        root.addWidget(self._grp_size)
        root.addWidget(self._grp_visuomotor)
        root.addWidget(self._grp_loops)
        root.addWidget(self._display_group())
        root.addWidget(self._run_group())
        root.addStretch()
        sections_help(self, keep=True)
        self._update_group_visibility()

    # ── trial type ────────────────────────────────────────────────────────
    def _trial_type_group(self) -> QGroupBox:
        grp = QGroupBox("Trial type")
        lay = QFormLayout(grp)
        lay.setSpacing(4)
        self._cmb_trial = compact(QComboBox())
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
        self._cmb_trial.setToolTip("What the stimulus does; the boxes below "
                                   "show only what this type uses.")
        lay.addRow(pairs_grid(("Type:", self._cmb_trial)))
        return grp

    def _update_group_visibility(self, *_a) -> None:
        """Show only what control.py reads for the selected trial type.
        Region types derive Diameter/X/Y and skip loop variables; Contrast
        and Size still use Orientation. Visuomotor drifts from the wheel, so
        temporal frequency and periods don't apply."""
        t = self._cmb_trial.currentData()
        region_like = t in REGION_TRIAL_TYPES
        grating_like = not region_like
        self._grp_map.setVisible(t == TRIAL_MAP)
        self._grp_tuning.setVisible(t == TRIAL_TUNING)
        self._grp_contrast.setVisible(t == TRIAL_CONTRAST)
        self._grp_size.setVisible(t == TRIAL_SIZE)
        self._grp_visuomotor.setVisible(t == TRIAL_VISUOMOTOR)
        self._grp_loops.setVisible(grating_like)

        self._grp_grating.setVisible(grating_like)
        for name in ("WaveTempPeriodInHz", "PeriodsToShow"):
            show_item(self._rows[name], t != TRIAL_VISUOMOTOR)
        for name in ("TriggersBlank", "TriggersStim"):
            show_item(self._rows[name], grating_like)

        show_orientation = grating_like or t in (TRIAL_CONTRAST, TRIAL_SIZE)
        for name in ("StimDiameter", "StimXPosition", "StimYPosition"):
            show_item(self._rows[name], grating_like)
        show_item(self._rows["Orientation"], show_orientation)
        self._grp_geometry.setVisible(grating_like or show_orientation)

    def _field_group(self, title: str, fields) -> QGroupBox:
        """Two fields a line, in aligned columns like the other panels."""
        grp = QGroupBox(title)
        items = []
        for name, label, lo, hi, step, dec in fields:
            value = getattr(self._s.params, name)
            if name in _TICK_FIELDS:
                box = spin(lo / _TICK_HZ, hi / _TICK_HZ, value / _TICK_HZ,
                           decimals=max(dec, 2), suffix=" s",
                           step=max(step / _TICK_HZ, 0.1 / _TICK_HZ))
            else:
                box = spin(lo, hi, value, decimals=dec, step=step)
            box.valueChanged.connect(self._emit)
            self._spins[name] = self._rows[name] = box
            items += [f"{label}:", box]
        QVBoxLayout(grp).addLayout(pairs_grid(*(items[k:k + 4]
                                                for k in range(0, len(items), 4))))
        return grp

    # ── loop variables ───────────────────────────────────────────────────
    def _loop_group(self) -> QGroupBox:
        grp = QGroupBox("Loop variables")
        lay = QVBoxLayout(grp)
        self._lst_loops = QListWidget()
        self._lst_loops.setMaximumHeight(90)
        self._lst_loops.currentRowChanged.connect(self._select_loop)
        lay.addWidget(self._lst_loops)

        self._edt_loop_name = compact(QLineEdit(), chars=20)
        self._edt_loop_name.setPlaceholderText("e.g. Orientation")
        self._edt_loop_vals = compact(QLineEdit(), chars=28)
        self._edt_loop_vals.setPlaceholderText("0,45,90,135  or  0:45:315")
        self._edt_loop_name.setToolTip("A stimulus parameter to step through.")
        self._edt_loop_vals.setToolTip("The values it takes, one per trial: "
                                       "a list, or start:step:stop.")
        lay.addLayout(pairs_grid(("Field name:", self._edt_loop_name),
                                 ("Values:", self._edt_loop_vals)))

        btn_add = QPushButton("Add / update")
        btn_add.clicked.connect(self._add_loop)
        btn_del = QPushButton("Delete")
        btn_del.clicked.connect(self._delete_loop)
        btn_add.setToolTip("Add this loop variable, or update it if the "
                           "name is already listed.")
        btn_del.setToolTip("Remove the selected loop variable.")
        lay.addLayout(button_row(btn_add, btn_del))
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
        lay = QVBoxLayout(grp)

        self._cmb_screen = compact(QComboBox())
        self._refresh_screens()
        self._cmb_screen.currentIndexChanged.connect(self._emit)
        self._cmb_screen.setToolTip("The monitor the stimulus is drawn on.")

        self._btn_identify = QPushButton("Identify displays")
        self._btn_identify.setToolTip(
            "Briefly show each display's number/name on that monitor, so "
            "you can match it to a \"Show on:\" entry above.")
        self._btn_identify.clicked.connect(self._identify_displays)

        self._chk_stretch = check(
            "Stretch to screen",
            checked=self._s.stretch_to_screen,
            tip="Fill the whole screen with the stimulus field.")
        self._chk_stretch.toggled.connect(self._emit)
        lay.addLayout(button_row(QLabel("Show on:"), self._cmb_screen,
                                 self._btn_identify, None, self._chk_stretch))
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
        """Show each screen's "Show on:" index/name on it for 3 s."""
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
            name: (round(box.value() * _TICK_HZ) if name in _TICK_FIELDS
                  else box.value())
            for name, box in self._spins.items()})
        return VisStimSettings(
            trial_type=self._cmb_trial.currentData() or TRIAL_GRATING,
            screen_index=max(0, self._cmb_screen.currentIndex()),
            stretch_to_screen=self._chk_stretch.isChecked(),
            params=p,
            loops=dict(self._s.loops),
        )
