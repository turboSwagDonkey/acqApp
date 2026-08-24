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

from acqApp.devices.dmd.calibration import (CalibrationError, DmdCalibration,
                                            PROBE_FRACS, STRIPE_OFFSETS,
                                            centre_out_probe,
                                            coarse_calibration, gray_planes,
                                            probe_verdict, run_calibration)


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


def coarse_exposures(_dmd_size: tuple[int, int]) -> int:
    """One dark reference plus a stripe per offset per axis."""
    return 1 + 2 * len(STRIPE_OFFSETS)


def sweep_exposures(dmd_size: tuple[int, int], *, probe: bool = True,
                    full: bool = True) -> int:
    """An UPPER BOUND on the project→grab pairs a run will cost.

    A bound rather than a count, because the Gray step is measured at the rig
    (`resolve_gray_step`), and a coarser code needs fewer planes: the worst case
    is the finest code resolving immediately. Honest only if it tracks what
    `run_calibration` actually does, so a test pins it — the operator is shown
    this number before any light is emitted.
    """
    n = (1 + 1 + 2 * len(PROBE_FRACS)) if probe else 0     # dark, spot, x…, y…
    if full:
        # checkerboard pair + one 4-exposure resolution probe + the finest sweep
        n += 2 + 4 + 2 * len(gray_planes(*dmd_size)[0])
    return n


