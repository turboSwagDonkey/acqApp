"""
XY(Z) stage — position-polling worker.

StagePollWorker polls a StageController (real or mock) for its position in
microns and reports it. It does NOT own the serial connection — the controller
does — so the same connection is shared with the GUI motion controls. The worker
never issues motion; it only reads.

Exposes (via acq.worker.PullWorker):
    worker.get_latest()  -> (x_um, y_um) | (x_um, y_um, z_um) | None — the
                            3-tuple only on a rig with a Z stage (controller.
                            settings.has_z); every existing caller already
                            indexes [0]/[1], so this is additive.
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
        # Read once at construction, not per-tick: has_z is fixed for the
        # life of a session (set at connect() time from the loaded config).
        self._has_z = controller.has_z

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
                xy = self._ctrl.read_xy_um()
                pos = (*xy, self._ctrl.read_z_um()) if self._has_z else xy
            except Exception as e:
                self.error.emit(f"stage: read failed ({e})")
                break
            self._publish(pos)
            elapsed = time.perf_counter() - t0
            if n % max(1, int(self._hz)) == 0 and elapsed > 0:
                self.rate_update.emit(n / elapsed)
