"""The stage's UI: settings panel and calibration dialog. The model (where
0,0 comes from) is in `settings.py`."""
from __future__ import annotations

import time

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QBoxLayout, QDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout,
    QWidget,
)

from acqApp import style
from acqApp.widgets import button_row, pairs_grid, spin
from acqApp.acq.worker import PullWorker
from acqApp.devices.stage import fov_store
from acqApp.devices.stage.axes_measure import AxesMeasurement
from acqApp.devices.stage.cam_axes import camera_jog, describe
from acqApp.devices.stage.control import step_toward
from acqApp.devices.stage.map_widget import StageMap
from acqApp.devices.stage.settings import (_BAD, WINDOW_DIAMETER_UM,
                                   WINDOW_RANGE_UM, StageSettings,
                                   config_path, load_settings)


class _FrameWorker(PullWorker):
    """establish_frame() off the GUI thread (minutes, into the hard limits).
    A PullWorker so a bug surfaces as `failed`, not a qFatal mid-move."""
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, controller, axes: tuple[str, ...] = ("x", "y")):
        super().__init__()
        self._ctrl = controller
        self._axes = axes

    def _run(self) -> None:
        try:
            self._ctrl.establish_frame(progress=self.progress.emit, axes=self._axes)
        except Exception as e:                  # noqa: BLE001 — reported via `failed`, not re-raised
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.finished_ok.emit()


