"""
Wheel encoder — settings dataclass + Qt settings panel.
"""

from __future__ import annotations
from dataclasses import dataclass

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QSpinBox, QWidget,
)


@dataclass
class EncoderSettings:
    channel:       str   = "Dev3/ai2"
    rate:          float = 50.0    # Hz
    volts_per_rev: float | None = None   # None → show raw voltage
    wheel_dia_mm:  float | None = None   # None → no linear distance


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

        self._edt_chan = QComboBox()
        self._edt_chan.setEditable(True)
        self._edt_chan.addItems(["Dev3/ai2", "Dev3/ai0", "Dev3/ai1"])
        self._edt_chan.setCurrentText(self._s.channel)
        lay.addRow("Channel:", self._edt_chan)

        self._spn_rate = QDoubleSpinBox()
        self._spn_rate.setRange(1.0, 1000.0)
        self._spn_rate.setSuffix(" Hz")
        self._spn_rate.setValue(self._s.rate)
        lay.addRow("Sample rate:", self._spn_rate)

        self._spn_vpr = QDoubleSpinBox()
        self._spn_vpr.setRange(0.0, 20.0)
        self._spn_vpr.setDecimals(3)
        self._spn_vpr.setSuffix(" V/rev")
        self._spn_vpr.setSpecialValueText("— (raw V)")
        self._spn_vpr.setValue(self._s.volts_per_rev or 0.0)
        lay.addRow("V / rev:", self._spn_vpr)

        self._spn_dia = QDoubleSpinBox()
        self._spn_dia.setRange(0.0, 500.0)
        self._spn_dia.setSuffix(" mm")
        self._spn_dia.setSpecialValueText("— (no linear)")
        self._spn_dia.setValue(self._s.wheel_dia_mm or 0.0)
        lay.addRow("Wheel dia:", self._spn_dia)

        for w in (self._edt_chan, self._spn_rate, self._spn_vpr, self._spn_dia):
            w.editingFinished.connect(self._emit) if hasattr(w, "editingFinished") \
                else w.currentTextChanged.connect(self._emit)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    def _emit(self) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> EncoderSettings:
        vpr = self._spn_vpr.value() or None
        dia = self._spn_dia.value() or None
        return EncoderSettings(
            channel=self._edt_chan.currentText(),
            rate=self._spn_rate.value(),
            volts_per_rev=vpr,
            wheel_dia_mm=dia,
        )
