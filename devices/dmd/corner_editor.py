"""Nudge an auto-fit calibration's four corners against an all-on frame, and
mark the optical vignette.

The sweep leaves a few px of error that an eye removes more easily than
another sweep; `with_corners` makes the dragged corners an exact 4-point
homography. The vignette (where the optics dim the field, often a circle well
inside the panel) is an independent advisory mark (`with_vignette`): a
registration fit doesn't measure dimness.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QPushButton, QVBoxLayout)

from acqApp import style
from acqApp.devices.dmd.calibration import (CalibrationError, DmdCalibration,
                                            with_corners, with_vignette,
                                            without_vignette)
from acqApp.devices.dmd.roi_panel import snapshot_levels

_FIELD_PEN = pg.mkPen(style.HEX["dmd"], width=2, style=Qt.PenStyle.DashLine)
_CORNER_PEN = pg.mkPen("#00d0ff", width=2)
_CORNER_HOVER = pg.mkPen("#4dff88", width=3)
_VIGNETTE_PEN = pg.mkPen(style.WARN, width=2, style=Qt.PenStyle.DotLine)


class CornerAdjustDialog(QDialog):
    """Drag the field's corners onto a frame (Apply refits exactly through
    them), and optionally mark a vignette circle; both applied together."""

    def __init__(self, calib: DmdCalibration, frame: np.ndarray, *,
                 parent=None):
        super().__init__(parent)
        self._calib = calib
        self._auto_corners = np.asarray(calib.accessible_corners(), float)
        self._result: DmdCalibration | None = None
        self.setWindowTitle("Adjust calibration corners / vignette")
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(900, 720)
        self._build(frame)

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self, frame: np.ndarray) -> None:
        root = QVBoxLayout(self)
        msg = QLabel(
            "Drag each corner onto where the DMD's lit field actually lands. "
            "\"Apply\" refits the registration exactly through the four new "
            "points — a manual correction to the sweep's fit, not a re-run. "
            "Below, you can also mark a circle where the optics dim the image "
            "past use — some rigs show this directly on an all-on frame.")
        msg.setWordWrap(True)
        root.addWidget(msg)

        gv = pg.GraphicsLayoutWidget()
        vb = pg.ViewBox(lockAspect=True, invertY=True)
        gv.addItem(vb)
        f = np.asarray(frame)
        img = pg.ImageItem(f, axisOrder="row-major")
        vb.addItem(img)
        img.setLevels(snapshot_levels(f))
        root.addWidget(gv, 1)

        self._outline = pg.PlotCurveItem(pen=_FIELD_PEN)
        vb.addItem(self._outline)

        w, h = self._calib.dmd_size
        dmd_pts = ((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1))
        self._targets: list[pg.TargetItem] = []
        for (x, y), (dx, dy) in zip(self._auto_corners, dmd_pts):
            t = pg.TargetItem(pos=(x, y), size=14, pen=_CORNER_PEN,
                              hoverPen=_CORNER_HOVER, movable=True,
                              label=f"({dx}, {dy})")
            t.sigPositionChanged.connect(self._on_moved)
            vb.addItem(t)
            self._targets.append(t)

        # The vignette circle, for RoiSet.dim() / the ROI editor's warning.
        cw, ch = self._calib.cam_size
        seed = self._calib.vignette or (cw / 2.0, ch / 2.0,
                                        0.4 * min(cw, ch))
        vcx, vcy, vr = seed
        self._vignette_roi = pg.CircleROI(
            [vcx - vr, vcy - vr], [2 * vr, 2 * vr], pen=_VIGNETTE_PEN,
            movable=True)
        vb.addItem(self._vignette_roi)
        self._vignette_roi.setVisible(self._calib.vignette is not None)

        self._chk_vignette = QCheckBox(
            "Mark the vignette boundary (where the optics dim past use)")
        self._chk_vignette.setChecked(self._calib.vignette is not None)
        self._chk_vignette.toggled.connect(self._vignette_roi.setVisible)
        root.addWidget(self._chk_vignette)

        self._lbl_delta = QLabel()
        self._lbl_delta.setWordWrap(True)
        self._lbl_delta.setStyleSheet(f"color:{style.muted()};")
        root.addWidget(self._lbl_delta)
        self._on_moved()
        vb.autoRange()

        row = QHBoxLayout()
        btn_reset = QPushButton("Reset to auto fit")
        btn_reset.clicked.connect(self._reset)
        row.addWidget(btn_reset)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Apply).setStyleSheet(
            style.solid_btn("dmd"))
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        root.addLayout(row)

    # ── live feedback ────────────────────────────────────────────────────────
    def _current_corners(self) -> np.ndarray:
        return np.array([[t.pos().x(), t.pos().y()] for t in self._targets],
                        float)

    def _on_moved(self, *_a) -> None:
        c = self._current_corners()
        closed = np.vstack([c, c[:1]])
        self._outline.setData(closed[:, 0], closed[:, 1])
        d = np.hypot(*(c - self._auto_corners).T)
        if d.max() < 0.5:
            self._lbl_delta.setText("No change from the auto fit yet.")
        else:
            self._lbl_delta.setText(
                "Moved from the auto fit: "
                + ", ".join(f"{v:.1f} px" for v in d))

    def _reset(self) -> None:
        for t, (x, y) in zip(self._targets, self._auto_corners):
            t.blockSignals(True)
            t.setPos(x, y)
            t.blockSignals(False)
        self._on_moved()

    # ── result ───────────────────────────────────────────────────────────────
    def _apply(self) -> None:
        try:
            result = with_corners(self._calib, self._current_corners())
        except CalibrationError as e:
            self._lbl_delta.setText(f"Could not apply: {e}")
            return
        if self._chk_vignette.isChecked():
            pos, size = self._vignette_roi.pos(), self._vignette_roi.size()
            r = float(size[0]) / 2.0
            result = with_vignette(result, float(pos[0]) + r, float(pos[1]) + r, r)
        else:
            result = without_vignette(result)
        self._result = result
        self.accept()

    @property
    def calibration(self) -> DmdCalibration | None:
        """The corner-adjusted calibration, or None if Cancel was pressed."""
        return self._result
