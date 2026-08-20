"""
The pupil camera's adapter: preview dock, LED, and the tracking thread.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from acqApp import config, style
from acqApp.acq.devices import ExposureControl
from acqApp.adapters.base import (PLOT_HISTORY, ModuleAdapter, _image_view,
                                 _plot)
from acqApp.devices.pupil_cam.acquisition import (MockPupilCameraWorker,
                                          PupilCameraWorker)
from acqApp.devices.pupil_cam.control import LedController, MockLedController
from acqApp.devices.pupil_cam.panel import SettingsPanel as PupilSettingsPanel
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.track_worker import PupilTrackWorker, track_params
from acqApp.devices.pupil_cam.video import VideoFileCameraWorker


class PupilCamModule(ModuleAdapter):
    key = "pupil_cam"
    tab_label = "Pupil cam"
    plot_label = "Pupil"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._overlay = None
        # Empty until build_views: the pupil module can be loaded without ever
        # building its dock, and _on_settings fires from the panel before then.
        self._search_items: tuple = ()
        self._limit_curve = None        # the circle in force
        self._limit_ghost = None        # rubber band while it is being placed
        self._limit_centre: tuple[float, float] | None = None   # first click
        self._last_fit: tuple[float, float, float] | None = None
        self._vb = None
        self._gv = None
        # Built with the dock, which the module can be loaded without.
        self._btn_limit = None
        self._btn_limit_fit = None
        self._lbl_limit = None
        self._curve = None
        self._y: list[float] = []
        # Tracking runs on its own thread (see devices/pupil_cam/track_worker.py): it is
        # the only consumer of the camera worker's frames and hands the GUI each
        # frame together with the fit made from it.
        self._track: PupilTrackWorker | None = None
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
        if self._search_items:
            self._set_search_visible(s.show_search)
        self._draw_limit(s)
        self._refresh_limit_bar()
        if self._track is not None:
            # Queued, not written: the tracker belongs to its own thread.
            self._track.configure(**track_params(s))

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("Pupil radius", "Radius", "px", "Frame", self.key)
        return pw

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

        # The search, not just the answer. The fitted outline alone cannot tell
        # a good fit from rays latching onto an eyelash or a corneal glint —
        # both draw a plausible circle. These show where the rays looked (the
        # dashed annulus), what they found, and which points the robust fit
        # threw away. This is what `threshold`, `min_r`/`max_r` and `exclude_deg`
        # are tuned against; it was the whole reason pupil_cam kept a separate
        # `_toy.py` after the other four were deleted.
        dash = pg.mkPen("#ffbf00", width=1, style=Qt.PenStyle.DashLine)
        self._ann_in = pg.PlotCurveItem(pen=dash)
        self._ann_out = pg.PlotCurveItem(pen=dash)
        self._pts_in = pg.ScatterPlotItem(size=5, pen=None,
                                          brush=pg.mkBrush("lime"))
        self._pts_out = pg.ScatterPlotItem(size=5, pen=None,
                                           brush=pg.mkBrush("red"))
        self._overlay = pg.PlotCurveItem(pen=pg.mkPen(style.HEX[self.key], width=2))
        # The search limit, in its own colour and NOT tied to the search
        # overlay: it changes what the tracker will accept, so it has to be
        # visible whenever it is in force.
        self._limit_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=2, style=Qt.PenStyle.DashLine))
        self._limit_ghost = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=1, style=Qt.PenStyle.DotLine))
        # Outline last so it draws on top of the points.
        for item in (self._ann_in, self._ann_out, self._pts_out, self._pts_in,
                     self._limit_curve, self._limit_ghost, self._overlay):
            vb.addItem(item)
        self._search_items = (self._ann_in, self._ann_out,
                              self._pts_in, self._pts_out)
        self._vb, self._gv = vb, gv
        vb.scene().sigMouseClicked.connect(self._on_click)
        vb.scene().sigMouseMoved.connect(self._on_move)
        self._set_search_visible(self.panel.settings.show_search
                                 if self.panel is not None else False)
        if self.panel is not None:
            self._draw_limit(self.panel.settings)

        # The eye-region controls live over the image, not in the settings
        # window: picking a region of the frame means looking at the frame, and
        # the settings are a separate floating window.
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(self._build_limit_bar())
        col.addWidget(row, 1)
        self.win.add_dock("Pupil cam", host, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

    def _set_search_visible(self, on: bool) -> None:
        for item in self._search_items:
            item.setVisible(on)

    def _on_click(self, ev) -> None:
        """One click handler, two jobs — placing the eye region wins.

        A pan drag never gets here: pyqtgraph only emits `sigMouseClicked` for a
        press+release that did not turn into a drag, so panning and zooming stay
        available while the region is being placed.
        """
        if self.panel is None or self._vb is None:
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        if self._btn_limit.isChecked():
            self._place_limit(p.x(), p.y())
            return
        self._seed_here(p.x(), p.y())

    def _seed_here(self, x: float, y: float) -> None:
        """Place the annulus by hand — the LabVIEW operator workflow, for when
        the auto-seed picks the wrong dark region and no threshold fixes it.

        Only while the search overlay is on: a stray click on the preview should
        not silently move the tracker's annulus.
        """
        if self._track is None or not self.panel.settings.show_search:
            return
        s = self.panel.settings
        lim = s.search_limit()
        if lim is not None and np.hypot(x - lim[0], y - lim[1]) > lim[2]:
            # The tracker would reject every fit made from this seed, so say so
            # rather than queue a seed that silently does nothing.
            self.win.status("click is outside the eye region — move the region "
                            "or clear it")
            return
        r = 0.5 * (s.min_r + s.max_r)
        self._track.seed(x, y, r)              # queued onto the tracker's thread
        self.win.status(f"pupil annulus seeded at ({x:.0f}, {y:.0f}) "
                        f"r={r:.0f} px")

    # ── the eye region (search limit) ──
    # Placed by two clicks — centre, then edge — with the circle following the
    # cursor in between. Not a press-drag: the viewbox owns dragging for
    # pan/zoom, and taking that over to draw would cost the ability to look
    # around the frame while placing the region.
    _FIT_MARGIN = 4.0          # region radius as a multiple of the fitted pupil

    def _build_limit_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)

        self._btn_limit = QPushButton("Set eye region")
        self._btn_limit.setCheckable(True)
        self._btn_limit.setToolTip(
            "Click the centre of the eye, then click again further out to set "
            "how far the tracker may look. Panning and zooming still work.\n"
            "Draw it generously — a couple of pupil radii of margin. The "
            "automatic seed gives up when more than half of what it searches is "
            "dark, and that is now this circle rather than the whole sensor.")
        self._btn_limit.toggled.connect(self._arm_limit)

        self._btn_limit_fit = QPushButton("From fit")
        self._btn_limit_fit.setToolTip(
            "Put the region around the pupil the tracker is currently on, with "
            f"{self._FIT_MARGIN:.0f}x its radius of margin. The quickest way in "
            "once anything is tracking at all.")
        self._btn_limit_fit.clicked.connect(self._limit_from_fit)

        self._btn_limit_off = QPushButton("Clear")
        self._btn_limit_off.setToolTip("Search the whole frame again.")
        self._btn_limit_off.clicked.connect(self._clear_limit)

        self._lbl_limit = QLabel()
        self._lbl_limit.setStyleSheet("color:#9aa0a6;")
        # Let it be clipped rather than hold the dock open at its own width —
        # the preview is what the dock is for.
        self._lbl_limit.setMinimumWidth(1)
        for w in (self._btn_limit, self._btn_limit_fit, self._btn_limit_off):
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

    def _limit_from_fit(self) -> None:
        """The region from whatever the tracker is currently on."""
        if self._last_fit is None:
            self.win.status("no pupil is being tracked — place the region by "
                            "hand, or seed the tracker first")
            return
        cx, cy, r = self._last_fit
        # Floor at the largest pupil the operator allows, so a fit taken on a
        # constricted pupil still leaves room for it to dilate.
        s = self.panel.settings
        rr = max(self._FIT_MARGIN * r, 2.5 * s.max_r)
        self.panel.set_limit(cx, cy, rr)
        self.win.status(f"eye region set from the current fit: "
                        f"({cx:.0f}, {cy:.0f}) r={rr:.0f} px")

    def _clear_limit(self) -> None:
        self._btn_limit.setChecked(False)
        self.panel.clear_limit()
        self.win.status("eye region cleared — searching the whole frame")

    def _refresh_limit_bar(self) -> None:
        if self.panel is None or self._lbl_limit is None:
            return
        lim = self.panel.settings.search_limit()
        if self._btn_limit.isChecked():
            self._lbl_limit.setText("click the centre of the eye"
                                    if self._limit_centre is None
                                    else "now click the outer edge")
        elif lim is None:
            self._lbl_limit.setText("whole frame")
        else:
            self._lbl_limit.setText(f"({lim[0]:.0f}, {lim[1]:.0f}) "
                                    f"r {lim[2]:.0f}")
        self._btn_limit_off.setEnabled(lim is not None)
        self._btn_limit_fit.setEnabled(self._last_fit is not None)

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
        # Both pupil workers declare set_exposure (`ExposureControl`) — the
        # mock's is documented as "kept for API parity" — so the old
        # hasattr() here was hedging against a case that does not exist. If a
        # future worker genuinely lacks it, this says so by name.
        if isinstance(self.worker, ExposureControl):
            self.worker.set_exposure(us)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings
        if s.video_path:
            # A third source beside real and mock, and the reason §5b A3 is worth
            # revisiting: the `if emulate:` pair below is now an `if` chain.
            # Checked before emulate so a clip replays either way — the point is
            # to tune tracking on real frames, which has nothing to do with
            # whether the rest of the rig is simulated.
            try:
                cam = self._adopt(VideoFileCameraWorker(s.video_path, fps=s.fps))
            except Exception as e:
                # A missing or compressed file must not kill the session: say so
                # and fall back, rather than leaving the operator with a dead tab.
                print(f"[main] pupil video {s.video_path!r} unusable ({e}) "
                      f"— falling back to the camera")
                self.win.status(f"pupil video unusable: {e}")
                cam = self._adopt(MockPupilCameraWorker(fps=s.fps) if emulate
                                  else PupilCameraWorker(
                                      exposure_us=s.exposure_us, fps=s.fps))
        elif emulate:
            cam = self._adopt(MockPupilCameraWorker(fps=s.fps))
        else:
            # cam=None → the worker opens/closes its own Basler on its thread.
            cam = self._adopt(PupilCameraWorker(exposure_us=s.exposure_us,
                                                fps=s.fps))
        # A fresh tracker per session, so no annulus lock is carried across one.
        self._track = PupilTrackWorker(cam.get_latest, history=PLOT_HISTORY)
        self._track.error.connect(self.win.on_worker_error)
        self._track.configure(**track_params(s))
        self._y.clear()

    def start(self) -> None:
        super().start()                 # camera first; the tracker idles until
        if self._track is not None:     # there is something to track
            self._track.start()

    def stop(self) -> None:
        if self._track is not None:     # stop the consumer before the producer
            self._track.stop()
            self._track = None
        super().stop()

    # ── display ──
    def update_display(self) -> None:
        if self._track is None:
            return
        radii = self._track.take_radii()
        if radii:
            self._y.extend(radii)
            del self._y[:-PLOT_HISTORY]
            self._curve.setData(self._y)

        pair = self._track.get_latest()
        if pair is None:
            return
        frame, res = pair               # the fit belongs to THIS frame
        # No `levels=` here: the LUT bar owns the levels, so forcing them every
        # frame would undo any contrast the user drags.
        self._img.setImage(frame, autoLevels=False)
        # Kept so "From fit" can put the region around whatever is being
        # tracked; only a good fit updates it, so a blink does not move it.
        if res.found:
            self._last_fit = (float(res.center_x), float(res.center_y),
                              float(res.radius))
            if self._btn_limit_fit is not None and not self._btn_limit_fit.isEnabled():
                self._btn_limit_fit.setEnabled(True)
        self._draw_outline(res)

    def _draw_outline(self, res) -> None:
        self._draw_search(res)
        if res.center_x is None or res.radius is None:
            self._overlay.setData([], [])
            return
        th = self._theta                              # precomputed once
        cx, cy = float(res.center_x), float(res.center_y or 0.0)
        if res.axes is None:
            self._overlay.setData(cx + res.radius * np.cos(th),
                                  cy + res.radius * np.sin(th))
            return
        a, b = res.axes
        t = np.radians(float(res.angle or 0.0))
        ca, sa = np.cos(t), np.sin(t)
        u, v = a * np.cos(th), b * np.sin(th)
        self._overlay.setData(cx + u * ca - v * sa, cy + u * sa + v * ca)

    def _draw_search(self, res) -> None:
        """The annulus and the per-ray edge points, green kept / red rejected.

        Skipped entirely when the overlay is off — this runs in the 30 Hz
        display tick and a scatter of `n_rays` points per frame is not free.
        """
        if self.panel is None or not self.panel.settings.show_search:
            return
        if res.edge_x is None or not len(res.edge_x):
            for item in self._search_items:
                item.setData([], [])
            return
        keep = (res.inliers if res.inliers is not None
                else np.ones(len(res.edge_x), dtype=bool))
        self._pts_in.setData(res.edge_x[keep], res.edge_y[keep])
        self._pts_out.setData(res.edge_x[~keep], res.edge_y[~keep])

        if res.radius is None:
            self._ann_in.setData([], [])
            self._ann_out.setData([], [])
            return
        th = self._theta
        cx, cy = float(res.center_x), float(res.center_y or 0.0)
        # The band the rays actually swept, as fractions of the fitted radius —
        # the same 0.45/1.55 the toy drew.
        for item, rr in ((self._ann_in, res.radius * 0.45),
                         (self._ann_out, res.radius * 1.55)):
            item.setData(cx + rr * np.cos(th), cy + rr * np.sin(th))

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"pupil_exposure_us":   s.exposure_us,
                "pupil_fps":           s.fps,
                "pupil_threshold":     s.threshold,
                "pupil_min_r":         s.min_r,
                "pupil_max_r":         s.max_r,
                "pupil_edge_select":   s.edge_select,
                "pupil_smooth_sigma":  s.smooth_sigma,
                "pupil_min_confidence": s.min_confidence,
                "pupil_smooth_median": s.smooth_median,
                "pupil_smooth_ema":    s.smooth_ema,
                # 0 = whole frame. Recorded because it decides which fits were
                # accepted, so a trace cannot be read without it.
                "pupil_limit_x":       s.limit_x,
                "pupil_limit_y":       s.limit_y,
                "pupil_limit_r":       s.limit_r,
                # "" for the camera. Recorded because frames replayed from a
                # clip are not this session's data, and nothing else in the file
                # would show it.
                "pupil_video":         s.video_path}
