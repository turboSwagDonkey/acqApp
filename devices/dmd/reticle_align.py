"""Calibrate by eye: the live camera with a fixed reticle, and the DMD lighting
the same shape through a guessed registration. The operator steers the guess
(centre, then angle, then X/Y size) until the light sits on the reticle, then
optionally bends a checkerboard onto a grid where one matrix can't follow the
optics.

Needs no fine pattern and no fit, so it survives the scattering that sank the
coarse-stripe decode (REFERENCE §5b). It opens dark; the light is on only
while Light on is pressed, and the caller ends dark.
"""
from __future__ import annotations

import time
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import (QKeyCombination, QSignalBlocker, Qt, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
                             QFormLayout, QHBoxLayout, QLabel, QPushButton,
                             QRadioButton, QVBoxLayout)

from acqApp import style
from acqApp.devices.dmd.calibration import (CHECKER_COLS, RETICLE_BOX,
                                            RETICLE_KINDS, DmdCalibration,
                                            ReticlePose, Warp, checker_frame,
                                            checker_lattice, reticle_frame,
                                            reticle_outline)
from acqApp.devices.dmd.roi_live import LiveImageFeed
from acqApp.devices.dmd.roi_panel import snapshot_levels
from acqApp.widgets import hrow, spin

_RETICLE_PEN = pg.mkPen("#00d0ff", width=2)
_CROSS_PEN = pg.mkPen("#00d0ff", width=1, style=Qt.PenStyle.DashLine)
_MESH_PEN = pg.mkPen("#ffa000", width=1, style=Qt.PenStyle.DashLine)
_NODE_PEN = pg.mkPen("#ffa000", width=2)
_PICKED_PEN = pg.mkPen("#ff3d00", width=4)
_PROJECT_MS = 40            # coalesces a held key or a drag into one upload per tick

STEPS = RETICLE_KINDS + ("bend",)
_STEP_HELP = {
    "centre": "Move the disc onto the circle. Arrows move it along the DMD's "
              "own axes, which may look rotated or flipped here.",
    "angle": "Turn the L onto the outline (←/→). If both arms can't match at "
             "any angle, tick Mirrored.",
    "size": "Stretch the box onto the outline: ←/→ width, ↑/↓ height.",
    "bend": "Where the lit board's corners miss the cyan grid, drag the "
            "nearest orange dot (or click it and use the arrows): the light "
            "follows the dot. Do this last; steps 1–3 keep the bend but "
            "don't show it.",
}


class _Node(pg.TargetItem):
    """A draggable board corner that also reports a plain click."""
    sigClicked = pyqtSignal(object)

    def mouseClickEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()
            self.sigClicked.emit(self)
        else:
            super().mouseClickEvent(ev)


def _lattice_lines(g: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """x, y for `connect="pairs"`: every row and column edge of the
    (ny, nx, 2) node grid `g`."""
    seg = np.concatenate([np.stack([g[:, :-1], g[:, 1:]], 2).reshape(-1, 2),
                          np.stack([g[:-1], g[1:]], 2).reshape(-1, 2)])
    return seg[:, 0], seg[:, 1]


def fresh_frame(source: Callable[[], object], *, settle: int = 2,
                timeout_s: float = 12.0,
                pump: Callable[[], None] = lambda: None) -> np.ndarray:
    """The first frame from `source()` after `settle` new ones, pumping the
    GUI while it waits: the frame size (the calibration's camera px) must be
    the one a just-switched preset delivers, not a stale cropped frame.
    Identity, not a counter: the display tick replaces the array per frame."""
    last = source()
    seen = 0
    deadline = time.monotonic() + timeout_s
    while True:
        pump()
        f = source()
        if f is not None and f is not last:
            last = f
            seen += 1
            if seen > settle:
                return np.asarray(f)
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"no new camera frame in {timeout_s:g} s — is the voltage "
                f"camera running (Live view)?")
        time.sleep(0.002)          # a bare spin starves the Qt thread


