"""Pupil camera — the Qt settings panel. The model is in `settings.py`.

Camera, frame source, eye region and LED.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
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
        self._build()

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
        return PupilSettings(
            exposure_us=self._spn_exp.value(),
            fps=self._spn_fps.value(),
            limit_x=self._spn_lx.value(),
            limit_y=self._spn_ly.value(),
            limit_r=self._spn_lr.value(),
            video_path=self._video,
        )