class CalibrationDialog(QDialog):
    """Everything that rewrites the saved calibration: rare, hard to undo, and
    kept off the main panel."""
    changed = pyqtSignal()          # calibration was rewritten

    def __init__(self, controller, settings: StageSettings, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._s = settings
        self._worker: _FrameWorker | None = None
        self.setWindowTitle("Stage calibration")
        self.setModal(True)
        self._build()
        self._update_status()

    def _build(self) -> None:
        lay = QVBoxLayout(self)

        self._lbl_status = QLabel("—")
        self._lbl_status.setWordWrap(True)
        lay.addWidget(self._lbl_status)

        frame = QGroupBox("Coordinate frame")
        fl = QVBoxLayout(frame)
        self._btn_reframe = QPushButton("Re-establish frame…")
        self._btn_reframe.setToolTip(
            "MOVES THE STAGE: drives X then Y into their reverse hard limits, "
            "then probes to re-measure the command→encoder map. Needed after a "
            "limit hit, when absolute go-to lands wrong.")
        self._btn_reframe.clicked.connect(self._reestablish_frame)
        fl.addWidget(self._btn_reframe)
        fl.addWidget(self._hint(
            "Needed when a hard limit has been hit and absolute go-to lands "
            "wrong. Drives into the reverse limits — the stage must be clear. "
            "Rewrites slope/offset; it never sets 0,0 for you, and clears it "
            "if it lies outside the remeasured travel."))
        self._lbl_progress = QLabel("")
        self._lbl_progress.setWordWrap(True)
        fl.addWidget(self._lbl_progress)
        lay.addWidget(frame)

        # Z has its own section: its hard limits are under the objective.
        self._lbl_status_z: QLabel | None = None
        self._btn_set_zero_z: QPushButton | None = None
        self._btn_reframe_z: QPushButton | None = None
        if self._s.has_z:
            self._build_focus_calibration(lay)

        self._lbl_cfg = self._hint(f"config: {config_path()}")
        lay.addWidget(self._lbl_cfg)

        # Modal, so the panel's STOP ALL is unreachable from here.
        self._btn_stop = QPushButton("STOP ALL")
        self._btn_stop.setStyleSheet(style.solid_btn("puffer"))
        self._btn_stop.clicked.connect(self._stop_all)
        lay.addWidget(self._btn_stop)

        row = QHBoxLayout()
        row.addStretch()
        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.accept)
        row.addWidget(self._btn_close)
        lay.addLayout(row)

    def _stop_all(self) -> None:
        # Into the label, not a modal — the stage may be mid-move into a limit.
        if self._ctrl is not None:
            try:
                self._ctrl.stop_all()
            except Exception as e:
                self._lbl_progress.setText(f"STOP ALL FAILED: {e}")
                return
        self._lbl_progress.setText("STOP ALL sent.")

    def keyPressEvent(self, event) -> None:
        # Esc is the panic key: stop the stage rather than dismiss (Qt's default).
        if event.key() == Qt.Key.Key_Escape:
            self._stop_all()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        if self._worker is not None:
            return                              # can't dismiss mid-move
        super().reject()

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray; font-size: 10px;")
        return lbl

    # ── status ──────────────────────────────────────────────────────────────
    @staticmethod
    def _show(lbl: QLabel, ok: bool, ok_text: str, bad_text: str) -> None:
        lbl.setText(ok_text if ok else bad_text)
        lbl.setStyleSheet("" if ok else f"color: {_BAD};")

    def _update_status(self) -> None:
        missing = [ax.name for ax in (self._s.x, self._s.y) if not ax.has_frame]
        self._show(
            self._lbl_status, not missing,
            f"Frame OK — 0,0 at {self._s.x.ref_counts:.0f}, "
            f"{self._s.y.ref_counts:.0f} counts. Invalid from the moment a "
            "hard limit is hit.",
            f"No valid frame for {', '.join(missing)}. Absolute go-to is "
            "disabled; jog still works. Re-establish the frame, then Set "
            "home.")
        if self._lbl_status_z is not None:
            z = self._s.z
            self._show(
                self._lbl_status_z, z.has_frame,
                f"Frame OK — 0,0 at {z.ref_counts:.0f} counts. Invalid "
                "from the moment a hard limit is hit.",
                f"No valid frame for {z.name}. Absolute go-to (Go, slider) "
                "is disabled; jog still works.")

    def _busy(self, on: bool) -> None:
        # Close too: closing mid-move orphans the worker.
        for b in (self._btn_reframe, self._btn_set_zero_z,
                  self._btn_reframe_z, self._btn_close):
            if b is not None:
                b.setEnabled(not on)

    # ── actions ─────────────────────────────────────────────────────────────
    def _reestablish_frame(self) -> None:
        if self._ctrl is None or self._worker is not None:
            return
        if QMessageBox.warning(
            self, "Re-establish frame",
            "THIS MOVES THE STAGE.\n\n"
            "X and then Y will be driven into their REVERSE hard limits, then "
            "probed to re-measure the command→encoder map. Takes a minute or more "
            "per axis.\n\n"
            "Slope/offset are rewritten. Your 0,0 is kept if it lies inside the "
            "remeasured travel, else cleared (set it again). The stage is left "
            "at a probe position, not at 0,0.\n\n"
            "Make sure the stage is clear, watch it, and keep Esc (STOP ALL) "
            "reachable. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_run(("x", "y"), "Starting…")

    def _start_run(self, axes: tuple[str, ...], text: str) -> None:
        self._busy(True)
        self._lbl_progress.setText(text)
        self._worker = _FrameWorker(self._ctrl, axes=axes)
        self._worker.progress.connect(self._lbl_progress.setText)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        self.changed.emit()                     # panel locks its motion controls

    # ── Focus (Z): as above, but the hard-limit run takes two warnings ──────
    def _build_focus_calibration(self, lay: QVBoxLayout) -> None:
        z = self._s.z
        self._lbl_status_z = QLabel("—")
        self._lbl_status_z.setWordWrap(True)
        lay.addWidget(self._lbl_status_z)

        zero = QGroupBox(f"Focus ({z.name}) zero")
        zl = QVBoxLayout(zero)
        self._btn_set_zero_z = QPushButton(f"Set {z.name} = 0 (here)")
        self._btn_set_zero_z.setToolTip(
            "Declare the CURRENT Z position as its own zero, independent of "
            "X/Y's 0,0. Doesn't move the stage.")
        self._btn_set_zero_z.clicked.connect(self._set_zero_z_here)
        zl.addWidget(self._btn_set_zero_z)
        zl.addWidget(self._hint(
            "Get the sample in focus yourself first — this doesn't move "
            "the stage. Soft limits land at ±half the stage's rated travel "
            "around this point. Saved to the config; survives restarts."))
        lay.addWidget(zero)

        # Offered only where the readout can drift (MCM6101-style).
        if getattr(self._ctrl, "supports_reframe", True):
            frame = QGroupBox(f"{z.name}: hard-limit calibration — read before use")
            frl = QVBoxLayout(frame)
            frl.addWidget(self._hint(
                "Motorized Z stage specs (Thorlabs datasheet): travel 25.4 mm (1\"); "
                "bidirectional repeatability 5 µm; backlash 10 µm; min. incremental "
                "movement 424 nm; min. repeatable movement 848 nm; max velocity "
                "3 mm/s; max acceleration 10.5 mm/s²."))
            self._btn_reframe_z = QPushButton(f"Re-establish {z.name} frame…")
            self._btn_reframe_z.setStyleSheet(style.solid_btn("puffer"))
            self._btn_reframe_z.setToolTip(
                "DANGER: drives Z through its full hard-limit travel, directly "
                "under the objective. Remove the objective and clear the stage "
                "first — see the two warnings this button raises.")
            self._btn_reframe_z.clicked.connect(self._reestablish_frame_z)
            frl.addWidget(self._btn_reframe_z)
            frl.addWidget(self._hint(
                "Unlike X/Y, this happens directly under the objective — a "
                "collision here can damage the objective and/or the sample, not "
                "just lose a coordinate frame. REMOVE THE OBJECTIVE and confirm "
                "nothing is mounted on, over, or under the stage before running "
                "this."))
            lay.addWidget(frame)
        else:
            lay.addWidget(self._hint(
                f"This rig's stage controller has no hard-limit frame "
                f"re-establish for {z.name} — its position readout is already "
                "a stable encoder count and never drifts, so 'Set "
                f"{z.name} = 0' above is this rig's complete Z calibration."))

    def _set_zero_z_here(self) -> None:
        if self._ctrl is None:
            return
        z = self._s.z
        if QMessageBox.question(
            self, f"Set {z.name} = 0",
            f"Set the CURRENT {z.name} position as its zero?\n\n"
            "Get the sample in focus first — this does NOT move the stage. "
            "Soft limits will be set to ±half the stage's rated travel "
            "around this point."
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._ctrl.set_z_zero_here()
        except Exception as e:
            QMessageBox.warning(self, "Stage", f"Could not set {z.name} zero: {e}")
            return
        self._lbl_progress.setText(f"{z.name} zero set here.")
        self._update_status()
        self.changed.emit()

    def _reestablish_frame_z(self) -> None:
        if self._ctrl is None or self._worker is not None:
            return
        z = self._s.z
        if QMessageBox.warning(
            self, f"Re-establish {z.name} frame — read first",
            f"THIS MOVES THE FOCUS STAGE ({z.name}) THROUGH ITS FULL HARD-LIMIT "
            "TRAVEL.\n\n"
            "Unlike X/Y, this motion happens directly under the objective. If "
            "an objective or a sample is anywhere near the stage, this WILL "
            "cause a collision — potentially damaging the objective and/or "
            "the sample.\n\n"
            "Motorized Z stage specs:\n"
            "  Travel range: 25.4 mm (1\")\n"
            "  Bidirectional repeatability: 5 µm\n"
            "  Backlash: 10 µm\n"
            "  Min. incremental movement: 424 nm\n"
            "  Min. repeatable movement: 848 nm\n"
            "  Max velocity: 3 mm/s\n"
            "  Max acceleration: 10.5 mm/s²\n\n"
            "REMOVE THE OBJECTIVE and make sure there is NOTHING mounted on, "
            "over, or under the Z stage before continuing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        # A separate re-check, deliberately a second dialog.
        if QMessageBox.warning(
            self, "Confirm: stage is clear",
            "Second confirmation — please re-check, right now:\n\n"
            "  • The objective has been physically removed or backed fully "
            "away.\n"
            "  • Nothing is mounted on, over, or under the Z stage.\n"
            "  • You are watching the stage and can reach Esc (STOP ALL).\n\n"
            f"Proceed with the {z.name} hard-limit calibration?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_run(("z",), f"Starting {z.name}…")

    def _on_done(self) -> None:
        self._end_run()
        self._update_status()
        self.changed.emit()

    def _on_failed(self, msg: str) -> None:
        self._end_run()
        self._lbl_progress.setText(f"Failed: {msg}")
        self._update_status()
        self.changed.emit()
        QMessageBox.warning(self, "Stage", f"Frame re-establish failed:\n{msg}")

    def _end_run(self) -> None:
        if self._worker is not None:
            self._worker.wait(1000)
            self._worker = None
        self._busy(False)

    @property
    def running(self) -> bool:
        return self._worker is not None

    def closeEvent(self, event) -> None:
        # Not while a frame run is driving the stage.
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class _MapAndPad(QBoxLayout):
    """The square map with the jog pad beside it or under it, whichever
    leaves the map bigger, packed together in the middle of the space."""

    def __init__(self, map_: QWidget, pad: QWidget) -> None:
        super().__init__(QBoxLayout.Direction.TopToBottom)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(10)
        self._map, self._pad = map_, pad
        self.addWidget(map_, 1)
        self.addWidget(pad, 0)

    def minimumSize(self) -> QSize:
        # The pad-under arrangement's: QBoxLayout would report the current
        # direction's, and beside would pin the column at map + pad wide.
        m, pad = self._map.minimumSize(), self._pad.sizeHint()
        return QSize(max(m.width(), pad.width()),
                     m.height() + self.spacing() + pad.height())

    def setGeometry(self, r: QRect) -> None:
        pad, gap = self._pad.sizeHint(), self.spacing()
        beside_side = min(r.width() - pad.width() - gap, r.height())
        under_side = min(r.width(), r.height() - pad.height() - gap)
        beside = beside_side > under_side
        want = (QBoxLayout.Direction.LeftToRight if beside
                else QBoxLayout.Direction.TopToBottom)
        if self.direction() != want:
            self.setDirection(want)     # for the size hints
        super().setGeometry(r)
        side = max(self._map.minimumWidth(), beside_side if beside else under_side)
        if beside:
            w, h = side + gap + pad.width(), side
        else:
            w, h = side, side + gap + pad.height()
        x = r.x() + max(0, (r.width() - w) // 2)
        y = r.y() + max(0, (r.height() - h) // 2)
        self._map.setGeometry(QRect(x, y, side, side))
        if beside:
            self._pad.setGeometry(QRect(x + side + gap,
                                        y + (side - pad.height()) // 2,
                                        pad.width(), pad.height()))
        else:
            self._pad.setGeometry(QRect(x + (side - pad.width()) // 2,
                                        y + side + gap,
                                        pad.width(), pad.height()))


class _StageView(QWidget):
    """The stage's always-shown page. Owns Esc = STOP ALL, hung on its
    top-level window when first shown: a shortcut whose widget is hidden
    never fires, and this page can be tabbed out of sight."""

    def __init__(self, on_esc) -> None:
        super().__init__()
        self._on_esc = on_esc
        self._esc: QShortcut | None = None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._esc is None:
            esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.window())
            esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            esc.activated.connect(self._on_esc)
            # Unhooked at once: a second page's Esc would make it ambiguous.
            self.destroyed.connect(
                lambda: (esc.setKey(QKeySequence()), esc.deleteLater()))
            self._esc = esc


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits StageSettings
    # The adapter handles it: only it can reach the camera frame.
    save_fov_requested = pyqtSignal()
    # A SavedFov, right-clicked on the map; the adapter finds its ROI set.
    fov_preview_requested = pyqtSignal(object)

    def __init__(self, settings: StageSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or load_settings()
        self._ctrl = None                       # bound while a session is running
        self._last_xy = (0.0, 0.0)
        self._last_z = 0.0      # only meaningful with a Z axis
        self._last_map_xy: tuple[float, float] | None = None
        # The FOV last gone to, until the stage drifts off it.
        self._active_fov = None
        self._map_fovs: list[fov_store.SavedFov] = []   # index-aligned with the map's
        self._axis_widgets: dict[str, dict] = {}
        self._cal_dialog: CalibrationDialog | None = None
        # Map drag: the commanded point chases the dragged target, capped.
        self._drag_cmd: tuple[float, float] | None = None
        self._drag_target: tuple[float, float] | None = None
        self._drag_t = 0.0
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(self._DRAG_TICK_MS)
        self._drag_timer.timeout.connect(self._drag_tick)
        self._cam_axes = self._s.cam_axes
        self._axes_run: AxesMeasurement | None = None
        # Set by the adapter: () -> newest camera frame, () -> its binning.
        self._grab_frame = lambda: None
        self._frame_binning = lambda: 1
        self._build()
        self.refresh_fovs()
        self._set_cam_axes(self._cam_axes, save=False)

    def _build(self) -> None:
        """Two widgets: `self.view`, the always-shown page (position, map,
        jog pad, go-to, STOP ALL), and this panel, the setup behind it."""
        self.view = _StageView(self._stop_all)
        vroot = QVBoxLayout(self.view)
        vroot.setContentsMargins(4, 4, 4, 4)
        vroot.setSpacing(6)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Live readout + frame status, the panel's headline ───────────────
        # X, Y (and Z) split one row equally, not by their text: no jitter.
        pos_row = QHBoxLayout()
        pos_row.setContentsMargins(0, 0, 0, 0)
        pos_row.setSpacing(4)
        # A stylesheet, not setFont: the app's stylesheet would override it.
        big = QFont(self.font())
        big.setPixelSize(16)
        big.setBold(True)
        big_w = QFontMetrics(big).horizontalAdvance("-0000.0") + 4

        def _readout(name: str) -> QLabel:
            lbl = QLabel("—")
            lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
            lbl.setMinimumWidth(big_w)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                              QSizePolicy.Policy.Preferred)
            cap = QLabel(name)
            cap.setStyleSheet("color:#9aa0a6;")
            pos_row.addWidget(cap)
            pos_row.addWidget(lbl, 1)
            return lbl

        self._lbl_x = _readout(self._s.x.name)
        self._lbl_y = _readout(self._s.y.name)
        self._lbl_z: QLabel | None = None
        if self._s.has_z:
            self._lbl_z = _readout(self._s.z.name)
        unit = QLabel("µm")
        unit.setStyleSheet("color:#9aa0a6;")
        pos_row.addWidget(unit)
        vroot.addLayout(pos_row)

        # Shown only when absolute moves are off.
        self._lbl_frame = QLabel()
        self._lbl_frame.setWordWrap(True)
        self._lbl_frame.setStyleSheet(f"color: {_BAD};")
        vroot.addWidget(self._lbl_frame)

        # ── Setup (camera axes, calibration; port and poll rate are
        # acqapp_local.json's): this panel ───────────────────────────────────
        self._spn_rotation = spin(
            -180.0, 180.0, self._s.frame_rotation_deg, decimals=1, suffix=" °",
            tooltip=self._ROTATION_TIP)
        self._spn_drag = spin(
            10.0, 1000.0, self._s.drag_speed_um_s, decimals=0, suffix=" µm/s",
            tooltip="Top speed when dragging the blue dot on the map. The "
                    "stage follows the drag no faster than this.")

        self._spn_rotation.valueChanged.connect(self._emit_settings)
        self._spn_drag.valueChanged.connect(self._emit_settings)

        # Measured stage -> camera axes: jogs, the map and the camera's arrows.
        self._lbl_axes = QLabel()
        self._lbl_axes.setStyleSheet("font-size: 10px;")
        self._lbl_axes.setWordWrap(True)        # measuring progress is long
        self._btn_measure_axes = QPushButton("Measure…")
        self._btn_measure_axes.setToolTip(
            "MOTION: nudges the stage +X, back, +Y, back (one step each) and "
            "finds how the camera picture shifts. Needs Live view on a "
            "textured sample in focus. Afterwards Jog X+ moves the view "
            "right and Y+ up (the picture slides the other way), the map "
            "turns to match the camera, and the camera view shows +X/+Y "
            "arrows (the way the view moves).")
        self._btn_measure_axes.clicked.connect(self._measure_axes)
        self._btn_clear_axes = QPushButton("Clear")
        self._btn_clear_axes.setToolTip(
            "Forget the measured axes: back to Frame rotation.")
        self._btn_clear_axes.clicked.connect(lambda: self._set_cam_axes(None))

        # ── Travel map (X/Y; Z is the readout's) ─────────────────────────────
        self._map = StageMap()
        self._map.set_axes(self._s.x, self._s.y, self._s.invert_y)
        self._map.drag_moved.connect(self._on_drag_moved)
        self._map.fov_menu_requested.connect(self._on_fov_menu)

        # ── Motion (enabled only while connected) ────────────────────────────
        self._pad = self._build_pad()
        vroot.addLayout(_MapAndPad(self._map, self._pad), 1)

        self._motion = QWidget()
        mv = QHBoxLayout(self._motion)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(6)

        # Saved FOVs (named position + snapshot, reused from routines).
        self._btn_save_fov = QPushButton("Save FOV…")
        self._btn_save_fov.setToolTip(
            "Name the current position and save it, with a camera snapshot, "
            "so it can be reached again from here or from a routine step.")
        self._btn_save_fov.clicked.connect(self.save_fov_requested)
        self._btn_goto_fov = QPushButton("Go to FOV…")
        self._btn_goto_fov.setToolTip("Pick a saved FOV and move there.")
        self._btn_goto_fov.clicked.connect(self._pick_and_goto_fov)
        for b in (self._btn_save_fov, self._btn_goto_fov):
            b.setStyleSheet(style.solid_btn("stage"))
            mv.addWidget(b)

        # Beside the motion widget, not in it: that is disabled during a frame
        # run, and a disabled parent's children can't be clicked.
        self._btn_stop_all = QPushButton("STOP ALL (Esc)")
        self._btn_stop_all.setStyleSheet(style.stop_btn())
        self._btn_stop_all.clicked.connect(self._stop_all)
        self._btn_home = QPushButton("Set home here")
        self._btn_home.setStyleSheet(style.solid_btn("stage"))
        self._btn_home.setToolTip(
            "Declare the CURRENT position as home (0,0), the centre of the "
            f"imaging window: the map draws the {WINDOW_DIAMETER_UM:g} µm "
            f"window around it and shows ±{WINDOW_RANGE_UM:g} µm. Doesn't "
            "move the stage, and doesn't limit where it can go.")
        self._btn_home.clicked.connect(self._set_home_here)
        vroot.addWidget(self._motion)
        stop_row = QHBoxLayout()
        stop_row.setSpacing(6)
        stop_row.addWidget(self._btn_home)
        stop_row.addWidget(self._btn_stop_all, 1)   # STOP ALL keeps the room
        vroot.addLayout(stop_row)

        # ── Setup ────────────────────────────────────────────────────────────
        self._btn_calibrate = QPushButton("Calibrate…")
        self._btn_calibrate.setToolTip(
            "Re-establish the coordinate frame (X/Y, and Z's zero). Rarely "
            "needed; it drives the stage into its hard limits.")
        self._btn_calibrate.clicked.connect(self._open_calibration)

        # Frame first (calibration, how the camera sits), then motion
        # feel; jog steps share the last line. Narrow: it folds under the map.
        steps = tuple(x for pair in self._steps for x in pair)
        measure = QHBoxLayout()
        measure.setSpacing(6)
        measure.addWidget(self._btn_measure_axes)
        measure.addWidget(self._btn_clear_axes)
        root.addLayout(button_row(self._btn_calibrate))
        root.addLayout(pairs_grid(
            ("Camera axes:", self._lbl_axes, None, measure),
            ("Frame rotation:", self._spn_rotation),
            ("Drag speed ≤", self._spn_drag),
            steps, per_row=1, gap=12))     # one per line: the column stays narrow
        root.addStretch()

        self._set_controls_enabled(False)
        self._update_frame_status()

    def _build_pad(self) -> QWidget:
        """Jog arrows (X/Y in a cross, Z beside); their (label, step) pairs
        go to `self._steps`, for the settings. Registers each axis's step and
        jog buttons."""
        def _arrow(text: str, key: str, sign: int, tip: str) -> QPushButton:
            b = QPushButton(text)
            b.setFixedSize(34, 28)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False: self._jog(key, sign))
            return b

        x, y = self._s.x.name, self._s.y.name
        left = _arrow("←", "x", -1, f"Jog {x}−")
        right = _arrow("→", "x", +1, f"Jog {x}+")
        up = _arrow("↑", "y", +1, f"Jog {y}+")
        down = _arrow("↓", "y", -1, f"Jog {y}−")
        # One grid, so Z's two rows line up with the cross's.
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.addWidget(up, 0, 1)
        grid.addWidget(left, 1, 0)
        grid.addWidget(down, 1, 1)
        grid.addWidget(right, 1, 2)

        spn_xy = spin(0.1, 5000.0, self._s.x.step_um, decimals=1, suffix=" µm",
                      tooltip="How far one X/Y jog moves.")
        steps = [("Step XY:", spn_xy)]

        self._axis_widgets["x"] = {"step": spn_xy, "buttons": [left, right]}
        self._axis_widgets["y"] = {"step": spn_xy, "buttons": [down, up]}
        if self._s.has_z:
            z = self._s.z.name
            z_up = _arrow(f"{z}▲", "z", +1, f"Jog {z}+")
            z_down = _arrow(f"{z}▼", "z", -1, f"Jog {z}−")
            grid.setColumnMinimumWidth(3, 8)    # the gap between X/Y and Z
            grid.addWidget(z_up, 0, 4)
            grid.addWidget(z_down, 1, 4)
            spn_z = spin(0.1, 5000.0, self._s.z.step_um, decimals=1,
                         suffix=" µm", tooltip=f"How far one {z} jog moves.")
            steps.append((f"Step {z}:", spn_z))
            self._axis_widgets["z"] = {"step": spn_z, "buttons": [z_down, z_up]}
        self._steps = steps

        pad = QWidget()
        pad.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        return pad

    # ── binding to a live controller ────────────────────────────────────────
    def bind_controller(self, controller) -> None:
        """Attach the connected controller (session start) or None (stop)."""
        self._ctrl = controller
        self._set_controls_enabled(controller is not None)
        if controller is None:
            self._cancel_axes_run()
            self.clear_position()
            if self._cal_dialog is not None and not self._cal_dialog.running:
                self._cal_dialog.reject()
                self._cal_dialog = None
        self._update_frame_status()

    def clear_position(self) -> None:
        """The readout stopped (unbound, or the poll died): take the dot off
        the map, so no drag starts from a stale position."""
        self._end_drag()
        self._map.clear_position()
        # Else a reconnect at the same spot never redraws the cleared dot.
        self._last_map_xy = None

    def _set_controls_enabled(self, on: bool) -> None:
        self._motion.setEnabled(on)      # Z and the FOV buttons ride along
        self._pad.setEnabled(on)
        self._btn_stop_all.setEnabled(on)
        self._btn_calibrate.setEnabled(on)
        self._btn_home.setEnabled(on)

    # ── calibration dialog ──────────────────────────────────────────────────
    def _open_calibration(self) -> None:
        if self._ctrl is None:
            return
        dlg = CalibrationDialog(self._ctrl, self._s, self)
        self._cal_dialog = dlg
        dlg.changed.connect(self._on_calibration_changed)
        dlg.exec()
        self._cal_dialog = None
        self._on_calibration_changed()

    def _set_home_here(self) -> None:
        """Declare the current X/Y as home (0,0), the window's centre. Never
        moves the stage."""
        if self._ctrl is None:
            return
        if QMessageBox.question(
            self, "Set home",
            "Set the CURRENT position as home (0,0), the centre of the "
            "imaging window?\n\nCentre the stage first: this does NOT move "
            "it. Soft limits are set to ±½ inch around this point."
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._call("Could not set home", lambda c: c.set_center_here()):
            self._on_calibration_changed()

    def _on_calibration_changed(self) -> None:
        """Calibration was rewritten (or a frame run started/ended) — re-sync."""
        self._apply_gating()
        self._map.update()
        self.settings_changed.emit(self.settings)

    # ── frame status ────────────────────────────────────────────────────────
    def _update_frame_status(self) -> None:
        """Show whether absolute moves (map drag, Go to FOV) can be trusted,
        and gate them; jogs are relative and stay."""
        missing = [ax.name for ax in (self._s.x, self._s.y) if not ax.has_frame]
        ok = not missing
        self._lbl_frame.setText(
            f"No frame for {', '.join(missing)}: only jogs work. "
            "Settings ▸ Calibrate…")
        self._lbl_frame.setVisible(not ok)
        self._btn_goto_fov.setEnabled(ok)
        self._sync_drag(ok)

    def _motion_busy(self) -> bool:
        """A frame run or an axes measurement is driving the stage."""
        return ((self._cal_dialog is not None and self._cal_dialog.running)
                or (self._axes_run is not None and self._axes_run.running))

    def _apply_gating(self) -> None:
        # No competing motion while something else is driving the stage.
        free = self._ctrl is not None and not self._motion_busy()
        self._motion.setEnabled(free)
        self._pad.setEnabled(free)
        self._update_frame_status()

    def _sync_drag(self, frame_ok: bool) -> None:
        """Map dragging is an absolute move: connected, framed, not busy.
        The axes buttons share the gate (less the frame: they jog)."""
        free = self._ctrl is not None and not self._motion_busy()
        on = free and frame_ok
        if not on:
            self._end_drag()
        self._map.set_draggable(on)
        self._btn_measure_axes.setEnabled(free)
        measuring = self._axes_run is not None and self._axes_run.running
        self._btn_clear_axes.setEnabled(self._cam_axes is not None
                                        and not measuring)

    # A position clamped to an edge reads back a count's rounding past it.
    _EDGE_TOL_UM = 0.5

    _POSITION_UNKNOWN = ("The stage position is unknown: no reading since "
                         "connecting, or the readout stopped.")

    def _outside_range(self, x_um: float, y_um: float) -> list[str]:
        """The X/Y axes on which (x, y) is outside where motion may go."""
        tol = self._EDGE_TOL_UM
        return [ax.name for ax, v in ((self._s.x, x_um), (self._s.y, y_um))
                if not ax.soft_limits_um()[0] - tol <= v
                <= ax.soft_limits_um()[1] + tol]

    def _call(self, what: str, fn) -> bool:
        """Run `fn(controller)` if connected, reporting failure in one place —
        the shape every motion command shares."""
        if self._ctrl is None:
            return False
        try:
            fn(self._ctrl)
        except Exception as e:
            QMessageBox.warning(self, "Stage", f"{what}: {e}")
            return False
        return True

    # ── motion handlers ─────────────────────────────────────────────────────
    # Every other motion command ends a map drag, or its next tick would undo it.
    def _jog(self, key: str, direction: int) -> None:
        self._end_drag()
        d = direction * self._axis_widgets[key]["step"].value()
        if key == "z":      # focus: the camera alignment is X/Y only
            self._call("Jog failed", lambda c: c.jog_um("z", d))
            return
        dx, dy = camera_jog(key, d, self._spn_rotation.value(), self._cam_axes)
        self._call("Jog failed", lambda c: c.jog_xy_um(dx, dy))

    def _pick_and_goto_fov(self) -> None:
        from acqApp.devices.stage.fov_picker import FovPicker
        dlg = FovPicker(self)
        dlg.exec()
        if dlg.fov is not None:
            self.goto_fov(dlg.fov)

    def goto_fov(self, fov) -> None:
        """MOTION: move to a saved FOV (and its Z, if both have one), asking
        first if either XY or Z is a large move."""
        if self._ctrl is None:
            return
        # A clamped move would land short of it and still call it "active".
        out = self._outside_range(fov.x_um, fov.y_um)
        if out:
            QMessageBox.warning(
                self, "Stage", f'FOV "{fov.name}" is outside the allowed '
                f"{'/'.join(out)} range.")
            return
        cur_x, cur_y = self._last_xy
        dist_xy = max(abs(fov.x_um - cur_x), abs(fov.y_um - cur_y))
        move_z = (self._s.has_z and fov.z_um is not None)
        dist_z = abs(fov.z_um - self._last_z) if move_z else 0.0
        known = self.position_known
        big_xy = known and dist_xy > self._s.confirm_move_um
        big_z = known and move_z and dist_z > self._s.confirm_move_z_um
        if big_xy or big_z or not known:
            msg = (f'Move to FOV "{fov.name}" at {fov.x_um:.0f}, '
                   f"{fov.y_um:.0f}")
            if move_z:
                msg += f", {fov.z_um:.0f}"
            parts = []
            if big_xy:
                parts.append(f"XY {dist_xy:.0f} µm")
            if big_z:
                parts.append(f"Z {dist_z:.0f} µm")
            msg += (f" µm ({', '.join(parts)})?" if known
                    else f" µm anyway?\n\n{self._POSITION_UNKNOWN}")
            if QMessageBox.question(
                self, "Confirm move", msg
            ) != QMessageBox.StandardButton.Yes:
                return

        def _go(c) -> None:
            c.move_to_um("x", fov.x_um)
            c.move_to_um("y", fov.y_um)
            if move_z:
                c.move_to_um("z", fov.z_um)

        self._end_drag()
        if self._call("Move failed", _go):
            # "Active" until the stage drifts off it again — see set_readout.
            self._active_fov = fov
            self.refresh_fovs()

    # ── drag on the map (MOTION, speed-capped) ──────────────────────────────
    _DRAG_TICK_MS = 100
    _DRAG_MAX_DT_S = 0.25   # a stalled GUI must not turn into one long jump

    def _on_drag_moved(self, x_um: float, y_um: float) -> None:
        if self._ctrl is None:
            return
        if self._drag_cmd is None:
            if not self.position_known:
                self._end_drag()
                QMessageBox.warning(self, "Stage", f"{self._POSITION_UNKNOWN} "
                                    f"Dragging starts from it, so it is off.")
                return
            out = self._outside_range(*self._last_xy)
            if out:
                # Its first step would clamp to the range's edge: a jump at
                # full speed, not a speed-capped drag.
                self._end_drag()
                QMessageBox.warning(
                    self, "Stage", f"{'/'.join(out)} is outside its allowed "
                    f"range, so dragging would jump to the range's edge. Use "
                    f"Go to, or untick Settings ▸ Calibrate… ▸ Limit to "
                    f"window.")
                return
        self._drag_target = (x_um, y_um)
        self._map.set_target(self._drag_target)
        if self._drag_cmd is None:
            self._drag_cmd = self._last_xy
            self._drag_t = time.monotonic()
            self._drag_timer.start()

    def _drag_tick(self) -> None:
        """One capped step toward the drag target; done once there and the
        mouse is up."""
        if self._drag_cmd is None or self._drag_target is None:
            self._end_drag()
            return
        now = time.monotonic()
        dt = min(now - self._drag_t, self._DRAG_MAX_DT_S)
        self._drag_t = now
        cmd = self._drag_cmd
        nxt = step_toward(cmd, self._drag_target, self._spn_drag.value() * dt)
        if nxt == cmd:
            if not self._map.dragging:
                self._end_drag()
            return

        def _go(c) -> None:
            for key, new, old in (("x", nxt[0], cmd[0]), ("y", nxt[1], cmd[1])):
                if new != old:
                    c.move_to_um(key, new)

        # Stopped first: the failure warning is modal and the timer would re-enter.
        self._drag_timer.stop()
        if not self._call("Drag move failed", _go):
            self._end_drag()
            return
        self._drag_cmd = nxt
        self._drag_timer.start()

    def _end_drag(self) -> None:
        self._drag_timer.stop()
        self._drag_cmd = self._drag_target = None
        self._map.cancel_drag()
        self._map.set_target(None)

    # The panic path (Esc, app-wide), so guarded hardest: a dead link is exactly
    # when it's pressed, and an escaping slot exception aborts the process.
    def _stop_all(self) -> None:
        self._end_drag()
        self._cancel_axes_run()
        self._call("STOP ALL failed", lambda c: c.stop_all())

    # ── camera axes (MOTION while measuring) ────────────────────────────────
    def set_camera_source(self, grab, binning) -> None:
        """`grab()` -> the newest camera frame or None; `binning()` -> its
        binning. The adapter's: only it can reach the camera."""
        self._grab_frame, self._frame_binning = grab, binning

    @property
    def cam_axes(self):
        return self._cam_axes

    def _set_cam_axes(self, axes, save: bool = True) -> None:
        self._cam_axes = axes
        measured = axes is not None
        self._lbl_axes.setText(describe(axes) if measured else "not measured")
        self._lbl_axes.setToolTip(
            "Angles are counter-clockwise from screen-right: the way the "
            "view moves for stage +X/+Y." if measured else
            "Jogs use Frame rotation until the axes are measured.")
        self._spn_rotation.setEnabled(not measured)
        self._spn_rotation.setToolTip(
            "Not used: the measured camera axes replace it (Clear them to use "
            "this)." if measured else self._ROTATION_TIP)
        self._map.set_cam_axes(axes)
        self._update_frame_status()
        if save:
            self._emit_settings()

    _ROTATION_TIP = (
        "Rotates only the JOG buttons' direction, so 'up' here matches 'up' "
        "on the camera regardless of how the stage is physically mounted. "
        "Absolute go-to, soft limits and calibration are unaffected. 0 = off. "
        "Camera axes → Measure… finds it for you.")

    def _measure_axes(self) -> None:
        if self._ctrl is None or self._motion_busy():
            return
        if self._grab_frame() is None:
            QMessageBox.information(
                self, "Measure camera axes",
                "No camera frame: start Live view on the voltage camera, on "
                "a textured sample in focus.")
            return
        step, ok = QInputDialog.getDouble(
            self, "Measure camera axes",
            "MOTION: moves the stage +X, back, +Y, back by this step.\n"
            "The picture must shift visibly but by well under half the "
            "field.\n\nStep (µm):", 50.0, 2.0, 1000.0, 1)
        if not ok:
            return
        if not self.position_known:
            QMessageBox.warning(self, "Measure camera axes",
                                f"{self._POSITION_UNKNOWN} The room for the "
                                f"nudges can't be checked.")
            return
        # A nudge clamped at a limit moves less than `step`: wrong axes, and
        # the full step back leaves the stage off its start.
        short = [ax.name for ax, v in zip((self._s.x, self._s.y), self._last_xy)
                 if ax.soft_min is not None and ax.soft_max is not None
                 and v + step > ax.soft_limits_um()[1] + self._EDGE_TOL_UM]
        if short:
            QMessageBox.warning(
                self, "Measure camera axes",
                f"No room for a +{step:g} µm nudge on {'/'.join(short)}: it "
                f"would stop at the limit and mis-measure. Move back from "
                f"that edge, or use a smaller step.")
            return
        self._end_drag()
        run = AxesMeasurement(self._ctrl, self._grab_frame, step,
                              int(self._frame_binning() or 1), self)
        run.progress.connect(self._lbl_axes.setText)
        run.finished.connect(self._on_axes_measured)
        run.failed.connect(self._on_axes_failed)
        # A child of the panel: freed per run, not kept until the panel goes.
        run.finished.connect(run.deleteLater)
        run.failed.connect(run.deleteLater)
        self._axes_run = run
        if not self._call("Could not start the measurement", lambda c: run.start()):
            self._axes_run = None
            run.deleteLater()
            return
        self._apply_gating()

    def _on_axes_measured(self, axes) -> None:
        self._axes_run = None
        self._set_cam_axes(axes)
        self._apply_gating()

    def _on_axes_failed(self, msg: str) -> None:
        self._axes_run = None
        self._set_cam_axes(self._cam_axes, save=False)    # restores the label
        self._apply_gating()
        QMessageBox.warning(self, "Measure camera axes", msg)

    def _cancel_axes_run(self) -> None:
        if self._axes_run is not None:
            self._axes_run.cancel()

    # ── saved FOVs on the map ───────────────────────────────────────────────
    def refresh_fovs(self) -> None:
        """Re-read this session's FOVs onto the map (plus the active one,
        even if it came from the archive)."""
        try:
            fovs = fov_store.list_session()
        except OSError:
            fovs = []
        act = self._active_fov
        if act is not None and all(f.path != act.path for f in fovs):
            fovs.append(act)
        self._map_fovs = fovs
        self._map.set_fovs([(f.name, f.x_um, f.y_um) for f in fovs],
                           act.name if act is not None else "")

    def _on_fov_menu(self, i: int, pos) -> None:
        fov = self._map_fovs[i]     # set with the map's list: always in range
        menu = QMenu(self)
        menu.addSection(fov.name)
        menu.addAction("Show snapshot + ROIs…").triggered.connect(
            lambda: self.fov_preview_requested.emit(fov))
        menu.exec(pos)

    # ── live readout from the poll worker ───────────────────────────────────
    # Below this a move is invisible on the map; skip the repaint.
    _MAP_EPS_UM = 0.5

    def _off_active_fov(self, x_um: float, y_um: float,
                       z_um: float | None) -> bool:
        fov = self._active_fov
        if abs(x_um - fov.x_um) >= self._MAP_EPS_UM \
                or abs(y_um - fov.y_um) >= self._MAP_EPS_UM:
            return True
        if self._s.has_z and fov.z_um is not None and z_um is not None:
            return abs(z_um - fov.z_um) >= self._MAP_EPS_UM
        return False

    def set_readout(self, x_um: float, y_um: float, z_um: float | None = None) -> None:
        if self._active_fov is not None and self._off_active_fov(x_um, y_um, z_um):
            self._active_fov = None
            self.refresh_fovs()
        self._last_xy = (x_um, y_um)
        self._lbl_x.setText(f"{x_um:.1f}")
        self._lbl_y.setText(f"{y_um:.1f}")
        if z_um is not None and self._lbl_z is not None:
            self._last_z = z_um
            self._lbl_z.setText(f"{z_um:.1f}")
        last = self._last_map_xy
        if (last is None or abs(x_um - last[0]) >= self._MAP_EPS_UM
                or abs(y_um - last[1]) >= self._MAP_EPS_UM):
            self._last_map_xy = (x_um, y_um)
            self._map.set_position(x_um, y_um)

    def _emit_settings(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def connected(self) -> bool:
        return self._ctrl is not None

    @property
    def active_fov_name(self) -> str:
        return self._active_fov.name if self._active_fov is not None else ""

    @property
    def position_known(self) -> bool:
        """A readout arrived since binding, and the poll hasn't died since."""
        return self._last_map_xy is not None

    @property
    def current_position(self) -> tuple[float, float, float | None]:
        """The last displayed X/Y(/Z). Use this, not the worker's
        `get_latest()`, which the display tick has already consumed."""
        return (*self._last_xy, self._last_z if self._s.has_z else None)

    @property
    def settings(self) -> StageSettings:
        s = self._s
        return StageSettings(
            port=s.port,
            controller=s.controller,
            poll_hz=s.poll_hz,
            confirm_move_um=s.confirm_move_um,
            confirm_move_z_um=s.confirm_move_z_um,
            margin_um=s.margin_um,
            invert_y=s.invert_y,
            frame_rotation_deg=self._spn_rotation.value(),
            drag_speed_um_s=self._spn_drag.value(),
            cam_axes=self._cam_axes,
            x=s.x, y=s.y, z=s.z,
        )
