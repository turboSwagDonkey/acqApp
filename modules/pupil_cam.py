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
from acqApp.devices import ExposureControl
from acqApp.modules.base import (PLOT_HISTORY, ModuleAdapter, _image_view,
                                 _plot)
from acqApp.pupil_cam.acquisition import (MockPupilCameraWorker,
                                          PupilCameraWorker)
from acqApp.pupil_cam.control import LedController, MockLedController
from acqApp.pupil_cam.settings import (PupilSettings,
                                       SettingsPanel as PupilSettingsPanel)
from acqApp.pupil_cam.track_worker import PupilTrackWorker, track_params


class PupilCamModule(ModuleAdapter):
    key = "pupil_cam"
    tab_label = "Pupil cam"
    plot_label = "Pupil"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._overlay = None
        self._curve = None
        self._y: list[float] = []
        # Tracking runs on its own thread (see pupil_cam/track_worker.py): it is
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

        self._overlay = pg.PlotCurveItem(pen=pg.mkPen(style.HEX[self.key], width=2))
        vb.addItem(self._overlay)
        self.win.add_dock("Pupil cam", row, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

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
        if emulate:
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

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"pupil_exposure_us": s.exposure_us,
                "pupil_fps":         s.fps,
                "pupil_threshold":   s.threshold,
                "pupil_min_r":       s.min_r,
                "pupil_max_r":       s.max_r}
