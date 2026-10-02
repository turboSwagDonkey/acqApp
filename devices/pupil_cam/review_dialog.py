"""Offline pupil review window: load a clip, tune the tracker, fit every frame,
fix the bad ones by hand. The model is `review.py`; this is only the view.

Drag the ellipse (move / resize / rotate handles) to correct a frame; the radius
trace and the saved table follow. Hand-edited frames are orange on the trace.
Play (or Space) runs the clip so the fit can be watched; the LUT bar and Auto box
beside the image work as in the live pupil view and change only the display.
Auto beside Threshold suggests parameters from frames spread over the clip.

The left column mirrors the live Pupil tab: the same TrackingControls widget
(eye region, tracking, smoothing and blinks, reflections), with the clip in
place of the camera. "Next suspect" walks the frames worth checking (no fit,
or a radius jumping off its neighbours); they are shaded red on the trace.

`ReviewWidget` is the whole thing in two parts, `side_widget` (the
controls) and `view_widget` (clip, playback, trace): standalone it lays them
side by side; embedded (the Pupil tab's Review mode) the host places them.
`PupilReviewDialog` is the standalone window.

Every slot that reads the clip or writes the sidecar is guarded: an exception
escaping a Qt slot aborts the process, which in the rig is the whole app.
"""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from acqApp.widgets import collapsible_groups, compact, sections_help, spin
from acqApp.acq.worker import PullWorker
from acqApp.devices.pupil_cam.autotune import NEEDS_HELP
from acqApp.devices.pupil_cam.clip import FILE_FILTER as _VIDEO_FILTER
from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
from acqApp.devices.pupil_cam.review import PupilReview
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.track_worker import AutoTuneWorker
from acqApp.devices.pupil_cam.tracking_panel import TrackingControls

_LEVELS_EVERY = 10      # while playing, re-derive Auto levels this often
_AUTO_FRAMES = 24       # frames spread over the clip for Auto
_SUSPECT_BANDS = 300    # most red bands drawn; the rest still get a dot
_SEED_FRAMES = 5        # frames the user marks when Auto needs help
_PREVIEW_MS = 150       # settle time before re-fitting the shown frame
# Threads that outlived a wait, held until they end: dropping a running
# QThread aborts the process. Module-level, so a deleted dialog can't drop them.
_PARKED: list[PullWorker] = []


class _TrackAllWorker(PullWorker):
    """`PupilReview.track_all` off the GUI thread. Cancelled through its own
    flag: PullWorker.run() clears `_stop` on entry, which would lose a Stop
    pressed before the thread got going."""

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(bool)        # False = stopped early

    def __init__(self, review: PupilReview) -> None:
        super().__init__()
        self._review = review
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        done = self._review.track_all(self.progress.emit, self._cancel.is_set)
        self.finished_ok.emit(done)


def _ellipse_xy(fit: PupilFit, n: int = 64):
    th = np.linspace(0, 2 * np.pi, n)
    t = np.radians(fit.angle_deg)
    u, v = fit.semi_major * np.cos(th), fit.semi_minor * np.sin(th)
    return (fit.center_x + u * np.cos(t) - v * np.sin(t),
            fit.center_y + u * np.sin(t) + v * np.cos(t))


