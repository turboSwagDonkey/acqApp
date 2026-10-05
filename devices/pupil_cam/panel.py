"""Pupil camera — the Qt settings panel. The model is in `settings.py`.

Top to bottom in the order you use it: camera, recorded clips, then the
tracking controls (`tracking_panel.TrackingControls`, the same widget Pupil
review shows), then the LED.

The adapter persists exactly `settings`: a knob not read back there is lost
at the next launch. That has happened; keep them in step.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QStackedWidget, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.widgets import RangeBar, SegmentedSwitch, sections_help, spin
from acqApp.devices.pupil_cam.rim import LOW_CONTRAST
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking_panel import TrackingControls

EXPOSURE_MIN_US = 20.0      # until the camera says its own minimum


def _fmt_us(us: float) -> str:
    return f"{us / 1000:.2f} ms" if us >= 1000 else f"{us:.0f} µs"


_VIDEO_FILTER = "Uncompressed AVI (*.avi);;All files (*)"


class SettingsPanel(QWidget):
    exposure_changed = pyqtSignal(float)
    led_toggled      = pyqtSignal(bool)
    led_intensity_changed = pyqtSignal(float)  # 0..1
    # Not the LED on/off: restoring it at launch would light an empty rig.
    settings_changed = pyqtSignal(object)  # PupilSettings
    mode_changed     = pyqtSignal(str)     # "live" | "review" (a click)
    auto_requested   = pyqtSignal()        # suggest tracking parameters

    def __init__(self, settings: PupilSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or PupilSettings()
        # (hz, exposure_limited) from the running camera; None before Start.
        self._measured: tuple[float, bool] | None = None
        self._video = self._s.video_path
        # Widgets emit as they're built; `settings` needs all of them.
        self._ready = False
        self._build()
        # Help on the section titles only, not on every control.
        sections_help(self)
        self._ready = True

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # Live or Review: the same tab either way; Review swaps the camera
        # and LED sections for the clip's, and the host fills that page.
        self.mode = SegmentedSwitch([("Live", "live"), ("Review", "review")],
                                    style.HEX["pupil_cam"])
        self.mode.changed.connect(self.mode_changed)
        outer.addWidget(self.mode)
        self._pages = QStackedWidget()
        outer.addWidget(self._pages, 1)
        live = QWidget()
        self._pages.addWidget(live)
        self.review_page = QWidget()
        rl = QVBoxLayout(self.review_page)
        rl.setContentsMargins(0, 0, 0, 0)
        self._pages.addWidget(self.review_page)

        root = QVBoxLayout(live)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Camera ──────────────────────────────────────────────────────────
        cam = QGroupBox("Camera")
        cl = QFormLayout(cam)
        cl.setSpacing(4)
        # Rate is typed; Exposure is a bar from the camera's minimum to the
        # longest that rate allows (1/rate), so it can't slow the camera.
        self._spn_hz = spin(1.0, 200.0, self._s.rate_hz,
                            decimals=1, suffix=" Hz")
        self._exp_min = EXPOSURE_MIN_US
        self._exp = RangeBar(self._exp_min, 1e6 / self._spn_hz.value(),
                             self._s.exposure_us, fmt=_fmt_us)
        self._exp.setToolTip("From the camera's shortest to the longest the "
                             "Rate allows.")
        self._exp.valueChanged.connect(self.exposure_changed)
        rate_row = QHBoxLayout()
        rate_row.setContentsMargins(0, 0, 0, 0)
        rate_row.addWidget(self._spn_hz)
        rate_row.addWidget(QLabel("Exposure"))
        rate_row.addWidget(self._exp, 1)
        cl.addRow("Rate:", rate_row)

        self._lbl_rate = QLabel()
        cl.addRow("Frame rate:", self._lbl_rate)
        self._spn_hz.valueChanged.connect(self._on_hz_changed)
        self._refresh_rate()

        self._chk_lut = QCheckBox("Show LUT")
        self._chk_lut.setChecked(self._s.show_lut)
        self._chk_lut.setToolTip("The brightness/contrast bar.")
        self._chk_lut.toggled.connect(self._emit)

        self._chk_auto = QCheckBox("Auto contrast")
        self._chk_auto.setChecked(self._s.auto_levels)
        self._chk_auto.setToolTip("Off: drag the bar's handles. Display only.")
        self._chk_auto.toggled.connect(self._emit)

        disp_row = QWidget()
        disp_lay = QHBoxLayout(disp_row)
        disp_lay.setContentsMargins(0, 0, 0, 0)
        disp_lay.addWidget(self._chk_lut)
        disp_lay.addWidget(self._chk_auto)
        disp_lay.addStretch()
        cl.addRow("Display:", disp_row)

        self._chk_dark = QCheckBox("Warn if too dark to track")
        self._chk_dark.setChecked(self._s.warn_dark)
        self._chk_dark.setToolTip(
            "From the pupil's contrast with the iris, while it is tracked. "
            "Recordings note it either way.")
        self._chk_dark.toggled.connect(self._emit)
        self._chk_dark.toggled.connect(lambda _on: self.show_contrast(self._contrast))
        cl.addRow("Light:", self._chk_dark)
        self._lbl_dark = QLabel()
        self._lbl_dark.setWordWrap(True)
        cl.addRow("", self._lbl_dark)
        self._cam_form = cl
        cl.setRowVisible(self._lbl_dark, False)
        self._contrast: float | None = None

        # ── Frame source ────────────────────────────────────────────────────
        self._lbl_vid = QLabel()
        self._lbl_vid.setWordWrap(True)
        self._chk_video = QCheckBox("Replay a clip instead")
        self._chk_video.setToolTip("Uncompressed AVI, from the next Live "
                                   "view. Flagged in the session file.")
        self._chk_video.toggled.connect(self._on_video_toggled)
        cl.addRow("Source:", self._chk_video)
        cl.addRow("", self._lbl_vid)
        self._show_video()
        root.addWidget(cam)

        self.tracking = TrackingControls(self._s, live=True)
        self.tracking.changed.connect(self._emit)
        self.tracking.auto_requested.connect(self.auto_requested)
        root.addWidget(self.tracking)

        # ── Illumination ────────────────────────────────────────────────────
        led = QGroupBox("Illumination")
        ll = QVBoxLayout(led)
        self._chk_led = QCheckBox("Eye-tracking LED")
        self._chk_led.toggled.connect(self.led_toggled)
        self._chk_led_follow = QCheckBox("Follow Live view")
        self._chk_led_follow.setChecked(self._s.led_follow_live)
        self._chk_led_follow.setToolTip("On with Live view/Record, off "
                                        "after.")
        self._chk_led_follow.toggled.connect(self._emit)
        ll.addWidget(self._chk_led)
        ll.addWidget(self._chk_led_follow)

        intensity_row = QWidget()
        il = QHBoxLayout(intensity_row)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(QLabel("Intensity:"))
        self._spn_intensity = spin(
            0.0, 100.0, self._s.led_intensity * 100.0, decimals=0, suffix=" %",
            tooltip="Of the LED driver's full scale.")
        self._spn_intensity.valueChanged.connect(
            lambda pct: self.led_intensity_changed.emit(pct / 100.0))
        self._spn_intensity.valueChanged.connect(self._emit)
        il.addWidget(self._spn_intensity)
        il.addStretch()
        ll.addWidget(intensity_row)

        root.addWidget(led)
        root.addStretch()

        self._spn_hz.valueChanged.connect(self._emit)
        self._exp.editingFinished.connect(self._emit)   # not every drag step

    # ── rate / exposure ──────────────────────────────────────────────────────
    def _on_hz_changed(self, hz: float) -> None:
        """The bar's long end follows the rate; a longer exposure is pulled in."""
        self._exp.setRange(self._exp_min, 1e6 / hz if hz > 0 else 1e6)
        self._refresh_rate()

    def set_exposure_min(self, us: float) -> None:
        """The camera's own shortest exposure, once it is open."""
        if us and us > 0 and abs(us - self._exp_min) > 1e-6:
            self._exp_min = float(us)
            self._exp.setRange(self._exp_min, self._exp.maximum())

    def _refresh_rate(self) -> None:
        if self._measured is not None:
            hz, limited = self._measured
            note = " (exposure-limited)" if limited else ""
            self._lbl_rate.setText(f"{hz:.1f} Hz — measured by camera{note}")
            self._lbl_rate.setStyleSheet("color:#2e7d32; font-weight:bold;")
            return
        rate = self._spn_hz.value()
        exp_us = self._exp.value()
        exp_hz = 1e6 / exp_us if exp_us > 0 else rate
        if exp_hz < rate - 1e-6:
            self._lbl_rate.setText(
                f"{exp_hz:.1f} Hz — limited by exposure "
                f"(use ≤{1e6 / rate:.0f} µs for {rate:g} Hz)")
            self._lbl_rate.setStyleSheet("color:#c47f00; font-weight:bold;")
        else:
            self._lbl_rate.setText(f"{rate:.1f} Hz — the requested rate")
            self._lbl_rate.setStyleSheet("color:#2e7d32;")

    def set_measured_rate(self, hz: float | None,
                          exposure_limited: bool = False) -> None:
        """The camera's measured rate; None reverts to the requested one."""
        self._measured = None if hz is None else (float(hz), bool(exposure_limited))
        self._refresh_rate()

    # ── the tracking controls (shared with Pupil review) ─────────────────────
    def set_limit(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """From the preview, as ONE settings change."""
        self.tracking.set_limit(x0, y0, x1, y1)

    def set_region_locked(self, on: bool) -> None:
        self.tracking.set_locked(on)

    def set_pins(self, pins) -> None:
        """From the preview, as ONE settings change."""
        self.tracking.set_pins(pins)

    def clear_pins(self) -> None:
        self.tracking.clear_pins()

    def apply_auto(self, s: PupilSettings) -> None:
        """Suggested tracking values as ONE settings change."""
        self.tracking.apply_auto(s)

    def show_mode(self, mode: str) -> None:
        """Show the Live or the Review page (the host decides if allowed)."""
        self.mode.set_value(mode)
        self._pages.setCurrentIndex(0 if mode == "live" else 1)

    def set_auto_busy(self, busy: bool) -> None:
        self.tracking.set_auto_busy(busy)

    def _emit(self, *_a) -> None:
        if not self._ready:
            return
        self.settings_changed.emit(self.settings)

    # ── frame source ─────────────────────────────────────────────────────────
    def _on_video_toggled(self, on: bool) -> None:
        """Checking asks for a clip; cancelling reverts to unchecked/camera."""
        if on:
            self._pick_video()
            if not self._video:
                self._chk_video.blockSignals(True)
                self._chk_video.setChecked(False)
                self._chk_video.blockSignals(False)
        else:
            self._set_video("")

    def _pick_video(self) -> None:
        start = str(Path(self._video).parent) if self._video else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pupil footage to replay", start, _VIDEO_FILTER)
        if path:                        # "" = cancelled; must not clear
            self._set_video(path)

    def _set_video(self, path: str) -> None:
        self._video = path
        self._show_video()
        self._emit()

    def _show_video(self) -> None:
        self._lbl_vid.setText(Path(self._video).name if self._video
                              else "camera (live)")
        self._chk_video.blockSignals(True)
        self._chk_video.setChecked(bool(self._video))
        self._chk_video.blockSignals(False)

    @property
    def settings(self) -> PupilSettings:
        """Everything the panel holds; the adapter persists exactly this."""
        return self.tracking.settings_into(PupilSettings(
            exposure_us=self._exp.value(),
            rate_hz=self._spn_hz.value(),
            video_path=self._video,
            show_lut=self._chk_lut.isChecked(),
            auto_levels=self._chk_auto.isChecked(),
            warn_dark=self._chk_dark.isChecked(),
            led_follow_live=self._chk_led_follow.isChecked(),
            led_intensity=self._spn_intensity.value() / 100.0,
        ))

    def show_contrast(self, contrast: float | None) -> None:
        """Pupil-iris contrast (grey levels) from tracking; None = unknown.
        Only says something when it is too low and the warning is on."""
        self._contrast = contrast
        low = (contrast is not None and contrast < LOW_CONTRAST
               and self._chk_dark.isChecked())
        if low:
            self._lbl_dark.setText(
                f"Too dark: the pupil is {contrast:.1f} grey levels darker "
                f"than the iris. Raise exposure or the LED.")
            self._lbl_dark.setStyleSheet("color:#c47f00; font-weight:bold;")
        self._cam_form.setRowVisible(self._lbl_dark, low)

    def set_led(self, on: bool) -> None:
        """Show the LED's state without re-emitting `led_toggled`."""
        self._chk_led.blockSignals(True)
        self._chk_led.setChecked(on)
        self._chk_led.blockSignals(False)
