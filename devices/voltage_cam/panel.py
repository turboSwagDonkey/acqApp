"""Voltage camera settings panel: a signal per parameter.

Resolution/binning/trigger/burst lock while running; capture rate (and so
exposure) is hot. The owner builds the worker from `get_config()` and wires
`target_hz_changed` to `worker.set_rate()`.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QWidget,
)

from acqApp.widgets import spin
from .presets import (
    AcqConfig, PRESETS, LINK_LABEL,
    PRESET_KEYS, DEFAULT_PRESET,
    BINNING_OPTIONS, BURST_TIMES_MAX,
    EXTERNAL_EDGE, TRIGGER_MODES,
    WRITER_MBPS,
)


class SettingsPanel(QWidget):
    resolution_changed = pyqtSignal(str)    # preset key
    binning_changed   = pyqtSignal(int)
    trigger_changed   = pyqtSignal(str)
    burst_changed     = pyqtSignal(int)     # frames per edge; 0 = until re-armed
    target_hz_changed = pyqtSignal(float)   # capture rate, hot; 0 = Max
    lut_visible_changed = pyqtSignal(bool)  # show/hide the histogram bar
    auto_levels_changed = pyqtSignal(bool)  # auto-recompute vs the operator's drag
    preview_avg_changed = pyqtSignal(int)   # frames to average in the preview only
    led_toggled       = pyqtSignal(bool)    # primary illumination on/off
    # A persisted MODE; the LED's own on/off is never persisted, or launch
    # would light an empty rig.
    led_follow_changed = pyqtSignal(bool)

    def __init__(self, config: AcqConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or AcqConfig()
        # (hz, exposure_limited) from the running camera; None = datasheet.
        self._measured: tuple[float, bool] | None = None
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("Camera settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_preset = QComboBox()
        for key in PRESET_KEYS:
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

        self._cmb_trigger = QComboBox()
        self._cmb_trigger.addItems(TRIGGER_MODES)
        self._cmb_trigger.setCurrentText(self._cfg.trigger_mode)
        self._cmb_trigger.currentTextChanged.connect(self.trigger_changed)
        lay.addRow("Trigger:", self._cmb_trigger)

        # -1 pulse of headroom: SYNCREADOUT asks for N+1 (presets.burst_pulses).
        self._spn_burst = spin(
            0, BURST_TIMES_MAX - 1, self._cfg.burst_frames, step=100,
            suffix=" frames", track=False,
            tooltip="External edge only. Each edge captures exactly this many "
                    "frames, then the camera waits for the next edge with no "
                    "re-arm.\nOff = one edge starts capture until re-armed.\n"
                    "A routine sets this from its Record length at Start.")
        self._spn_burst.setSpecialValueText("Off")
        self._spn_burst.valueChanged.connect(self.burst_changed)
        lay.addRow("Frames per edge:", self._spn_burst)
        self._cmb_trigger.currentTextChanged.connect(self._sync_burst_enabled)

        self._spn_target_hz = spin(
            0.0, 100_000.0, self._cfg.target_hz, decimals=1, step=50.0,
            suffix=" Hz", track=False,
            tooltip="Frames per second. Exposure is set to the longest this "
                    "rate allows.\n0 = as fast as this resolution allows.\n"
                    "A rate it can't reach is clamped, and the console says so.")
        self._spn_target_hz.setSpecialValueText("Max")
        self._spn_target_hz.valueChanged.connect(self.target_hz_changed)
        lay.addRow("Capture rate:", self._spn_target_hz)

        self._lbl_rate = QLabel()
        lay.addRow("Frame rate:", self._lbl_rate)

        # Whether a recording can be written: full frame at bin 1 offers more
        # than the writer sustains and silently sheds the rest.
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

        self._spn_preview_avg = spin(
            1, 8, self._cfg.preview_avg, prefix="avg ",
            tooltip="Average this many recent preview frames before display.\n"
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

        self._locked = [self._cmb_preset, self._cmb_binning, self._cmb_trigger,
                        self._spn_burst]
        self._running = False
        self._sync_burst_enabled()

        for sig in (self._cmb_preset.currentIndexChanged,
                    self._cmb_binning.currentIndexChanged,
                    self._cmb_trigger.currentIndexChanged,
                    self._spn_target_hz.valueChanged):
            sig.connect(lambda *_: self._refresh_rate())
        self._refresh_rate()

    def _refresh_rate(self) -> None:
        cfg = self.get_config()
        self._refresh_recordability(cfg)
        exp = f"exposure {cfg.exposure_us:.0f} µs"
        if self._measured is not None:
            hz, _ = self._measured
            self._lbl_rate.setText(f"{hz:.1f} Hz — camera · {exp}")
            self._lbl_rate.setStyleSheet("color:#2e7d32; font-weight:bold;")
            return
        link = LINK_LABEL.get(cfg.link, cfg.link)
        text = (f"{cfg.rate_hz:.1f} Hz · {exp} "
                f"(max ~{cfg.ceiling_hz:.0f} Hz on {link})")
        if cfg.rate_unreachable:
            text += f" · {cfg.target_hz:.0f} REQUESTED — NOT REACHABLE"
        self._lbl_rate.setText(text)
        self._lbl_rate.setStyleSheet(
            "color:#c62828; font-weight:bold;" if cfg.rate_unreachable
            else "color:#2e7d32;")

    def _refresh_recordability(self, cfg: AcqConfig) -> None:
        """`WRITER_MBPS` is the whole write path, not a disk benchmark.
        Binning cuts bytes, not rate, so 2×2 is the lever."""
        hz = self._measured[0] if self._measured is not None else cfg.rate_hz
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
            f"Use 2×2 binning, a smaller ROI, or a capture rate ≤ "
            f"{cap:.0f} Hz.")
        self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_measured_rate(self, hz: float | None,
                          exposure_limited: bool = False) -> None:
        """The camera's own rate; None reverts to the datasheet estimate."""
        self._measured = None if hz is None else (float(hz), bool(exposure_limited))
        self._refresh_rate()

    def get_config(self) -> AcqConfig:
        # `link` has no widget; dropping it would put a USB3 rig on the
        # CoaXPress table, every estimate ~7× high.
        return AcqConfig(
            preset_key   = self._cmb_preset.currentData(),
            binning      = self._cmb_binning.currentData(),
            trigger_mode = self._cmb_trigger.currentText(),
            link         = self._cfg.link,
            target_hz    = self._spn_target_hz.value(),
            burst_frames = self._spn_burst.value(),
            show_lut     = self._chk_lut.isChecked(),
            auto_levels  = self._chk_auto.isChecked(),
            preview_avg  = self._spn_preview_avg.value(),
            led_follow_live = self._chk_led_follow.isChecked(),
        ).fit_exposure()

    def set_led(self, on: bool) -> None:
        """Show the LED state without re-emitting `led_toggled`."""
        self._chk_led.blockSignals(True)
        self._chk_led.setChecked(on)
        self._chk_led.blockSignals(False)

    def set_running(self, running: bool) -> None:
        """Lock the structural settings while running."""
        self._running = running
        for w in self._locked:
            w.setEnabled(not running)
        self._sync_burst_enabled()

    def _sync_burst_enabled(self, *_a) -> None:
        self._spn_burst.setEnabled(
            not self._running and self._cmb_trigger.currentText() == EXTERNAL_EDGE)

    def set_preset(self, key: str) -> None:
        """Structural: takes effect at the next Start."""
        if key in PRESET_KEYS:
            self._cmb_preset.setCurrentIndex(PRESET_KEYS.index(key))

    def set_binning(self, n: int) -> None:
        """Structural: takes effect at the next Start."""
        if n in BINNING_OPTIONS:
            self._cmb_binning.setCurrentIndex(BINNING_OPTIONS.index(n))

    def set_trigger_mode(self, mode: str) -> None:
        """Structural: takes effect at the next Start."""
        if mode in TRIGGER_MODES:
            self._cmb_trigger.setCurrentText(mode)

    def set_burst_frames(self, n: int) -> None:
        """Frames per edge. Structural: next Start."""
        self._spn_burst.setValue(int(n))

    def set_rate(self, hz: float) -> None:
        """Capture rate; hot, like a manual edit."""
        self._spn_target_hz.setValue(hz)

    @property
    def rate_request_hz(self) -> float:
        """What the box holds; 0 = Max."""
        return self._spn_target_hz.value()
