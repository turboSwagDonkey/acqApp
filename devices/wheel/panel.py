"""Wheel encoder — the Qt settings panel. The model is in `settings.py`."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QFormLayout, QGroupBox, QLabel, QWidget

from acqApp.widgets import pairs_grid, sections_help, spin

from acqApp.devices.wheel.settings import EncoderSettings


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits EncoderSettings

    def __init__(self, settings: EncoderSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or EncoderSettings()
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Encoder settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        # Channel and sample rate have no widget: acqapp_local.json's (or
        # rigs.json's channel), read at launch.
        # 0 means "not calibrated" (None in the settings).
        self._spn_vpr = spin(0.0, 20.0, self._s.volts_per_rev or 0.0,
                             decimals=3, suffix=" V/rev")
        self._spn_vpr.setSpecialValueText("— (raw V)")
        self._spn_vpr.setToolTip("Encoder volts per full turn. 0: report "
                                 "raw volts.")

        self._spn_dia = spin(0.0, 500.0, self._s.wheel_dia_mm or 0.0,
                             decimals=2, suffix=" mm")
        self._spn_dia.setSpecialValueText("— (no linear)")
        self._spn_dia.setToolTip("Turns the wheel's rotation into distance "
                                 "run. 0: speed and distance in revolutions.")

        lay.addRow(pairs_grid(
            ("V / rev:", self._spn_vpr, "Wheel dia:", self._spn_dia),
            per_row=1))

        # valueChanged, not editingFinished: arrow-key nudges never "finish"
        # editing, so they never reached the worker or the saved settings.
        for w in (self._spn_vpr, self._spn_dia):
            w.valueChanged.connect(self._emit)

        # Live speed + net distance, set by the adapter.
        self._lbl_readout = QLabel("speed —   distance —")
        f = self._lbl_readout.font()
        f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        self._lbl_readout.setFont(f)
        self._lbl_readout.setToolTip("Live speed and net distance run.")
        lay.addRow("Live:", self._lbl_readout)

        sections_help(self, keep=True)
        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    def set_readout(self, text: str) -> None:
        """Show the live speed/distance line (formatted by the caller)."""
        self._lbl_readout.setText(text)

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> EncoderSettings:
        vpr = self._spn_vpr.value() or None
        dia = self._spn_dia.value() or None
        return EncoderSettings(
            channel=self._s.channel,
            rate=self._s.rate,
            volts_per_rev=vpr,
            wheel_dia_mm=dia,
        )
