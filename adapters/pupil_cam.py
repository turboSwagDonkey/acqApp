"""
The pupil camera's adapter: preview dock, LED, and the eye region.

The tracker was removed on 2026-08-24 (PLAN §7 (ai)) and is in
`archive/pupil_tracking/`. The camera still previews and records exactly as
before; what is gone is the fit, the radius trace and the search overlay. The
eye region stays — it is drawn by hand and is recorded with the session.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from acqApp import config
from acqApp.acq.devices import ExposureControl
from acqApp.adapters.base import ModuleAdapter, _image_view
from acqApp.devices.pupil_cam.acquisition import (MockPupilCameraWorker,
                                          PupilCameraWorker)
from acqApp.devices.pupil_cam.control import LedController, MockLedController
from acqApp.devices.pupil_cam.panel import SettingsPanel as PupilSettingsPanel
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.video import VideoFileCameraWorker


class PupilCamModule(ModuleAdapter):
    key = "pupil_cam"
    tab_label = "Pupil cam"
    # No plot_label: with no tracker there is no per-frame scalar to trace.

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        # Empty until build_views: the module can be loaded without ever
        # building its dock, and _on_settings fires from the panel before then.
        self._limit_curve = None        # the circle in force
        self._limit_ghost = None        # rubber band while it is being placed
        self._limit_centre: tuple[float, float] | None = None   # first click
        self._vb = None
        self._gv = None
        self._btn_limit = None
        self._btn_limit_off = None
        self._lbl_limit = None
        self._theta = np.linspace(0, 2 * np.pi, 48)

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = PupilSettingsPanel(
            config.load_dataclass(PupilSettings, self.key))
        self.panel.exposure_changed.connect(self._on_exposure)
        self.panel.led_toggled.connect(self._on_led)
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        self._draw_limit(s)
        self._refresh_limit_bar()

    def build_views(self) -> None:
        self._img, hist, gv, vb, row = _image_view()
        # Pupil frames are 8-bit, so pin the histogram to 0–255: the bar then
        # shows an absolute brightness scale instead of rescaling to each frame,
        # and the handles still drag to adjust contrast.
        self._img.setLevels((0, 255))
        hist.setHistogramRange(0, 255)
        hist.setLevels(0, 255)
        self.win.register_pg_view(hist)
        self.win.register_pg_view(gv)

        self._limit_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=2, style=Qt.PenStyle.DashLine))
        self._limit_ghost = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=1, style=Qt.PenStyle.DotLine))
        for item in (self._limit_curve, self._limit_ghost):
            vb.addItem(item)
        self._vb, self._gv = vb, gv
        vb.scene().sigMouseClicked.connect(self._on_click)
        vb.scene().sigMouseMoved.connect(self._on_move)
        if self.panel is not None:
            self._draw_limit(self.panel.settings)

        # The region controls live over the image, not in the settings window:
        # picking a region of the frame means looking at the frame, and the
        # settings are a separate floating window.
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(self._build_limit_bar())
        col.addWidget(row, 1)
        self.win.add_dock("Pupil cam", host, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

    def _on_click(self, ev) -> None:
        """Place the region. A pan drag never gets here — pyqtgraph only emits
        `sigMouseClicked` for a press+release that did not become a drag."""
        if self.panel is None or self._vb is None:
            return
        if not self._btn_limit.isChecked():
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        self._place_limit(p.x(), p.y())

    # ── the eye region ──
    # Placed by two clicks — centre, then edge — with the circle following the
    # cursor in between. Not a press-drag: the viewbox owns dragging for
    # pan/zoom, and taking that over would cost the ability to look around the
    # frame while placing the region.
    def _build_limit_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)

        self._btn_limit = QPushButton("Set eye region")
        self._btn_limit.setCheckable(True)
        self._btn_limit.setToolTip(
            "Click the centre of the eye, then click again further out to set "
            "the radius. Panning and zooming still work.")
        self._btn_limit.toggled.connect(self._arm_limit)

        self._btn_limit_off = QPushButton("Clear")
        self._btn_limit_off.setToolTip("Remove the region.")
        self._btn_limit_off.clicked.connect(self._clear_limit)

        self._lbl_limit = QLabel()
        self._lbl_limit.setStyleSheet("color:#9aa0a6;")
        # Let it be clipped rather than hold the dock open at its own width —
        # the preview is what the dock is for.
        self._lbl_limit.setMinimumWidth(1)
        lay.addWidget(QLabel("Eye:"))
        for w in (self._btn_limit, self._btn_limit_off):
            lay.addWidget(w)
        lay.addWidget(self._lbl_limit, 1)
        self._refresh_limit_bar()
        return bar

    def _arm_limit(self, on: bool) -> None:
        self._limit_centre = None
        if self._limit_ghost is not None:
            self._limit_ghost.setData([], [])
        if self._gv is not None:
            self._gv.setCursor(Qt.CursorShape.CrossCursor if on
                               else Qt.CursorShape.ArrowCursor)
        self._refresh_limit_bar()

    def _place_limit(self, x: float, y: float) -> None:
        if self._limit_centre is None:
            self._limit_centre = (x, y)
            self._refresh_limit_bar()
            return
        cx, cy = self._limit_centre
        r = float(np.hypot(x - cx, y - cy))
        if r < 1.0:                 # a double-click on the centre: keep waiting
            return
        self.panel.set_limit(cx, cy, r)
        self._limit_centre = None
        self._btn_limit.setChecked(False)       # done — no toggle to remember
        self.win.status(f"eye region set at ({cx:.0f}, {cy:.0f}) r={r:.0f} px")

    def _clear_limit(self) -> None:
        self._btn_limit.setChecked(False)
        self.panel.clear_limit()
        self.win.status("eye region cleared")

    def _refresh_limit_bar(self) -> None:
        if self.panel is None or self._lbl_limit is None:
            return
        lim = self.panel.settings.search_limit()
        if self._btn_limit.isChecked():
            self._lbl_limit.setText("click the centre of the eye"
                                    if self._limit_centre is None
                                    else "now click the outer edge")
        elif lim is None:
            self._lbl_limit.setText("no region")
        else:
            self._lbl_limit.setText(
                f"({lim[0]:.0f}, {lim[1]:.0f}) r {lim[2]:.0f}")
        self._btn_limit_off.setEnabled(lim is not None)

    def _on_move(self, pos) -> None:
        """Follow the cursor between the two clicks, so the region is placed by
        looking at it rather than by reading back three numbers."""
        if self._limit_centre is None or self._vb is None:
            return
        p = self._vb.mapSceneToView(pos)
        cx, cy = self._limit_centre
        r = float(np.hypot(p.x() - cx, p.y() - cy))
        th = self._theta
        self._limit_ghost.setData(cx + r * np.cos(th), cy + r * np.sin(th))

    def _draw_limit(self, s) -> None:
        """Outline the region in force, or clear it when there is none."""
        if self._limit_curve is None:
            return
        self._limit_ghost.setData([], [])
        lim = s.search_limit()
        if lim is None:
            self._limit_curve.setData([], [])
            return
        cx, cy, r = lim
        th = self._theta
        self._limit_curve.setData(cx + r * np.cos(th), cy + r * np.sin(th))

    # ── controllers ──
    def build_controller(self, emulate: bool) -> None:
        if emulate:
            self.controller = MockLedController()
            return
        try:
            self.controller = LedController()
        except Exception as e:
            print(f"[main] eye-tracking LED unavailable ({e}) — using mock")
            self.controller = MockLedController()

    def _on_led(self, on: bool) -> None:
        if self.controller is not None:
            self.controller.set(on)

    def _on_exposure(self, us: float) -> None:
        if isinstance(self.worker, ExposureControl):
            self.worker.set_exposure(us)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings
        if s.video_path:
            # A third source beside real and mock — the reason §5b A3 is worth
            # revisiting. Checked before emulate so a clip replays either way.
            try:
                self._adopt(VideoFileCameraWorker(s.video_path, fps=s.fps))
            except Exception as e:
                # A missing or compressed file must not kill the session: say so
                # and fall back, rather than leaving a dead tab.
                print(f"[main] pupil video {s.video_path!r} unusable ({e}) "
                      f"— falling back to the camera")
                self.win.status(f"pupil video unusable: {e}")
                self._adopt(MockPupilCameraWorker(fps=s.fps) if emulate
                            else PupilCameraWorker(exposure_us=s.exposure_us,
                                                   fps=s.fps))
        elif emulate:
            self._adopt(MockPupilCameraWorker(fps=s.fps))
        else:
            # cam=None → the worker opens/closes its own Basler on its thread.
            self._adopt(PupilCameraWorker(exposure_us=s.exposure_us, fps=s.fps))

    # ── display ──
    def update_display(self) -> None:
        if self.worker is None:
            return
        frame = self.worker.get_latest()
        if frame is None:
            return
        # No `levels=` here: the LUT bar owns the levels, so forcing them every
        # frame would undo any contrast the user drags.
        self._img.setImage(frame, autoLevels=False)

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"pupil_exposure_us": s.exposure_us,
                "pupil_fps":         s.fps,
                # 0 = no region. Recorded because it is operator-set geometry
                # that nothing else in the file would show.
                "pupil_limit_x":     s.limit_x,
                "pupil_limit_y":     s.limit_y,
                "pupil_limit_r":     s.limit_r,
                # "" for the camera. Recorded because frames replayed from a
                # clip are not this session's data.
                "pupil_video":       s.video_path}
