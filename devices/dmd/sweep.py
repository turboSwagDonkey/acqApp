"""Running the calibration sweep against the real rig, and the dialog that does.

`calibration.py` is the pure half — it takes `project` and `grab` as callables
and never touches hardware. This is the other half: the two callables, and the
window that asks before emitting light.

**The whole difficulty is `grab`.** `ModuleHost.latest_frame` hands back the
frame the voltage camera last *displayed*, which is by definition older than the
pattern just projected. Decode forty planes from the frame before each and the
fit comes back confident and wrong — an rms of 0.4 px on garbage looks exactly
like an rms of 0.4 px on a registration. `FreshGrabber` is what makes the
project→grab pairing mean what it says.

Nothing here decides to actuate; `CalibrationDialog` asks, and the operator
answers (PLAN §2).
"""
from __future__ import annotations

import time
from typing import Callable

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout,
)

from acqApp import style

from acqApp.devices.dmd.calibration import (STRIPE_OFFSETS, CalibrationError,
                                            DmdCalibration, calibrate)


class SweepCancelled(CalibrationError):
    """The operator stopped the sweep. Not a failure — do not report it as one."""


class FreshGrabber:
    """`grab()` = the first camera frame that arrived AFTER the last `project()`.

    Identity, not a frame counter: the display tick replaces the array object
    once per frame, so `f is not last` is exactly "a new frame has been
    displayed" — and a counter would mean widening `ModuleHost` again, which
    §5b A4 makes a deliberate act rather than a convenience.

    `settle` frames are DISCARDED before the one that counts. The frame in
    flight when `project()` returns may have been exposed across the mirror
    flip, and one such frame per plane is a decode error on every pixel. Two is
    cheap insurance at ~100 ms a plane.

    **It fails the safe way round.** The adapter reassigns `_last_frame` only
    when the display tick actually consumed a new frame (`get_latest()` returns
    None otherwise), so identity cannot change without a real exposure. If a
    driver ever handed back one reused array object per frame, this would time
    out with the message below rather than quietly registering stale frames —
    a stopped sweep, not a wrong calibration.
    """

    def __init__(self, source: Callable[[], object], *, settle: int = 2,
                 timeout_s: float = 5.0,
                 pump: Callable[[], None] | None = None) -> None:
        self._source = source
        self._settle = max(0, int(settle))
        self._timeout = float(timeout_s)
        self._pump = pump or (lambda: None)
        self._last = source()          # prime, so the first grab waits for new
        self.n_grabs = 0
        self.waited_s = 0.0

    def grab(self) -> np.ndarray:
        t0 = time.monotonic()
        deadline = t0 + self._timeout
        seen = 0
        while True:
            self._pump()
            f = self._source()
            if f is not None and f is not self._last:
                self._last = f
                seen += 1
                if seen > self._settle:
                    self.n_grabs += 1
                    self.waited_s += time.monotonic() - t0
                    return np.asarray(f)
            if time.monotonic() > deadline:
                raise CalibrationError(
                    f"no new camera frame in {self._timeout:g} s. The voltage "
                    f"camera has to be RUNNING for the sweep to see anything — "
                    f"press Free run (or Record) and try again.")
            time.sleep(0.002)          # a bare spin starves the Qt thread


def sweep_exposures(_dmd_size: tuple[int, int] = (0, 0)) -> int:
    """One dark reference plus a stripe per offset per axis. A test pins this —
    it is the number the operator is shown before any light is emitted."""
    return 1 + 2 * len(STRIPE_OFFSETS)


