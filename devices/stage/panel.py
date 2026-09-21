"""
The stage's UI: the settings panel and the calibration dialog.

Split from `settings.py`, which keeps the model and persistence — the axis
calibration, soft limits and origin **shared with the standalone
`stage_control` app**. That half has no Qt and is the one worth reading
when the question is "where does 0,0 come from"; this half is widgets.

`CalibrationDialog` runs `establish_frame()` on a `_FrameWorker` thread.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

from acqApp import style
from acqApp.acq.worker import PullWorker
from acqApp.devices.stage.map_widget import StageMap, ZGauge
from acqApp.devices.stage.settings import (_BAD, _C_CUR, _C_HOME, _C_ORIGIN,
                                   _C_SOFT, StageSettings, config_path,
                                   load_settings)


def _available_ports(current: str) -> list[str]:
    """Serial ports present on this machine, `current` first so the saved
    setting is never dropped because the device is unplugged right now.
    Enumeration failing must not stop the panel from being built."""
    ports = []
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
    except Exception:                       # noqa: BLE001 — a combo box isn't worth a crash
        pass
    ordered = [current] if current else []
    ordered += [p for p in sorted(ports) if p != current]
    return ordered


class _FrameWorker(PullWorker):
    """Runs controller.establish_frame() off the GUI thread — it drives both axes
    into their reverse hard limits and can block for minutes.

    Subclasses PullWorker (not QThread) so a bug in establish_frame() surfaces
    as `error`/`failed`, not a qFatal that aborts the process mid-move.
    """
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
    """Everything that rewrites the saved calibration, kept off the main panel.

    These two actions are rare, hard to undo, and one of them drives into the
    hard limits — they don't belong next to the buttons used all day. The session
    home is NOT here: it's a bookmark, not calibration.
    """
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

        zero = QGroupBox("Origin (0,0)")
        zl = QVBoxLayout(zero)
        self._btn_set_zero = QPushButton("Set 0,0 = center (here)")
        self._btn_set_zero.setToolTip(
            "Declare the CURRENT position as 0,0 and put soft limits at ±½ inch "
            "around it. Doesn't move the stage — centre it first.")
        self._btn_set_zero.clicked.connect(self._set_zero_here)
        zl.addWidget(self._btn_set_zero)
        zl.addWidget(self._hint(
            "Centre the stage yourself first — this doesn't move it. "
            "Saved to the config; survives restarts."))
        lay.addWidget(zero)

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
            "Rewrites slope/offset only; it never sets 0,0 for you."))
        self._lbl_progress = QLabel("")
        self._lbl_progress.setWordWrap(True)
        fl.addWidget(self._lbl_progress)
        lay.addWidget(frame)

        # ── Focus (Z), separate section: a focus axis under a scope is a
        # different risk profile from X/Y's open-table travel — driving it
        # into its hard limits can ram the objective into the sample instead
        # of just losing a coordinate frame. See _reestablish_frame_z's
        # two-stage warning.
        self._lbl_status_z: QLabel | None = None
        self._btn_set_zero_z: QPushButton | None = None
        self._btn_reframe_z: QPushButton | None = None
        if self._s.has_z:
            self._build_focus_calibration(lay)

        self._lbl_cfg = self._hint(f"config: {config_path()}")
        lay.addWidget(self._lbl_cfg)

        # This dialog is modal, so the panel's STOP ALL is unreachable while
        # it's up — and the frame re-establish drives into the hard limits
        # from right here. The abort has to exist on this window too.
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
        # Qt's default Esc on a QDialog is reject(). Esc is the app-wide panic
        # key, and this is the one window where the stage may be driving into a
        # limit — so it stops the stage instead of dismissing the dialog.
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
    def _update_status(self) -> None:
        missing = [ax.name for ax in (self._s.x, self._s.y) if not ax.has_frame]
        if missing:
            self._lbl_status.setText(
                f"No valid frame for {', '.join(missing)}. Absolute go-to is "
                "disabled; jog still works. Re-establish the frame, then set 0,0.")
            self._lbl_status.setStyleSheet(f"color: {_BAD};")
        else:
            self._lbl_status.setText(
                f"Frame OK — 0,0 at {self._s.x.ref_counts:.0f}, "
                f"{self._s.y.ref_counts:.0f} counts. Invalid from the moment a "
                "hard limit is hit.")
            self._lbl_status.setStyleSheet("")
        if self._lbl_status_z is not None:
            z = self._s.z
            if not z.has_frame:
                self._lbl_status_z.setText(
                    f"No valid frame for {z.name}. Absolute go-to (Go, slider) "
                    "is disabled; jog still works.")
                self._lbl_status_z.setStyleSheet(f"color: {_BAD};")
            else:
                self._lbl_status_z.setText(
                    f"Frame OK — 0,0 at {z.ref_counts:.0f} counts. Invalid "
                    "from the moment a hard limit is hit.")
                self._lbl_status_z.setStyleSheet("")

    def _busy(self, on: bool) -> None:
        self._btn_set_zero.setEnabled(not on)
        self._btn_reframe.setEnabled(not on)
        if self._btn_set_zero_z is not None:
            self._btn_set_zero_z.setEnabled(not on)
        if self._btn_reframe_z is not None:
            self._btn_reframe_z.setEnabled(not on)
        self._btn_close.setEnabled(not on)      # closing mid-move orphans the worker

    # ── actions ─────────────────────────────────────────────────────────────
    def _set_zero_here(self) -> None:
        if self._ctrl is None:
            return
        if QMessageBox.question(
            self, "Set 0,0 = center",
            "Set the CURRENT position as 0,0 (true center)?\n\n"
            "Centre the stage first — this does NOT move it. Soft limits will be "
            "set to ±½ inch around this point."
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._ctrl.set_center_here()
        except Exception as e:
            QMessageBox.warning(self, "Stage", f"Could not set origin: {e}")
            return
        self._lbl_progress.setText("0,0 set here; soft limits ±0.5 inch.")
        self._update_status()
        self.changed.emit()

    def _reestablish_frame(self) -> None:
        if self._ctrl is None or self._worker is not None:
            return
        if QMessageBox.warning(
            self, "Re-establish frame",
            "THIS MOVES THE STAGE.\n\n"
            "X and then Y will be driven into their REVERSE hard limits, then "
            "probed to re-measure the command→encoder map. Takes a minute or more "
            "per axis.\n\n"
            "Your 0,0 is NOT changed — only slope/offset are rewritten. The stage "
            "is left at a probe position, not at 0,0.\n\n"
            "Make sure the stage is clear, watch it, and keep Esc (STOP ALL) "
            "reachable. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._busy(True)
        self._lbl_progress.setText("Starting…")
        self._worker = _FrameWorker(self._ctrl)
        self._worker.progress.connect(self._lbl_progress.setText)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        self.changed.emit()                     # panel locks its motion controls

    # ── Focus (Z) — its own section: same shape as Origin/Coordinate frame
    #    above, but Z's "drive to hard limits" step gets a second, distinct
    #    warning on top of the first, because what's at stake if it's wrong
    #    is the objective and the sample, not just a lost coordinate frame.
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

        # This backend's readout never drifts (see StageController.
        # supports_reframe), so on this rig the button below would always
        # refuse — not worth offering, and definitely not worth tempting an
        # operator into two "are you sure" warnings for a guaranteed no-op.
        # Kept for a future rig whose Z motor lives on hardware that DOES
        # need it (an MCM6101-style controller).
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
        # First warning: what this does and why it's different from X/Y,
        # plus the datasheet specs so the operator knows exactly what the
        # hardware is capable of before deciding.
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
        # Second, separate warning: a deliberate re-check, not a repeat of the
        # first — this is the "multiple warnings" gate, not one dialog with a
        # lot of text.
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
        self._busy(True)
        self._lbl_progress.setText(f"Starting {z.name}…")
        self._worker = _FrameWorker(self._ctrl, axes=("z",))
        self._worker.progress.connect(self._lbl_progress.setText)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        self.changed.emit()                     # panel locks its motion controls

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
        # The stage is mid-move into a hard limit; letting the dialog go would
        # leave the worker with nothing watching it.
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits StageSettings
    # The adapter handles this one: it's the only side that can reach the
    # voltage camera's frame (`ModuleHost.latest_frame`), the same split
    # `devices/dmd/panel.py`'s `rois_edit_requested` uses.
    save_fov_requested = pyqtSignal()

    def __init__(self, settings: StageSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or load_settings()
        self._ctrl = None                       # bound while a session is running
        self._last_xy = (0.0, 0.0)
        self._last_z = 0.0      # meaningless (and unused) unless self._s.has_z
        self._last_map_xy: tuple[float, float] | None = None
        self._last_gauge_z: float | None = None
        # The FOV a goto_fov() last issued a move to, cleared the moment the
        # live position drifts off it again (set_readout) — the Save panel's
        # "append active FOV name" option (see active_fov_name).
        self._active_fov = None
        self._axis_widgets: dict[str, dict] = {}
        self._cal_dialog: CalibrationDialog | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── Connection + live readout ───────────────────────────────────────
        grp = QGroupBox("XY stage")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_port = QComboBox()
        self._cmb_port.setEditable(True)
        # Offer the ports that exist right now, not a list of numbers that
        # were true for the hardware of the day. Windows renumbers these
        # freely — a stale suggestion is how you end up pointed at the port
        # some other device took over.
        self._cmb_port.addItems(_available_ports(self._s.port))
        self._cmb_port.setCurrentText(self._s.port)
        lay.addRow("Port:", self._cmb_port)

        self._spn_rate = QDoubleSpinBox()
        self._spn_rate.setRange(0.5, 50.0)
        self._spn_rate.setSuffix(" Hz")
        self._spn_rate.setValue(self._s.poll_hz)
        lay.addRow("Poll rate:", self._spn_rate)

        self._spn_rotation = QDoubleSpinBox()
        self._spn_rotation.setRange(-180.0, 180.0)
        self._spn_rotation.setDecimals(1)
        self._spn_rotation.setSuffix(" °")
        self._spn_rotation.setValue(self._s.frame_rotation_deg)
        self._spn_rotation.setToolTip(
            "Rotates only the JOG buttons' direction, so 'up' here matches "
            "'up' on the camera regardless of how the stage is physically "
            "mounted. Absolute go-to, soft limits and calibration are "
            "unaffected. 0 = off.")
        lay.addRow("Frame rotation:", self._spn_rotation)

        # Port, poll rate and frame rotation are settings that live in the
        # panel rather than in the calibration file; without this they were
        # never announced, so nothing could persist them and the port
        # reverted every launch.
        self._cmb_port.currentTextChanged.connect(self._emit_settings)
        self._spn_rate.valueChanged.connect(self._emit_settings)
        self._spn_rotation.valueChanged.connect(self._emit_settings)

        self._lbl_x = QLabel("—")
        self._lbl_y = QLabel("—")
        lay.addRow(f"{self._s.x.name} (µm):", self._lbl_x)
        lay.addRow(f"{self._s.y.name} (µm):", self._lbl_y)
        self._lbl_z: QLabel | None = None
        if self._s.has_z:
            self._lbl_z = QLabel("—")
            lay.addRow(f"{self._s.z.name} (µm):", self._lbl_z)
        root.addWidget(grp)

        # ── Travel map (+ Z gauge, beside it) ────────────────────────────────
        map_grp = QGroupBox("Position in travel")
        outer = QHBoxLayout(map_grp)
        outer.setContentsMargins(4, 4, 4, 4)
        ml = QVBoxLayout()
        self._map = StageMap()
        self._map.set_axes(self._s.x, self._s.y, self._s.invert_y)
        ml.addWidget(self._map)
        legend = QLabel(
            f'<span style="color:{_C_CUR}">●</span> position &nbsp; '
            f'<span style="color:{_C_ORIGIN}">✚</span> 0,0 &nbsp; '
            f'<span style="color:{_C_HOME}">◆</span> home &nbsp; '
            f'<span style="color:{_C_SOFT}">▭</span> soft limits')
        legend.setStyleSheet("font-size: 10px;")
        ml.addWidget(legend)
        outer.addLayout(ml, 1)

        # Z is depth, not a second position on the table — its own gauge
        # beside the map rather than a third axis squeezed into it. A caption
        # under it, the same row the map's legend occupies, since the colours
        # are the map's own legend (position/origin/home/soft limits share
        # _C_CUR/_C_ORIGIN/_C_HOME/_C_SOFT) — this only needs to say WHICH
        # axis the bar is.
        self._z_gauge: ZGauge | None = None
        if self._s.has_z:
            zcol = QVBoxLayout()
            self._z_gauge = ZGauge()
            self._z_gauge.set_axis(self._s.z)
            zcol.addWidget(self._z_gauge, 1)
            zcap = QLabel(self._s.z.name)
            zcap.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            zcap.setStyleSheet("font-size: 10px; color:#9aa0a6;")
            zcol.addWidget(zcap)
            outer.addLayout(zcol)
        root.addWidget(map_grp, 1)

        # ── Motion controls (enabled only while connected) ──────────────────
        self._motion = QGroupBox("Motion")
        grid = QGridLayout(self._motion)
        grid.setSpacing(3)
        grid.addWidget(QLabel("Jog −"), 0, 1)
        grid.addWidget(QLabel("Step µm"), 0, 2)
        grid.addWidget(QLabel("Jog +"), 0, 3)
        grid.addWidget(QLabel("Go to µm"), 0, 4)
        def _btn(text: str, width: int, row: int, col: int, slot) -> QPushButton:
            b = QPushButton(text)
            b.setMaximumWidth(width)
            b.clicked.connect(slot)
            grid.addWidget(b, row, col)
            return b

        # Z rides the same grid as X/Y now — one Motion group, one control
        # shape for every axis, rather than X/Y's grid plus Z's own slider
        # block below it. (Z's *calibration* — CalibrationDialog's "Focus
        # (Z)" section — stays separate; that risk profile is genuinely
        # different, drives into the objective, and needs its own warnings.
        # The jog/goto shape here doesn't.)
        axis_rows = [("x", self._s.x), ("y", self._s.y)]
        if self._s.has_z:
            axis_rows.append(("z", self._s.z))
        for r, (key, ax) in enumerate(axis_rows, start=1):
            grid.addWidget(QLabel(ax.name), r, 0)
            btn_minus = _btn("−", 28, r, 1, lambda _, k=key: self._jog(k, -1))

            spn_step = QDoubleSpinBox()
            spn_step.setRange(0.1, 5000.0)
            spn_step.setDecimals(1)
            spn_step.setValue(ax.step_um)
            spn_step.setMaximumWidth(70)
            grid.addWidget(spn_step, r, 2)

            btn_plus = _btn("+", 28, r, 3, lambda _, k=key: self._jog(k, +1))

            spn_goto = QDoubleSpinBox()
            spn_goto.setRange(*ax.soft_limits_um())
            spn_goto.setDecimals(1)
            spn_goto.setSuffix(" µm")
            spn_goto.setMaximumWidth(90)
            grid.addWidget(spn_goto, r, 4)

            btn_go = _btn("Go", 34, r, 5, lambda _, k=key: self._goto(k))
            btn_stop = _btn("Stop", 44, r, 6, lambda _, k=key: self._stop(k))

            self._axis_widgets[key] = {
                "step": spn_step, "goto": spn_goto,
                "buttons": [btn_minus, btn_plus, btn_go, btn_stop],
            }
        root.addWidget(self._motion)

        # ── Session home + go-to-origin (navigation, not calibration) ───────
        self._nav = QGroupBox("Home (this session)")
        nl = QVBoxLayout(self._nav)
        nl.setSpacing(3)
        nrow = QHBoxLayout()
        self._btn_set_home = QPushButton("Set home here")
        self._btn_set_home.setToolTip(
            "Bookmark the current position as this session's working home. "
            "Doesn't move the stage, and isn't saved — it's cleared when the "
            "session ends and never touches the calibrated 0,0.")
        self._btn_set_home.clicked.connect(self._set_home_here)
        nrow.addWidget(self._btn_set_home)

        self._btn_go_home = QPushButton("Go home")
        self._btn_go_home.clicked.connect(self._go_home)
        nrow.addWidget(self._btn_go_home)

        self._btn_clear_home = QPushButton("Clear")
        self._btn_clear_home.setMaximumWidth(50)
        self._btn_clear_home.clicked.connect(self._clear_home)
        nrow.addWidget(self._btn_clear_home)
        nl.addLayout(nrow)

        self._lbl_home = QLabel("home: not set")
        self._lbl_home.setStyleSheet("font-size: 10px;")
        nl.addWidget(self._lbl_home)

        self._btn_go_zero = QPushButton("Go to 0,0 (true zero)")
        self._btn_go_zero.setToolTip("Absolute move of both axes to the "
                                     "calibrated origin.")
        self._btn_go_zero.clicked.connect(self._go_zero)
        nl.addWidget(self._btn_go_zero)
        root.addWidget(self._nav)

        # ── Saved FOVs (named position + snapshot, reused from routines) ────
        self._fovs = QGroupBox("Saved FOVs")
        fovl = QHBoxLayout(self._fovs)
        self._btn_save_fov = QPushButton("Save current as FOV…")
        self._btn_save_fov.setToolTip(
            "Name the current position and save it, with a camera snapshot, "
            "so it can be reached again from here or from a routine step.")
        self._btn_save_fov.clicked.connect(self.save_fov_requested)
        fovl.addWidget(self._btn_save_fov)
        self._btn_goto_fov = QPushButton("Go to FOV…")
        self._btn_goto_fov.clicked.connect(self._pick_and_goto_fov)
        fovl.addWidget(self._btn_goto_fov)
        root.addWidget(self._fovs)

        # STOP ALL lives OUTSIDE the motion group on purpose: the group gets
        # disabled during a frame re-establish (so no competing move can be
        # issued), and a Qt child of a disabled parent is unclickable no matter
        # what we set on it. The abort must stay live exactly then.
        self._btn_stop_all = QPushButton("STOP ALL")
        self._btn_stop_all.setStyleSheet(style.solid_btn("puffer"))
        self._btn_stop_all.clicked.connect(self._stop_all)
        root.addWidget(self._btn_stop_all)

        # ── Calibration, behind a door ──────────────────────────────────────
        self._lbl_frame = QLabel("—")
        self._lbl_frame.setWordWrap(True)
        self._lbl_frame.setStyleSheet("font-size: 10px;")
        root.addWidget(self._lbl_frame)

        self._btn_calibrate = QPushButton("Calibrate…")
        self._btn_calibrate.setToolTip(
            "Set the true zero and re-establish the coordinate frame. Rarely "
            "needed; one of these drives the stage into its hard limits.")
        self._btn_calibrate.clicked.connect(self._open_calibration)
        root.addWidget(self._btn_calibrate)

        # Esc = STOP ALL, anywhere in the app.
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc.activated.connect(self._stop_all)

        self._set_controls_enabled(False)
        self._update_frame_status()
        self._update_home_label()

    # ── binding to a live controller ────────────────────────────────────────
    def bind_controller(self, controller) -> None:
        """Attach the connected controller (session start) or None (stop)."""
        self._ctrl = controller
        # Home is session-scoped: starting or ending a session clears it.
        self._s.x.home_counts = self._s.y.home_counts = None
        self._set_controls_enabled(controller is not None)
        if controller is None:
            self._map.clear_position()
            if self._z_gauge is not None:
                self._z_gauge.clear_position()
            if self._cal_dialog is not None and not self._cal_dialog.running:
                self._cal_dialog.reject()
                self._cal_dialog = None
        self._update_frame_status()
        self._update_home_label()

    def _set_controls_enabled(self, on: bool) -> None:
        self._motion.setEnabled(on)      # Z rides along — it's a row in here now
        self._nav.setEnabled(on)
        self._fovs.setEnabled(on)
        self._btn_stop_all.setEnabled(on)
        self._btn_calibrate.setEnabled(on)

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

    def _on_calibration_changed(self) -> None:
        """Calibration was rewritten (or a frame run started/ended) — re-sync."""
        busy = self._cal_dialog is not None and self._cal_dialog.running
        # No competing motion while the stage is driving into a hard limit.
        connected = self._ctrl is not None and not busy
        self._motion.setEnabled(connected)
        self._nav.setEnabled(connected)
        self._fovs.setEnabled(connected)
        self._refresh_goto_ranges()
        self._update_frame_status()
        self._update_home_label()
        self._map.update()
        if self._z_gauge is not None:
            self._z_gauge.update()          # origin/soft-limits may have moved
        self.settings_changed.emit(self.settings)

    # ── frame status ────────────────────────────────────────────────────────
    def _update_frame_status(self) -> None:
        """Show whether absolute go-to can be trusted, and gate the buttons."""
        missing = [ax.name for ax in (self._s.x, self._s.y) if not ax.has_frame]
        ok = not missing
        if ok:  # absolute targets are meaningless without a frame; jog isn't
            self._lbl_frame.setText("Frame OK — absolute go-to calibrated.")
            self._lbl_frame.setStyleSheet("color: gray; font-size: 10px;")
        else:
            self._lbl_frame.setText(
                f"No valid frame for {', '.join(missing)} — absolute go-to "
                "disabled. Jog still works. Use Calibrate…")
            self._lbl_frame.setStyleSheet(f"color: {_BAD}; font-size: 10px;")
        self._btn_go_zero.setEnabled(ok)
        for key, w in self._axis_widgets.items():
            ax_ok = ok if key in ("x", "y") else self._axis(key).has_frame
            w["goto"].setEnabled(ax_ok)
            w["buttons"][2].setEnabled(ax_ok)   # the "Go" button
        self._update_home_label()

    def _axis(self, key: str):
        if key == "x":
            return self._s.x
        if key == "y":
            return self._s.y
        return self._s.z

    def _refresh_goto_ranges(self) -> None:
        """Re-range the go-to spin boxes after the soft limits move."""
        for key, w in self._axis_widgets.items():
            lo, hi = self._axis(key).soft_limits_um()
            w["goto"].setRange(lo, hi)

    # ── session home ────────────────────────────────────────────────────────
    def _update_home_label(self) -> None:
        hx, hy = self._s.x.home_um(), self._s.y.home_um()
        have = hx is not None and hy is not None
        if have:
            self._lbl_home.setText(f"home: {hx:.1f}, {hy:.1f} µm (this session only)")
        else:
            self._lbl_home.setText("home: not set")
        self._btn_go_home.setEnabled(have)
        self._btn_clear_home.setEnabled(have)

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

    def _set_home_here(self) -> None:
        if self._call("Could not set home", lambda c: c.set_home_here()):
            self._update_home_label()
            self._map.update()

    def _clear_home(self) -> None:
        # The label and map clear either way — the bookmark is ours, not the
        # controller's, so a failed round-trip must not strand the UI.
        self._call("Could not clear home", lambda c: c.clear_home())
        self._update_home_label()
        self._map.update()

    def _go_home(self) -> None:
        self._call("Move failed", lambda c: c.go_home())

    def _go_zero(self) -> None:
        self._call("Move failed", lambda c: c.go_to_center())

    # ── motion handlers ─────────────────────────────────────────────────────
    def _jog(self, key: str, direction: int) -> None:
        step = self._axis_widgets[key]["step"].value()
        self._call("Jog failed", lambda c: c.jog_um(key, direction * step))

    def _goto(self, key: str) -> None:
        if self._ctrl is None:
            return
        target = self._axis_widgets[key]["goto"].value()
        if key == "x":
            cur = self._last_xy[0]
        elif key == "y":
            cur = self._last_xy[1]
        else:
            cur = self._last_z
        thresh = self._s.confirm_move_z_um if key == "z" else self._s.confirm_move_um
        if abs(target - cur) > thresh:
            if QMessageBox.question(
                self, "Confirm move",
                f"Move {self._axis(key).name} from {cur:.0f} to {target:.0f} µm "
                f"({abs(target - cur):.0f} µm)?"
            ) != QMessageBox.StandardButton.Yes:
                return
        self._call("Move failed", lambda c: c.move_to_um(key, target))

    def _pick_and_goto_fov(self) -> None:
        from acqApp.devices.stage.fov_picker import FovPicker
        dlg = FovPicker(self)
        dlg.exec()
        if dlg.fov is not None:
            self.goto_fov(dlg.fov)

    def goto_fov(self, fov) -> None:
        """MOTION: absolute move to a saved FOV's X/Y, through the same
        confirm-before-a-large-move guard as a manual Go. Also moves Z when
        both the FOV and this rig have one — the FOV's Z goes through the
        same confirm distance check as X/Y even though it isn't part of the
        max() below, so a saved focus far from the current one doesn't sneak
        through silently because X/Y happened to be close."""
        if self._ctrl is None:
            return
        cur_x, cur_y = self._last_xy
        dist_xy = max(abs(fov.x_um - cur_x), abs(fov.y_um - cur_y))
        move_z = (self._s.has_z and fov.z_um is not None)
        dist_z = abs(fov.z_um - self._last_z) if move_z else 0.0
        big_xy = dist_xy > self._s.confirm_move_um
        big_z = move_z and dist_z > self._s.confirm_move_z_um
        if big_xy or big_z:
            msg = (f'Move to FOV "{fov.name}" at {fov.x_um:.0f}, '
                   f"{fov.y_um:.0f}")
            if move_z:
                msg += f", {fov.z_um:.0f}"
            parts = []
            if big_xy:
                parts.append(f"XY {dist_xy:.0f} µm")
            if big_z:
                parts.append(f"Z {dist_z:.0f} µm")
            msg += f" µm ({', '.join(parts)})?"
            if QMessageBox.question(
                self, "Confirm move", msg
            ) != QMessageBox.StandardButton.Yes:
                return

        def _go(c) -> None:
            c.move_to_um("x", fov.x_um)
            c.move_to_um("y", fov.y_um)
            if move_z:
                c.move_to_um("z", fov.z_um)

        if self._call("Move failed", _go):
            # "Active" until the stage drifts off it again — see set_readout.
            self._active_fov = fov

    # The panic path (Esc, app-wide), so guarded hardest: a dead link is exactly
    # when it's pressed, and an escaping slot exception aborts the process.
    def _stop(self, key: str) -> None:
        self._call("Stop failed", lambda c: c.stop(key))

    def _stop_all(self) -> None:
        self._call("STOP ALL failed", lambda c: c.stop_all())

    # ── live readout from the poll worker ───────────────────────────────────
    # Below this, a move isn't visually distinguishable on the map or the Z
    # gauge (a few mm of travel drawn into ~300 px) — both widgets' set_
    # position always repaints, so guard here like wheel.py's _axis/_title
    # do for the analogous reason: a stationary stage otherwise repaints
    # every poll tick.
    _MAP_EPS_UM = 0.5

    def _off_active_fov(self, x_um: float, y_um: float,
                       z_um: float | None) -> bool:
        """Has the live position drifted off `self._active_fov`'s spot? Same
        epsilon as the map/gauge repaint guard — noise below it isn't a
        move."""
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
        self._last_xy = (x_um, y_um)
        self._lbl_x.setText(f"{x_um:8.1f}")
        self._lbl_y.setText(f"{y_um:8.1f}")
        if z_um is not None and self._lbl_z is not None:
            self._last_z = z_um
            self._lbl_z.setText(f"{z_um:8.1f}")
            if (self._z_gauge is not None
                    and (self._last_gauge_z is None
                        or abs(z_um - self._last_gauge_z) >= self._MAP_EPS_UM)):
                self._last_gauge_z = z_um
                self._z_gauge.set_position(z_um)
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
        """The name of the FOV the stage is currently sitting at, or "" —
        see `_active_fov`/`_off_active_fov`."""
        return self._active_fov.name if self._active_fov is not None else ""

    @property
    def current_position(self) -> tuple[float, float, float | None]:
        """The most recently displayed X/Y(/Z) — a durable snapshot, not a
        one-shot read. Callers that need "where is the stage right now" on
        demand (Save FOV) must use this rather than the poll worker's own
        `get_latest()`, which hands back a value exactly once and is already
        drained every ~33 ms by the display tick — a second consumer racing
        it for the same single-use value loses almost every time, which is
        why FOVs silently failed to save with a fully connected stage."""
        return (*self._last_xy, self._last_z if self._s.has_z else None)

    @property
    def settings(self) -> StageSettings:
        s = self._s
        return StageSettings(
            port=self._cmb_port.currentText(),
            controller=s.controller,
            poll_hz=self._spn_rate.value(),
            confirm_move_um=s.confirm_move_um,
            confirm_move_z_um=s.confirm_move_z_um,
            margin_um=s.margin_um,
            invert_y=s.invert_y,
            frame_rotation_deg=self._spn_rotation.value(),
            x=s.x, y=s.y, z=s.z,
        )
