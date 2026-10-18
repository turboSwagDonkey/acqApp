"""XY(Z) stage position poller. Reads only; the controller owns the
connection, shared with the GUI's motion controls.

    worker.get_latest()  -> (x_um, y_um[, z_um]) | None; z only with a Z stage
    worker.set_sink(fn)  -> record every sample
    worker.error         -> pyqtSignal(str)
"""
from __future__ import annotations
import time

from acqApp.acq.worker import PullWorker, paced


class StagePollWorker(PullWorker):
    def __init__(self, controller, poll_hz: float = 4.0):
        super().__init__()
        self._ctrl = controller
        self._hz   = max(0.5, poll_hz)
        self._has_z = controller.has_z      # fixed for the connection

    def _run(self) -> None:
        # paced() is replay-tested but not yet run on the physical stage:
        # confirm cadence / no missed reads there before trusting it.
        for _ in paced(1.0 / self._hz, time.perf_counter()):
            if self._stop:
                break
            try:
                xy = self._ctrl.read_xy_um()
                pos = (*xy, self._ctrl.read_z_um()) if self._has_z else xy
            except Exception as e:
                self.error.emit(f"stage: read failed ({e})")
                break
            self._publish(pos)
