"""The pupil camera's adapter: preview dock, LED, eye region and the EyeLoop
tracker (`devices/pupil_cam/track_worker.py`). Tracking never gates the
camera: off or unavailable, the worker is a pass-through."""
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
                                  _image_view, _plot, led_controller)
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
    plot_label = "Pupil"

    def __init__(self, win) -> None:
        super().__init__(win)
        # None until build_views; _on_settings can fire before then.
        self._limit_curve = None
        self._limit_ghost = None        # rubber band while dragging
        self._vb = None
        self._gv = None
        self._btn_limit = None
        self._btn_limit_off = None
        self._lbl_limit = None
        self._cmb_view = None
        self._view_mode = "full"        # "full" | "bare" | "crop"
        self._theta = np.linspace(0, 2 * np.pi, 48)
        # Cached: `panel.settings` rebuilds from ~25 widgets per call.
        self._settings: PupilSettings | None = None
        self._last_img_rect: QRectF | None = None
        # ── tracking ──
        self._track: PupilTrackWorker | None = None
        self._fit_curve = None
        self._pin_curve = None
        self._mask_img = None
        self._mask_rgba = None
        self._btn_pin = None
        self._curve = None
        self._trace: list[tuple[float, bool]] = []  # (radius, is_blink)
        self._blink_regions: list = []  # pooled LinearRegionItems
        self._plot_widget = None
        self._last_frame = None
        self._said: str | None = None   # last tracker complaint, said once

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = PupilSettingsPanel(
            config.load_dataclass(PupilSettings, self.key))
        self.panel.exposure_changed.connect(self._on_exposure)
        self.panel.led_toggled.connect(self._on_led)
        self.panel.led_intensity_changed.connect(self._on_led_intensity)
        self.panel.settings_changed.connect(self._on_settings)
        self._settings = self.panel.settings
        return self.panel

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("Pupil radius", "Radius", "px", "Frame", self.key)
        self._plot_widget = pw
        return pw

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        prev_limit = None if self._settings is None else self._settings.search_limit()
        prev_auto = None if self._settings is None else self._settings.auto_levels
        self._settings = s
        if self._hist is not None:
            self._hist.setVisible(s.show_lut)
        if s.auto_levels and not prev_auto:
            self._reset_levels()
        self._sync_auto_to_lut(s.auto_levels)
        self._draw_limit(s)
        self._draw_pins(s)
        self._refresh_limit_bar()
        if (self._view_mode == "crop" and self._vb is not None
                and s.search_limit() != prev_limit):
            self._vb.autoRange()
        # The worker keeps its own copy, never a half-edited panel.
        if self._track is not None:
            self._track.configure(s)

    def build_views(self) -> None:
        self._img, hist, chk_auto, gv, vb, row, self._rec_dot = _image_view(
            DragRectViewBox)
        self._hist = hist
        self._chk_auto_lut = chk_auto
        self._chk_auto_lut.toggled.connect(self._sync_auto_from_lut)
        # 8-bit frames: an absolute 0-255 scale (Auto overrides per frame).
        self._img.setLevels((0, 255))
        hist.setHistogramRange(0, 255)
        hist.setLevels(0, 255)
        s = self.panel.settings if self.panel is not None else None
        if s is not None:
            hist.setVisible(s.show_lut)
            self._chk_auto_lut.setChecked(s.auto_levels)
        self.win.register_pg_view(hist)
        self.win.register_pg_view(gv)

        self._limit_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=2, style=Qt.PenStyle.DashLine))
        self._limit_ghost = pg.PlotCurveItem(
            pen=pg.mkPen("#00e5ff", width=1, style=Qt.PenStyle.DotLine))
        # connect="finite": NaN separates several outlines in one curve.
        self._fit_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#7fff6a", width=2), connect="finite")
        self._pin_curve = pg.PlotCurveItem(
            pen=pg.mkPen("#ff9d3d", width=1), connect="finite")
        self._mask_img = pg.ImageItem()
        self._mask_img.setZValue(1)
        vb.addItem(self._mask_img)
        for item in (self._limit_curve, self._limit_ghost, self._fit_curve,
                     self._pin_curve):
            vb.addItem(item)
        self._vb, self._gv = vb, gv
        vb.scene().sigMouseClicked.connect(self._on_click)
        vb.dragged.connect(self._on_limit_drag)
        if s is not None:
            self._draw_limit(s)
            self._draw_pins(s)
            self._apply_view_mode()

        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(self._build_limit_bar())
        col.addWidget(row, 1)
        self.win.add_dock("Pupil cam", host, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

    def _on_click(self, ev) -> None:
        """Place a pin (the region uses a drag)."""
        if self.panel is None or self._vb is None:
            return
        if self._btn_pin is None or not self._btn_pin.isChecked():
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        self._place_pin(p.x(), p.y())

    # ── the eye region ──
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
        self._lbl_limit.setMinimumWidth(1)      # clip, don't widen the dock
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
            "by this, only how it's displayed.")
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
            self._btn_pin.setChecked(False)
        if self._limit_ghost is not None:
            self._limit_ghost.setData([], [])
        if self._vb is not None:
            self._vb.set_draw_mode(on)
        self._refresh_limit_bar()

    def _on_limit_drag(self, x0: float, y0: float, x1: float, y1: float,
                       finished: bool) -> None:
        self._limit_ghost.setData(*self._rect_xy(x0, y0, x1, y1))
        self._refresh_limit_bar()
        if finished:
            if x1 - x0 >= 1.0 and y1 - y0 >= 1.0:
                self.panel.set_limit(x0, y0, x1, y1)
                self.win.status(
                    f"eye region set at ({x0:.0f}, {y0:.0f})-({x1:.0f}, {y1:.0f})")
            self._limit_ghost.setData([], [])
            self._btn_limit.setChecked(False)

    @staticmethod
    def _rect_xy(x0: float, y0: float, x1: float, y1: float):
        return (np.array([x0, x1, x1, x0, x0], float),
                np.array([y0, y0, y1, y1, y0], float))

    def _clear_limit(self) -> None:
        self._btn_limit.setChecked(False)
        self.panel.clear_limit()
        self.win.status("eye region cleared")

    # ── pinned reflections (full-frame px, so the region can move) ──
    def _arm_pin(self, on: bool) -> None:
        if on and self._btn_limit is not None:
            self._btn_limit.setChecked(False)
        if self._gv is not None:
            self._gv.setCursor(Qt.CursorShape.CrossCursor if on
                               else Qt.CursorShape.ArrowCursor)
        self._refresh_limit_bar()

    def _place_pin(self, x: float, y: float) -> None:
        """Add a pin sized to the blob under the click, or remove the one hit."""
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
            except Exception as e:      # no clone/cv2 — a pin is still useful
                print(f"[pupil_cam] could not size the pin ({e}) — using {r:g} px")
        pins.append((float(x), float(y), float(r)))
        self.panel.set_pins(pins)
        self.win.status(f"reflection pinned at ({x:.0f}, {y:.0f}) r={r:.0f} px")

    def _draw_pins(self, s) -> None:
        if self._pin_curve is None:
            return
        pins = s.cr_pins
        if not pins:
            self._pin_curve.setData([], [])
            return
        c = np.array(pins, float)                       # (n, 3): x, y, r
        n = len(self._theta)
        xs = np.full((len(c), n + 1), np.nan)           # NaN column breaks circles
        ys = np.full((len(c), n + 1), np.nan)
        xs[:, :n] = c[:, :1] + c[:, 2:] * np.cos(self._theta)
        ys[:, :n] = c[:, 1:2] + c[:, 2:] * np.sin(self._theta)
        self._pin_curve.setData(xs.ravel(), ys.ravel())

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
        bare = self._view_mode == "bare"
        for item in (self._limit_curve, self._fit_curve, self._pin_curve,
                     self._mask_img):
            if item is not None:
                item.setVisible(not bare)
        if self._vb is not None:
            self._vb.autoRange()

    def _draw_limit(self, s) -> None:
        if self._limit_curve is None:
            return
        lim = s.search_limit()
        if lim is None:
            self._limit_curve.setData([], [])
            return
        self._limit_curve.setData(*self._rect_xy(*lim))

    # ── controllers ──
    def build_controller(self, emulate: bool) -> None:
        self.controller = led_controller(
            emulate, "pupil_led", LedController, MockLedController,
            "eye-tracking LED")
        # A new controller starts at full scale; apply the saved level first.
        self.controller.set_intensity(self.panel.settings.led_intensity)

    def _on_led_intensity(self, fraction: float) -> None:
        if self.controller is not None:
            self.controller.set_intensity(fraction)

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
        if hasattr(cam, "hz_update"):
            # Bind the panel, not self, so the connection doesn't keep the
            # whole adapter alive.
            panel = self.panel
            cam.hz_update.connect(lambda _n, hz: panel.set_measured_rate(hz))
        # Fresh per session: EyeLoop searches from the previous centre.
        self._track = PupilTrackWorker(cam.get_latest, s, history=PLOT_HISTORY)
        self._track.error.connect(self.win.on_worker_error)
        self._trace.clear()
        for reg in self._blink_regions:
            reg.setVisible(False)
        self._said = None
        self._reset_levels()

    def _build_camera(self, s, emulate: bool):
        if s.video_path:                # emulate or not
            try:
                return self._adopt(
                    VideoFileCameraWorker(s.video_path, rate_hz=s.rate_hz))
            except Exception as e:
                print(f"[main] pupil video {s.video_path!r} unusable ({e}) "
                      f"— falling back to the camera")
                self.win.status(f"pupil video unusable: {e}")
        if emulate:
            return self._adopt(MockPupilCameraWorker(rate_hz=s.rate_hz))
        return self._adopt(PupilCameraWorker(exposure_us=s.exposure_us,
                                             rate_hz=s.rate_hz))

    def start(self) -> None:
        super().start()
        if self._track is not None:
            self._track.start()
        if self.panel.settings.led_follow_live:
            self._apply_led_follow(True)

    def stop(self) -> None:
        if self._track is not None:     # consumer before producer
            self._track.stop()
            self._track = None
        super().stop()
        self.panel.set_measured_rate(None)
        if self.panel.settings.led_follow_live:
            self._apply_led_follow(False)
        if self._fit_curve is not None:
            self._fit_curve.setData([], [])
        if self._mask_img is not None:
            self._mask_img.clear()

    # ── display ──
    def update_display(self) -> None:
        """Frames come from the tracker, not the camera: `get_latest()`
        consumes, and the ellipse must be drawn over the frame it was fit to."""
        self._sync_rec_dot()
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
        self._paint(shown, self._settings is not None
                    and self._settings.auto_levels)
        # Full-frame coordinates, so overlays align even when cropped.
        if rect != self._last_img_rect:
            self._img.setRect(rect)
            self._last_img_rect = rect
        self._draw_fit(tr.fit)
        self._draw_mask(tr)

    def _display_frame(self, frame):
        """-> (array, QRectF): the region crop in "crop" view, else the frame."""
        h, w = frame.shape[:2]
        if self._view_mode == "crop" and self._settings is not None:
            box = self._settings.crop_box(frame.shape)
            if box is not None:
                x0, y0, x1, y1 = box
                return frame[y0:y1, x0:x1], QRectF(x0, y0, x1 - x0, y1 - y0)
        return frame, QRectF(0, 0, w, h)

    def _update_blink_overlay(self) -> None:
        """Shade each run of blink frames. Rebuilt every tick: the trace
        scrolls. Region items are pooled, not recreated."""
        flags = np.fromiter((b for _r, b in self._trace), bool, len(self._trace))
        edges = np.diff(np.concatenate(([False], flags, [False])).astype(np.int8))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        runs = [(s - 0.5, e - 0.5) for s, e in zip(starts, ends)]
        while len(self._blink_regions) < len(runs):
            reg = pg.LinearRegionItem(movable=False,
                                      brush=pg.mkBrush(220, 40, 40, 60),
                                      pen=pg.mkPen(None))
            reg.setZValue(-10)
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
        """Say once why nothing is tracked; otherwise there's just no ellipse."""
        msg = self._track.track_error
        if msg == self._said:
            return
        self._said = msg
        if msg:
            self.win.status(f"pupil tracking off: {msg}")

    def _draw_fit(self, fit) -> None:
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
        """Red over what reflection removal blanked. The only way to see a
        rim wrongly masked: the fit still reports fine."""
        if self._mask_img is None:
            return
        show = (tr.mask is not None and tr.box is not None
                and self._settings is not None and self._settings.cr_show_mask)
        if not show:
            self._mask_img.clear()
            return
        if self._mask_rgba is None or self._mask_rgba.shape[:2] != tr.mask.shape:
            self._mask_rgba = np.zeros(tr.mask.shape + (4,), np.uint8)
            self._mask_rgba[..., 0] = 255
        np.multiply(tr.mask, 140, out=self._mask_rgba[..., 3],
                    casting="unsafe")
        x0, y0, x1, y1 = tr.box
        self._mask_img.setImage(self._mask_rgba, autoLevels=False)
        self._mask_img.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    # ── recording ──
    FIT_STREAMS = ("pupil_x", "pupil_y", "pupil_major", "pupil_minor",
                   "pupil_angle")
    # 1/0, NaN where there was no fit to judge a blink against.
    BLINK_STREAM = "pupil_blink"

    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))
        if self._track is not None:
            self._track.set_fit_sink(
                lambda fit, is_blink, at: self._record_fit(rec, fit, is_blink, at))

    def _record_fit(self, rec, fit, is_blink: bool, at: float) -> None:
        """On the tracker thread. NaN rows where there was no fit. `at` is
        when the frame was pulled (no camera timestamp)."""
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
        # Everything that shapes the trace travels with it — the threshold
        # above all, which moves the radius ~60% at an unchanged fit rate.
        return {"pupil_exposure_us": s.exposure_us,
                "pupil_rate_hz":     s.rate_hz,
                "pupil_limit_x0":    s.limit_x0,       # all 0 = no region
                "pupil_limit_y0":    s.limit_y0,
                "pupil_limit_x1":    s.limit_x1,
                "pupil_limit_y1":    s.limit_y1,
                "pupil_video":       s.video_path,     # "" = the camera
                "pupil_track":           s.track,
                "pupil_tracker":         "eyeloop" if s.track else "",
                "pupil_track_threshold": s.track_threshold,
                "pupil_track_blur":      s.track_blur,
                "pupil_track_model":     s.track_model,
                "pupil_smooth":          s.smooth,
                "pupil_smooth_window":   s.smooth_window,
                "pupil_blink_detect":         s.blink_detect,
                "pupil_blink_drop_frac":      s.blink_drop_frac,
                "pupil_blink_baseline_window": s.blink_baseline_window,
                "pupil_cr_remove":       s.cr_remove,
                "pupil_cr_threshold":    s.cr_threshold,
                "pupil_cr_pad":          s.cr_pad,
                "pupil_cr_ring":         s.cr_ring,
                "pupil_cr_reach":        s.cr_reach,
                # Flattened [x, y, r, ...] for HDF5.
                "pupil_cr_pins":         [v for pin in s.cr_pins for v in pin]}

    def final_metadata(self) -> dict[str, Any]:
        """Fit rate is a floor, not a quality measure (docs/EYELOOP.md)."""
        s = self.panel.settings
        if self._track is None or not s.track:
            return {}
        out = {"pupil_frames_tracked": self._track.frames_seen,
               "pupil_fits":           self._track.fits}
        if s.blink_detect:
            out["pupil_blinks_flagged"] = self._track.blinks
        return out
