"""Visual stim — the settings panel.

One spinbox per parameter (guiVisStimDAQ.m's listbox + text box replaced).
No DAQ status: gating rides the shared clock tick, not a hardware line.
Trial types not in settings.IMPLEMENTED_TRIAL_TYPES are listed but disabled.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QLabel, QLineEdit, QListWidget, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import QSignalBlocker, Qt, QTimer, pyqtSignal

from acqApp import style
from acqApp.widgets import (button_row, check, compact, pairs_grid,
                            sections_help, show_item, spin)
from acqApp.acq.sync import DEFAULT_TICK_MS
from .regions import IGNORED_COLUMN, N_REGIONS, N_ROWS
from .settings import (IMPLEMENTED_TRIAL_TYPES, REGION_TRIAL_TYPES,
                       SWEEP_TRIAL_TYPES, TRIAL_CONTRAST, TRIAL_GRATING,
                       TRIAL_MAP, TRIAL_SIZE, TRIAL_SPATIAL_FREQ, TRIAL_TUNING,
                       TRIAL_TYPES, TRIAL_VISUOMOTOR, LoopVar, StimParams,
                       VisStimSettings, parse_values)
from .sweep import N_ORIENTATIONS
from .window import reported_width_cm

_TRIAL_TYPE_LABELS = {
    TRIAL_GRATING:    "Grating (drifting)",
    TRIAL_MAP:        "Map (region flash)",
    TRIAL_TUNING:     "Orientation tuning",
    TRIAL_CONTRAST:   "Contrast tuning",
    TRIAL_SPATIAL_FREQ: "Spatial frequency",
    TRIAL_SIZE:       "Size tuning",
    TRIAL_VISUOMOTOR: "Visuomotor",
}

# Stored as shared-clock tick counts, shown in seconds. Assumes main.py's
# SyncController runs at DEFAULT_TICK_MS.
_TICK_HZ = 1000.0 / DEFAULT_TICK_MS
_TICK_FIELDS = frozenset({
    "WaitTrigger", "TriggersBlank", "TriggersStim",
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
    ("PeriodsToShow",        "Periods to show",      0, 1_000_000, 1, 0),
]
_TRIGGER_FIELDS = [
    ("WaitTrigger",   "Prime wait",     0, 100000, 1, 0),
    ("TriggersBlank", "Blank duration", 0, 100000, 1, 0),
    ("TriggersStim",  "Stim duration",  0, 100000, 1, 0),
]
# Already seconds, 0.1 s steps: whole ticks at DEFAULT_TICK_MS.
_MAP_FIELDS = [
    ("MapGreySeconds",  "Grey",     0, 10000, 0.1, 1),
    ("MapWhiteSeconds", "White",  0.1, 10000, 0.1, 1),
    ("MapBlackSeconds", "Black",    0, 10000, 0.1, 1),
    ("MapRepeats",      "Repeats",  1, 1000, 1, 0),
]
_TIPS = {
    "MapGreySeconds":
        "Grey on all 9 regions before the first flash and again after the "
        "last one, this long each (the right-hand column is always black). "
        "Part of the trial: the recording's trial marks bracket both.",
    "MapWhiteSeconds":
        "How long each region is lit white. Each region shows white once, "
        "then black, before the next region lights; the others stay grey.",
    "MapBlackSeconds":
        "Black that follows each white straight away, on the same region. "
        "0 = no black: the next region lights as soon as the white ends.",
    "MapRepeats":
        "Passes through all 9 regions, each in a new random order (saved "
        "as map_order in the session metadata). A pass never opens on the "
        "region the previous one closed on.",
}
# Shared by the tuning types; an optional 7th item is the suffix.
_SWEEP_FIELDS = [
    ("TuneOnSeconds",    "Stimulus", 0.1, 10000, 0.1, 1),
    ("TuneGapSeconds",   "Gap",        0, 10000, 0.1, 1),
    ("TuneGreySeconds",  "Grey",       0, 10000, 0.1, 1),
    ("TuneDriftHz",      "Drift",      0, 50, 0.5, 1, " Hz"),
    ("TuneCyclesPerDeg", "Spatial freq", 0.001, 10, 0.01, 3, " cyc/°"),
    ("TuneContrastPct",  "Contrast",   0, 100, 5, 0, " %"),
]
# Used by several trial types: the General box.
_GENERAL_FIELDS = [
    ("ScreenWidthCm",  "Screen",       0, 1000, 0.5, 1, " cm wide"),
    ("ViewDistanceCm", "Eye distance", 1, 1000, 0.5, 1, " cm"),
    ("BKGColor",       "Grey level",   0, 1, 0.01, 2),
]
# Per tuning type: its repeats field and the unit of its levels.
_SWEEP_REPEATS = {
    TRIAL_TUNING:       "TuningRepeats",
    TRIAL_CONTRAST:     "ContrastRepeats",
    TRIAL_SPATIAL_FREQ: "SpatialFreqRepeats",
    TRIAL_SIZE:         "SizeRepeats",
}
_LEVEL_UNITS = {TRIAL_CONTRAST: "%", TRIAL_SPATIAL_FREQ: "cyc/°",
                TRIAL_SIZE: "°"}
_TIPS.update({
    "TuneRegion":
        "Where the gratings go: the 1-9 grid square (as in the map trial) "
        "whose neurons responded best. The circle is that square's width "
        "(size tuning sets its own).",
    "TuneDriftHz": "Grating drift speed, cycles per second.",
    "TuneOnSeconds":
        "How long each grating is on. Each one is followed by the Gap.",
    "TuneGapSeconds":
        "Mean-grey screen after each grating, before the next one.",
    "TuneGreySeconds":
        "Mean-grey screen at the start and again at the end of the trial, "
        "this long each. The trial marks in the recording bracket both.",
    "TuneCyclesPerDeg":
        "Grating spatial frequency, cycles per degree of visual angle "
        "(the spatial frequency trial sweeps it instead).",
    "TuneContrastPct":
        "Grating contrast (the contrast trial sweeps it instead).",
    "BKGColor":
        "The grey every trial type rests on: backgrounds, gaps, and the "
        "map's unlit regions. In luminance, gamma-corrected: 0.5 is "
        "halfway between black and white, the gratings' own mean.",
    "ScreenWidthCm":
        "Visible width of the stimulus screen; with Eye distance, converts "
        "degrees to pixels. 0 = the size the monitor reports (shown here). "
        "Projectors, TVs and adapters can report it wrong: check it with a "
        "ruler once, and type the measured width if it's off.",
    "ViewDistanceCm":
        "Eye to screen, straight on. With Screen width, converts degrees "
        "to pixels. Measure on the rig.",
    **{field: "Passes through every condition (8 orientations × each level), "
              "each pass in a new random order (saved in the session "
              "metadata as <type>_order)."
       for field in _SWEEP_REPEATS.values()},
})
_VISUOMOTOR_FIELDS = [
    ("VisuomotorGain", "Gain (px drift / wheel unit)", -100, 100, 0.1, 3),
    ("VisuomotorDurationTicks", "Trial duration", 1, 100000, 1, 0),
]


class RegionPicker(QWidget):
    """The 1-9 regions as buttons laid out like the screen (column-major),
    with the always-black last column drawn; click one to choose it."""
    changed = pyqtSignal(int)

    def __init__(self, value: int = 1, parent=None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self._group = QButtonGroup(self)
        for i in range(N_REGIONS):
            col, row = divmod(i, N_ROWS)
            b = QPushButton(str(i + 1))
            b.setCheckable(True)
            b.setFixedSize(30, 22)
            b.setStyleSheet(style.toggle_btn("vis_stim")
                            + "QPushButton{padding:0}")
            self._group.addButton(b, i + 1)
            grid.addWidget(b, row, col)
        black = QLabel()
        black.setFixedSize(16, N_ROWS * 22 + (N_ROWS - 1) * 2)
        black.setStyleSheet(f"background:#000;border:1px solid {style.line()}")
        black.setToolTip("Always black, never used.")
        grid.addWidget(black, 0, IGNORED_COLUMN, N_ROWS, 1)
        self._group.idClicked.connect(self.changed)
        self.set_value(value)

    def set_value(self, value: int) -> None:
        b = self._group.button(max(1, min(N_REGIONS, int(value))))
        if b is not None:
            b.setChecked(True)

    def value(self) -> int:
        return self._group.checkedId()


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # VisStimSettings
    run_requested    = pyqtSignal()
    stop_requested   = pyqtSignal()

    def __init__(self, settings: VisStimSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or VisStimSettings()
        self._spins: dict[str, QDoubleSpinBox] = {}
        self._rows: dict[str, QWidget] = {}    # field -> its spin box
        self._file_dir = ""                    # last Save/Load folder
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Every group `_update_group_visibility` touches exists before the
        # trial combo is wired to it; visual order is the addWidget calls.
        self._grp_geometry = self._field_group("Stimulus geometry",
                                                _GEOMETRY_FIELDS)
        self._chk_stretch = check(
            "Stretch to screen", checked=self._s.stretch_to_screen,
            tip="Fill the whole screen with the stimulus field (overrides "
                "Diameter and position).")
        self._chk_stretch.toggled.connect(self._emit)
        self._grp_geometry.layout().addWidget(self._chk_stretch)
        self._grp_grating = self._field_group("Grating & timing", _GRATING_FIELDS)
        self._grp_trigger = self._field_group(
            "Trial timing (shared clock ticks)", _TRIGGER_FIELDS)
        self._grp_map = self._field_group("Map", _MAP_FIELDS)
        self._lbl_map_total = self._total_label(
            "2 × Grey + 9 regions × (White + Black) × Repeats. "
            "Times round to the 10 Hz clock (0.1 s steps).")
        self._grp_map.layout().addWidget(self._lbl_map_total)
        self._grp_sweep = self._sweep_group()
        self._grp_visuomotor = self._field_group("Visuomotor trial",
                                                  _VISUOMOTOR_FIELDS)
        self._grp_loops = self._loop_group()
        general_group = self._general_group()
        trial_type_group = self._trial_type_group()

        root.addWidget(trial_type_group)
        root.addWidget(general_group)
        root.addWidget(self._grp_geometry)
        root.addWidget(self._grp_grating)
        root.addWidget(self._grp_trigger)
        root.addWidget(self._grp_map)
        root.addWidget(self._grp_sweep)
        root.addWidget(self._grp_visuomotor)
        root.addWidget(self._grp_loops)
        root.addWidget(self._run_group())
        root.addStretch()
        sections_help(self, keep=True)
        self._update_group_visibility()
        self._update_totals()

    @staticmethod
    def _total_label(tip: str) -> QLabel:
        lbl = QLabel()
        lbl.setStyleSheet("font-weight: 600")
        lbl.setToolTip(tip)
        return lbl

    def _sweep_group(self) -> QGroupBox:
        """One box for the four tuning types. Region, Repeats and Levels edit
        the current type's value: unshown spins hold each type's own."""
        grp = QGroupBox("Tuning")
        lay = QVBoxLayout(grp)
        for name in ("TuneRegion", *_SWEEP_REPEATS.values()):
            box = spin(1, 1000, getattr(self._s.params, name), decimals=0)
            box.valueChanged.connect(self._emit)
            self._spins[name] = box             # never laid out
        self._region = RegionPicker(int(self._s.params.TuneRegion))
        self._region.setToolTip(_TIPS["TuneRegion"])
        self._region.changed.connect(self._spins["TuneRegion"].setValue)
        self._spins["TuneRegion"].valueChanged.connect(
            lambda v: self._region.set_value(int(v)))
        self._spn_repeats = spin(1, 1000, 1, decimals=0,
                                 tooltip=_TIPS["TuningRepeats"])
        self._spn_repeats.valueChanged.connect(self._set_repeats)
        lay.addLayout(self._field_grid(
            _SWEEP_FIELDS, before=[("Region:", self._region),
                                   ("Repeats:", self._spn_repeats)]))
        self._edt_levels = compact(QLineEdit(), chars=28)
        self._edt_levels.editingFinished.connect(self._set_levels)
        lay.addLayout(pairs_grid(("Levels:", self._edt_levels)))
        self._lbl_sweep_total = self._total_label(
            "2 × Grey + conditions × Repeats × (Stimulus + Gap). Before it, "
            "unrecorded, a pretrial shows each orientation once (Stimulus + "
            "Gap each). Times round to the 10 Hz clock (0.1 s steps).")
        lay.addWidget(self._lbl_sweep_total)
        return grp

    def _show_sweep_type(self, t: str) -> None:
        """Point Repeats and Levels at tuning type `t`."""
        with QSignalBlocker(self._spn_repeats):
            self._spn_repeats.setValue(self._spins[_SWEEP_REPEATS[t]].value())
        show_item(self._edt_levels, t in _LEVEL_UNITS)
        if t in _LEVEL_UNITS:
            unit = _LEVEL_UNITS[t]
            self._edt_levels._grid_label.setText(f"Levels ({unit}):")
            self._edt_levels.setText(
                ", ".join(f"{v:g}" for v in self._s.levels[t]))
            self._edt_levels.setToolTip(
                f"The values swept, in {unit}: a list, or start:step:stop. "
                f"Each is shown at all {N_ORIENTATIONS} orientations.")

    def _set_repeats(self, value: float) -> None:
        self._spins[_SWEEP_REPEATS[self._cmb_trial.currentData()]].setValue(value)

    def _set_levels(self) -> None:
        t = self._cmb_trial.currentData()
        if t not in _LEVEL_UNITS:
            return
        vals = parse_values(self._edt_levels.text())
        if vals and vals != self._s.levels[t]:
            self._s.levels[t] = vals
            self._emit()
        self._edt_levels.setText(", ".join(f"{v:g}" for v in self._s.levels[t]))

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
        btn_load = QPushButton("Load…")
        btn_load.setToolTip("Replace every setting below with a saved "
                            ".json file.")
        btn_load.clicked.connect(self._load_file)
        btn_save = QPushButton("Save…")
        btn_save.setToolTip("Save every setting below (type, parameters, "
                            "loops, display) to a .json file.")
        btn_save.clicked.connect(self._save_file)
        lay.addRow(button_row(pairs_grid(("Type:", self._cmb_trial)),
                              right=(btn_load, btn_save)))
        return grp

    # ── settings file ─────────────────────────────────────────────────────
    def _save_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save visual stim settings",
            str(Path(self._file_dir) / "vis_stim_settings.json"),
            "Settings (*.json)")
        if not path:
            return
        self._file_dir = str(Path(path).parent)
        try:
            Path(path).write_text(json.dumps(self.settings.to_dict(), indent=2),
                                  encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load visual stim settings", self._file_dir,
            "Settings (*.json);;All files (*)")
        if not path:
            return
        self._file_dir = str(Path(path).parent)
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        if not isinstance(d, dict):
            QMessageBox.warning(self, "Load failed",
                                "Not a visual stim settings file.")
            return
        self.set_settings(VisStimSettings.from_dict(d))

    def set_settings(self, s: VisStimSettings) -> None:
        """Show `s` in every widget, then emit it once."""
        self._s = s
        widgets = [self._cmb_trial, self._cmb_screen, self._chk_stretch,
                   *self._spins.values()]
        with ExitStack() as blocked:
            for w in widgets:
                blocked.enter_context(QSignalBlocker(w))
            idx = self._cmb_trial.findData(s.trial_type)
            self._cmb_trial.setCurrentIndex(idx if idx >= 0 else 0)
            for name, box in self._spins.items():
                v = getattr(s.params, name)
                box.setValue(v / _TICK_HZ if name in _TICK_FIELDS else v)
            self._chk_stretch.setChecked(s.stretch_to_screen)
            self._refresh_screens()
        self._region.set_value(int(s.params.TuneRegion))
        self._refresh_loops()
        self._update_group_visibility()
        self._emit()

    def _update_group_visibility(self, *_a) -> None:
        """Show only what control.py reads for the selected trial type.
        Region types place and time themselves and skip loop variables.
        Visuomotor drifts from the wheel, so temporal frequency and periods
        don't apply."""
        t = self._cmb_trial.currentData()
        grating_like = t not in REGION_TRIAL_TYPES
        self._grp_map.setVisible(t == TRIAL_MAP)
        # Region types lead in with their own grey/pretrial: no Prime wait.
        self._grp_trigger.setVisible(grating_like)
        self._grp_sweep.setVisible(t in SWEEP_TRIAL_TYPES)
        if t in SWEEP_TRIAL_TYPES:
            self._show_sweep_type(t)
        show_item(self._rows["TuneContrastPct"], t != TRIAL_CONTRAST)
        show_item(self._rows["TuneCyclesPerDeg"], t != TRIAL_SPATIAL_FREQ)
        self._grp_visuomotor.setVisible(t == TRIAL_VISUOMOTOR)
        self._grp_loops.setVisible(grating_like)
        self._grp_grating.setVisible(grating_like)
        self._grp_geometry.setVisible(grating_like)
        for name in ("WaveTempPeriodInHz", "PeriodsToShow"):
            show_item(self._rows[name], t != TRIAL_VISUOMOTOR)
        self._update_totals()

    def _field_group(self, title: str, fields) -> QGroupBox:
        grp = QGroupBox(title)
        QVBoxLayout(grp).addLayout(self._field_grid(fields))
        return grp

    def _field_grid(self, fields, before=()):
        """Two fields a line, in aligned columns like the other panels;
        `before` is (label, widget) pairs to lead with."""
        items = [x for pair in before for x in pair]
        for name, label, lo, hi, step, dec, *suffix in fields:
            value = getattr(self._s.params, name)
            if name in _TICK_FIELDS:
                box = spin(lo / _TICK_HZ, hi / _TICK_HZ, value / _TICK_HZ,
                           decimals=max(dec, 2), suffix=" s",
                           step=max(step / _TICK_HZ, 0.1 / _TICK_HZ))
            else:
                box = spin(lo, hi, value, decimals=dec, step=step,
                           suffix=suffix[0] if suffix else
                           " s" if name.endswith("Seconds") else "")
            box.setToolTip(_TIPS.get(name, ""))
            box.valueChanged.connect(self._emit)
            self._spins[name] = self._rows[name] = box
            items += [f"{label}:", box]
        return pairs_grid(*(items[k:k + 4] for k in range(0, len(items), 4)))

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

    # ── general: the display and what several trial types share ──────────
    def _general_group(self) -> QGroupBox:
        grp = QGroupBox("General")
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

        lay.addLayout(self._field_grid(_GENERAL_FIELDS, before=[
            ("Show on:", self._cmb_screen), (None, self._btn_identify)]))
        self._cmb_screen.currentIndexChanged.connect(self._show_reported_width)
        self._show_reported_width()
        return grp

    def _show_reported_width(self, *_a) -> None:
        """What a 0 Screen width means for the selected display."""
        screens = QGuiApplication.screens()
        i = self._cmb_screen.currentIndex()
        cm = reported_width_cm(screens[i]) if 0 <= i < len(screens) else 0.0
        self._spins["ScreenWidthCm"].setSpecialValueText(
            f"auto ({cm:.1f} cm)" if cm else "auto (unknown)")

    def _refresh_screens(self) -> None:
        with QSignalBlocker(self._cmb_screen):
            self._cmb_screen.clear()
            for i, scr in enumerate(QGuiApplication.screens()):
                g = scr.geometry()
                self._cmb_screen.addItem(
                    f"{i}: {scr.name()} ({g.width()}x{g.height()})")
            if 0 <= self._s.screen_index < self._cmb_screen.count():
                self._cmb_screen.setCurrentIndex(self._s.screen_index)

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
        self._lbl_progress = QLabel("Ready")
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        lay.addWidget(self._lbl_progress)
        lay.addWidget(self._bar)
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

    def set_progress_fraction(self, fraction: float) -> None:
        self._bar.setValue(round(max(0.0, min(1.0, fraction)) * 1000))

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
        self._update_totals()
        self.settings_changed.emit(self.settings)

    def _update_totals(self) -> None:
        v = {name: box.value() for name, box in self._spins.items()}
        per_region = v["MapWhiteSeconds"] + v["MapBlackSeconds"]
        total = (2 * v["MapGreySeconds"]
                 + N_REGIONS * per_region * v["MapRepeats"])
        self._lbl_map_total.setText(f"Total: {total:g} s")

        t = self._cmb_trial.currentData()
        if t not in SWEEP_TRIAL_TYPES:
            return
        per_stim = v["TuneOnSeconds"] + v["TuneGapSeconds"]
        n_levels = len(self._s.levels[t]) if t in _LEVEL_UNITS else 1
        total = (2 * v["TuneGreySeconds"] + N_ORIENTATIONS * n_levels
                 * v[_SWEEP_REPEATS[t]] * per_stim)
        self._lbl_sweep_total.setText(
            f"Total: {total:g} s  (+ {N_ORIENTATIONS * per_stim:g} s "
            f"pretrial first)")

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
            levels=dict(self._s.levels),
        )
