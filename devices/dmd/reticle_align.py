"""Calibrate by eye: the live camera with a fixed reticle, and the DMD lighting
the same shape through a guessed registration. The operator steers the guess
(centre, then angle, then X/Y size) until the light sits on the reticle.

Needs no fine pattern and no fit, so it survives the scattering that sank the
coarse-stripe decode (REFERENCE §5b). The light is on while this is open; the
caller ends dark.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QKeyCombination, Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
                             QFormLayout, QHBoxLayout, QLabel, QPushButton,
                             QRadioButton, QVBoxLayout)

from acqApp import style
from acqApp.devices.dmd.calibration import (RETICLE_BOX, RETICLE_KINDS,
                                            DmdCalibration, ReticlePose,
                                            manual_seed, reticle_frame,
                                            reticle_outline)
from acqApp.devices.dmd.roi_panel import snapshot_levels
from acqApp.widgets import hrow, spin

_RETICLE_PEN = pg.mkPen("#00d0ff", width=2)
_CROSS_PEN = pg.mkPen("#00d0ff", width=1, style=Qt.PenStyle.DashLine)
_LIVE_MS = 50
_PROJECT_MS = 40            # coalesces a held arrow key into one upload per tick

_STEP_HELP = {
    "centre": "Move the disc onto the circle. Arrows move it along the DMD's "
              "own axes, which may look rotated or flipped here.",
    "angle": "Turn the L onto the outline (←/→). If both arms can't match at "
             "any angle, tick Mirrored.",
    "size": "Stretch the box onto the outline: ←/→ width, ↑/↓ height.",
}


class ReticleAlignDialog(QDialog):
    """Steer a `ReticlePose` until the projected shape lands on the reticle."""

    def __init__(self, project: Callable[[np.ndarray], None],
                 dmd_size: tuple[int, int], frame: np.ndarray, *,
                 live_source: Callable[[], object] | None = None,
                 seed: DmdCalibration | None = None, parent=None):
        """`project` takes a device-sized frame; `seed` (same camera size)
        is where the guess starts, else a centred rectangle."""
        super().__init__(parent)
        self._project = project
        self._dmd_size = tuple(dmd_size)
        f = np.asarray(frame)
        self._cam_size = (f.shape[1], f.shape[0])
        if seed is None or tuple(seed.cam_size) != self._cam_size:
            seed = None
        self._seed = seed
        self._start = ReticlePose.from_calibration(
            seed or manual_seed(self._dmd_size, self._cam_size))
        self._live_source = live_source
        self._last_frame = frame
        self._result: DmdCalibration | None = None
        self.setWindowTitle("Align DMD to camera reticle")
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(1100, 720)

        self._push = QTimer(self, singleShot=True, interval=_PROJECT_MS)
        self._push.timeout.connect(self._send)
        self._build(f)
        self._set_pose(self._start)
        self._timer = QTimer(self)
        if live_source is not None:
            self._timer.timeout.connect(self._refresh)
            self._timer.start(_LIVE_MS)

    def done(self, result: int) -> None:
        self._timer.stop()
        self._push.stop()
        super().done(result)

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self, frame: np.ndarray) -> None:
        root = QHBoxLayout(self)
        self._view = pg.GraphicsLayoutWidget()
        self._view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        vb = pg.ViewBox(lockAspect=True, invertY=True)
        self._view.addItem(vb)
        self._img = pg.ImageItem(frame, axisOrder="row-major")
        self._img.setLevels(snapshot_levels(frame))
        vb.addItem(self._img)
        cx, cy = (self._cam_size[0] - 1) / 2.0, (self._cam_size[1] - 1) / 2.0
        for angle, pos in ((0, cy), (90, cx)):
            vb.addItem(pg.InfiniteLine(pos=pos, angle=angle, pen=_CROSS_PEN,
                                       movable=False))
        self._outline = pg.PlotCurveItem(pen=_RETICLE_PEN)
        vb.addItem(self._outline)
        root.addWidget(self._view, 1)

        side = QVBoxLayout()
        head = QLabel("<b>The DMD is lit while this is open.</b> Do it before "
                      "the animal is on the rig. Work the steps in order, "
                      "then go round again to tidy up.")
        head.setWordWrap(True)
        side.addWidget(head)

        self._steps = QButtonGroup(self)
        names = {"centre": "1  Centre", "angle": "2  Angle", "size": "3  Size"}
        for i, kind in enumerate(RETICLE_KINDS):
            b = QRadioButton(names[kind])
            self._steps.addButton(b, i)
            side.addWidget(b)
        self._steps.button(0).setChecked(True)
        self._steps.idToggled.connect(self._on_step)

        w, h = self._dmd_size
        form = QFormLayout()
        self._spn_cx = spin(-w, 2 * w, decimals=1, step=1.0, suffix=" mir")
        self._spn_cy = spin(-h, 2 * h, decimals=1, step=1.0, suffix=" mir")
        form.addRow("Centre:", hrow(self._spn_cx, self._spn_cy))
        self._spn_angle = spin(-180.0, 180.0, decimals=2, step=0.1, suffix=" °")
        self._spn_angle.setWrapping(True)
        self._chk_mirror = QCheckBox("Mirrored")
        form.addRow("Angle:", hrow(self._spn_angle, self._chk_mirror))
        self._spn_sx = spin(0.01, 50.0, decimals=4, step=0.002)
        self._spn_sy = spin(0.01, 50.0, decimals=4, step=0.002)
        form.addRow("Scale:", hrow(self._spn_sx, self._spn_sy))
        form.addRow("", QLabel("mirrors per camera px, X / Y"))
        self._spn_box = spin(10.0, 95.0, 100 * RETICLE_BOX, decimals=0,
                             step=5.0, suffix=" %",
                             tooltip="The size step's box, as a share of the "
                                     "camera frame. Shrink it if the DMD "
                                     "can't reach its edges.")
        form.addRow("Box:", hrow(self._spn_box))
        side.addLayout(form)
        for s in (self._spn_cx, self._spn_cy, self._spn_angle, self._spn_sx,
                  self._spn_sy, self._spn_box):
            s.valueChanged.connect(self._changed)
        self._chk_mirror.toggled.connect(self._changed)

        self._lbl_help = QLabel()
        self._lbl_help.setWordWrap(True)
        side.addWidget(self._lbl_help)
        keys = QLabel("Arrow keys nudge this step; Shift × 10. Click the "
                      "image if a box has taken the arrows.")
        keys.setWordWrap(True)
        keys.setStyleSheet(f"color:{style.muted()};")
        side.addWidget(keys)
        self._lbl_status = QLabel()
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet(f"color:{style.WARN};")
        side.addWidget(self._lbl_status)
        side.addStretch()

        row = QHBoxLayout()
        b_levels = QPushButton("Auto levels")
        b_levels.clicked.connect(self._auto_levels)
        b_reset = QPushButton("Reset")
        b_reset.setToolTip("Back to " + ("the loaded fit" if self._seed
                                         else "the starting rectangle"))
        b_reset.clicked.connect(lambda: self._set_pose(self._start))
        row.addWidget(b_levels)
        row.addWidget(b_reset)
        row.addStretch()
        side.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply
                              | QDialogButtonBox.StandardButton.Cancel)
        apply_btn = bb.button(QDialogButtonBox.StandardButton.Apply)
        apply_btn.setText("Use this alignment")
        apply_btn.setStyleSheet(style.solid_btn("dmd"))
        apply_btn.clicked.connect(self._apply)
        bb.rejected.connect(self.reject)
        side.addWidget(bb)
        root.addLayout(side)

        for key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up,
                    Qt.Key.Key_Down):
            for shift in (False, True):
                mods = Qt.KeyboardModifier.ShiftModifier if shift else \
                    Qt.KeyboardModifier.NoModifier
                sc = QShortcut(QKeySequence(QKeyCombination(mods, key)), self)
                sc.activated.connect(
                    lambda k=key, n=10 if shift else 1: self.nudge(k, n))
        self._on_step()
        vb.autoRange()

    # ── state ────────────────────────────────────────────────────────────────
    @property
    def kind(self) -> str:
        return RETICLE_KINDS[self._steps.checkedId()]

    def pose(self) -> ReticlePose:
        return ReticlePose(self._spn_cx.value(), self._spn_cy.value(),
                           self._spn_angle.value(), self._spn_sx.value(),
                           self._spn_sy.value(), self._chk_mirror.isChecked())

    def _set_pose(self, p: ReticlePose) -> None:
        angle = (p.angle_deg + 180.0) % 360.0 - 180.0
        for s, v in ((self._spn_cx, p.cx), (self._spn_cy, p.cy),
                     (self._spn_angle, angle), (self._spn_sx, p.sx),
                     (self._spn_sy, p.sy)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        self._chk_mirror.blockSignals(True)
        self._chk_mirror.setChecked(p.mirrored)
        self._chk_mirror.blockSignals(False)
        self._changed()

    def nudge(self, key: Qt.Key, n: int = 1) -> None:
        """One arrow press on the current step's knobs."""
        x = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(key, 0) * n
        y = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(key, 0) * n
        if self.kind == "centre":
            self._spn_cx.stepBy(x)
            self._spn_cy.stepBy(y)
        elif self.kind == "angle":
            self._spn_angle.stepBy(x)
        else:
            self._spn_sx.stepBy(x)
            self._spn_sy.stepBy(-y)          # up = taller

    def _on_step(self, *_a) -> None:
        self._lbl_help.setText(_STEP_HELP[self.kind])
        self._changed()
        self._view.setFocus()

    def _changed(self, *_a) -> None:
        o = reticle_outline(self.kind, self._cam_size,
                            box_frac=self._spn_box.value() / 100.0)
        self._outline.setData(o[:, 0], o[:, 1])
        self._push.start()

    def _send(self) -> None:
        try:
            self._project(reticle_frame(
                self.kind, self.pose(), self._dmd_size, self._cam_size,
                box_frac=self._spn_box.value() / 100.0))
            self._lbl_status.clear()
        except Exception as e:   # noqa: BLE001 — escaping a slot aborts the app
            self._lbl_status.setText(f"Could not project: {e}")

    # ── live image ───────────────────────────────────────────────────────────
    def _refresh(self) -> None:
        f = self._live_source()
        if f is not None and f is not self._last_frame:
            self._last_frame = f
            self._img.setImage(np.asarray(f), autoLevels=False)

    def _auto_levels(self) -> None:
        self._img.setLevels(snapshot_levels(np.asarray(self._last_frame)))

    # ── result ───────────────────────────────────────────────────────────────
    def _apply(self) -> None:
        calib = self.pose().to_calibration(self._dmd_size, self._cam_size)
        if self._seed is not None and self._seed.vignette is not None:
            calib = replace(calib, vignette=self._seed.vignette)
        self._result = calib
        self.accept()

    @property
    def calibration(self) -> DmdCalibration | None:
        """The aligned calibration, or None if Cancel was pressed."""
        return self._result