class ReviewWidget(QWidget):
    """`busy()` says whether live tracking is running: EyeLoop's config is
    process-global, so fitting a clip then would corrupt the live fits.
    `embedded`: build the two parts but leave placing them to the host."""

    def __init__(self, video: str = "", settings: PupilSettings | None = None,
                 busy: Callable[[], bool] | None = None, parent=None,
                 embedded: bool = False) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self._busy = busy or (lambda: False)
        self._seed = settings
        self.review: PupilReview | None = None
        self._worker: _TrackAllWorker | None = None
        self._auto_worker: AutoTuneWorker | None = None
        self._frame = 0
        self._data = None
        self._dirty = False
        self._loading = False        # programmatic widget/ROI updates
        # The eye region was invented on open, not drawn: Auto may move it.
        self._region_default = False
        self._build()
        self._set_running()
        if video:
            self.open_video(video)

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)

        # In the live tab's "Camera" place: where the frames come from.
        rec = QGroupBox("Recording")
        rf = QFormLayout(rec)
        rf.setSpacing(4)
        self._btn_open = QPushButton("Open recording…")
        self._btn_open.clicked.connect(self._pick_video)
        self._lbl_file = QLabel("no clip")
        self._lbl_file.setWordWrap(True)
        rf.addRow(self._btn_open)
        rf.addRow("Clip:", self._lbl_file)
        self._chk_lut = QCheckBox("Show LUT")
        self._chk_lut.setChecked(True)
        self._chk_lut.setToolTip("The brightness/contrast bar.")
        self._chk_auto_contrast = QCheckBox("Auto contrast")
        self._chk_auto_contrast.setChecked(True)
        self._chk_auto_contrast.setToolTip("Off: drag the bar's handles. "
                                           "Display only.")
        disp = QHBoxLayout()
        disp.addWidget(self._chk_lut)
        disp.addWidget(self._chk_auto_contrast)
        rf.addRow("Display:", disp)
        side.addWidget(rec)

        self._ctl = TrackingControls(self._seed or PupilSettings(), live=False)
        self._ctl.changed.connect(self._params_edited)
        self._ctl.auto_requested.connect(self._auto)
        self._ctl.region_wanted.connect(self._default_region)
        side.addWidget(self._ctl)

        act = QGroupBox("Track and save")
        al = QVBoxLayout(act)
        self._btn_track = QPushButton("Apply to all frames")
        self._btn_track.setToolTip("Fit every frame with these settings. "
                                   "Hand edits are kept.")
        self._btn_track.clicked.connect(self._track_all)
        self._btn_revert = QPushButton("Revert")
        self._btn_revert.setToolTip("Back to the previous trace and the "
                                    "settings that made it.")
        self._btn_revert.clicked.connect(self._revert)
        apply_row = QHBoxLayout()
        apply_row.addWidget(self._btn_track, 1)
        apply_row.addWidget(self._btn_revert)
        self._prog = QLabel("")
        self._prog.setWordWrap(True)
        self._lbl_stale = QLabel("")
        self._lbl_stale.setStyleSheet("color: #ff9d3d")
        self._lbl_stale.setWordWrap(True)
        self._btn_save = QPushButton("Save")
        self._btn_save.setToolTip("Beside the clip; the clip is untouched.")
        self._btn_save.clicked.connect(self.save)
        al.addLayout(apply_row)
        for w in (self._prog, self._lbl_stale, self._btn_save):
            al.addWidget(w)
        side.addWidget(act)
        side.addStretch()

        left = QWidget()
        left.setLayout(side)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if not self._embedded:
            scroll.setFixedWidth(380)
        self.side_widget = scroll
        sections_help(left)        # help on the section titles, as the live tab
        collapsible_groups(left, "pupil_review")

        self.view_widget = QWidget()
        mid = QVBoxLayout(self.view_widget)
        mid.setContentsMargins(0, 0, 0, 0)
        # The live view's bar above the image, same place, same names; the
        # region here is the cyan box itself, so no Set eye region.
        self._top_bar = QHBoxLayout()
        mid.addLayout(self._top_bar)
        # Image + LUT bar with an Auto box, as adapters/base.py's _image_view
        # builds for the live view (devices may not import adapters).
        self._img = pg.ImageItem(axisOrder="row-major")
        self._hist = pg.HistogramLUTWidget()
        self._hist.setImageItem(self._img)
        self._hist.setFixedWidth(86)
        self._hist.setHistogramRange(0, 255)
        self._hist.setLevels(0, 255)
        self._gv = pg.GraphicsView()
        self._vb = pg.ViewBox(lockAspect=True, invertY=True)
        self._gv.setCentralItem(self._vb)
        self._vb.addItem(self._img)
        self._chk_auto = QCheckBox("Auto")
        self._chk_auto.setChecked(True)
        self._chk_auto.setToolTip(
            "Auto contrast, per frame. Off: drag the LUT bar's handles. "
            "Display only; tracking always sees the raw frame.")
        self._chk_auto.toggled.connect(lambda _on: self._repaint())
        # Same pair as the live view: the box over the bar and the
        # Display row are one setting.
        self._chk_auto.toggled.connect(self._chk_auto_contrast.setChecked)
        self._chk_auto_contrast.toggled.connect(self._chk_auto.setChecked)
        self._chk_lut.toggled.connect(self._hist.setVisible)
        self._hist.item.sigLevelsChanged.connect(
            lambda *_a: None if self._chk_auto.isChecked() else self._repaint())
        lut_col = QVBoxLayout()
        lut_col.setSpacing(2)
        lut_col.addWidget(self._chk_auto, alignment=Qt.AlignmentFlag.AlignHCenter)
        lut_col.addWidget(self._hist)
        view_row = QHBoxLayout()
        view_row.addLayout(lut_col)
        view_row.addWidget(self._gv, 1)
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0
        self._fit_curve = pg.PlotCurveItem(pen=pg.mkPen("#7fff6a", width=2))
        self._vb.addItem(self._fit_curve)
        self._pin_curve = pg.PlotCurveItem(pen=pg.mkPen("#ff9d3d", width=1),
                                           connect="finite")
        self._vb.addItem(self._pin_curve)
        # Red over what reflection removal blanked, as in the live view.
        self._mask_img = pg.ImageItem()
        self._mask_img.setZValue(1)
        self._vb.addItem(self._mask_img)
        self._mask: tuple | None = None         # (frame, mask, box)
        self._vb.scene().sigMouseClicked.connect(self._on_image_click)
        # Auto's help: frames to mark, the marks so far, the circle shown.
        self._seed_frames: list[int] | None = None
        self._seed_marks: list = []
        self._seed_roi = None
        self._seed_r0 = 0.0
        # Created on the first clip, once the frame size is known.
        self._roi = None
        self._region = None
        mid.addLayout(view_row, 1)

        # Shown only while Auto asks for help.
        self._seed_bar = QWidget()
        sb = QHBoxLayout(self._seed_bar)
        sb.setContentsMargins(0, 0, 0, 0)
        self._lbl_seed = QLabel("")
        self._lbl_seed.setStyleSheet("color:#ffd166; font-weight:bold;")
        self._btn_seed_next = QPushButton("Next")
        self._btn_seed_next.clicked.connect(self._seed_next)
        btn_skip = QPushButton("Skip frame")
        btn_skip.clicked.connect(lambda: self._seed_next(skip=True))
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self._seed_cancel)
        sb.addWidget(self._lbl_seed, 1)
        for b in (self._btn_seed_next, btn_skip, btn_cancel):
            sb.addWidget(b)
        self._seed_bar.hide()
        mid.addWidget(self._seed_bar)

        nav = QHBoxLayout()
        self._sld = QSlider(Qt.Orientation.Horizontal)
        self._sld.valueChanged.connect(self.goto)
        self._spn_frame = compact(QSpinBox())
        self._spn_frame.valueChanged.connect(self.goto)
        btn_prev = QPushButton("◀")
        btn_next = QPushButton("▶")
        btn_prev.clicked.connect(lambda: self.goto(self._frame - 1))
        btn_next.clicked.connect(lambda: self.goto(self._frame + 1))
        self._btn_sus_prev = QPushButton("◀ Suspect")
        self._btn_sus = QPushButton("Suspect ▶")
        for b in (self._btn_sus_prev, self._btn_sus):
            b.setToolTip("Frames worth checking (red on the trace): no "
                         "fit, or a jumpy radius.")
        self._btn_sus_prev.clicked.connect(lambda: self._jump_suspect(-1))
        self._btn_sus.clicked.connect(self._next_suspect)
        self._lbl_sus = QLabel("")
        for w in (btn_prev, self._sld, btn_next, self._spn_frame,
                  self._btn_sus_prev, self._btn_sus, self._lbl_sus):
            nav.addWidget(w)
        mid.addLayout(nav)

        play = QHBoxLayout()
        self._btn_play = QPushButton("Play")
        self._btn_play.setToolTip("Space")
        self._btn_play.clicked.connect(self.toggle_play)
        self._spn_rate = spin(1.0, 200.0, 20.0, decimals=1, suffix=" Hz")
        self._spn_rate.valueChanged.connect(self._rate_changed)
        self._chk_loop = QCheckBox("Loop")
        play.addWidget(self._btn_play)
        play.addWidget(QLabel("Rate:"))
        play.addWidget(self._spn_rate)
        play.addWidget(self._chk_loop)
        play.addStretch()
        self._cmb_view = QComboBox()
        for label, key in (("Full + region", "full"),
                           ("Full, no overlay", "bare"),
                           ("Cropped to region", "crop")):
            self._cmb_view.addItem(label, key)
        self._cmb_view.setToolTip("Display only.")
        compact(self._cmb_view)
        self._cmb_view.currentIndexChanged.connect(self._view_changed)
        mid.addLayout(play)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # The shown frame re-fitted with unapplied settings: (frame, fit).
        self._preview: tuple | None = None
        self._overlay_at: int | None = None     # last frame given an overlay
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._run_preview)
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc.activated.connect(self.toggle_play)

        edit = QHBoxLayout()
        self._lbl_state = QLabel("")
        self._btn_pin = QPushButton("Keep auto fit")
        self._btn_pin.setToolTip("Lock this frame's ellipse.")
        self._btn_pin.clicked.connect(self._pin_current)
        self._btn_reset = QPushButton("Reset frame to auto")
        self._btn_reset.clicked.connect(self._reset_current)
        self._btn_new = QPushButton("Place ellipse here")
        self._btn_new.setToolTip("For a frame with no fit.")
        self._btn_new.clicked.connect(self._place_new)
        self._btn_pin_cr = QPushButton("Pin reflection")
        self._btn_pin_cr.setCheckable(True)
        self._btn_pin_cr.setToolTip("Then click a fixed reflection to pin it; "
                                    "click again to unpin.")
        self._btn_pin_cr.toggled.connect(
            lambda on: self._prog.setText("click a reflection to pin or unpin "
                                          "it" if on else ""))
        for w in (self._lbl_state, self._btn_new, self._btn_pin, self._btn_reset):
            edit.addWidget(w)
        mid.addLayout(edit)

        self._plot = pg.PlotWidget(title="Pupil radius (px)")
        self._plot.setMaximumHeight(200)
        self._auto_curve = self._plot.plot(pen=pg.mkPen("#888888", width=1))
        self._final_curve = self._plot.plot(pen=pg.mkPen("#7fff6a", width=1))
        self._edit_pts = pg.ScatterPlotItem(
            pen=None, brush=pg.mkBrush("#ff9d3d"), size=7)
        self._plot.addItem(self._edit_pts)
        # Suspect frames: a red band each (pooled), plus a dot on the trace.
        self._sus_pts = pg.ScatterPlotItem(
            pen=None, brush=pg.mkBrush("#ff4d4d"), size=6, symbol="x")
        self._plot.addItem(self._sus_pts)
        self._sus_bands: list = []
        self._suspects: list[int] = []
        self._cursor = pg.InfiniteLine(angle=90, movable=True,
                                       pen=pg.mkPen("#00e5ff"))
        self._cursor.sigPositionChangeFinished.connect(
            lambda: self.goto(int(round(self._cursor.value()))))
        self._plot.addItem(self._cursor)
        mid.addWidget(self._plot)
        self._top_bar.addWidget(QLabel("Eye:"))
        self._top_bar.addWidget(self._btn_pin_cr)
        self._top_bar.addWidget(QLabel("drag the cyan box"), 1)
        self._top_bar.addWidget(QLabel("View:"))
        self._top_bar.addWidget(self._cmb_view)
        if not self._embedded:
            root = QHBoxLayout(self)
            root.addWidget(self.side_widget)
            root.addWidget(self.view_widget, 1)

    def _set_running(self) -> None:
        """Lock what must not change under a running fit or Auto."""
        track, auto = self._worker is not None, self._auto_worker is not None
        seed = self._seed_frames is not None
        self._btn_open.setEnabled(not (track or auto or seed))
        self._ctl.set_auto_busy(auto or seed, text="…" if auto else "marking",
                                enabled=not track and self.review is not None)
        self._btn_track.setEnabled(not auto)
        self._btn_track.setText("Stop" if track else "Apply to all frames")
        self._btn_revert.setEnabled(not (track or auto or seed)
                                    and self.review is not None
                                    and bool(self.review.history))

    # ── loading ──────────────────────────────────────────────────────────────
    def _pick_video(self) -> None:
        if not self._confirm_discard():
            return
        start = str(self.review.video.parent) if self.review else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Pupil recording", start, _VIDEO_FILTER)
        if path:
            self.open_video(path)

    def open_video(self, path: str) -> bool:
        if self._worker is not None or self._auto_worker is not None:
            self._prog.setText("Stop the running job before opening another clip.")
            return False
        try:
            rev = PupilReview.load(path, self._seed)
        except Exception as e:                          # noqa: BLE001 — bad file
            QMessageBox.warning(self, "Pupil review", f"Can't open {path}:\n{e}")
            return False
        self.pause()
        self.review = rev
        n = len(rev)
        h, w = rev.reader.height, rev.reader.width
        self._region_default = rev.settings.search_limit() is None
        if self._region_default:
            # Tracking returns nothing without a region.
            rev.settings = dataclasses.replace(
                rev.settings, limit_x0=w * 0.25, limit_x1=w * 0.75,
                limit_y0=h * 0.25, limit_y1=h * 0.75)
            if rev.tracked_with is not None and rev.tracked_with.search_limit() is None:
                rev.tracked_with = rev.settings
        self._data = None
        self._levels = None
        self._loading = True
        self._spn_rate.setValue(rev.reader.hz or rev.settings.rate_hz or 20.0)
        self._lbl_file.setText(Path(path).name)
        for w_ in (self._sld, self._spn_frame):
            w_.setRange(0, max(0, n - 1))
        self._plot.setXRange(0, max(1, n - 1))
        self._loading = False
        self._show_settings(rev.settings)
        self._draw_pins()
        self._dirty = False
        self._show_stale()
        msg = f"{n} frames" + ("" if rev.tracked else " — not tracked yet")
        if rev.sidecar_note:
            msg += f"\n{rev.sidecar_note} (kept as *.old when you save)"
        self._prog.setText(msg)
        self._set_running()
        self.goto(0, force=True)
        self._refresh_plot()
        self._vb.autoRange()
        return True

    def _show_settings(self, st: PupilSettings) -> None:
        """Put `st` in the controls and the region box, without counting it
        as an edit."""
        self._loading = True
        try:
            self._ctl.show_settings(st)
            self._build_region(st)
        finally:
            self._loading = False

    def _build_region(self, st: PupilSettings) -> None:
        if self._region is not None:
            self._vb.removeItem(self._region)
            self._region = None
        if st.search_limit() is None:       # "Eye region" unticked
            return
        x0, y0, x1, y1 = st.search_limit()
        self._region = pg.RectROI((x0, y0), (x1 - x0, y1 - y0),
                                  pen=pg.mkPen("#00e5ff", width=2))
        self._region.sigRegionChangeFinished.connect(self._region_dragged)
        self._region.setVisible(self._view() != "bare")
        self._vb.addItem(self._region)

    def _region_dragged(self) -> None:
        """Box dragged on the clip -> the X0..Y1 numbers (ONE change)."""
        if self._loading:
            return
        p, s = self._region.pos(), self._region.size()
        self._region_default = False
        self._ctl.set_limit(p.x(), p.y(), p.x() + s.x(), p.y() + s.y())

    def _sync_region_box(self) -> None:
        """The controls' region (ticked, unticked, restored) -> the box."""
        x0, y0, x1, y1 = self._ctl.region()
        if x1 <= x0 or y1 <= y0:
            if self._region is not None:
                self._vb.removeItem(self._region)
                self._region = None
            return
        if self._region is None:
            self._loading = True
            try:
                self._build_region(self._read_settings())
            finally:
                self._loading = False
            return
        p, s = self._region.pos(), self._region.size()
        if (abs(p.x() - x0) + abs(p.y() - y0) + abs(s.x() - (x1 - x0))
                + abs(s.y() - (y1 - y0))) < 0.5:
            return
        self._loading = True
        try:
            self._region.setPos((x0, y0), finish=False)
            self._region.setSize((x1 - x0, y1 - y0), finish=False)
        finally:
            self._loading = False

    def _default_region(self) -> None:
        """'Eye region' ticked with none before: the middle half of the clip."""
        if self.review is None:
            return
        h, w = self.review.reader.height, self.review.reader.width
        self._region_default = True
        self._ctl.set_limit(w * 0.25, h * 0.25, w * 0.75, h * 0.75)

    # ── parameters ───────────────────────────────────────────────────────────
    def _read_settings(self) -> PupilSettings:
        return self._ctl.settings_into(self.review.settings)

    def _params_edited(self, *_a) -> None:
        if self._loading or self.review is None:
            return
        self._sync_region_box()
        new = self._read_settings()
        if new != self.review.settings:
            self.review.settings = new
            self._dirty = True
        self._show_stale()
        # Stabilize changes the table without a re-track; pins are drawn.
        self._draw_pins()
        self._refresh_plot()
        self._preview = None
        self._show_fit(rebuild_roi=not (self.playing or self.seeding))
        self._want_preview()

    def _show_stale(self) -> None:
        rev = self.review
        self._lbl_stale.setText(
            "Settings changed — Apply to all frames to keep them."
            if rev is not None and rev.stale else "")

    # ── Auto ─────────────────────────────────────────────────────────────────
    def _auto(self) -> None:
        rev = self.review
        if rev is None or self._worker is not None or self._auto_worker is not None:
            return
        self.pause()
        n = len(rev)
        idx = np.unique(np.linspace(0, n - 1, min(_AUTO_FRAMES, n)).astype(int))
        try:
            frames = [np.array(rev.reader.luma(int(i))) for i in idx]
        except Exception as e:                          # noqa: BLE001 — bad clip
            self._prog.setText(f"Auto: can't read the clip ({e})")
            return
        st = self._read_settings()
        region = None if self._region_default else st.search_limit()
        self._auto_worker = AutoTuneWorker(frames, region)
        self._auto_worker.done.connect(self._on_auto)
        self._auto_worker.error.connect(self._on_auto_error)
        self._prog.setText("finding parameters…")
        self._set_running()
        self._auto_worker.start()

    def _on_auto(self, res) -> None:
        seeded = self._seed_frames is not None
        self._end_auto()
        if not seeded and (res is None or res.confidence < NEEDS_HELP):
            self._seed_start()
            return
        self._seed_frames = None
        self._set_running()
        if res is None:
            self._prog.setText("Auto: still no pupil found there.")
            return
        new = res.apply(self._read_settings())
        self._show_settings(new)
        if res.region is not None:
            self._region_default = False
        self._params_edited()
        self._prog.setText(f"Auto: {res.notes}. Apply to all frames to keep.")

    def _on_auto_error(self, msg: str) -> None:
        self._seed_frames = None
        self._end_auto()
        self._prog.setText(f"Auto failed: {msg}")

    def _end_auto(self) -> None:
        self._park(self._auto_worker)
        self._auto_worker = None
        self._set_running()

    # ── Auto's help: the user marks the pupil on a few frames ────────────────
    @property
    def seeding(self) -> bool:
        return self._seed_frames is not None and not self._seed_bar.isHidden()

    def _seed_start(self) -> None:
        n = len(self.review)
        k = min(_SEED_FRAMES, n)
        self._seed_frames = [int(i) for i in
                             np.unique(np.linspace(n * 0.1, n * 0.9, k).astype(int))]
        self._seed_marks = []
        self._seed_bar.show()
        self._set_running()
        self._seed_show()

    def _seed_show(self) -> None:
        """Go to the next frame to mark."""
        k = len(self._seed_marks)
        self._clear_seed_roi()
        self.goto(self._seed_frames[k], force=True)
        self._btn_seed_next.setEnabled(False)
        self._lbl_seed.setText(
            f"Auto needs help ({k + 1}/{len(self._seed_frames)}): click the "
            f"pupil's centre. Resize the circle to fit it, if you like.")

    def _on_image_click(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or self.review is None:
            return
        if not self.seeding:
            if self._btn_pin_cr.isChecked():
                if self._vb.sceneBoundingRect().contains(ev.scenePos()):
                    p = self._vb.mapSceneToView(ev.scenePos())
                    self.toggle_pin(p.x(), p.y())
                    ev.accept()
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        h, w = self.review.reader.height, self.review.reader.width
        r = self._seed_r0 or max(5.0, min(h, w) * 0.06)
        self._clear_seed_roi()
        self._seed_roi = pg.CircleROI((p.x() - r, p.y() - r), (2 * r, 2 * r),
                                      pen=pg.mkPen("#ffd166", width=2))
        self._seed_r0 = r
        self._vb.addItem(self._seed_roi)
        self._btn_seed_next.setEnabled(True)
        ev.accept()

    def toggle_pin(self, x: float, y: float) -> None:
        """Unpin the pin under (x, y), or pin the reflection there, sized to
        the bright blob on this frame (as the live tab does)."""
        st = self._read_settings()
        pins = list(st.cr_pins)
        for i, (px, py, pr) in enumerate(pins):
            if np.hypot(x - px, y - py) <= pr:
                pins.pop(i)
                self._ctl.set_pins(pins)
                return
        r = 8.0
        try:
            from acqApp.devices.pupil_cam.eyeloop_tracker import measure_reflection
            r = measure_reflection(self._data, (x, y), threshold=st.cr_threshold)
        except Exception:                               # noqa: BLE001 — no cv2
            pass
        pins.append((float(x), float(y), float(r)))
        self._ctl.set_pins(pins)

    def _draw_pins(self) -> None:
        pins = self._read_settings().cr_pins if self.review is not None else []
        if not pins:
            self._pin_curve.setData([], [])
            return
        th = np.linspace(0, 2 * np.pi, 33)
        xs, ys = [], []
        for px, py, pr in pins:
            xs += list(px + pr * np.cos(th)) + [np.nan]
            ys += list(py + pr * np.sin(th)) + [np.nan]
        self._pin_curve.setData(np.array(xs), np.array(ys))

    def _clear_seed_roi(self) -> None:
        if self._seed_roi is not None:
            self._vb.removeItem(self._seed_roi)
            self._seed_roi = None

    def _seed_mark(self):
        """(x, y, r or None) from the circle: r only if it was resized."""
        roi = self._seed_roi
        d = roi.size()[0]
        p = roi.pos()
        r = d / 2.0
        drawn = abs(r - self._seed_r0) > 0.5
        if drawn:
            self._seed_r0 = r       # the next circle starts at this size
        return (p.x() + r, p.y() + r, r if drawn else None)

    def _seed_next(self, skip: bool = False) -> None:
        if self._seed_frames is None:
            return
        self._seed_marks.append(None if skip or self._seed_roi is None
                                else self._seed_mark())
        if len(self._seed_marks) < len(self._seed_frames):
            self._seed_show()
            return
        self._clear_seed_roi()
        self._seed_bar.hide()
        marks = [m for m in self._seed_marks if m is not None]
        if not marks:
            self._seed_cancel()
            return
        frames = [np.array(self.review.reader.luma(i)) for i in self._seed_frames]
        st = self._read_settings()
        region = None if self._region_default else st.search_limit()
        self._auto_worker = AutoTuneWorker(frames, region, self._seed_marks)
        self._auto_worker.done.connect(self._on_auto)
        self._auto_worker.error.connect(self._on_auto_error)
        self._prog.setText("finding parameters from your marks…")
        self._set_running()
        self._auto_worker.start()

    def _seed_cancel(self) -> None:
        self._clear_seed_roi()
        self._seed_bar.hide()
        self._seed_frames = None
        self._seed_marks = []
        self._prog.setText("Auto cancelled.")
        self._set_running()
        self.goto(self._frame, force=True)

    # ── tracking ─────────────────────────────────────────────────────────────
    def _track_all(self) -> None:
        if self.review is None:
            return
        if self._worker is not None:        # the button doubles as Stop
            self._worker.cancel()
            return
        if self._busy():
            self._prog.setText("Live tracking is running — stop it first "
                               "(both use the same EyeLoop state).")
            return
        if PupilReview.fitting():
            self._prog.setText("Another clip is being tracked — wait for it "
                               "(both use the same EyeLoop state).")
            return
        self.pause()
        self.review.settings = self._read_settings()
        self._worker = _TrackAllWorker(self.review)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_tracked)
        self._worker.error.connect(self._on_track_error)
        self._set_running()
        self._worker.start()

    def _revert(self) -> None:
        if self.review is None or not self.review.revert():
            return
        self._show_settings(self.review.settings)
        self._draw_pins()
        self._dirty = True
        self._preview = None
        self._show_stale()
        self._set_running()
        self.goto(self._frame, force=True)
        self._refresh_plot()
        self._prog.setText("reverted to the previous trace")

    def _on_progress(self, i: int, n: int) -> None:
        self._prog.setText(f"tracking {i}/{n}")

    def _on_tracked(self, done: bool) -> None:
        self._end_worker()
        if done:
            self._dirty = True
            self._preview = None
            self._prog.setText("done")
        else:
            self._prog.setText("stopped — the previous fits are kept")
        self._show_stale()
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _on_track_error(self, msg: str) -> None:
        self._end_worker()
        self._prog.setText(f"Tracking failed — the previous fits are kept.\n{msg}")

    def _end_worker(self) -> None:
        self._park(self._worker)
        self._worker = None
        self._set_running()

    def _park(self, w: PullWorker | None) -> None:
        """Drop a finished worker; hold one still running until it ends."""
        if w is None or w.wait(3000):
            return
        _PARKED.append(w)
        w.finished.connect(lambda w=w: _PARKED.remove(w) if w in _PARKED else None)

    # ── frame navigation / display ───────────────────────────────────────────
    def goto(self, i: int, force: bool = False) -> None:
        rev = self.review
        if rev is None:
            return
        i = max(0, min(int(i), len(rev) - 1))
        if i == self._frame and not force:
            return
        try:
            data = np.ascontiguousarray(rev.reader.luma(i))
        except Exception as e:                          # noqa: BLE001 — bad frame
            self.pause()
            self._prog.setText(f"frame {i} unreadable ({e})")
            return
        self._frame = i
        self._loading = True
        for w in (self._sld, self._spn_frame):
            w.setValue(i)
        self._cursor.setValue(i)
        self._loading = False
        self._data = data
        self._repaint()
        if self._preview is not None and self._preview[0] != i:
            self._preview = None
        if self._mask is not None and self._mask[0] != i:
            self._mask = None
            self._mask_img.clear()
        # No handle while playing: rebuilding it per frame is wasted work.
        self._show_fit(rebuild_roi=not (self.playing or self.seeding))
        if self.playing:
            # Every frame gets its overlay, straight away: the tracker walks
            # on from the frame before, so no warm-up is needed.
            sequential = self._overlay_at is not None and i == self._overlay_at + 1
            self._overlay(i, warmup=0 if sequential else 2)
        else:
            self._want_preview()

    def _repaint(self) -> None:
        """The current frame through the LUT. Auto re-derives percentiles each
        frame when still, every few while playing (as the live view does);
        manual re-passes the LUT bar's own levels."""
        data = self._data
        if data is None:
            return
        if self._chk_auto.isChecked():
            if (self._levels is None or not self.playing
                    or self._level_ctr % _LEVELS_EVERY == 0):
                lo, hi = np.percentile(data[::4, ::4], (1, 99))
                self._levels = (float(lo), float(hi))
            self._level_ctr += 1
            levels = self._levels
        else:
            levels = self._hist.item.getLevels()
        h, w = data.shape
        rect = QRectF(0, 0, w, h)
        if self._view() == "crop" and self.review is not None:
            box = self._read_settings().crop_box(data.shape)
            if box is not None:
                x0, y0, x1, y1 = box
                data = data[y0:y1, x0:x1]
                rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        self._img.setImage(data, autoLevels=False, levels=levels)
        self._img.setRect(rect)

    def _view(self) -> str:
        return self._cmb_view.currentData() or "full"

    def _view_changed(self, *_a) -> None:
        bare = self._view() == "bare"
        for item in (self._fit_curve, self._pin_curve, self._mask_img,
                     self._region, self._roi):
            if item is not None:
                item.setVisible(not bare)
        self._repaint()
        self._vb.autoRange()

    # ── playback ─────────────────────────────────────────────────────────────
    @property
    def playing(self) -> bool:
        return self._timer.isActive()

    def toggle_play(self) -> None:
        if self.review is None:
            return
        if self.playing:
            self.pause()
            return
        if self._frame >= len(self.review) - 1:
            self.goto(0)                    # play from the top after the end
        self._btn_play.setText("Pause")
        self._timer.start(self._interval_ms())
        self.goto(self._frame, force=True)  # drops the handle

    def pause(self) -> None:
        if not self.playing:
            return
        self._timer.stop()
        self._btn_play.setText("Play")
        if self.review is not None:         # the handle comes back
            self.goto(self._frame, force=True)

    def _interval_ms(self) -> int:
        return max(1, int(round(1000.0 / self._spn_rate.value())))

    def _rate_changed(self, *_a) -> None:
        if self.playing:
            self._timer.setInterval(self._interval_ms())

    def _tick(self) -> None:
        last = len(self.review) - 1
        if self._frame >= last:
            if self._chk_loop.isChecked():
                self.goto(0)
            else:
                self.pause()
            return
        self.goto(self._frame + 1)

    # ── the shown frame, with settings not yet applied ───────────────────────
    def _needs_preview(self) -> bool:
        rev = self.review
        return (rev is not None and not rev.is_edited(self._frame)
                and (rev.stale or not rev.tracked)
                and self._read_settings().search_limit() is not None)

    def _wants_mask(self) -> bool:
        """What removal blanks is always shown while it is on."""
        st = self._read_settings() if self.review is not None else None
        return (st is not None and st.cr_remove
                and st.search_limit() is not None)

    def _want_preview(self) -> None:
        if self._needs_preview() or self._wants_mask():
            self._preview_timer.start(_PREVIEW_MS)
        else:
            self._preview_timer.stop()
        if not self._wants_mask():
            self._mask = None
            self._draw_mask()

    def _run_preview(self) -> None:
        if not self.playing:
            self._overlay(self._frame)

    def _overlay(self, i: int, warmup: int = 2) -> None:
        """Fit frame `i` with the current settings and draw it (when they
        aren't applied yet), and what reflection removal blanked there."""
        needs, mask = self._needs_preview(), self._wants_mask()
        if not (needs or mask) or self.seeding or self._worker is not None:
            self._overlay_at = None
            return
        if self._busy():
            self._lbl_state.setText(f"frame {i}: no preview while live "
                                    f"tracking runs")
            return
        try:
            fit = self.review.preview_fit(i, self._read_settings(), warmup=warmup)
        except Exception as e:                          # noqa: BLE001 — no EyeLoop
            self._overlay_at = None
            self._lbl_state.setText(f"frame {i}: no preview ({e})")
            return
        self._overlay_at = i
        if needs:
            self._preview = (i, fit)
            self._show_fit(rebuild_roi=False)
        self._mask = ((i, self.review.last_mask, self.review.last_box)
                      if mask else None)
        self._draw_mask()

    def _draw_mask(self) -> None:
        m = self._mask
        if m is None or m[0] != self._frame or m[1] is None or m[2] is None:
            self._mask_img.clear()
            return
        _i, mask, box = m
        rgba = np.zeros(mask.shape + (4,), np.uint8)
        rgba[..., 0] = 255
        rgba[..., 3] = np.where(mask, 140, 0)
        x0, y0, x1, y1 = box
        self._mask_img.setImage(rgba, autoLevels=False)
        self._mask_img.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))

    def _show_fit(self, rebuild_roi: bool = True) -> None:
        rev = self.review
        i = self._frame
        fit = rev.fit_at(i)
        edited = rev.is_edited(i)
        if self._preview is not None and self._preview[0] == i and not edited:
            # Unapplied settings: show what they'd give here, no edit handle.
            if self._roi is not None:
                self._vb.removeItem(self._roi)
                self._roi = None
            pfit = self._preview[1]
            self._fit_curve.setPen(pg.mkPen("#ffd166", width=2,
                                            style=Qt.PenStyle.DashLine))
            self._fit_curve.setData(*(_ellipse_xy(pfit) if pfit is not None
                                      else ([], [])))
            self._lbl_state.setText(
                f"frame {i}: preview — " + ("no fit" if pfit is None else
                                            "Apply to all frames to keep"))
            self._btn_reset.setEnabled(False)
            self._btn_pin.setEnabled(False)
            self._btn_new.setEnabled(pfit is None)
            return
        self._fit_curve.setPen(pg.mkPen("#ff9d3d" if edited else "#7fff6a",
                                        width=2))
        if (rebuild_roi or self.playing or self.seeding) and self._roi is not None:
            self._vb.removeItem(self._roi)
            self._roi = None
        if fit is None:
            self._fit_curve.setData([], [])
        else:
            self._fit_curve.setData(*_ellipse_xy(fit))
            if rebuild_roi:
                self._make_roi(fit)
        self._lbl_state.setText(
            f"frame {i}: " + ("hand-edited" if edited
                              else "no fit" if fit is None else "auto"))
        self._btn_reset.setEnabled(edited)
        self._btn_pin.setEnabled(fit is not None and not edited)
        self._btn_new.setEnabled(fit is None)

    def _make_roi(self, fit: PupilFit) -> None:
        """An ellipse handle at `fit`. ROI pos is the unrotated box's corner,
        rotated about itself: centre = pos + R(angle)(a, b)."""
        a, b = fit.semi_major, fit.semi_minor
        t = np.radians(fit.angle_deg)
        c, s = np.cos(t), np.sin(t)
        pos = (fit.center_x - (a * c - b * s), fit.center_y - (a * s + b * c))
        self._roi = pg.EllipseROI(pos, (2 * a, 2 * b), angle=fit.angle_deg,
                                  pen=pg.mkPen("#ffffff", width=1))
        self._roi.sigRegionChangeFinished.connect(self._roi_edited)
        self._roi.setVisible(self._view() != "bare")
        self._vb.addItem(self._roi)

    def _roi_fit(self) -> PupilFit:
        a, b = self._roi.size()[0] / 2.0, self._roi.size()[1] / 2.0
        ang = float(self._roi.angle())
        t = np.radians(ang)
        p = self._roi.pos()
        return PupilFit(float(p.x() + a * np.cos(t) - b * np.sin(t)),
                        float(p.y() + a * np.sin(t) + b * np.cos(t)),
                        float(a), float(b), ang)

    # ── hand edits ───────────────────────────────────────────────────────────
    def _roi_edited(self) -> None:
        if self._loading or self._roi is None:
            return
        self.edit_frame(self._frame, self._roi_fit(), keep_roi=True)

    def edit_frame(self, i: int, fit: PupilFit, keep_roi: bool = False) -> None:
        """Pin frame `i` to `fit` and redraw what depends on it. `keep_roi`:
        the edit came from dragging the handle, which must not be rebuilt
        under the user's mouse."""
        self.pause()
        self.review.set_manual(i, fit)
        self._dirty = True
        if i == self._frame:
            self._show_fit(rebuild_roi=not keep_roi)
        self._refresh_plot()

    def _pin_current(self) -> None:
        fit = self.review.fit_at(self._frame)
        if fit is not None:
            self.edit_frame(self._frame, fit)

    def _reset_current(self) -> None:
        self.review.clear_manual(self._frame)
        self._dirty = True
        self.goto(self._frame, force=True)
        self._refresh_plot()

    def _place_new(self) -> None:
        """Seed an ellipse at the middle of the eye region for a frame the
        tracker missed; then drag it into place."""
        x0, y0, x1, y1 = self._read_settings().search_limit()
        r = max(5.0, min(x1 - x0, y1 - y0) / 6.0)
        self.edit_frame(self._frame, PupilFit((x0 + x1) / 2, (y0 + y1) / 2,
                                              r, r, 0.0))

    def _next_suspect(self) -> None:
        self._jump_suspect(+1)

    def _jump_suspect(self, step: int) -> None:
        if self.review is None:
            return
        sus = self._suspects
        nxt = ([i for i in sus if i > self._frame] if step > 0
               else [i for i in sus if i < self._frame][::-1])
        if nxt:
            self.goto(nxt[0])
        else:
            self._prog.setText("no further suspect frames" if step > 0
                               else "no earlier suspect frames")

    # ── plot ─────────────────────────────────────────────────────────────────
    def _refresh_plot(self) -> None:
        rev = self.review
        if rev is None:
            return
        n = len(rev)
        x = np.arange(n)
        r_auto = np.where(np.isnan(rev.auto[:, 0]), np.nan,
                          (rev.auto[:, 2] + rev.auto[:, 3]) / 2.0)
        self._auto_curve.setData(x, r_auto, connect="finite")
        self._final_curve.setData(x, rev.radius(), connect="finite")
        ed = np.flatnonzero(rev.edited)
        radius = rev.radius()
        self._edit_pts.setData(ed, radius[ed])
        self._draw_suspects(radius)

    def _draw_suspects(self, radius: np.ndarray) -> None:
        """Red bands over runs of suspect frames, a red x on each. Nothing
        before the first track: every frame would be 'no fit'."""
        rev = self.review
        sus = rev.suspects() if rev.tracked or rev.edited.any() else []
        self._suspects = sus
        runs: list[tuple[int, int]] = []
        for i in sus:
            if runs and i == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((i, i))
        while len(self._sus_bands) < min(len(runs), _SUSPECT_BANDS):
            band = pg.LinearRegionItem(movable=False,
                                       brush=pg.mkBrush(255, 77, 77, 60),
                                       pen=pg.mkPen(None))
            band.setZValue(-10)
            self._plot.addItem(band)
            self._sus_bands.append(band)
        for k, band in enumerate(self._sus_bands):
            if k < len(runs):
                a, b = runs[k]
                band.setRegion((a - 0.5, b + 0.5))
                band.show()
            else:
                band.hide()
        sus_arr = np.asarray(sus, dtype=int)
        y = radius[sus_arr] if sus_arr.size else np.array([])
        # A frame with no fit has no radius: put its x on the axis floor.
        floor = np.nanmin(radius) if np.isfinite(radius).any() else 0.0
        self._sus_pts.setData(sus_arr, np.where(np.isnan(y), floor, y))
        self._lbl_sus.setText(f"{len(sus)} to check" if sus else
                              ("none to check" if rev.tracked else ""))

    # ── saving / closing ─────────────────────────────────────────────────────
    def save(self) -> bool:
        """False (and says why) when the sidecar can't be written."""
        if self.review is None:
            return True
        self.review.settings = self._read_settings()
        try:
            js, npz = self.review.save()
        except Exception as e:                          # noqa: BLE001 — disk
            QMessageBox.warning(self, "Pupil review",
                                f"Couldn't save next to the clip:\n{e}")
            return False
        self._dirty = False
        self._show_stale()
        self._prog.setText(f"saved {js.name}, {npz.name}")
        return True

    def _confirm_discard(self) -> bool:
        if self.review is None or not self._dirty:
            return True
        ans = QMessageBox.question(
            self, "Pupil review", "Save changes to this recording's tracking?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Cancel:
            return False
        if ans == QMessageBox.StandardButton.Save:
            return self.save()
        return True

    def can_close(self) -> bool:
        """Ask about running work and unsaved edits. Ask first: a Cancel must
        leave the running work alone."""
        if self._worker is not None:
            ans = QMessageBox.question(
                self, "Pupil review", "Tracking is still running. Stop it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return False
        return self._confirm_discard()

    def shutdown(self) -> None:
        """Stop playback and any running job (after `can_close`)."""
        self._timer.stop()
        self._preview_timer.stop()
        if self._seed_frames is not None:
            self._seed_cancel()
        if self._worker is not None:
            self._worker.cancel()
            self._end_worker()
        if self._auto_worker is not None:
            self._end_auto()

    def reject(self) -> None:
        """Esc: through closeEvent, so nothing unsaved or running is lost."""
        self.close()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape and self.isWindow():
            self.reject()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev) -> None:
        if not self.can_close():
            ev.ignore()
            return
        self.shutdown()
        ev.accept()


class PupilReviewDialog(ReviewWidget):
    """The review as its own window (run_pupil_review.py)."""

    def __init__(self, video: str = "", settings: PupilSettings | None = None,
                 busy: Callable[[], bool] | None = None, parent=None) -> None:
        super().__init__(video, settings, busy, parent)
        self.setWindowTitle("Pupil review")
        self.resize(1200, 800)
