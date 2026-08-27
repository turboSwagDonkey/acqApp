"""
The pupil camera's adapter: preview dock, LED, the eye region and the tracker.

The hand-rolled tracker was removed on 2026-08-24 (PLAN §7 (ai)) and stays
retired in `archive/pupil_tracking/`. EyeLoop replaced it on 2026-08-26, behind
`devices/pupil_cam/track_worker.py`; this file is where it reaches the operator
— the ellipse, the radius trace, the pins, and the five recorded streams.

**Tracking never gates the camera.** With no clone, or with tracking off, the
worker is a pass-through and the preview and recording are what they were.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from acqApp import config
from acqApp.acq.devices import ExposureControl
from acqApp.adapters.base import (PLOT_HISTORY, ModuleAdapter, _image_view,
                                  _plot)
from acqApp.devices.pupil_cam.acquisition import (MockPupilCameraWorker,
                                          PupilCameraWorker)
from acqApp.devices.pupil_cam.control import LedController, MockLedController
from acqApp.devices.pupil_cam.panel import SettingsPanel as PupilSettingsPanel
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.track_worker import PupilTrackWorker
from acqApp.devices.pupil_cam.video import VideoFileCameraWorker


class PupilCamModule(ModuleAdapter):
    key = "pupil_cam"
    tab_label = "Pupil cam"
    plot_label = "Pupil"        # the radius trace; empty again if EyeLoop goes

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
        # ── tracking ──
        self._track: PupilTrackWorker | None = None
        self._fit_curve = None          # the fitted ellipse
        self._pin_curve = None          # the operator's pinned reflections
        self._mask_img = None           # what reflection removal blanked
        self._btn_pin = None
        self._curve = None              # the radius trace
        self._y: list[float] = []
        self._last_frame = None         # newest displayed frame, for sizing a pin
        self._said: str | None = None   # last tracker complaint, so it is said once

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = PupilSettingsPanel(
            config.load_dataclass(PupilSettings, self.key))
        self.panel.exposure_changed.connect(self._on_exposure)
        self.panel.led_toggled.connect(self._on_led)
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("Pupil radius", "Radius", "px", "Frame", self.key)
        return pw

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        self._draw_limit(s)
        self._draw_pins(s)
        self._refresh_limit_bar()
        # The worker holds its own copy so it never reads a half-edited panel
        # from another thread.
        if self._track is not None:
            self._track.configure(s)

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
        # The fit, in the module's own colour so it cannot be read as the
        # region. `connect="finite"` lets one curve carry several closed
        # outlines separated by NaN — that is how the pins are drawn.
        self._fit_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#7fff6a", width=2), connect="finite")
        self._pin_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#ff9d3d", width=1), connect="finite")
        # Under the outlines: it says which pixels the fit never saw.
        self._mask_img = pg.ImageItem()
        self._mask_img.setZValue(1)
        vb.addItem(self._mask_img)
        for item in (self._limit_curve, self._limit_ghost, self._fit_curve,
                     self._pin_curve):
            vb.addItem(item)
        self._vb, self._gv = vb, gv
        vb.scene().sigMouseClicked.connect(self._on_click)
        vb.scene().sigMouseMoved.connect(self._on_move)
        if self.panel is not None:
            self._draw_limit(self.panel.settings)
            self._draw_pins(self.panel.settings)

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
        """Place the region, or a pin. A pan drag never gets here — pyqtgraph
        only emits `sigMouseClicked` for a press+release that did not become a
        drag."""
        if self.panel is None or self._vb is None:
            return
        armed_limit = self._btn_limit.isChecked()
        armed_pin = self._btn_pin is not None and self._btn_pin.isChecked()
        if not (armed_limit or armed_pin):
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        if armed_limit:
            self._place_limit(p.x(), p.y())
        else:
            self._place_pin(p.x(), p.y())

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
        self._btn_pin = QPushButton("Pin reflection")
        self._btn_pin.setCheckable(True)
        self._btn_pin.setToolTip(
            "Click a fixed reflection to mark it, and click a marked one again "
            "to remove it.\nPinned reflections are removed without the guards "
            "the automatic pass needs — they are rig geometry, so clear them "
            "when the optics move.")
        self._btn_pin.toggled.connect(self._arm_pin)

        lay.addWidget(QLabel("Eye:"))
        for w in (self._btn_limit, self._btn_limit_off, self._btn_pin):
            lay.addWidget(w)
        lay.addWidget(self._lbl_limit, 1)
        self._refresh_limit_bar()
        return bar

    def _arm_limit(self, on: bool) -> None:
        if on and self._btn_pin is not None:
            self._btn_pin.setChecked(False)     # one mode at a time
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

    # ── pinned reflections ──
    # Full-frame pixels, like the eye region: moving the region must not walk a
    # pin off the reflection it marks.
    def _arm_pin(self, on: bool) -> None:
        if on and self._btn_limit is not None:
            self._btn_limit.setChecked(False)
        if self._gv is not None:
            self._gv.setCursor(Qt.CursorShape.CrossCursor if on
                               else Qt.CursorShape.ArrowCursor)
        self._refresh_limit_bar()

    def _place_pin(self, x: float, y: float) -> None:
        """Add a pin, or remove the one clicked. Its radius is measured off the
        blob actually under the click, so a pin covers what it marks."""
        pins = list(self.panel.settings.cr_pins)
        for i, (px, py, pr) in enumerate(pins):
            if np.hypot(x - px, y - py) <= pr:
                pins.pop(i)
                self.panel.set_pins(pins)
                self.win.status(f"reflection at ({px:.0f}, {py:.0f}) unpinned")
                return

        frame = self._last_frame
        r = 8.0
        if frame is not None:
            try:
                from acqApp.devices.pupil_cam.eyeloop_tracker import (
                    measure_reflection)
                r = measure_reflection(
                    frame, (x, y), threshold=self.panel.settings.cr_threshold)
            except Exception as e:      # no clone, no cv2 — a pin is still useful
                print(f"[pupil_cam] could not size the pin ({e}) — using {r:g} px")
        pins.append((float(x), float(y), float(r)))
        self.panel.set_pins(pins)
        self.win.status(f"reflection pinned at ({x:.0f}, {y:.0f}) r={r:.0f} px")

    def _draw_pins(self, s) -> None:
        """Every pin as its own closed circle, NaN-separated in one curve."""
        if self._pin_curve is None:
            return
        xs: list[float] = []
        ys: list[float] = []
        th = self._theta
        for cx, cy, r in s.cr_pins:
            xs.extend(cx + r * np.cos(th))
            ys.extend(cy + r * np.sin(th))
            xs.append(np.nan)           # break, so the circles are not joined
            ys.append(np.nan)
        self._pin_curve.setData(np.array(xs, float), np.array(ys, float))

    def _refresh_limit_bar(self) -> None:
        if self.panel is None or self._lbl_limit is None:
            return
        lim = self.panel.settings.search_limit()
        if self._btn_limit.isChecked():
            self._lbl_limit.setText("click the centre of the eye"
                                    if self._limit_centre is None
                                    else "now click the outer edge")
        elif self._btn_pin is not None and self._btn_pin.isChecked():
            self._lbl_limit.setText("click a reflection to pin or unpin it")
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
        cam = self._build_camera(s, emulate)
        # A fresh tracker per session: EyeLoop walks out from the previous
        # frame's centre, and last session's centre describes a different eye.
        self._track = PupilTrackWorker(cam.get_latest, s, history=PLOT_HISTORY)
        self._track.error.connect(self.win.on_worker_error)
        self._y.clear()
        self._said = None

    def _build_camera(self, s, emulate: bool):
        if s.video_path:
            # A third source beside real and mock — the reason §5b A3 is worth
            # revisiting. Checked before emulate so a clip replays either way.
            try:
                return self._adopt(VideoFileCameraWorker(s.video_path, fps=s.fps))
            except Exception as e:
                # A missing or compressed file must not kill the session: say so
                # and fall back, rather than leaving a dead tab.
                print(f"[main] pupil video {s.video_path!r} unusable ({e}) "
                      f"— falling back to the camera")
                self.win.status(f"pupil video unusable: {e}")
                return self._adopt(
                    MockPupilCameraWorker(fps=s.fps) if emulate
                    else PupilCameraWorker(exposure_us=s.exposure_us, fps=s.fps))
        if emulate:
            return self._adopt(MockPupilCameraWorker(fps=s.fps))
        # cam=None → the worker opens/closes its own Basler on its thread.
        return self._adopt(PupilCameraWorker(exposure_us=s.exposure_us,
                                             fps=s.fps))

    def start(self) -> None:
        super().start()                 # the camera first; the tracker idles
        if self._track is not None:     # until there is something to track
            self._track.start()

    def stop(self) -> None:
        if self._track is not None:     # the consumer before the producer
            self._track.stop()
            self._track = None
        super().stop()

    # ── display ──
    def update_display(self) -> None:
        """Paint the newest tracked frame.

        The frames come from the tracker, not from the camera: `get_latest()`
        consumes, so two readers would take turns and the ellipse would be
        drawn over a frame it was not fitted to.
        """
        if self._track is None:
            return
        self._say_tracker_state()
        radii = self._track.take_radii()
        if radii and self._curve is not None:
            self._y.extend(radii)
            del self._y[:-PLOT_HISTORY]
            self._curve.setData(self._y)

        tr = self._track.get_latest()
        if tr is None:
            return
        self._last_frame = tr.frame
        # No `levels=` here: the LUT bar owns the levels, so forcing them every
        # frame would undo any contrast the user drags.
        self._img.setImage(tr.frame, autoLevels=False)
        self._draw_fit(tr.fit)
        self._draw_mask(tr)

    def last_frame(self):
        return self._last_frame

    def _say_tracker_state(self) -> None:
        """Say once why nothing is being tracked. A missing clone is otherwise
        invisible: the preview simply has no ellipse on it."""
        msg = self._track.track_error
        if msg == self._said:
            return
        self._said = msg
        if msg:
            self.win.status(f"pupil tracking off: {msg}")

    def _draw_fit(self, fit) -> None:
        """The fitted ellipse, or nothing. Cleared on every failed frame, so a
        stale outline can never stand in for a fit that did not happen."""
        if self._fit_curve is None:
            return
        if fit is None:
            self._fit_curve.setData([], [])
            return
        th = self._theta
        t = np.radians(float(fit.angle_deg))
        ca, sa = np.cos(t), np.sin(t)
        u, v = fit.semi_major * np.cos(th), fit.semi_minor * np.sin(th)
        self._fit_curve.setData(fit.center_x + u * ca - v * sa,
                                fit.center_y + u * sa + v * ca)

    def _draw_mask(self, tr) -> None:
        """Red over the pixels reflection removal blanked, inside the crop.

        The only way to see what `cr_threshold` is doing: the failure it guards
        against — masking the rim, which erases the boundary and inflates the
        radius — reports a perfectly good fit.
        """
        if self._mask_img is None:
            return
        # Cheap tests first: `panel.settings` builds a whole dataclass (6 us,
        # measured), and most frames have no mask to draw at all.
        show = (tr.mask is not None and tr.box is not None
                and self.panel is not None and self.panel.settings.cr_show_mask)
        if not show:
            self._mask_img.clear()
            return
        rgba = np.zeros(tr.mask.shape + (4,), np.uint8)
        rgba[..., 0] = 255
        rgba[..., 3] = np.where(tr.mask, 140, 0)
        x0, y0, x1, y1 = tr.box
        self._mask_img.setImage(rgba, autoLevels=False)
        self._mask_img.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    # ── recording ──
    # The five ellipse streams, and what each one is. Scalars, so they land in
    # `/<stream>/values` beside the frames and share the session clock.
    FIT_STREAMS = ("pupil_x", "pupil_y", "pupil_major", "pupil_minor",
                   "pupil_angle")

    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))
        if self._track is not None:
            self._track.set_fit_sink(
                lambda fit, at: self._record_fit(rec, fit, at))

    def _record_fit(self, rec, fit, at: float) -> None:
        """One tracked frame's ellipse; NaN in all five where there was no fit,
        so a gap is in the file rather than a row nobody wrote.

        Runs on the tracker's thread. `at` is when the frame was pulled, not
        exposed — these frames carry no camera timestamp, so this stream and
        `pupil_cam`'s are stamped independently.
        """
        vals = ((fit.center_x, fit.center_y, fit.semi_major, fit.semi_minor,
                 fit.angle_deg) if fit is not None else (float("nan"),) * 5)
        for name, v in zip(self.FIT_STREAMS, vals):
            rec.put(name, float(v), at=at)

    def detach_sink(self) -> None:
        super().detach_sink()
        if self._track is not None:
            self._track.set_fit_sink(None)

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
                "pupil_video":       s.video_path,
                # The tracking settings travel with the trace. Threshold above
                # all: it SETS the radius (a 60 % swing over 25-60 on the rig
                # clips) at an unchanged fit rate, so a pupil trace without the
                # threshold behind it is not reproducible.
                "pupil_track":           s.track,
                "pupil_tracker":         "eyeloop" if s.track else "",
                "pupil_track_threshold": s.track_threshold,
                "pupil_track_blur":      s.track_blur,
                "pupil_track_model":     s.track_model,
                "pupil_cr_remove":       s.cr_remove,
                "pupil_cr_threshold":    s.cr_threshold,
                "pupil_cr_pad":          s.cr_pad,
                "pupil_cr_ring":         s.cr_ring,
                "pupil_cr_reach":        s.cr_reach,
                # Flattened to [x, y, r, x, y, r…] for HDF5, as the retired
                # tracker's excluded angles were. Where the fixed reflections
                # were taken out is part of what produced the radius.
                "pupil_cr_pins":         [v for pin in s.cr_pins for v in pin]}

    def final_metadata(self) -> dict[str, Any]:
        """How tracking went, as against how it was configured.

        Both numbers are needed to read the trace: frames are dropped when a
        fit is slower than the camera. `fits`/`tracked` is the fit rate, which
        is a floor, NOT a quality measure — docs/EYELOOP.md.
        """
        if self._track is None or not self.panel.settings.track:
            return {}
        return {"pupil_frames_tracked": self._track.frames_seen,
                "pupil_fits":           self._track.fits}
