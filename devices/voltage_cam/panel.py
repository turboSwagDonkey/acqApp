"""
Voltage-imaging camera — settings panel.

SettingsPanel : QWidget that emits a signal per parameter.
                Resolution/binning/trigger lock while acquisition runs;
                exposure is hot-changeable at any time. `set_trigger_mode()`
                lets a routine drive the combo itself (adapters/
                voltage_cam.py's `set_external_trigger`), the same way
                `set_preset()` already lets the DMD calibration drive the
                resolution combo.

The owner (MainWindow / toy) reads .get_config() to build an AcqConfig
before starting the worker, and wires exposure_changed to worker.set_exposure().
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QSpinBox, QWidget,
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
    lut_visible_changed = pyqtSignal(bool)  # show/hide the histogram bar
    auto_levels_changed = pyqtSignal(bool)  # auto-recompute vs the operator's drag
    preview_avg_changed = pyqtSignal(int)   # frames to average in the preview only
    led_toggled       = pyqtSignal(bool)    # primary illumination on/off
    led_follow_changed = pyqtSignal(bool)   # the Follow Live view MODE, not the LED state
    # Any parameter edit. The LED's own ON/OFF is deliberately NOT one of
    # these: it's runtime state, and restoring it at launch would turn the
    # illumination on in an empty rig (devices/pupil_cam/panel.py's same
    # rule). `led_follow_live` is a MODE, not that state, so it persists
    # like show_lut/auto_levels — it only ever fires the LED from an
    # explicit Live/Record start, never at launch.

    def __init__(self, config: AcqConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or AcqConfig()
        # (hz, exposure_limited) reported by the running camera, or None when we
        # only have the datasheet estimate (before Start).
        self._measured: tuple[float, bool] | None = None
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Camera settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_preset = QComboBox()
        for key in PRESET_KEYS:
            # Show the descriptive label (dims + Hz); store the stable key.
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

        # A frame period can't be shorter than the exposure inside it, so Rate
        # always caps Exposure's maximum to 1/rate — independent of Link, which
        # only decides whether moving one *also* moves the other.
        self._spn_hz = QDoubleSpinBox()
        self._spn_hz.setRange(0.001, 100_000.0)
        self._spn_hz.setDecimals(3)
        self._spn_hz.setSuffix(" Hz")
        self._spn_hz.setValue(1e6 / self._cfg.exposure_us if self._cfg.exposure_us > 0 else 100.0)
        self._chk_hz_link = QCheckBox("Link")
        self._chk_hz_link.setToolTip(
            "Keep Rate and Exposure locked together (Exposure = 1 / Rate)")
        self._chk_hz_link.toggled.connect(self._on_hz_link_toggled)
        hz_row = QWidget()
        hz_lay = QHBoxLayout(hz_row)
        hz_lay.setContentsMargins(0, 0, 0, 0)
        hz_lay.addWidget(self._spn_hz)
        hz_lay.addWidget(self._chk_hz_link)
        lay.addRow("Rate:", hz_row)

        self._hz_syncing = False
        self._spn_hz.valueChanged.connect(self._on_hz_changed)
        self._spn_exposure.valueChanged.connect(self._on_exposure_changed_for_hz)
        self._on_hz_changed(self._spn_hz.value())    # apply the initial cap

        self._cmb_trigger = QComboBox()
        self._cmb_trigger.addItems(TRIGGER_MODES)
        self._cmb_trigger.setCurrentText(self._cfg.trigger_mode)
        self._cmb_trigger.currentTextChanged.connect(self.trigger_changed)
        lay.addRow("Trigger:", self._cmb_trigger)

        # Effective frame rate = min(readout ceiling, 1/exposure). Without this
        # readout the default 10 ms exposure silently caps every preset above
        # ~4432×256 at 100 Hz, and the preset label looks like a lie.
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

        self._chk_lut = QCheckBox("Show LUT")
        self._chk_lut.setChecked(self._cfg.show_lut)
        self._chk_lut.setToolTip(
            "Show or hide the histogram/contrast bar beside the preview.")
        self._chk_lut.toggled.connect(self.lut_visible_changed)

        self._chk_auto = QCheckBox("Auto contrast")
        self._chk_auto.setChecked(self._cfg.auto_levels)
        self._chk_auto.setToolTip(
            "On (default): levels are recomputed from each frame's own "
            "brightness range.\nOff: drag the LUT's handles yourself — the "
            "app leaves them exactly where you put them.")
        self._chk_auto.toggled.connect(self.auto_levels_changed)

        self._spn_preview_avg = QSpinBox()
        self._spn_preview_avg.setRange(1, 8)
        self._spn_preview_avg.setValue(self._cfg.preview_avg)
        self._spn_preview_avg.setPrefix("avg ")
        self._spn_preview_avg.setToolTip(
            "Average this many recent preview frames before display.\n"
            "1 = off. The recorded file still gets every raw frame.")
        self._spn_preview_avg.valueChanged.connect(self.preview_avg_changed)

        disp_row = QWidget()
        disp_lay = QHBoxLayout(disp_row)
        disp_lay.setContentsMargins(0, 0, 0, 0)
        disp_lay.addWidget(self._chk_lut)
        disp_lay.addWidget(self._chk_auto)
        disp_lay.addWidget(self._spn_preview_avg)
        lay.addRow("Display:", disp_row)

        led = QGroupBox("Illumination")
        ll = QHBoxLayout(led)
        self._chk_led = QCheckBox("Primary LED")
        self._chk_led.toggled.connect(self.led_toggled)
        self._chk_led_follow = QCheckBox("Follow Live view")
        self._chk_led_follow.setChecked(self._cfg.led_follow_live)
        self._chk_led_follow.setToolTip(
            "Turn the LED on when Live view/Record starts and off when it "
            "stops. The checkbox above still overrides it at any time.")
        self._chk_led_follow.toggled.connect(self.led_follow_changed)
        ll.addWidget(self._chk_led)
        ll.addWidget(self._chk_led_follow)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)
        root.addRow(led)

        self._locked = [self._cmb_preset, self._cmb_binning, self._cmb_trigger]

        for sig in (self._cmb_preset.currentIndexChanged,
                    self._cmb_binning.currentIndexChanged,
                    self._spn_exposure.valueChanged):
            sig.connect(lambda *_: self._refresh_rate())
        self._refresh_rate()

    def _on_hz_changed(self, hz: float) -> None:
        """Rate always caps Exposure's ceiling; Link also drives it to the cap."""
        if self._hz_syncing:
            return
        self._hz_syncing = True
        try:
            max_us = 1e6 / hz if hz > 0 else self._spn_exposure.maximum()
            self._spn_exposure.setMaximum(max_us)   # Qt clamps the value too
            if self._chk_hz_link.isChecked():
                self._spn_exposure.setValue(max_us)
        finally:
            self._hz_syncing = False

    def _on_exposure_changed_for_hz(self, us: float) -> None:
        """Only Link pulls Rate along; otherwise Rate stays the operator's cap."""
        if self._hz_syncing or not self._chk_hz_link.isChecked():
            return
        self._hz_syncing = True
        try:
            self._spn_hz.setValue(1e6 / us if us > 0 else self._spn_hz.maximum())
        finally:
            self._hz_syncing = False

    def _on_hz_link_toggled(self, linked: bool) -> None:
        if linked:
            self._on_hz_changed(self._spn_hz.value())

    def _refresh_rate(self) -> None:
        cfg = self.get_config()
        self._refresh_recordability(cfg)
        # Prefer the camera's own measured rate once a capture is running — it
        # can't disagree with the real link the way the datasheet estimate can.
        if self._measured is not None:
            hz, limited = self._measured
            note = " (exposure-limited)" if limited else ""
            self._lbl_rate.setText(f"{hz:.1f} Hz — measured by camera{note}")
            self._lbl_rate.setStyleSheet("color:#2e7d32; font-weight:bold;")
            return
        link = LINK_LABEL.get(cfg.link, cfg.link)
        if cfg.exposure_limited:
            self._lbl_rate.setText(
                f"{cfg.expected_hz:.1f} Hz — limited by exposure "
                f"({link} readout allows {cfg.readout_hz:.1f}; "
                f"use ≤{cfg.max_exposure_us:.0f} µs)")
            self._lbl_rate.setStyleSheet("color:#c47f00; font-weight:bold;")
        else:
            self._lbl_rate.setText(
                f"{cfg.expected_hz:.1f} Hz — at {link} readout limit")
            self._lbl_rate.setStyleSheet("color:#2e7d32;")

    def _refresh_recordability(self, cfg: AcqConfig | None = None) -> None:
        """Say whether a recording of this configuration fits the writer.

        `WRITER_MBPS` is the whole path (worker → Recorder → HDF5Writer →
        NVMe), not a disk benchmark, and it's deliberately pessimistic — see
        presets.py for what it is and isn't. Binning is the lever: on this
        camera it cuts bytes, not time, so 2×2 keeps the full frame rate at a
        quarter of the data.
        """
        cfg = cfg if cfg is not None else self.get_config()
        hz = self._measured[0] if self._measured is not None else cfg.expected_hz
        mbps = cfg.frame_bytes * hz / (1 << 20)
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
            f"{cap:.0f} Hz (exposure ≥ {1e6 / cap:.0f} µs).")
        self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_measured_rate(self, hz: float | None,
                          exposure_limited: bool = False) -> None:
        """Show the camera's own measured frame rate. Pass None to revert to the
        datasheet estimate (e.g. when the session stops)."""
        self._measured = None if hz is None else (float(hz), bool(exposure_limited))
        self._refresh_rate()

    def get_config(self) -> AcqConfig:
        # `link` has no widget — it comes from the config the panel was built
        # with (and, on the rig, from _check_link.py). Carry it through rather
        # than rebuilding a default: dropping it silently reverts a USB3 rig to
        # the CoaXPress readout table, and every Hz estimate reads ~7× high.
        return AcqConfig(
            preset_key   = self._cmb_preset.currentData(),
            binning      = self._cmb_binning.currentData(),
            exposure_us  = self._spn_exposure.value(),
            trigger_mode = self._cmb_trigger.currentText(),
            link         = self._cfg.link,
            show_lut     = self._chk_lut.isChecked(),
            auto_levels  = self._chk_auto.isChecked(),
            preview_avg  = self._spn_preview_avg.value(),
            led_follow_live = self._chk_led_follow.isChecked(),
        )

    def set_led(self, on: bool) -> None:
        """Sync the checkbox to actual state without re-emitting led_toggled
        — the adapter calls this when Follow Live view fires the LED itself,
        so the checkbox still shows the truth without a feedback loop."""
        self._chk_led.blockSignals(True)
        self._chk_led.setChecked(on)
        self._chk_led.blockSignals(False)

    def set_running(self, running: bool) -> None:
        """Lock structural settings (resolution/binning/trigger) while running."""
        for w in self._locked:
            w.setEnabled(not running)

    def set_preset(self, key: str) -> None:
        """Programmatically select a resolution preset (e.g. forcing full
        frame before a DMD calibration). Structural, like a user's own combo
        click: it only takes effect at the next Start."""
        if key in PRESET_KEYS:
            self._cmb_preset.setCurrentIndex(PRESET_KEYS.index(key))

    def set_binning(self, n: int) -> None:
        """Programmatically select a binning factor (e.g. from a Mode
        preset). Structural, like `set_preset()`: only takes effect at the
        next Start."""
        if n in BINNING_OPTIONS:
            self._cmb_binning.setCurrentIndex(BINNING_OPTIONS.index(n))

    def set_trigger_mode(self, mode: str) -> None:
        """Programmatically select a trigger mode — a routine forces External
        edge before it opens its recording, for a TTL start or for any
        `trigger` step (see adapters/voltage_cam.py's `set_external_trigger`).
        Structural, like `set_preset()`: only takes effect at the next Start."""
        if mode in TRIGGER_MODES:
            self._cmb_trigger.setCurrentText(mode)

    def set_exposure(self, us: float) -> None:
        """Programmatically set exposure (e.g. from a Mode preset). Hot, like
        the operator's own spinbox edit — the spinbox's own range clamps it,
        and Rate/Link move with it exactly as they would from a manual edit."""
        self._spn_exposure.setValue(us)

    @property
    def exposure_us(self) -> float:
        return self._spn_exposure.value()
