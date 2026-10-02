"""Pupil review: building the widget (`ReviewWidget._build`). Split out of
review_dialog.py, which owns the state; this only lays it out."""
from __future__ import annotations

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp.widgets import collapsible_groups, compact, sections_help, spin
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking_panel import TrackingControls


class _LayoutMixin:
    """`_build`: the side controls and the clip/playback/trace view."""

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
        disp.addStretch()
        rf.addRow("Display:", disp)
        side.addWidget(rec)

        self._ctl = TrackingControls(self._seed or PupilSettings(), live=False)
        self._ctl.changed.connect(self._params_edited)
        self._ctl.auto_requested.connect(self._auto)
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
        self._btn_pin_cr.setToolTip("Then click a fixed reflection to pin it, "
                                    "or a pin to remove it. One click per press.")
        self._btn_pin_cr.toggled.connect(self._pin_armed)
        for w in (self._lbl_state, self._btn_new, self._btn_pin, self._btn_reset):
            edit.addWidget(w)
        mid.addLayout(edit)

        # Many frames at once: from the last edited frame up to this one.
        gap = QHBoxLayout()
        self._btn_fill = QPushButton("Fill gap")
        self._btn_fill.clicked.connect(self._fill_gap)
        self._btn_retrack = QPushButton("Re-track gap")
        self._btn_retrack.clicked.connect(self._retrack_gap)
        self._btn_undo_gap = QPushButton("Undo")
        self._btn_undo_gap.setToolTip("Back to before the last Fill or "
                                      "Re-track gap.")
        self._btn_undo_gap.clicked.connect(self._undo_gap)
        gap.addWidget(QLabel("Since the last edit:"))
        for b in (self._btn_fill, self._btn_retrack, self._btn_undo_gap):
            gap.addWidget(b)
        gap.addStretch()
        mid.addLayout(gap)

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
        self._top_bar.addStretch(1)
        self._top_bar.addWidget(QLabel("View:"))
        self._top_bar.addWidget(self._cmb_view)
        if not self._embedded:
            root = QHBoxLayout(self)
            root.addWidget(self.side_widget)
            root.addWidget(self.view_widget, 1)
