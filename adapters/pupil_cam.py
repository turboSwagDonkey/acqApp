"""
The pupil camera's adapter: preview dock, LED, and the tracking thread.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

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
        self._vb = None
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
        # Outline last so it draws on top of the points.
        for item in (self._ann_in, self._ann_out, self._pts_out, self._pts_in,
                     self._overlay):
            vb.addItem(item)
        self._search_items = (self._ann_in, self._ann_out,
                              self._pts_in, self._pts_out)
        self._vb = vb
        vb.scene().sigMouseClicked.connect(self._on_click)
        self._set_search_visible(self.panel.settings.show_search
                                 if self.panel is not None else False)

        self.win.add_dock("Pupil cam", row, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

    def _set_search_visible(self, on: bool) -> None:
        for item in self._search_items:
            item.setVisible(on)

    def _on_click(self, ev) -> None:
        """Place the annulus by hand — the LabVIEW operator workflow.

        For when the auto-seed picks the wrong dark region and no threshold
        fixes it. Only while the search overlay is on: a stray click on the
        preview should not silently move the tracker's annulus.
        """
        if self._track is None or self.panel is None:
            return
        if not self.panel.settings.show_search:
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        s = self.panel.settings
        r = 0.5 * (s.min_r + s.max_r)
        self._track.seed(p.x(), p.y(), r)      # queued onto the tracker's thread
        self.win.status(f"pupil annulus seeded at ({p.x():.0f}, {p.y():.0f}) "
                        f"r={r:.0f} px")

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
                # "" for the camera. Recorded because frames replayed from a
                # clip are not this session's data, and nothing else in the file
                # would show it.
                "pupil_video":         s.video_path}
