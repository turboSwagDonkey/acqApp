"""Electrically tunable lens — the Qt settings panel. The model is in
`settings.py`.

Offset (µm) and Setpoint (V) are one value: volts are stored, the offset is
them through the calibration, and editing either moves the other.
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QFormLayout, QGridLayout, QGroupBox, QLabel,
                             QVBoxLayout, QWidget)

from acqApp import style
from acqApp.widgets import sections_help, spin

from acqApp.devices.etl.settings import EtlSettings

_UM_SPAN = 5000.0       # the offset box's bounds before a calibration narrows them


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits EtlSettings

    def __init__(self, settings: EtlSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or EtlSettings()
        self._build()

    def _build(self) -> None:
        """`strip`: the everyday controls on one line (Offset, Setpoint, then
        what was written). `calibration`: the rest. Both stack here; with a
        Stage tab the window lends them out: the strip on top of the stage
        controls, the calibration into the Stage's Settings."""
        self._spn_um = spin(-_UM_SPAN, _UM_SPAN, 0.0, decimals=1, step=1.0,
                            suffix=" µm")
        self._spn_um.setToolTip(
            "Path 2's focal plane relative to path 1's, along stage Z: + is "
            "where +Z would take path 1. Needs the ETL calibration.")

        lo, hi = self._s.limits
        self._spn_v = spin(lo, hi, self._s.clamped(), decimals=3, step=0.01,
                           suffix=" V")
        self._spn_v.setToolTip("The voltage held on the ETL driver's input; "
                               "it moves path 2's focus at once.")

        self._lbl_out = QLabel("—")
        self._lbl_out.setToolTip("What the DAQ last wrote.")
        self._lbl_out.setStyleSheet(f"color:{style.muted()}")

        self.strip = QWidget()
        self.strip.setObjectName("etlStrip")
        self.strip.setStyleSheet(style.accent_panel("etl"))
        row = QGridLayout(self.strip)
        row.setContentsMargins(6, 3, 6, 3)
        row.setHorizontalSpacing(6)
        row.setVerticalSpacing(1)
        name = QLabel("ETL")
        name.setStyleSheet(f"font-weight:bold;color:{style.HEX['etl']}")
        name.setToolTip("Path 2's focus: its plane is drawn on the Z gauge "
                        "(P2) once calibrated. Alt+↑/↓ nudges it 1 µm "
                        "(0.01 V uncalibrated), with Shift 10×.")
        # On the strip: live wherever it is lent, while it shows.
        for keys, steps in (("Alt+Up", 1), ("Alt+Down", -1),
                            ("Alt+Shift+Up", 10), ("Alt+Shift+Down", -10)):
            sc = QShortcut(QKeySequence(keys), self.strip)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda n=steps: self.nudge(n))
        row.addWidget(name, 0, 0)
        row.addWidget(QLabel("Offset"), 0, 1)
        row.addWidget(self._spn_um, 0, 2)
        row.addWidget(QLabel("Setpoint"), 0, 3)
        row.addWidget(self._spn_v, 0, 4)
        row.addWidget(self._lbl_out, 1, 1, 1, 4)
        row.setColumnStretch(5, 1)

        # ── the calibration and the driver's range ──
        self._spn_cofocal = spin(lo, hi, min(hi, max(lo, self._s.cofocal_volts)),
                                 decimals=3, step=0.01, suffix=" V")
        self._spn_cofocal.setToolTip(
            "The setpoint at which both paths are in focus on the same plane.")
        self._spn_gain = spin(-10000.0, 10000.0, self._s.um_per_volt, decimals=2,
                              step=1.0, suffix=" µm/V")
        self._spn_gain.setSpecialValueText("— (not calibrated)")
        self._spn_gain.setToolTip(
            "How far path 2's plane moves per volt, as the stage Z that "
            "refocuses path 1 on it (negative if it moves toward −Z). "
            "0: volts only, no offset shown on the stage.")
        rng = QLabel(f"{lo:g} – {hi:g} V")
        rng.setToolTip("The driver's input range: min_volts / max_volts in "
                       "acqapp_local.json.")
        # Its own accent: lent to the Stage's Settings, it keeps the ETL's look.
        self.calibration = QGroupBox("ETL calibration")
        self.calibration.setStyleSheet(style.accent_panel("etl"))
        al = QFormLayout(self.calibration)
        al.setSpacing(4)
        al.addRow("Co-focal at:", self._spn_cofocal)
        al.addRow("Gain:", self._spn_gain)
        al.addRow("Range:", rng)

        # valueChanged: arrow-key nudges never "finish" editing.
        self._spn_v.valueChanged.connect(self._on_volts)
        self._spn_um.valueChanged.connect(self._on_um)
        self._spn_cofocal.valueChanged.connect(self._on_calibration)
        self._spn_gain.valueChanged.connect(self._on_calibration)
        self._sync_offset()

        sections_help(self, keep=True)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(6)
        self.take_back()

    def take_back(self) -> None:
        """The strip and the calibration home again, from wherever lent."""
        self._root.insertWidget(0, self.strip)
        self._root.insertWidget(1, self.calibration)
        self.strip.show()
        self.calibration.show()

    # ── the coupled pair ──
    def _sync_offset(self) -> None:
        """Offset box from the volts: its range is the volts range through
        the calibration; disabled without one."""
        s = self.settings
        with QSignalBlocker(self._spn_um):
            if s.calibrated:
                ends = sorted((v - s.cofocal_volts) * s.um_per_volt for v in s.limits)
                self._spn_um.setRange(*ends)
                self._spn_um.setValue(s.offset_um())
            else:
                self._spn_um.setRange(-_UM_SPAN, _UM_SPAN)
                self._spn_um.setValue(0.0)
        self._spn_um.setEnabled(s.calibrated)

    def _on_volts(self, *_a) -> None:
        self._sync_offset()
        self._emit()

    def _on_um(self, um: float) -> None:
        s = self.settings
        if s.calibrated:
            self._spn_v.setValue(s.volts_for(um))     # emits through _on_volts

    def _on_calibration(self, *_a) -> None:
        self._sync_offset()
        self._emit()

    _NUDGE_UM = 1.0         # per Alt+arrow, calibrated
    _NUDGE_V = 0.01         # per Alt+arrow, volts only

    def nudge(self, steps: int) -> None:
        """Path 2's focus `steps` notches up (+) or down: µm when calibrated,
        else volts. Through the spin boxes, so the save and write follow."""
        if self.settings.calibrated:
            self._spn_um.setValue(self._spn_um.value() + steps * self._NUDGE_UM)
        else:
            self._spn_v.setValue(self._spn_v.value() + steps * self._NUDGE_V)

    # ── for the adapter ──
    def set_output(self, text: str) -> None:
        self._lbl_out.setText(text)

    def set_volts(self, v: float) -> None:
        """Through the spin box, so the save and the write follow."""
        self._spn_v.setValue(float(v))

    def set_offset_um(self, um: float) -> bool:
        """False (nothing changes) without a calibration."""
        if not self.settings.calibrated:
            return False
        self._spn_um.setValue(float(um))
        return True

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> EtlSettings:
        return replace(self._s, volts=self._spn_v.value(),
                       cofocal_volts=self._spn_cofocal.value(),
                       um_per_volt=self._spn_gain.value())