class CalibrationDialog(QDialog):
    """Ask, then run the stripe sweep, then offer to save what it measured."""

    def __init__(self, projector, grab_source, *, parent=None,
                 real: bool = True, on_saved=None, set_live=None):
        super().__init__(parent)
        self._proj = projector
        self._source = grab_source
        self._on_saved = on_saved
        # Starts the camera itself rather than telling the operator to go and
        # press Live view in another part of the window. Restored afterwards to
        # whatever it was, so this leaves the rig as it found it.
        self._set_live = set_live
        self._calib: DmdCalibration | None = None
        self._cancel = False
        self._running = False
        self.setWindowTitle("DMD calibration")
        self.resize(760, 520)
        self._build(real)

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self, real: bool) -> None:
        w, h = self._proj.resolution
        n = sweep_exposures()
        root = QVBoxLayout(self)

        head = QLabel(
            f"<b>This projects light.</b> It steps a narrow stripe across the "
            f"DMD ({w}x{h}) at {n - 1} known offsets, images each with the "
            f"voltage camera, and fits a straight line to where they land — "
            f"giving the affine between camera pixels and mirrors, and so "
            f"which mirrors the camera can see.<br><br>"
            f"<b>{n} exposures.</b> Nothing narrower than 5 % of the panel: "
            f"this relay scatters enough to erase fine patterns, so coarse "
            f"ones are all that survive.<br><br>"
            f"The camera is started for the run and put back afterwards, and "
            f"the DMD does not need Display pressed — this drives both. "
            f"<b>dmdGUI_project must be closed</b>, though: one process owns "
            f"the USB.")
        head.setWordWrap(True)
        root.addWidget(head)

        if not real:
            warn = QLabel(
                "<b>Emulate is on.</b> The mock projector emits nothing, so no "
                "stripe will be visible and this will stop with that as the "
                "reason. It still exercises the whole path without light.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#c86;")
            root.addWidget(warn)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family:Consolas,monospace;")
        root.addWidget(self._log, 1)

        self._bar = QProgressBar()
        self._bar.setRange(0, n)
        # Explicitly 0: a QProgressBar starts at -1 and draws an empty strip
        # with no text, which reads as a broken widget rather than "not started".
        self._bar.setValue(0)
        self._bar.setFormat("%v / %m exposures")
        root.addWidget(self._bar)

        row = QHBoxLayout()
        self._btn_run = QPushButton(f"Calibrate ({n} exposures)")
        self._btn_run.setStyleSheet(style.solid_btn("dmd"))
        self._btn_run.clicked.connect(self._run)
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._request_cancel)
        self._btn_save = QPushButton("Save calibration…")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save)
        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.reject)
        for b in (self._btn_run, self._btn_stop, self._btn_save):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self._btn_close)
        root.addLayout(row)

    # ── logging ──────────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        self._log.appendPlainText(msg)
        QApplication.processEvents()

    def _request_cancel(self) -> None:
        self._cancel = True
        self.log("[sweep] stopping at the next exposure…")

    def _pump(self) -> None:
        """Keep the event loop turning while the sweep blocks on a frame.

        The camera delivers into the GUI thread, so a sweep that blocked it
        would wait forever for the frame it is blocking. Cancellation is checked
        here because this is the one place that runs on every exposure.
        """
        QApplication.processEvents()
        if self._cancel:
            raise SweepCancelled("stopped by the operator")

    # ── running ──────────────────────────────────────────────────────────────
    def _run(self) -> None:
        if self._running:
            return
        self._running = True
        self._cancel = False
        self._calib = None
        for b in (self._btn_run, self._btn_save, self._btn_close):
            b.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._log.clear()
        self._bar.setValue(0)
        w, h = self._proj.resolution

        # Start the camera if it is not already running; remember whether we
        # did, so the finally block can put it back.
        was_live = True
        if self._set_live is not None:
            was_live = bool(self._set_live(True))
            if not was_live:
                self.log("[sweep] started the live view for this run")

        # A longer timeout on the first grab: a camera that has just been told
        # to start has to build its worker and deliver a frame.
        grabber = FreshGrabber(self._source, timeout_s=12.0, pump=self._pump)

        def project(frame) -> None:
            self._proj.project_frame(frame)

        def grab():
            f = grabber.grab()
            self._bar.setValue(grabber.n_grabs)
            return f

        t0 = time.monotonic()
        try:
            self._calib = calibrate(project, grab, (w, h), log=self.log)
            self._btn_save.setEnabled(True)
        except SweepCancelled as e:
            self.log(f"[sweep] {e}")
        except CalibrationError as e:
            self.log(f"FAILED: {e}")
        except Exception as e:                      # noqa: BLE001
            self.log(f"FAILED ({type(e).__name__}): {e}")
        finally:
            # Always leave the panel dark: a run that ends holding its last
            # stripe is a projector still on the sample.
            try:
                self._proj.stop()
            except Exception as e:                  # noqa: BLE001
                self.log(f"[sweep] could not stop the projector: {e}")
            if self._set_live is not None and not was_live:
                self._set_live(False)
                self.log("[sweep] live view stopped again")
            self.log(f"[sweep] {grabber.n_grabs} exposures in "
                     f"{time.monotonic() - t0:.1f} s "
                     f"({1000 * grabber.waited_s / max(1, grabber.n_grabs):.0f} "
                     f"ms per frame waited)")
            self._running = False
            self._btn_stop.setEnabled(False)
            for b in (self._btn_run, self._btn_close):
                b.setEnabled(True)

    # ── result ───────────────────────────────────────────────────────────────
    @property
    def calibration(self) -> DmdCalibration | None:
        return self._calib

    def _save(self) -> None:
        from datetime import datetime
        from pathlib import Path

        from PyQt6.QtWidgets import QFileDialog

        if self._calib is None:
            return
        name = f"dmd_calib_{datetime.now():%Y%m%d_%H%M%S}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save DMD calibration", name, "Calibration (*.json)")
        if not path:
            return
        self._calib.save(path)
        self.log(f"[sweep] saved to {Path(path).name}")
        if self._on_saved is not None:
            self._on_saved(path)
        self.accept()
