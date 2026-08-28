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
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from acqApp import config
from acqApp.acq.devices import ExposureControl
from acqApp.adapters.base import (PLOT_HISTORY, DragRectViewBox, ModuleAdapter,
                                  _image_view, _plot)
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
        self._limit_curve = None        # the box in force
        self._limit_ghost = None        # rubber band while it is being dragged
        self._vb = None
        self._gv = None
        self._btn_limit = None
        self._btn_limit_off = None
        self._lbl_limit = None
        self._cmb_view = None
        self._view_mode = "full"        # "full" | "bare" | "crop"
        self._theta = np.linspace(0, 2 * np.pi, 48)
        # Cached copy of the panel's settings, refreshed by `_on_settings` —
        # so the per-tick display path reads a plain attribute instead of
        # rebuilding a whole PupilSettings from ~25 widgets every frame.
        self._settings: PupilSettings | None = None
        self._last_img_rect: QRectF | None = None   # skip a no-op setRect
        # ── tracking ──
        self._track: PupilTrackWorker | None = None
        self._fit_curve = None          # the fitted ellipse
        self._pin_curve = None          # the operator's pinned reflections
        self._mask_img = None           # what reflection removal blanked
        self._mask_rgba = None          # reused buffer for _draw_mask
        self._btn_pin = None
        self._curve = None              # the radius trace
        self._trace: list[tuple[float, bool]] = []  # (radius, is_blink), rolling
        self._blink_regions: list = []  # pooled LinearRegionItems, shown/hidden
        self._plot_widget = None
        self._last_frame = None         # newest displayed frame, for sizing a pin
        self._said: str | None = None   # last tracker complaint, so it is said once

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = PupilSettingsPanel(
            config.load_dataclass(PupilSettings, self.key))
        self.panel.exposure_changed.connect(self._on_exposure)
        self.panel.led_toggled.connect(self._on_led)
        self.panel.settings_changed.connect(self._on_settings)
        self._settings = self.panel.settings    # seed the cache; _on_settings
        return self.panel                       # keeps it fresh from here on

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("Pupil radius", "Radius", "px", "Frame", self.key)
        self._plot_widget = pw
        return pw

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        prev_limit = None if self._settings is None else self._settings.search_limit()
        self._settings = s
        self._draw_limit(s)
        self._draw_pins(s)
        self._refresh_limit_bar()
        # Only refit on an actual region change — this fires on every edit in
        # the panel (exposure, threshold, ...), not just a moved region.
        if (self._view_mode == "crop" and self._vb is not None
                and s.search_limit() != prev_limit):
            self._vb.autoRange()
        # The worker holds its own copy so it never reads a half-edited panel
        # from another thread.
        if self._track is not None:
            self._track.configure(s)

    def build_views(self) -> None:
        self._img, hist, gv, vb, row = _image_view(DragRectViewBox)
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
        vb.dragged.connect(self._on_limit_drag)
        if self.panel is not None:
            self._draw_limit(self.panel.settings)
            self._draw_pins(self.panel.settings)
            self._apply_view_mode()

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
        """Place a pin. The eye region uses a drag (`_on_limit_drag`), not a
        click, so only pinning is left here."""
        if self.panel is None or self._vb is None:
            return
        if self._btn_pin is None or not self._btn_pin.isChecked():
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        self._place_pin(p.x(), p.y())

    # ── the eye region ──
    # Placed by a press-drag from one corner to the other, with the box
    # following the cursor as it is dragged — `DragRectViewBox` owns the
    # armed/unarmed drag-vs-pan split, so wheel-zoom is untouched either way.
    def _build_limit_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)

        self._btn_limit = QPushButton("Set eye region")
        self._btn_limit.setCheckable(True)
        self._btn_limit.setToolTip(
            "Press and drag a box around the eye. While armed, drag draws the "
            "box instead of panning; wheel-zoom still works.")
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

        self._cmb_view = QComboBox()
        for label, key in (("Full + region", "full"),
                           ("Full, no overlay", "bare"),
                           ("Cropped to region", "crop")):
            self._cmb_view.addItem(label, key)
        self._cmb_view.setToolTip(
            "How the preview shows the frame — the region itself is unchanged "
            "by this, only how it is displayed.")
        self._cmb_view.currentIndexChanged.connect(self._on_view_mode_changed)

        lay.addWidget(QLabel("Eye:"))
        for w in (self._btn_limit, self._btn_limit_off, self._btn_pin):
            lay.addWidget(w)
        lay.addWidget(self._lbl_limit, 1)
        lay.addWidget(QLabel("View:"))
        lay.addWidget(self._cmb_view)
        self._refresh_limit_bar()
        return bar

    def _arm_limit(self, on: bool) -> None:
        if on and self._btn_pin is not None:
            self._btn_pin.setChecked(False)     # one mode at a time
        if self._limit_ghost is not None:
            self._limit_ghost.setData([], [])
        if self._vb is not None:
            self._vb.set_draw_mode(on)
        self._refresh_limit_bar()

    def _on_limit_drag(self, x0: float, y0: float, x1: float, y1: float,
                       finished: bool) -> None:
        """`DragRectViewBox.dragged`, only ever emitted while armed."""
        self._limit_ghost.setData(*self._rect_xy(x0, y0, x1, y1))
        self._refresh_limit_bar()
        if finished:
            if x1 - x0 >= 1.0 and y1 - y0 >= 1.0:
                self.panel.set_limit(x0, y0, x1, y1)
                self.win.status(
                    f"eye region set at ({x0:.0f}, {y0:.0f})-({x1:.0f}, {y1:.0f})")
            self._limit_ghost.setData([], [])
            self._btn_limit.setChecked(False)   # done — no toggle to remember

    @staticmethod
    def _rect_xy(x0: float, y0: float, x1: float, y1: float):
        """A closed rectangle outline as (xs, ys), for PlotCurveItem."""
        return (np.array([x0, x1, x1, x0, x0], float),
                np.array([y0, y0, y1, y1, y0], float))

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
            self._lbl_limit.setText("drag from one corner to the other")
        elif self._btn_pin is not None and self._btn_pin.isChecked():
            self._lbl_limit.setText("click a reflection to pin or unpin it")
        elif lim is None:
            self._lbl_limit.setText("no region")
        else:
            self._lbl_limit.setText(
                f"({lim[0]:.0f}, {lim[1]:.0f})-({lim[2]:.0f}, {lim[3]:.0f})")
        self._btn_limit_off.setEnabled(lim is not None)

    def _on_view_mode_changed(self, *_a) -> None:
        if self._cmb_view is not None:
            self._view_mode = self._cmb_view.currentData()
        self._apply_view_mode()

    def _apply_view_mode(self) -> None:
        """"bare" hides every overlay; "crop" (re)fits the view to the region
        the next time a frame is drawn — see `_display_frame`."""
        bare = self._view_mode == "bare"
        for item in (self._limit_curve, self._fit_curve, self._pin_curve,
                     self._mask_img):
            if item is not None:
                item.setVisible(not bare)
        if self._vb is not None:
            self._vb.autoRange()

    def _draw_limit(self, s) -> None:
        """Outline the region in force, or clear it when there is none."""
        if self._limit_curve is None:
            return
        lim = s.search_limit()
        if lim is None:
            self._limit_curve.setData([], [])
            return
        self._limit_curve.setData(*self._rect_xy(*lim))

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
        # Show the camera's REAL measured rate, not the requested one — the
        # same pattern as devices/voltage_cam/panel.py's set_measured_rate.
        # Binds `panel`, not `self`, so the connection (which outlives this
        # call, living on `cam`) only keeps the panel alive, not the whole
        # adapter — worker, tracker and plot curves included.
        if hasattr(cam, "fps_update"):
            panel = self.panel
            cam.fps_update.connect(lambda _n, fps: panel.set_measured_rate(fps))
        # A fresh tracker per session: EyeLoop walks out from the previous
        # frame's centre, and last session's centre describes a different eye.
        self._track = PupilTrackWorker(cam.get_latest, s, history=PLOT_HISTORY)
        self._track.error.connect(self.win.on_worker_error)
        self._trace.clear()
        for reg in self._blink_regions:     # a stale band must not outlive its session
            reg.setVisible(False)
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
        self.panel.set_measured_rate(None)          # back to the requested rate

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
        tracked = self._track.take_tracked()
        if tracked and self._curve is not None:
            self._trace.extend(tracked)
            del self._trace[:-PLOT_HISTORY]
            self._curve.setData([radius for radius, _blink in self._trace])
            self._update_blink_overlay()

        tr = self._track.get_latest()
        if tr is None:
            return
        self._last_frame = tr.frame
        shown, rect = self._display_frame(tr.frame)
        # No `levels=` here: the LUT bar owns the levels, so forcing them every
        # frame would undo any contrast the user drags.
        self._img.setImage(shown, autoLevels=False)
        # Positions the image at its own full-frame pixel coordinates even when
        # cropped, so the fit/pin/region overlays (still in full-frame pixels)
        # stay aligned instead of drawing over a shifted image. Skipped when
        # unchanged — every tick in "full"/"bare" view, since the frame size
        # doesn't move — rather than making pyqtgraph redo the transform for
        # the same rect it already has.
        if rect != self._last_img_rect:
            self._img.setRect(rect)
            self._last_img_rect = rect
        self._draw_fit(tr.fit)
        self._draw_mask(tr)

    def _display_frame(self, frame):
        """What `update_display` paints: the region crop in "crop" view, the
        whole frame otherwise. `(array, QRectF)` — the rect positions it.

        Reads the cached `self._settings`, not `self.panel.settings` — that
        property rebuilds a whole `PupilSettings` from every widget in the
        panel, and this runs once a display tick.
        """
        h, w = frame.shape[:2]
        if self._view_mode == "crop" and self._settings is not None:
            box = self._settings.crop_box(frame.shape)
            if box is not None:
                x0, y0, x1, y1 = box
                return frame[y0:y1, x0:x1], QRectF(x0, y0, x1 - x0, y1 - y0)
        return frame, QRectF(0, 0, w, h)

    def _update_blink_overlay(self) -> None:
        """Shade each contiguous run of suspected-blink frames behind the
        radius trace. Rebuilt from `self._trace` every tick — the trace is a
        rolling window, so a frame's x position (its index in `_trace`) shifts
        as older points drop off the front, and every region must shift with
        it.

        A pool of `LinearRegionItem`s is reused rather than recreated: a run
        of blinks would otherwise churn plot items every display tick.
        """
        runs: list[tuple[float, float]] = []
        n = len(self._trace)
        i = 0
        while i < n:
            if self._trace[i][1]:
                j = i
                while j < n and self._trace[j][1]:
                    j += 1
                runs.append((i - 0.5, j - 0.5))     # padded to cover the point
                i = j
            else:
                i += 1
        while len(self._blink_regions) < len(runs):
            reg = pg.LinearRegionItem(movable=False,
                                      brush=pg.mkBrush(220, 40, 40, 60),
                                      pen=pg.mkPen(None))
            reg.setZValue(-10)          # behind the radius curve
            if self._plot_widget is not None:
                self._plot_widget.addItem(reg)
            self._blink_regions.append(reg)
        for reg, span in zip(self._blink_regions, runs):
            reg.setRegion(span)
            reg.setVisible(True)
        for reg in self._blink_regions[len(runs):]:
            reg.setVisible(False)

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
        # Cheap test first: most frames have no mask to draw at all.
        show = (tr.mask is not None and tr.box is not None
                and self._settings is not None and self._settings.cr_show_mask)
        if not show:
            self._mask_img.clear()
            return
        # Reused across ticks rather than a fresh np.zeros(...) every one —
        # this runs every display tick for as long as "Show what was removed"
        # is left on, which is exactly the tuning workflow it exists for.
        # Only the alpha channel changes frame to frame; red is fixed once.
        if self._mask_rgba is None or self._mask_rgba.shape[:2] != tr.mask.shape:
            self._mask_rgba = np.zeros(tr.mask.shape + (4,), np.uint8)
            self._mask_rgba[..., 0] = 255
        self._mask_rgba[..., 3] = np.where(tr.mask, 140, 0)
        x0, y0, x1, y1 = tr.box
        self._mask_img.setImage(self._mask_rgba, autoLevels=False)
        self._mask_img.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    # ── recording ──
    # The five ellipse streams, and what each one is. Scalars, so they land in
    # `/<stream>/values` beside the frames and share the session clock.
    FIT_STREAMS = ("pupil_x", "pupil_y", "pupil_major", "pupil_minor",
                   "pupil_angle")
    # A sixth, alongside them: 1.0/0.0 flagged/not, NaN where there was no fit
    # at all — a blink cannot be judged without a radius to judge it against.
    BLINK_STREAM = "pupil_blink"

    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))
        if self._track is not None:
            self._track.set_fit_sink(
                lambda fit, is_blink, at: self._record_fit(rec, fit, is_blink, at))

    def _record_fit(self, rec, fit, is_blink: bool, at: float) -> None:
        """One tracked frame's ellipse (+ blink flag); NaN in all six where
        there was no fit, so a gap is in the file rather than a row nobody
        wrote.

        Runs on the tracker's thread. `at` is when the frame was pulled, not
        exposed — these frames carry no camera timestamp, so this stream and
        `pupil_cam`'s are stamped independently.
        """
        vals = ((fit.center_x, fit.center_y, fit.semi_major, fit.semi_minor,
                 fit.angle_deg) if fit is not None else (float("nan"),) * 5)
        for name, v in zip(self.FIT_STREAMS, vals):
            rec.put(name, float(v), at=at)
        rec.put(self.BLINK_STREAM,
                float("nan") if fit is None else float(is_blink), at=at)

    def detach_sink(self) -> None:
        super().detach_sink()
        if self._track is not None:
            self._track.set_fit_sink(None)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"pupil_exposure_us": s.exposure_us,
                "pupil_fps":         s.fps,
                # All 0 = no region. Recorded because it is operator-set
                # geometry that nothing else in the file would show.
                "pupil_limit_x0":    s.limit_x0,
                "pupil_limit_y0":    s.limit_y0,
                "pupil_limit_x1":    s.limit_x1,
                "pupil_limit_y1":    s.limit_y1,
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
                # Averaging changes the trace itself (not just the display),
                # so it must travel with it the same way threshold does.
                "pupil_smooth":          s.smooth,
                "pupil_smooth_window":   s.smooth_window,
                # Same reasoning: the flag is a property of the recorded trace,
                # not a display-only choice, so the threshold that produced it
                # travels with it too.
                "pupil_blink_detect":         s.blink_detect,
                "pupil_blink_drop_frac":      s.blink_drop_frac,
                "pupil_blink_baseline_window": s.blink_baseline_window,
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
        out = {"pupil_frames_tracked": self._track.frames_seen,
               "pupil_fits":           self._track.fits}
        if self.panel.settings.blink_detect:
            out["pupil_blinks_flagged"] = self._track.blinks
        return out
