"""Pupil camera — the Qt settings panel. The model is in `settings.py`.

Camera, frame source, eye region, tracking, corneal reflection and LED.

Every control here writes into `settings`, and `settings` is what the adapter
persists — so a knob that is not read back in that property is a knob the
operator loses at the next launch. That has happened; keep them in step.
"""
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
        self._video = self._s.video_path     # held as state, shown by basename
        # Placed on the preview, so held here rather than in a widget — the
        # label and the Clear button are only a readout of this list.
        self._pins = list(self._s.cr_pins)
        # Widgets emit as they are built, and `settings` reads all of them —
        # so nothing is emitted until every group exists.
        self._ready = False
        self._build()
        self._ready = True

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Camera ──────────────────────────────────────────────────────────
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
        self._lbl_vid = QLabel()
        self._lbl_vid.setWordWrap(True)
        btn_vid = QPushButton("Sample video…")
        btn_vid.setToolTip(
            "Replay a recorded clip instead of the camera.\nUncompressed AVI "
            "only (IYUV/I420/YV12, Y800 or BI_RGB) — this venv has no decoder.\n"
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

        root.addWidget(self._build_limit())
        root.addWidget(self._build_track())
        root.addWidget(self._build_cr())

        # ── Illumination ────────────────────────────────────────────────────
        led = QGroupBox("Illumination")
        ll = QVBoxLayout(led)
        self._chk_led = QCheckBox("Eye-tracking LED")
        self._chk_led.toggled.connect(self.led_toggled)
        ll.addWidget(self._chk_led)
        root.addWidget(led)
        root.addStretch()

        for w in (self._spn_exp, self._spn_fps):
            w.valueChanged.connect(self._emit)

    # ── eye region ───────────────────────────────────────────────────────────
    def _build_limit(self) -> QGroupBox:
        """The circle the eye sits in — the numbers only.

        **It is placed on the preview, not here**: a region of the frame is
        picked by looking at the frame. These three exist for typing an exact
        circle and for reading back the one in force, and are what persists and
        what the session file records.
        """
        box = QGroupBox("Eye region")
        box.setToolTip(
            "The eye only ever appears in one part of the frame on a head-fixed "
            "animal. Radius 0 = no region.")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        hint = QLabel("Set it on the pupil preview — the buttons above the "
                      "image. These are for typing an exact circle.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa0a6;")
        vb.addWidget(hint)

        self._spn_lx, self._spn_ly, self._spn_lr = (self._px_spin(v) for v in
                                                    (self._s.limit_x,
                                                     self._s.limit_y,
                                                     self._s.limit_r))
        # One row, not three form rows: it is a single circle, and three labelled
        # rows made a numeric-entry form out of something spatial.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for label, w in (("X", self._spn_lx), ("Y", self._spn_ly),
                         ("R", self._spn_lr)):
            row.addWidget(QLabel(label))
            row.addWidget(w, 1)
        self._btn_limit_clear = QPushButton("Clear")
        self._btn_limit_clear.setToolTip("No region.")
        self._btn_limit_clear.clicked.connect(self.clear_limit)
        row.addWidget(self._btn_limit_clear)
        vb.addLayout(row)

        for w in (self._spn_lx, self._spn_ly, self._spn_lr):
            w.valueChanged.connect(self._limit_edited)
        self._limit_edited()
        return box

    # ── tracking ─────────────────────────────────────────────────────────
    def _build_track(self) -> QGroupBox:
        """The EyeLoop knobs. Off by default — see `settings.py` for why.

        Threshold is the one that matters: it sets the reported radius (a 60 %
        swing over 25-60 on the rig clips) at an unchanged 151/151 fit rate, so
        no fit-rate readout will tell the operator it is wrong. It is
        illumination-dependent and belongs to a session, not to the rig.
        """
        box = QGroupBox("Pupil tracking")
        box.setToolTip(
            "Fits an ellipse to the pupil inside the eye region, which is the "
            "crop it needs — without a region nothing is tracked.")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        self._chk_track = QCheckBox("Track the pupil")
        self._chk_track.setChecked(self._s.track)
        self._chk_track.setToolTip(
            "Needs an EyeLoop clone beside the repo (docs/EYELOOP.md). Without "
            "one the camera runs exactly as before and the preview says so.")
        vb.addWidget(self._chk_track)

        form = QFormLayout()
        form.setSpacing(4)
        self._spn_thr = self._int_spin(self._s.track_threshold, 1, 254)
        self._spn_thr.setToolTip(
            "Pixels darker than this are pupil. THE consequential number: it "
            "sets the radius, and a wrong one still fits every frame.")
        form.addRow("Threshold:", self._spn_thr)

        self._spn_blur = self._int_spin(self._s.track_blur, 1, 21, step=2)
        self._spn_blur.setToolTip("Blur kernel before thresholding; odd only.")
        form.addRow("Blur:", self._spn_blur)

        self._cmb_model = QComboBox()
        for label, key in (("Ellipse", "ellipsoid"), ("Circle", "circular")):
            self._cmb_model.addItem(label, key)
        i = self._cmb_model.findData(self._s.track_model)
        self._cmb_model.setCurrentIndex(i if i >= 0 else 0)
        self._cmb_model.setToolTip(
            "Circle is ~2.5x cheaper and just as steady on the test clips; the "
            "ellipse is what EyeLoop was adopted for.")
        form.addRow("Model:", self._cmb_model)
        vb.addLayout(form)

        self._chk_track.toggled.connect(self._emit)
        self._cmb_model.currentIndexChanged.connect(self._emit)
        for w in (self._spn_thr, self._spn_blur):
            w.valueChanged.connect(self._emit)
        return box

    # ── corneal reflection ───────────────────────────────────────────────
    def _build_cr(self) -> QGroupBox:
        """Removal of the IR reflections, done here because EyeLoop's own never
        runs (disabled upstream in three places at once).

        The gain is real but clip-dependent — it tightened radius scatter by
        0.9 px on one rig clip and could not safely reach the reflection on the
        other. The defaults are chosen not to inflate the radius.
        """
        box = QGroupBox("Corneal reflection")
        vb = QVBoxLayout(box)
        vb.setSpacing(4)

        self._chk_cr = QCheckBox("Remove reflections")
        self._chk_cr.setChecked(self._s.cr_remove)
        vb.addWidget(self._chk_cr)

        form = QFormLayout()
        form.setSpacing(4)
        self._spn_cr_thr = self._int_spin(self._s.cr_threshold, 1, 254)
        self._spn_cr_thr.setToolTip(
            "Brighter than this is a reflection. Not a delicate number: the "
            "pupil sits near 22 and the glints saturate at 235.")
        form.addRow("Threshold:", self._spn_cr_thr)

        self._spn_cr_pad = self._int_spin(self._s.cr_pad, 0, 20)
        self._spn_cr_pad.setToolTip(
            "Grow each blob — the spikes are wider than the core.")
        form.addRow("Pad:", self._spn_cr_pad)

        self._spn_cr_ring = self._int_spin(self._s.cr_ring, 1, 40)
        self._spn_cr_ring.setToolTip(
            "Width of the annulus each blob is filled from.")
        form.addRow("Ring:", self._spn_cr_ring)

        self._spn_cr_reach = QDoubleSpinBox()
        self._spn_cr_reach.setRange(0.10, 1.20)
        self._spn_cr_reach.setSingleStep(0.05)
        self._spn_cr_reach.setDecimals(2)
        self._spn_cr_reach.setKeyboardTracking(False)
        self._spn_cr_reach.setValue(self._s.cr_reach)
        self._spn_cr_reach.setToolTip(
            "How far out to look, as a fraction of the fitted ellipse. Past "
            "~0.85 it masks the rim, which erases the pupil boundary and "
            "INFLATES the radius — the failure looks like a good fit.")
        form.addRow("Reach:", self._spn_cr_reach)
        vb.addLayout(form)

        self._chk_cr_mask = QCheckBox("Show what was removed")
        self._chk_cr_mask.setChecked(self._s.cr_show_mask)
        self._chk_cr_mask.setToolTip(
            "Paints the removed pixels red on the preview. This is how the "
            "threshold is set.")
        vb.addWidget(self._chk_cr_mask)

        # Pins are placed on the preview, like the eye region and for the same
        # reason: they are positions in the frame.
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        self._lbl_pins = QLabel()
        self._lbl_pins.setStyleSheet("color:#9aa0a6;")
        self._btn_pins_clear = QPushButton("Clear pins")
        self._btn_pins_clear.setToolTip(
            "Pinned reflections are rig geometry — clear them when the optics "
            "move, or they mark places nothing reflects any more.")
        self._btn_pins_clear.clicked.connect(self.clear_pins)
        prow.addWidget(self._lbl_pins, 1)
        prow.addWidget(self._btn_pins_clear)
        vb.addLayout(prow)
        self._show_pins()

        self._chk_cr.toggled.connect(self._emit)
        self._chk_cr_mask.toggled.connect(self._emit)
        for w in (self._spn_cr_thr, self._spn_cr_pad, self._spn_cr_ring,
                  self._spn_cr_reach):
            w.valueChanged.connect(self._emit)
        return box

    # ── pins (placed on the preview) ─────────────────────────────────────
    def set_pins(self, pins) -> None:
        """Write the pin list in from the preview, as ONE settings change."""
        self._pins = [tuple(float(v) for v in pin) for pin in pins]
        self._show_pins()
        self._emit()

    def clear_pins(self) -> None:
        self.set_pins([])

    def _show_pins(self) -> None:
        n = len(self._pins)
        self._lbl_pins.setText(
            "no pinned reflections" if not n
            else f"{n} pinned reflection{'s' if n > 1 else ''}")
        self._btn_pins_clear.setEnabled(bool(n))

    @staticmethod
    def _int_spin(value: int, lo: int, hi: int, step: int = 1) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        # As in _px_spin: typing "120" would otherwise save at 1, 12 and 120.
        s.setKeyboardTracking(False)
        s.setValue(int(value))
        return s

    @staticmethod
    def _px_spin(value: float) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(0.0, 20_000.0)       # any sensor, and 0 = off for the radius
        s.setDecimals(0)
        s.setSuffix(" px")
        # Typing "150" would otherwise emit at 1, 15 and 150 — three saves for
        # one edit, with the circle jumping across the frame on the way.
        s.setKeyboardTracking(False)
        s.setValue(value)
        return s

    def _limit_edited(self, *_a) -> None:
        self._btn_limit_clear.setEnabled(self._spn_lr.value() > 0.0)
        self._emit()

    def set_limit(self, cx: float, cy: float, r: float) -> None:
        """Write the circle in from the preview, as ONE settings change."""
        for w, v in ((self._spn_lx, cx), (self._spn_ly, cy), (self._spn_lr, r)):
            w.blockSignals(True)
            w.setValue(float(v))
            w.blockSignals(False)
        self._limit_edited()

    def clear_limit(self) -> None:
        self.set_limit(0.0, 0.0, 0.0)

    def _emit(self, *_a) -> None:
        if not self._ready:             # mid-build: not a whole panel yet
            return
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
    def settings(self) -> PupilSettings:
        """Everything the panel holds. The adapter persists exactly this."""
        return PupilSettings(
            exposure_us=self._spn_exp.value(),
            fps=self._spn_fps.value(),
            limit_x=self._spn_lx.value(),
            limit_y=self._spn_ly.value(),
            limit_r=self._spn_lr.value(),
            video_path=self._video,
            track=self._chk_track.isChecked(),
            track_threshold=self._spn_thr.value(),
            track_blur=self._spn_blur.value(),
            track_model=self._cmb_model.currentData(),
            cr_remove=self._chk_cr.isChecked(),
            cr_threshold=self._spn_cr_thr.value(),
            cr_pad=self._spn_cr_pad.value(),
            cr_ring=self._spn_cr_ring.value(),
            cr_reach=self._spn_cr_reach.value(),
            cr_pins=list(self._pins),
            cr_show_mask=self._chk_cr_mask.isChecked(),
        )
