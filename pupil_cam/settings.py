"""
Pupil camera — settings dataclass + Qt panel.

Bundles the camera (exposure, frame rate), the pupil-tracking parameters
(threshold + radius bounds fed to tracking.detect), and the eye-tracking LED
toggle into one settings tab.
"""
from __future__ import annotations
from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QSpinBox,
    QVBoxLayout, QWidget,
)


@dataclass
class PupilSettings:
    exposure_us: float = 8000.0
    fps:         float = 20.0
    threshold:   int   = 60      # seed-blob threshold for tracking.detect
    min_r:       int   = 10      # px
    max_r:       int   = 80      # px
    # ── annulus edge search (tracking.find_circular_edge) ──
    n_rays:       int   = 64        # search lines through the annulus
    polarity:     str   = "rising"  # dark pupil → bright iris, scanning outward
    min_strength: float = 4.0       # min |gradient| (grey levels/px) per ray
    fit:          str   = "circle"  # "circle" or "ellipse"
    # Display-only: draw the annulus and per-ray edge points over the preview,
    # and allow click-to-seed. Persisted (it is a working preference, not
    # runtime state like the LED) but deliberately absent from the session
    # metadata — how the operator was looking at the fit is not a property of
    # the recording.
    show_search:  bool  = False


class SettingsPanel(QWidget):
    exposure_changed = pyqtSignal(float)   # hot-applied to the worker while running
    led_toggled      = pyqtSignal(bool)    # eye-tracking illumination on/off
    # Any parameter edit. The LED is deliberately NOT one of these: it is
    # runtime state, and restoring it at launch would turn the illumination on
    # in an empty rig.
    settings_changed = pyqtSignal(object)  # emits PupilSettings

    def __init__(self, settings: PupilSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or PupilSettings()
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Camera ──────────────────────────────────────────────────────────────
        cam = QGroupBox("Camera")
        cl = QFormLayout(cam)
        cl.setSpacing(4)
        self._spn_exp = QDoubleSpinBox()
        self._spn_exp.setRange(50.0, 100_000.0)
        self._spn_exp.setDecimals(0)
        self._spn_exp.setSuffix(" µs")
        self._spn_exp.setValue(self._s.exposure_us)
        self._spn_exp.valueChanged.connect(self.exposure_changed)
        cl.addRow("Exposure:", self._spn_exp)

        self._spn_fps = QDoubleSpinBox()
        self._spn_fps.setRange(1.0, 200.0)
        self._spn_fps.setSuffix(" fps")
        self._spn_fps.setValue(self._s.fps)
        cl.addRow("Frame rate:", self._spn_fps)
        root.addWidget(cam)

        # ── Pupil tracking ──────────────────────────────────────────────────────
        trk = QGroupBox("Pupil tracking")
        tl = QFormLayout(trk)
        tl.setSpacing(4)
        self._spn_thr = QSpinBox()
        self._spn_thr.setRange(0, 255)
        self._spn_thr.setValue(self._s.threshold)
        tl.addRow("Threshold:", self._spn_thr)

        self._spn_min = QSpinBox()
        self._spn_min.setRange(1, 500)
        self._spn_min.setSuffix(" px")
        self._spn_min.setValue(self._s.min_r)
        tl.addRow("Min radius:", self._spn_min)

        self._spn_max = QSpinBox()
        self._spn_max.setRange(2, 1000)
        self._spn_max.setSuffix(" px")
        self._spn_max.setValue(self._s.max_r)
        tl.addRow("Max radius:", self._spn_max)

        # ── annulus edge search (the IMAQ Find Circular Edge controls) ──
        self._spn_rays = QSpinBox()
        self._spn_rays.setRange(8, 360)
        self._spn_rays.setValue(self._s.n_rays)
        self._spn_rays.setToolTip(
            "Search lines cast outward through the annulus. More rays give a "
            "steadier fit and cost time roughly linearly.")
        tl.addRow("Search lines:", self._spn_rays)

        self._cmb_pol = QComboBox()
        self._cmb_pol.addItems(["rising", "falling", "any"])
        self._cmb_pol.setCurrentText(self._s.polarity)
        self._cmb_pol.setToolTip(
            "Edge polarity scanning outward.\n"
            "rising  — dark pupil on a brighter iris (the usual IR setup)\n"
            "falling — bright pupil (retro-illumination)\n"
            "any     — strongest transition either way")
        tl.addRow("Edge polarity:", self._cmb_pol)

        self._spn_str = QDoubleSpinBox()
        self._spn_str.setRange(0.0, 100.0)
        self._spn_str.setDecimals(1)
        self._spn_str.setSingleStep(0.5)
        self._spn_str.setValue(self._s.min_strength)
        self._spn_str.setToolTip(
            "Minimum intensity gradient (grey levels per px) for a ray to "
            "count as having found the pupil edge. Raise it if eyelashes or "
            "noise are being accepted; lower it if the pupil is low-contrast.")
        tl.addRow("Min edge strength:", self._spn_str)

        self._cmb_fit = QComboBox()
        self._cmb_fit.addItems(["circle", "ellipse"])
        self._cmb_fit.setCurrentText(self._s.fit)
        self._cmb_fit.setToolTip(
            "Shape least-squares-fitted to the edge points. Ellipse handles an "
            "off-axis eye; radius is then the mean of the semi-axes.")
        tl.addRow("Fit shape:", self._cmb_fit)

        self._chk_search = QCheckBox("Show search overlay")
        self._chk_search.setChecked(self._s.show_search)
        self._chk_search.setToolTip(
            "Draw the annulus the rays sweep and every edge point they found — "
            "green kept, red rejected as an outlier — over the pupil preview.\n"
            "A wrong radius usually shows as rays latching onto an eyelash or a "
            "glint, which the fitted circle alone cannot tell you.\n"
            "While it is on, clicking the preview places the annulus by hand.")
        tl.addRow(self._chk_search)
        root.addWidget(trk)

        # ── Illumination ────────────────────────────────────────────────────────
        led = QGroupBox("Illumination")
        ll = QVBoxLayout(led)
        self._chk_led = QCheckBox("Eye-tracking LED")
        self._chk_led.toggled.connect(self.led_toggled)
        ll.addWidget(self._chk_led)
        root.addWidget(led)
        root.addStretch()

        for w in (self._spn_exp, self._spn_fps, self._spn_thr, self._spn_min,
                  self._spn_max, self._spn_rays, self._spn_str):
            w.valueChanged.connect(self._emit)
        for c in (self._cmb_pol, self._cmb_fit):
            c.currentTextChanged.connect(self._emit)
        self._chk_search.toggled.connect(self._emit)

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def track_params(self) -> tuple[int, int, int]:
        """(threshold, min_r, max_r) for tracking.detect — cheap per-frame read."""
        return (self._spn_thr.value(), self._spn_min.value(), self._spn_max.value())

    @property
    def track_kwargs(self) -> dict:
        """Annulus edge-search options for tracking.detect / PupilTracker.

        Read every display tick alongside track_params, so it stays a plain
        dict of widget values rather than rebuilding the dataclass at 30 Hz.
        """
        return {
            "n_rays":       self._spn_rays.value(),
            "polarity":     self._cmb_pol.currentText(),
            "min_strength": self._spn_str.value(),
            "fit":          self._cmb_fit.currentText(),
        }

    @property
    def settings(self) -> PupilSettings:
        return PupilSettings(
            exposure_us=self._spn_exp.value(),
            fps=self._spn_fps.value(),
            threshold=self._spn_thr.value(),
            min_r=self._spn_min.value(),
            max_r=self._spn_max.value(),
            n_rays=self._spn_rays.value(),
            polarity=self._cmb_pol.currentText(),
            min_strength=self._spn_str.value(),
            fit=self._cmb_fit.currentText(),
            show_search=self._chk_search.isChecked(),
        )
