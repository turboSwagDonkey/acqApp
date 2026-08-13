"""
The stage's UI: the settings panel and the calibration dialog.

Split from `settings.py`, which keeps the model and the persistence — the axis
calibration, soft limits and origin **shared with the standalone
`stage_control` app**. That half has no Qt in it and is the half worth reading
when the question is "where does 0,0 come from"; this half is widgets.

`CalibrationDialog` drives both axes into their reverse hard limits, so it runs
`establish_frame()` on a `_FrameWorker` thread — it can block for minutes.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.stage.map_widget import StageMap
from acqApp.stage.settings import (_BAD, _C_CUR, _C_HOME, _C_ORIGIN,
                                   _C_SOFT, StageSettings, config_path,
                                   load_settings)


class _FrameWorker(QThread):
    """Runs controller.establish_frame() off the GUI thread — it drives both axes
    into their reverse hard limits and can block for minutes."""
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._ctrl = controller

    def run(self) -> None:                      # noqa: D102 — see class docstring
        try:
            self._ctrl.establish_frame(progress=self.progress.emit)
        except Exception as e:                  # noqa: BLE001 — never kill the process
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.finished_ok.emit()


class CalibrationDialog(QDialog):
    """Everything that rewrites the saved calibration, kept off the main panel.

    These two actions are rare, are hard to undo, and one of them drives into the
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
            "around it. Does not move the stage — centre it first.")
        self._btn_set_zero.clicked.connect(self._set_zero_here)
        zl.addWidget(self._btn_set_zero)
        zl.addWidget(self._hint(
            "Centre the stage yourself first — this does not move it. "
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

        self._lbl_cfg = self._hint(f"config: {config_path()}")
        lay.addWidget(self._lbl_cfg)

        # This dialog is modal, so the panel's STOP ALL is unreachable while it
        # is up — and the frame re-establish drives into the hard limits from
        # right here. The abort has to exist on this window too.
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
        # Reported into the label, not a modal: the stage may be mid-move into a
        # hard limit, and an escaping exception here aborts the process.
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

    def _busy(self, on: bool) -> None:
        self._btn_set_zero.setEnabled(not on)
        self._btn_reframe.setEnabled(not on)
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
            "X and then Y will be driven into their REVERSE hard limits and then "
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
        self._worker = _FrameWorker(self._ctrl, self)
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

    def __init__(self, settings: StageSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or load_settings()
        self._ctrl = None                       # bound while a session is running
        self._last_xy = (0.0, 0.0)
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
        self._cmb_port.addItems([self._s.port, "COM54", "COM3", "COM4"])
        self._cmb_port.setCurrentText(self._s.port)
        lay.addRow("Port:", self._cmb_port)

        self._spn_rate = QDoubleSpinBox()
        self._spn_rate.setRange(0.5, 50.0)
        self._spn_rate.setSuffix(" Hz")
        self._spn_rate.setValue(self._s.poll_hz)
        lay.addRow("Poll rate:", self._spn_rate)

        # Port and poll rate are the two settings that live in the panel rather
        # than in the calibration file; without this they were never announced,
        # so nothing could persist them and the port reverted every launch.
        self._cmb_port.currentTextChanged.connect(self._emit_settings)
        self._spn_rate.valueChanged.connect(self._emit_settings)

        self._lbl_x = QLabel("—")
        self._lbl_y = QLabel("—")
        lay.addRow(f"{self._s.x.name} (µm):", self._lbl_x)
        lay.addRow(f"{self._s.y.name} (µm):", self._lbl_y)
        root.addWidget(grp)

        # ── Travel map ──────────────────────────────────────────────────────
        map_grp = QGroupBox("Position in travel")
        ml = QVBoxLayout(map_grp)
        ml.setContentsMargins(4, 4, 4, 4)
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

        for r, (key, ax) in enumerate((("x", self._s.x), ("y", self._s.y)), start=1):
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
            "Does not move the stage, and is not saved — it is cleared when the "
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
            if self._cal_dialog is not None and not self._cal_dialog.running:
                self._cal_dialog.reject()
                self._cal_dialog = None
        self._update_frame_status()
        self._update_home_label()

    def _set_controls_enabled(self, on: bool) -> None:
        self._motion.setEnabled(on)
        self._nav.setEnabled(on)
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
        self._motion.setEnabled(self._ctrl is not None and not busy)
        self._nav.setEnabled(self._ctrl is not None and not busy)
        self._refresh_goto_ranges()
        self._update_frame_status()
        self._update_home_label()
        self._map.update()
        self.settings_changed.emit(self.settings)

    # ── frame status ────────────────────────────────────────────────────────
    def _update_frame_status(self) -> None:
        """Show whether absolute go-to can be trusted, and gate the buttons."""
        missing = [ax.name for ax in (self._s.x, self._s.y) if not ax.has_frame]
        ok = not missing
        if ok:  # absolute targets are meaningless without a frame; jog is not
            self._lbl_frame.setText("Frame OK — absolute go-to calibrated.")
            self._lbl_frame.setStyleSheet("color: gray; font-size: 10px;")
        else:
            self._lbl_frame.setText(
                f"No valid frame for {', '.join(missing)} — absolute go-to "
                "disabled. Jog still works. Use Calibrate…")
            self._lbl_frame.setStyleSheet(f"color: {_BAD}; font-size: 10px;")
        self._btn_go_zero.setEnabled(ok)
        for w in self._axis_widgets.values():
            w["goto"].setEnabled(ok)
            w["buttons"][2].setEnabled(ok)      # the "Go" button
        self._update_home_label()

    def _axis(self, key: str):
        return self._s.x if key == "x" else self._s.y

    def _refresh_goto_ranges(self) -> None:
        """Re-range the go-to spin boxes after the soft limits move."""
        for key, w in self._axis_widgets.items():
            w["goto"].setRange(*self._axis(key).soft_limits_um())

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
        """Run `fn(controller)` if connected, reporting failure in one place.

        Every motion command has this shape: do nothing without a controller,
        and never let a serial error reach the GUI thread as a traceback.
        """
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
        if self._ctrl is not None:
            self._ctrl.clear_home()
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
        cur = self._last_xy[0] if key == "x" else self._last_xy[1]
        if abs(target - cur) > self._s.confirm_move_um:
            if QMessageBox.question(
                self, "Confirm move",
                f"Move {self._axis(key).name} from {cur:.0f} to {target:.0f} µm "
                f"({abs(target - cur):.0f} µm)?"
            ) != QMessageBox.StandardButton.Yes:
                return
        self._call("Move failed", lambda c: c.move_to_um(key, target))

    # Guarded like every other command, and for a sharper reason: this is the
    # panic path (Esc, app-wide). A dead serial link is exactly when it gets
    # pressed, and an exception escaping a slot aborts the process.
    def _stop(self, key: str) -> None:
        self._call("Stop failed", lambda c: c.stop(key))

    def _stop_all(self) -> None:
        self._call("STOP ALL failed", lambda c: c.stop_all())

    # ── live readout from the poll worker ───────────────────────────────────
    def set_readout(self, x_um: float, y_um: float) -> None:
        self._last_xy = (x_um, y_um)
        self._lbl_x.setText(f"{x_um:8.1f}")
        self._lbl_y.setText(f"{y_um:8.1f}")
        self._map.set_position(x_um, y_um)

    def _emit_settings(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    @property
    def settings(self) -> StageSettings:
        s = self._s
        return StageSettings(
            port=self._cmb_port.currentText(),
            poll_hz=self._spn_rate.value(),
            confirm_move_um=s.confirm_move_um,
            margin_um=s.margin_um,
            invert_y=s.invert_y,
            x=s.x, y=s.y,
        )
