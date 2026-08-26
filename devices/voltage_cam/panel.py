"""
Voltage-imaging camera — settings panel.

SettingsPanel : QWidget that emits a signal per parameter.
                Resolution/binning/trigger lock while acquisition runs;
                exposure is hot-changeable at any time.

The owner (MainWindow / toy) reads .get_config() to build an AcqConfig
before starting the worker, and wires exposure_changed to worker.set_exposure().
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QWidget,
)

from .presets import (
    AcqConfig, PRESETS, LINK_LABEL,
    PRESET_KEYS, DEFAULT_PRESET,
    BINNING_OPTIONS,
    TRIGGER_MODES,
    WRITER_MBPS,
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
        # (fps, exposure_limited) reported by the running camera, or None when we
        # only have the datasheet estimate (before Start).
        self._measured: tuple[float, bool] | None = None
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Camera settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_preset = QComboBox()
        for key in PRESET_KEYS:
            # Show the descriptive label (dims + fps); store the stable key.
            self._cmb_preset.addItem(PRESETS[key].label, key)
        start = self._cfg.preset_key if self._cfg.preset_key in PRESET_KEYS else DEFAULT_PRESET
        self._cmb_preset.setCurrentIndex(PRESET_KEYS.index(start))
        self._cmb_preset.currentIndexChanged.connect(
            lambda i: self.resolution_changed.emit(self._cmb_preset.itemData(i)))
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

        # Effective frame rate = min(readout ceiling, 1/exposure). Without this
        # readout the default 10 ms exposure silently caps every preset above
        # ~4432×256 at 100 fps, and the preset label looks like a lie.
        self._lbl_rate = QLabel()
        lay.addRow("Frame rate:", self._lbl_rate)

        # Whether a RECORDING of this configuration can actually be written. The
        # camera happily offers ~2200 MB/s at full frame and the writer sustains
        # ~1000, so a bin-1 session silently keeps about half its frames — the
        # single most consequential fact about a configuration, and until now it
        # was only ever printed to a console the operator may never see.
        self._lbl_rec = QLabel()
        self._lbl_rec.setWordWrap(True)
        lay.addRow("Recording:", self._lbl_rec)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

        self._locked = [self._cmb_preset, self._cmb_binning, self._cmb_trigger]

        for sig in (self._cmb_preset.currentIndexChanged,
                    self._cmb_binning.currentIndexChanged,
                    self._spn_exposure.valueChanged):
            sig.connect(lambda *_: self._refresh_rate())
        self._refresh_rate()

    def _refresh_rate(self) -> None:
        self._refresh_recordability()
        # Prefer the camera's own measured rate once a capture is running — it
        # can't disagree with the real link the way the datasheet estimate can.
        if self._measured is not None:
            fps, limited = self._measured
            note = " (exposure-limited)" if limited else ""
            self._lbl_rate.setText(f"{fps:.1f} fps — measured by camera{note}")
            self._lbl_rate.setStyleSheet("color:#2e7d32; font-weight:bold;")
            return
        cfg = self.get_config()
        link = LINK_LABEL.get(cfg.link, cfg.link)
        if cfg.exposure_limited:
            self._lbl_rate.setText(
                f"{cfg.expected_fps:.1f} fps — limited by exposure "
                f"({link} readout allows {cfg.readout_fps:.1f}; "
                f"use ≤{cfg.max_exposure_us:.0f} µs)")
            self._lbl_rate.setStyleSheet("color:#c47f00; font-weight:bold;")
        else:
            self._lbl_rate.setText(
                f"{cfg.expected_fps:.1f} fps — at {link} readout limit")
            self._lbl_rate.setStyleSheet("color:#2e7d32;")

    def _refresh_recordability(self) -> None:
        """Say whether a recording of this configuration fits the writer.

        `WRITER_MBPS` is the whole path (worker → Recorder → HDF5Writer →
        NVMe), not a disk benchmark, and it is deliberately pessimistic — see
        presets.py for what it is and is not. Binning is the lever: on this
        camera it cuts bytes, not time, so 2×2 keeps the full frame rate at a
        quarter of the data.
        """
        cfg = self.get_config()
        fps = self._measured[0] if self._measured is not None else cfg.expected_fps
        mbps = cfg.frame_bytes * fps / (1 << 20)
        if mbps <= WRITER_MBPS:
            self._lbl_rec.setText(
                f"{mbps:.0f} MB/s — fits the writer (~{WRITER_MBPS:.0f} MB/s)")
            self._lbl_rec.setStyleSheet("color:#2e7d32;")
            return
        keep = WRITER_MBPS / mbps
        cap = WRITER_MBPS / (cfg.frame_bytes / (1 << 20))
        self._lbl_rec.setText(
            f"⚠ {mbps:.0f} MB/s — only ~{100 * keep:.0f}% of frames can be "
            f"written (~{WRITER_MBPS:.0f} MB/s). Live view is unaffected. "
            f"Use 2×2 binning, a smaller ROI, or cap the rate near "
            f"{cap:.0f} fps (exposure ≥ {1e6 / cap:.0f} µs).")
        self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_measured_rate(self, fps: float | None,
                          exposure_limited: bool = False) -> None:
        """Show the camera's own measured frame rate. Pass None to revert to the
        datasheet estimate (e.g. when the session stops)."""
        self._measured = None if fps is None else (float(fps), bool(exposure_limited))
        self._refresh_rate()

    def get_config(self) -> AcqConfig:
        # `link` has no widget — it comes from the config the panel was built
        # with (and, on the rig, from _check_link.py). Carry it through rather
        # than rebuilding a default: dropping it silently reverts a USB3 rig to
        # the CoaXPress readout table, and every fps estimate reads ~7× high.
        return AcqConfig(
            preset_key   = self._cmb_preset.currentData(),
            binning      = self._cmb_binning.currentData(),
            exposure_us  = self._spn_exposure.value(),
            trigger_mode = self._cmb_trigger.currentText(),
            link         = self._cfg.link,
        )

    def set_running(self, running: bool) -> None:
        """Lock structural settings (resolution/binning/trigger) while running."""
        for w in self._locked:
            w.setEnabled(not running)

    @property
    def exposure_us(self) -> float:
        return self._spn_exposure.value()
