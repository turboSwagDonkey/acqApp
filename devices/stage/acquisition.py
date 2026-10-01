"""XY(Z) stage position poller. Reads only; the controller owns the
connection, shared with the GUI's motion controls.

    worker.get_latest()  -> (x_um, y_um[, z_um]) | None; z only with a Z stage
    worker.set_sink(fn)  -> record every sample
    worker.rate_update   -> pyqtSignal(float)   # samples / second
    worker.error         -> pyqtSignal(str)
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
        self._has_z = controller.has_z      # fixed for the connection

    def _run(self) -> None:
        # paced() is replay-tested but not yet run on the physical stage:
        # confirm cadence / no missed reads there before trusting it.
        self._stop = False
        period = 1.0 / self._hz
        rate_every = max(1, int(self._hz))   # ~once/sec, fixed for the run
        t0 = time.perf_counter()
        for n in paced(period, t0):
            if self._stop:
                break
            try:
                xy = self._ctrl.read_xy_um()
                pos = (*xy, self._ctrl.read_z_um()) if self._has_z else xy
            except Exception as e:
                self.error.emit(f"stage: read failed ({e})")
                break
            self._publish(pos)
            if n % rate_every == 0:
                elapsed = time.perf_counter() - t0
                if elapsed > 0:
                    self.rate_update.emit(n / elapsed)
