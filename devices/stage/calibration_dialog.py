"""The stage calibration dialog: re-establishing the X/Y frame and Z's
zero. Rare, hard to undo, and kept off the main panel."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout,
)

from acqApp import style
from acqApp.acq.worker import PullWorker
from acqApp.devices.stage.settings import _BAD, StageSettings, config_path


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
