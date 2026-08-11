"""
XY stage — position-polling worker.

StagePollWorker polls a StageController (real or mock) for the X/Y position in
microns and reports it. It does NOT own the serial connection — the controller
does — so the same connection is shared with the GUI motion controls. The worker
never issues motion; it only reads.

Exposes (via acq.worker.PullWorker):
    worker.get_latest()  -> (x_um, y_um) | None
    worker.set_sink(fn)  -> record every sample
    worker.rate_update   -> pyqtSignal(float)   # samples / second
    worker.error         -> pyqtSignal(str)
"""
from __future__ import annotations
import time

from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker


class StagePollWorker(PullWorker):
    rate_update = pyqtSignal(float)      # `error` is inherited from PullWorker

    def __init__(self, controller, poll_hz: float = 4.0):
        super().__init__()
        self._ctrl = controller
        self._hz   = max(0.5, poll_hz)

    def _run(self) -> None:
        self._stop = False
        period = 1.0 / self._hz
        t0 = time.perf_counter()
        n = 0
        while not self._stop:
            try:
                xy = self._ctrl.read_xy_um()
            except Exception as e:
                self.error.emit(f"stage: read failed ({e})")
                break
            n += 1
            self._publish(xy)
            elapsed = time.perf_counter() - t0
            if n % max(1, int(self._hz)) == 0 and elapsed > 0:
                self.rate_update.emit(n / elapsed)
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