class ReticleAlignDialog(QDialog):
    """Steer a `ReticlePose` until the projected shape lands on the reticle."""

    def __init__(self, project: Callable[[np.ndarray], None],
                 dmd_size: tuple[int, int], frame: np.ndarray, *,
                 live_source: Callable[[], object] | None = None,
                 seed: DmdCalibration | None = None,
                 stop: Callable[[], None] | None = None, parent=None):
        """`project` takes a device-sized frame; `stop` darkens the DMD.
        Opens dark: nothing is projected until Light on. `seed` (same camera
        size) is where the guess starts, else a centred rectangle."""
        super().__init__(parent)
        self._project = project
        self._stop = stop or (lambda: None)
        self._lit = False
        self._bend_src: Warp | None = None     # the bend Box/Board resample from
        self._dmd_size = tuple(dmd_size)
        f = np.asarray(frame)
        self._cam_size = (f.shape[1], f.shape[0])
        if seed is None or tuple(seed.cam_size) != self._cam_size:
            seed = None
        self._seed = seed
        self._start = (ReticlePose.from_calibration(seed) if seed is not None
                       else ReticlePose.centred(self._dmd_size, self._cam_size))
        self._start_warp = seed.warp if seed is not None else None
        self._warp: Warp | None = None
        self._nodes: list[_Node] = []
        self._picked: int | None = None
        self._result: DmdCalibration | None = None
        self.setWindowTitle("Align DMD to camera reticle")
        self.setStyleSheet(style.accent_panel("dmd"))
        self.resize(1100, 720)

        self._push = QTimer(self, singleShot=True, interval=_PROJECT_MS)
        self._push.timeout.connect(self._send)
        self._build(f)
        self._reset()
        self._feed = LiveImageFeed(self, self._img, live_source, frame)

    def done(self, result: int) -> None:
        self._feed.stop()
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
        self._grid = pg.PlotCurveItem(pen=_RETICLE_PEN, connect="pairs")
        self._mesh = pg.PlotCurveItem(pen=_MESH_PEN, connect="pairs")
        for it in (self._outline, self._mesh, self._grid):    # target on top
            vb.addItem(it)
        self._vb = vb
        root.addWidget(self._view, 1)

        side = QVBoxLayout()
        head = QLabel("<b>Light on projects onto the sample.</b> Do this "
                      "before the animal is on the rig. Work steps 1–3 in "
                      "order and go round again to tidy up; bend last, only "
                      "if the board's edges still miss.")
        head.setWordWrap(True)
        side.addWidget(head)
        self._btn_light = QPushButton("Light on")
        self._btn_light.setCheckable(True)
        self._btn_light.setStyleSheet(style.toggle_btn("dmd"))
        self._btn_light.setToolTip("THIS PROJECTS LIGHT: shows the reticle "
                                   "shape on the DMD and follows every change.")
        self._btn_light.toggled.connect(self.set_lit)
        side.addWidget(self._btn_light)

        self._steps = QButtonGroup(self)
        names = {"centre": "1  Centre", "angle": "2  Angle", "size": "3  Size",
                 "bend": "4  Bend (optional)"}
        for i, kind in enumerate(STEPS):
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
                             tooltip="The size step's box and the bend step's "
                                     "board, as a share of the camera frame. "
                                     "Shrink it if the DMD can't reach its "
                                     "edges.")
        form.addRow("Box:", hrow(self._spn_box))
        self._spn_cols = spin(2, 16, CHECKER_COLS, suffix=" squares",
                              tooltip="Squares across the bend step's board, "
                                      "which fills the box. Its corners are "
                                      "the control points; changing this "
                                      "keeps the bend.")
        form.addRow("Board:", hrow(self._spn_cols))
        side.addLayout(form)
        for s in (self._spn_cx, self._spn_cy, self._spn_angle, self._spn_sx,
                  self._spn_sy):
            s.valueChanged.connect(self._changed)
        self._chk_mirror.toggled.connect(self._changed)
        for s in (self._spn_box, self._spn_cols):
            s.valueChanged.connect(lambda *_a: self._set_warp(self._bend_src))

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
        b_reset.setToolTip("Back to " + ("the loaded calibration" if self._seed
                                         else "the starting rectangle"))
        b_reset.clicked.connect(self._reset)
        b_flat = QPushButton("Flatten")
        b_flat.setToolTip("Undo the bend: the board's dots back on the grid")
        b_flat.clicked.connect(lambda: self._set_warp(None))
        row.addWidget(b_levels)
        row.addWidget(b_reset)
        row.addWidget(b_flat)
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
        return STEPS[self._steps.checkedId()]

    def pose(self) -> ReticlePose:
        return ReticlePose(self._spn_cx.value(), self._spn_cy.value(),
                           self._spn_angle.value(), self._spn_sx.value(),
                           self._spn_sy.value(), self._chk_mirror.isChecked())

    def _set_pose(self, p: ReticlePose) -> None:
        angle = (p.angle_deg + 180.0) % 360.0 - 180.0
        for s, v in ((self._spn_cx, p.cx), (self._spn_cy, p.cy),
                     (self._spn_angle, angle), (self._spn_sx, p.sx),
                     (self._spn_sy, p.sy)):
            with QSignalBlocker(s):
                s.setValue(v)
        with QSignalBlocker(self._chk_mirror):
            self._chk_mirror.setChecked(p.mirrored)
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
        elif self.kind == "size":
            self._spn_sx.stepBy(x)
            self._spn_sy.stepBy(-y)          # up = taller
        elif self._picked is not None:
            p = self.pose()
            mirror = 1.0 / np.sqrt(p.sx * p.sy)     # one mirror, in camera px
            node = self._nodes[self._picked]
            node.setPos(node.pos().x() + x * mirror,
                        node.pos().y() + y * mirror)

    def _on_step(self, *_a) -> None:
        self._lbl_help.setText(_STEP_HELP[self.kind])
        self._changed()
        self._view.setFocus()

    def _changed(self, *_a) -> None:
        bend = self.kind == "bend"
        self._outline.setVisible(not bend)
        for it in (self._grid, self._mesh, *self._nodes):
            it.setVisible(bend)
        if not bend:
            o = reticle_outline(self.kind, self._cam_size,
                                box_frac=self._spn_box.value() / 100.0)
            self._outline.setData(o[:, 0], o[:, 1])
        self._schedule()

    def _schedule(self) -> None:
        if self._lit and not self._push.isActive():   # a restart would starve a held key or drag
            self._push.start()

    def set_lit(self, on: bool) -> None:
        """Light on projects the current shape and follows edits; off stops."""
        self._lit = bool(on)
        with QSignalBlocker(self._btn_light):
            self._btn_light.setChecked(self._lit)
        if self._lit:
            self._send()
            return
        self._push.stop()
        try:
            self._stop()
        except Exception as e:   # noqa: BLE001 — escaping a slot aborts the app
            self._lbl_status.setText(f"Could not turn the light off: {e}")

    def _send(self) -> None:
        if not self._lit:
            return
        try:
            if self.kind == "bend":
                frame = checker_frame(self._calibration(), self._warp.xs,
                                      self._warp.ys)
            else:
                frame = reticle_frame(
                    self.kind, self.pose(), self._dmd_size, self._cam_size,
                    box_frac=self._spn_box.value() / 100.0)
            self._project(frame)
            self._lbl_status.clear()
        except Exception as e:   # noqa: BLE001 — escaping a slot aborts the app
            self._lbl_status.setText(f"Could not project: {e}")

    def _reset(self) -> None:
        self._restore_warp(self._start_warp)
        self._set_pose(self._start)

    # ── bend ─────────────────────────────────────────────────────────────────
    @property
    def warp(self) -> Warp:
        return self._warp

    def _restore_warp(self, warp: Warp | None) -> None:
        """`warp` on the board it was bent on, so it comes back unresampled."""
        if warp is not None:
            with QSignalBlocker(self._spn_cols), QSignalBlocker(self._spn_box):
                self._spn_cols.setValue(len(warp.xs) - 1)
                self._spn_box.setValue(
                    100.0 * (warp.xs[-1] - warp.xs[0]) / self._cam_size[0])
        self._set_warp(warp)

    def _set_warp(self, warp: Warp | None) -> None:
        """Board nodes on the current lattice, carrying `warp`'s bend
        (None: flat). `warp` stays the source, so Box/Board changes resample
        the original bend, not a copy of a copy."""
        self._bend_src = warp
        xs, ys = checker_lattice(self._cam_size, int(self._spn_cols.value()),
                                 box_frac=self._spn_box.value() / 100.0)
        self._warp = (Warp.flat(xs, ys) if warp is None
                      else warp.resampled(xs, ys))
        for n in self._nodes:
            self._vb.removeItem(n)
        self._nodes, self._picked = [], None
        bent = self._warp.nodes + np.column_stack(
            [self._warp.dx.ravel(), self._warp.dy.ravel()])
        for k, (x, y) in enumerate(bent):
            n = _Node((x, y), size=14, symbol="o", pen=_NODE_PEN,
                      brush=pg.mkBrush(255, 160, 0, 60))
            n.sigPositionChanged.connect(lambda _n, k=k: self._node_moved(k))
            n.sigClicked.connect(lambda _n, k=k: self._pick(k))
            self._vb.addItem(n)
            self._nodes.append(n)
        self._draw_bend()
        self._changed()

    def _node_moved(self, k: int) -> None:
        pos = self._nodes[k].pos()
        w = self._warp
        x, y = w.xs[k % len(w.xs)], w.ys[k // len(w.xs)]
        self._warp = self._bend_src = w.moved(k, pos.x() - x, pos.y() - y)
        self._pick(k)
        self._draw_bend()
        self._schedule()

    def _pick(self, k: int) -> None:
        if self._picked is not None and self._picked != k:
            self._nodes[self._picked].setPen(_NODE_PEN)
        self._picked = k
        self._nodes[k].setPen(_PICKED_PEN)
        self._view.setFocus()

    def _draw_bend(self) -> None:
        w = self._warp
        grid = w.nodes.reshape(len(w.ys), len(w.xs), 2)
        self._grid.setData(*_lattice_lines(grid))
        self._mesh.setData(*_lattice_lines(grid + np.stack([w.dx, w.dy], -1)))

    def _calibration(self) -> DmdCalibration:
        return self.pose().to_calibration(self._dmd_size, self._cam_size,
                                          self._warp)

    # ── live image ───────────────────────────────────────────────────────────
    def _auto_levels(self) -> None:
        self._img.setLevels(snapshot_levels(np.asarray(self._feed.frame)))

    # ── result ───────────────────────────────────────────────────────────────
    def _apply(self) -> None:
        self._result = self._calibration()
        self.accept()

    @property
    def calibration(self) -> DmdCalibration | None:
        """The aligned calibration, or None if Cancel was pressed."""
        return self._result
