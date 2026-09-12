"""
XY stage — position-polling worker.

StagePollWorker polls a StageController (real or mock) for the X/Y position in
microns and reports it. It does NOT own the serial connection — the controller
does — so the same connection is shared with the GUI motion controls. The worker
never issues motion; it only reads.

Exposes (via acq.worker.PullWorker):
    worker.get_latest()  -> (x_um, y_um, z_counts) | None
    worker.set_sink(fn)  -> record every sample
    worker.rate_update   -> pyqtSignal(float)   # samples / second
    worker.error         -> pyqtSignal(str)

`z_counts` is None unless StageSettings.z_enabled — raw encoder counts, not
microns, since Z has no measured calibration (see devices/stage/settings.py).
"""
from __future__ import annotations
import time

from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker, paced


class StagePollWorker(PullWorker):
    rate_update = pyqtSignal(float)      # `error` is inherited from PullWorker

    def __init__(self, controller, poll_hz: float = 4.0):
        super().__init__()
        self._ctrl = controller
        self._hz   = max(0.5, poll_hz)

    def _run(self) -> None:
        # NOTE: pacing uses acq.worker.paced(), which paces this REAL device
        # poll loop. Verified equivalent to the old inline pacing idiom by
        # replay test + jitter measurement, but NOT yet run against the
        # physical stage — confirm poll cadence/no-missed-reads on real
        # hardware before trusting this in an experiment. See paced()'s
        # docstring.
        self._stop = False
        period = 1.0 / self._hz
        t0 = time.perf_counter()
        for n in paced(period, t0):
            if self._stop:
                break
            try:
                x, y = self._ctrl.read_xy_um()
                z = self._ctrl.read_z_counts()
            except Exception as e:
                self.error.emit(f"stage: read failed ({e})")
                break
            self._publish((x, y, z))
            elapsed = time.perf_counter() - t0
            if n % max(1, int(self._hz)) == 0 and elapsed > 0:
                self.rate_update.emit(n / elapsed)
