"""Pupil camera — the Qt settings panel. The model is in `settings.py`."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp.devices.pupil_cam.settings import PupilSettings

_VIDEO_FILTER = "Uncompressed AVI (*.avi);;All files (*)"


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
        # Held as plain state, not a widget value: it is a path, and the label is
        # only its basename.
        self._video = self._s.video_path
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

        # ── Frame source ────────────────────────────────────────────────────
        # Replaying a clip is how the tracker gets tuned against a real eye
        # without an animal: the mock's clean disc cannot show whether a
        # setting survives fur, lashes and the glint.
        self._lbl_vid = QLabel()
        self._lbl_vid.setWordWrap(True)
        btn_vid = QPushButton("Sample video…")
        btn_vid.setToolTip(
            "Replay a recorded clip instead of the camera, to tune tracking on "
            "real footage.\nUncompressed AVI only (IYUV/I420/YV12, Y800 or "
            "BI_RGB) — this venv has no video decoder.\n"
            "Takes effect on the next Live view; a session recorded from a clip "
            "is flagged in the file's metadata.")
        btn_vid.clicked.connect(self._pick_video)
        self._btn_vid_clear = QPushButton("Use camera")
        self._btn_vid_clear.setToolTip("Go back to the camera (or the mock).")
        self._btn_vid_clear.clicked.connect(lambda: self._set_video(""))
        vrow = QHBoxLayout()
        vrow.setContentsMargins(0, 0, 0, 0)
        vrow.addWidget(btn_vid)
        vrow.addWidget(self._btn_vid_clear)
        cl.addRow("Source:", self._lbl_vid)
        cl.addRow("", vrow)
        self._show_video()
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

        # Everything below is TUNING. On a working rig the operator touches
        # threshold, the two radii and the overlay; these ten were found once
        # against real footage and then left alone. Collapsed by default so
        # the tab shows the four controls that are actually used — and
        # auto-expanded below if any of them differs from its default, so a
        # non-standard setting can never hide behind a closed group.
        self._adv = QGroupBox("Advanced tracking")
        self._adv.setCheckable(True)
        self._adv.setToolTip("Edge search, fitting and temporal smoothing. "
                             "Defaults are tuned to this rig's IR footage.")
        _advbox = QVBoxLayout(self._adv)
        _advbox.setContentsMargins(6, 4, 6, 4)
        self._adv_host = QWidget()
        al = QFormLayout(self._adv_host)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(4)
        _advbox.addWidget(self._adv_host)
        self._adv.toggled.connect(self._adv_host.setVisible)

        # ── annulus edge search (the IMAQ Find Circular Edge controls) ──
        self._spn_rays = QSpinBox()
        self._spn_rays.setRange(8, 360)
        self._spn_rays.setValue(self._s.n_rays)
        self._spn_rays.setToolTip(
            "Search lines cast outward through the annulus. More rays give a "
            "steadier fit and cost time roughly linearly.")
        al.addRow("Search lines:", self._spn_rays)

        self._cmb_pol = QComboBox()
        self._cmb_pol.addItems(["rising", "falling", "any"])
        self._cmb_pol.setCurrentText(self._s.polarity)
        self._cmb_pol.setToolTip(
            "Edge polarity scanning outward.\n"
            "rising  — dark pupil on a brighter iris (the usual IR setup)\n"
            "falling — bright pupil (retro-illumination)\n"
            "any     — strongest transition either way")
        al.addRow("Edge polarity:", self._cmb_pol)

        self._spn_str = QDoubleSpinBox()
        self._spn_str.setRange(0.0, 100.0)
        self._spn_str.setDecimals(1)
        self._spn_str.setSingleStep(0.5)
        self._spn_str.setValue(self._s.min_strength)
        self._spn_str.setToolTip(
            "Minimum intensity gradient (grey levels per px) for a ray to "
            "count as having found the pupil edge. Raise it if eyelashes or "
            "noise are being accepted; lower it if the pupil is low-contrast.")
        al.addRow("Min edge strength:", self._spn_str)

        self._cmb_edge = QComboBox()
        self._cmb_edge.addItems(["first", "strongest"])
        self._cmb_edge.setCurrentText(self._s.edge_select)
        self._cmb_edge.setToolTip(
            "Which edge along each ray is taken as the pupil boundary.\n"
            "first     — the innermost sustained edge. Correct by construction: "
            "scanning outward from inside the pupil, its rim is what you meet "
            "first.\n"
            "strongest — the largest gradient on the ray. Only safe when the "
            "pupil edge is the highest-contrast thing around; on real IR "
            "footage the eyelid/fur margin is a ~200 grey-level step against "
            "the pupil's ~30, so the fit lands on the eyelid.")
        al.addRow("Edge choice:", self._cmb_edge)

        self._spn_sigma = QDoubleSpinBox()
        self._spn_sigma.setRange(0.0, 50.0)
        self._spn_sigma.setDecimals(1)
        self._spn_sigma.setSingleStep(0.5)
        self._spn_sigma.setSuffix(" px")
        self._spn_sigma.setValue(self._s.smooth_sigma)
        self._spn_sigma.setToolTip(
            "Smoothing along each ray before the gradient is taken. Raise it "
            "if fur or lash texture is being taken for the pupil edge, lower it "
            "if a small pupil's edge is being smoothed away. 1.5 tracks the "
            "rig's full-frame footage as it stands.")
        al.addRow("Edge smoothing:", self._spn_sigma)

        self._spn_conf = QDoubleSpinBox()
        self._spn_conf.setRange(0.0, 1.0)
        self._spn_conf.setDecimals(2)
        self._spn_conf.setSingleStep(0.01)
        self._spn_conf.setValue(self._s.min_confidence)
        self._spn_conf.setToolTip(
            "Confidence below which the frame counts as lost.\n"
            "confidence = (rays kept / rays cast) x exp(-rms / 2). Only about "
            "half the rays ever find an edge on a real eye — lashes, lids, the "
            "glint — so a good real fit peaks near 0.28 and the old 0.25 "
            "discarded frames whose rms was 1.3 px. Raise it towards 0.3 for a "
            "clean synthetic disc.")
        al.addRow("Min confidence:", self._spn_conf)

        self._cmb_fit = QComboBox()
        self._cmb_fit.addItems(["circle", "ellipse"])
        self._cmb_fit.setCurrentText(self._s.fit)
        self._cmb_fit.setToolTip(
            "Shape least-squares-fitted to the edge points. Ellipse handles an "
            "off-axis eye; radius is then the mean of the semi-axes.")
        al.addRow("Fit shape:", self._cmb_fit)

        self._spn_smed = QSpinBox()
        self._spn_smed.setRange(1, 31)
        self._spn_smed.setSuffix(" frames")
        self._spn_smed.setValue(self._s.smooth_median)
        self._spn_smed.setToolTip(
            "Median window over consecutive tracked frames. 1 disables it.\n"
            "This is what stops the outline jumping: a half-closed lid throws "
            "the fit off for one or two frames and it returns, which a median "
            "of 3 removes outright.\n"
            "It never runs across a blink — lost frames stay lost, so a blink "
            "still shows in the trace instead of being smoothed away.")
        al.addRow("Smoothing window:", self._spn_smed)

        self._spn_sema = QDoubleSpinBox()
        self._spn_sema.setRange(0.05, 1.0)
        self._spn_sema.setDecimals(2)
        self._spn_sema.setSingleStep(0.05)
        self._spn_sema.setValue(self._s.smooth_ema)
        self._spn_sema.setToolTip(
            "Weight given to the newest frame after the median. 1.0 disables "
            "the EMA; lower is smoother but lags real dilation. 0.5 settles "
            "within about three frames.")
        al.addRow("Smoothing weight:", self._spn_sema)

        self._spn_reseed = QSpinBox()
        self._spn_reseed.setRange(1, 600)
        self._spn_reseed.setSuffix(" frames")
        self._spn_reseed.setValue(self._s.reseed_after)
        self._spn_reseed.setToolTip(
            "How long the eye may stay shut before the tracker re-thresholds "
            "the whole frame looking for the pupil.\n"
            "Even then it keeps the pre-blink position if that search finds "
            "nothing, so a reopening eye is picked up where it left off. Raise "
            "it if long blinks cost the lock; lower it if a pupil that really "
            "moved takes too long to be found again.")
        al.addRow("Re-seed after:", self._spn_reseed)

        # Naming click-to-seed in the LABEL, not just the tooltip: on this rig
        # the auto-seed cannot work — at threshold 60 the dark mask exceeds half
        # the sensor and `coarse_seed` bails — so seeding by hand IS the
        # operating procedure, and this checkbox is the only way to reach it.
        self._chk_search = QCheckBox("Show search overlay — click the preview "
                                     "to place the pupil")
        self._chk_search.setChecked(self._s.show_search)
        self._chk_search.setToolTip(
            "Draw the annulus the rays sweep and every edge point they found — "
            "green kept, red rejected as an outlier — over the pupil preview.\n"
            "A wrong radius usually shows as rays latching onto an eyelash or a "
            "glint, which the fitted circle alone cannot tell you.\n"
            "While it is on, clicking the preview places the annulus by hand. "
            "On this rig's framing that is the normal way to start tracking: "
            "the automatic seed gives up when the dark region covers more than "
            "half the frame.")
        tl.addRow(self._chk_search)
        root.addWidget(trk)
        root.addWidget(self._adv)

        # ── Illumination ────────────────────────────────────────────────────────
        led = QGroupBox("Illumination")
        ll = QVBoxLayout(led)
        self._chk_led = QCheckBox("Eye-tracking LED")
        self._chk_led.toggled.connect(self.led_toggled)
        ll.addWidget(self._chk_led)
        root.addWidget(led)
        root.addStretch()

        for w in (self._spn_exp, self._spn_fps, self._spn_thr, self._spn_min,
                  self._spn_max, self._spn_rays, self._spn_str,
                  self._spn_sigma, self._spn_conf, self._spn_smed,
                  self._spn_sema, self._spn_reseed):
            w.valueChanged.connect(self._emit)
        for c in (self._cmb_pol, self._cmb_fit, self._cmb_edge):
            c.currentTextChanged.connect(self._emit)
        self._chk_search.toggled.connect(self._emit)

        # Start collapsed, unless this rig is running something non-default —
        # a tuned value hidden behind a closed group is worse than a long tab.
        tuned = self._non_default_advanced()
        self._adv.setChecked(bool(tuned))
        self._adv_host.setVisible(bool(tuned))
        if tuned:
            self._adv.setTitle(f"Advanced tracking — {len(tuned)} changed "
                               f"({', '.join(tuned[:3])}"
                               f"{', …' if len(tuned) > 3 else ''})")

    _ADVANCED = ("n_rays", "polarity", "min_strength", "edge_select",
                 "smooth_sigma", "min_confidence", "fit", "smooth_median",
                 "smooth_ema", "reseed_after")

    def _non_default_advanced(self) -> list[str]:
        """Which advanced settings differ from the shipped defaults."""
        base = PupilSettings()
        return [k for k in self._ADVANCED
                if getattr(self._s, k) != getattr(base, k)]

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    # ── frame source ─────────────────────────────────────────────────────────
    def _pick_video(self) -> None:
        start = str(Path(self._video).parent) if self._video else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pupil footage to replay", start, _VIDEO_FILTER)
        if path:                       # empty = cancelled, which must not clear
            self._set_video(path)

    def _set_video(self, path: str) -> None:
        self._video = path
        self._show_video()
        self._emit()

    def _show_video(self) -> None:
        self._lbl_vid.setText(Path(self._video).name if self._video
                              else "camera (live)")
        self._lbl_vid.setToolTip(self._video)
        self._btn_vid_clear.setEnabled(bool(self._video))

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
            "n_rays":         self._spn_rays.value(),
            "polarity":       self._cmb_pol.currentText(),
            "min_strength":   self._spn_str.value(),
            "fit":            self._cmb_fit.currentText(),
            "edge_select":    self._cmb_edge.currentText(),
            "smooth_sigma":   self._spn_sigma.value(),
            "min_confidence": self._spn_conf.value(),
            "smooth_median":  self._spn_smed.value(),
            "smooth_ema":     self._spn_sema.value(),
            "reseed_after":   self._spn_reseed.value(),
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
            edge_select=self._cmb_edge.currentText(),
            smooth_sigma=self._spn_sigma.value(),
            min_confidence=self._spn_conf.value(),
            smooth_median=self._spn_smed.value(),
            smooth_ema=self._spn_sema.value(),
            reseed_after=self._spn_reseed.value(),
            show_search=self._chk_search.isChecked(),
            video_path=self._video,
        )