class CalibrationDialog(QDialog):
    """Ask, then run the sweep, then offer to save what it measured.

    Two runs, because they are two different decisions. **Probe** is twelve dim
    exposures that answer "do the fields overlap, at what scale and angle" and
    produce no calibration — the right first thing to do at a rig nobody has
    registered yet. **Full** is that plus 42 more (2 checkerboard, 2x20 Gray
    planes on a 1024x768 panel) and a saved transform.
    """

    def __init__(self, projector, grab_source, *, parent=None,
                 real: bool = True, on_saved=None):
        super().__init__(parent)
        self._proj = projector
        self._source = grab_source
        self._on_saved = on_saved
        self._calib: DmdCalibration | None = None
        self._cancel = False
        self._running = False
        self.setWindowTitle("DMD calibration sweep")
        self.resize(760, 520)
        self._build(real)

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self, real: bool) -> None:
        w, h = self._proj.resolution
        n_probe = sweep_exposures((w, h), full=False)
        n_coarse = coarse_exposures((w, h))
        n_full = sweep_exposures((w, h))
        root = QVBoxLayout(self)

        head = QLabel(
            f"<b>This projects light.</b> The sweep drives the DMD "
            f"({w}x{h}) through a set of patterns and images each one with the "
            f"voltage camera.<ul>"
            f"<li><b>Probe</b> — {n_probe} exposures, none larger than the "
            f"panel: a centre spot, then a bar grown along each axis. Measures "
            f"where the DMD lands, its scale and its rotation. Saves nothing."
            f"</li>"
            f"<li><b>Coarse</b> — {n_coarse}. Steps a narrow stripe across "
            f"each axis and fits a straight line to where it lands. "
            f"<b>Nothing narrower than {100 * 0.05:.0f} % of the panel, so it "
            f"works on a relay that cannot resolve single mirrors</b>, and a "
            f"stripe that falls off the frame is dropped rather than biasing "
            f"the fit. No keystone term.</li>"
            f"<li><b>Full calibration</b> — up to {n_full}, including "
            f"full-panel checkerboards and Gray-coded stripes. Adds keystone "
            f"and a residual over thousands of points — when the optics resolve "
            f"the stripes.</li>"
            f"</ul>"
            f"Before any: the voltage camera must be running, and "
            f"<b>dmdGUI_project must be closed</b> — one process owns the USB.")
        head.setWordWrap(True)
        root.addWidget(head)

        if not real:
            warn = QLabel(
                "<b>Emulate is on.</b> The mock projector emits nothing, so "
                "the camera will see no modulation and the sweep will stop at "
                "the probe. That exercises the whole path without light, which "
                "is a useful thing to do — but it cannot register anything.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#c86;")
            root.addWidget(warn)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family:Consolas,monospace;")
        root.addWidget(self._log, 1)

        self._bar = QProgressBar()
        self._bar.setRange(0, n_full)
        # Explicitly 0: a QProgressBar starts at -1 and draws an empty strip
        # with no text, which reads as a broken widget rather than "not started".
        self._bar.setValue(0)
        self._bar.setFormat("%v / %m exposures")
        root.addWidget(self._bar)

        row = QHBoxLayout()
        self._btn_probe = QPushButton(f"Probe ({n_probe})")
        self._btn_probe.clicked.connect(lambda: self._run(mode="probe"))
        self._btn_coarse = QPushButton(f"Coarse ({n_coarse})")
        self._btn_coarse.setStyleSheet(style.solid_btn("dmd"))
        self._btn_coarse.setToolTip(
            "Rectangles only. Use this when the full sweep cannot decode — a "
            "relay that does not resolve single mirrors makes the Gray stripes "
            "useless while leaving the bars perfectly measurable.")
        self._btn_coarse.clicked.connect(lambda: self._run(mode="coarse"))
        self._btn_full = QPushButton(f"Full (up to {n_full})")
        self._btn_full.clicked.connect(lambda: self._run(mode="full"))
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._request_cancel)
        self._btn_save = QPushButton("Save calibration…")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save)
        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.reject)
        for b in (self._btn_probe, self._btn_coarse, self._btn_full,
                  self._btn_stop, self._btn_save):
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
    def _run(self, *, mode: str) -> None:
        if self._running:
            return
        self._running = True
        self._cancel = False
        self._calib = None
        for b in (self._btn_probe, self._btn_coarse, self._btn_full,
                  self._btn_save, self._btn_close):
            b.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._log.clear()
        w, h = self._proj.resolution
        self._bar.setRange(0, coarse_exposures((w, h)) if mode == "coarse"
                           else sweep_exposures((w, h), full=mode == "full"))
        self._bar.setValue(0)

        grabber = FreshGrabber(self._source, pump=self._pump)

        def project(frame) -> None:
            self._proj.project_frame(frame)

        def grab():
            f = grabber.grab()
            self._bar.setValue(grabber.n_grabs)
            return f

        t0 = time.monotonic()
        try:
            if mode == "full":
                self._calib = run_calibration(project, grab, (w, h),
                                              log=self.log)
            elif mode == "coarse":
                self._calib = coarse_calibration(project, grab, (w, h),
                                                 log=self.log)
            else:
                steps = centre_out_probe(project, grab, (w, h), log=self.log)
                self.log("")
                self.log(probe_verdict(steps, steps[0].cam_shape, (w, h)))
            if self._calib is not None:
                self.log("")
                self.log(self._calib.describe())
                self._btn_save.setEnabled(True)
        except SweepCancelled as e:
            self.log(f"\n[sweep] {e}")
        except CalibrationError as e:
            self.log(f"\nFAILED: {e}")
        except Exception as e:                      # noqa: BLE001
            self.log(f"\nFAILED ({type(e).__name__}): {e}")
        finally:
            # Always leave the panel dark: a sweep that ends holding its last
            # Gray plane is a projector still on the sample.
            try:
                self._proj.stop()
            except Exception as e:                  # noqa: BLE001
                self.log(f"[sweep] could not stop the projector: {e}")
            self.log(f"[sweep] {grabber.n_grabs} exposures in "
                     f"{time.monotonic() - t0:.1f} s "
                     f"({1000 * grabber.waited_s / max(1, grabber.n_grabs):.0f} "
                     f"ms per frame waited)")
            self._running = False
            self._btn_stop.setEnabled(False)
            for b in (self._btn_probe, self._btn_coarse, self._btn_full,
                      self._btn_close):
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
