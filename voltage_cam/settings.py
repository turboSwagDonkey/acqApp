"""
Voltage-imaging camera — settings panel.

SettingsPanel : QWidget that emits a signal per parameter.
                Resolution/binning/trigger lock while acquisition runs;
                exposure is hot-changeable at any time.

The owner (MainWindow / toy) reads .get_config() to build an AcqConfig
before starting the worker, and wires exposure_changed to worker.set_exposure().
"""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QWidget,
)

from .presets import (
    AcqConfig,
    PRESET_KEYS, DEFAULT_PRESET,
    BINNING_OPTIONS, DEFAULT_BINNING,
    TRIGGER_MODES, DEFAULT_TRIGGER,
)


class SettingsPanel(QWidget):
    """Camera acquisition settings panel."""

    exposure_changed  = pyqtSignal(float)   # µs — hot-changeable
    resolution_changed = pyqtSignal(str)    # preset key
    binning_changed   = pyqtSignal(int)
    trigger_changed   = pyqtSignal(str)

    def __init__(self, config: AcqConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or AcqConfig()
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Camera settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_preset = QComboBox()
        self._cmb_preset.addItems(PRESET_KEYS)
        self._cmb_preset.setCurrentText(self._cfg.preset_key)
        self._cmb_preset.currentTextChanged.connect(self.resolution_changed)
        lay.addRow("Resolution:", self._cmb_preset)

        self._cmb_binning = QComboBox()
        for b in BINNING_OPTIONS:
            self._cmb_binning.addItem(f"{b}×{b}", b)
        self._cmb_binning.setCurrentIndex(BINNING_OPTIONS.index(self._cfg.binning))
        self._cmb_binning.currentIndexChanged.connect(
            lambda i: self.binning_changed.emit(BINNING_OPTIONS[i])
        )
        lay.addRow("Binning:", self._cmb_binning)

        self._spn_exposure = QDoubleSpinBox()
        self._spn_exposure.setRange(0.01, 1_000_000.0)
        self._spn_exposure.setSingleStep(500.0)
        self._spn_exposure.setDecimals(1)
        self._spn_exposure.setSuffix(" µs")
        self._spn_exposure.setValue(self._cfg.exposure_us)
        self._spn_exposure.valueChanged.connect(self.exposure_changed)
        lay.addRow("Exposure:", self._spn_exposure)

        self._cmb_trigger = QComboBox()
        self._cmb_trigger.addItems(TRIGGER_MODES)
        self._cmb_trigger.setCurrentText(self._cfg.trigger_mode)
        self._cmb_trigger.currentTextChanged.connect(self.trigger_changed)
        lay.addRow("Trigger:", self._cmb_trigger)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

        self._locked = [self._cmb_preset, self._cmb_binning, self._cmb_trigger]

    # ── Public API ────────────────────────────────────────────────────────────

    def get_config(self) -> AcqConfig:
        return AcqConfig(
            preset_key   = self._cmb_preset.currentText(),
            binning      = self._cmb_binning.currentData(),
            exposure_us  = self._spn_exposure.value(),
            trigger_mode = self._cmb_trigger.currentText(),
        )

    def set_running(self, running: bool) -> None:
        """Lock structural settings (resolution/binning/trigger) while running."""
        for w in self._locked:
            w.setEnabled(not running)

    @property
    def exposure_us(self) -> float:
        return self._spn_exposure.value()
