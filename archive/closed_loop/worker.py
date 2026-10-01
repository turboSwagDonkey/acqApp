"""Closed loop — the thread that samples a source and runs the rule."""
from __future__ import annotations

import threading
import time

from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker
from acqApp.closed_loop.settings import (POLL_HZ, LoopRule, LoopSettings,
                                         SignalSource)


class ClosedLoopWorker(PullWorker):
    """Samples one `SignalSource` and runs a `LoopRule` over it.

    `fired` is emitted here and queued, so actuation is on the GUI thread.
    Disarmed, the condition is still shown but never fires, so a threshold
    can be set against a live animal without actuating anything.
    """

    fired = pyqtSignal(str, float, float)      # (target, duration_s, value)

    _STOP_WAIT_MS = 2000

    def __init__(self, source: SignalSource,
                 settings: LoopSettings | None = None,
                 poll_hz: float = POLL_HZ) -> None:
        super().__init__()
        self._source = source
        self._rule = LoopRule(settings)
        self._period = 1.0 / max(1.0, poll_hz)
        self._armed = False
        self._recorded = 0
        self._cfg_lock = threading.Lock()
        self._pending: LoopSettings | None = None

    # ── GUI side ─────────────────────────────────────────────────────────────
    def set_armed(self, on: bool) -> None:
        self._armed = bool(on)

    @property
    def armed(self) -> bool:
        return self._armed

    def configure(self, settings: LoopSettings) -> None:
        """Queued: the panel edits on the GUI thread, `update()` runs here."""
        with self._cfg_lock:
            self._pending = settings

    @property
    def n_fires(self) -> int:
        """Fires this SESSION; Live view counts too, so it can exceed the
        file's."""
        return self._rule.n_fires

    @property
    def recorded_fires(self) -> int:
        """Fires handed to the sink (attached only while recording).
        `Recorder.put` can still shed one (counted in the file's `recorder_*`
        attributes), so this is not a guarantee of `len(/closed_loop)`."""
        return self._recorded

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop:
            now = time.perf_counter()

            with self._cfg_lock:
                pending, self._pending = self._pending, None
            if pending is not None:
                self._rule.configure(pending)

            sample = self._source.read()
            value, at = (None, now) if sample is None \
                else (float(sample[0]), float(sample[1]))

            if self._armed:
                hit = self._rule.update(value, at)
                ok = self._rule.last_satisfied
            else:
                self._rule.idle()
                hit = False
                ok = self._rule.satisfied(value)

            # Readout for the display tick, fired or not. Not via _publish():
            # the sink carries fires only, not a 200 Hz copy of the wheel.
            self._set_latest((value, ok, self._rule.n_fires, self._armed))

            if hit:
                s = self._rule.settings
                sink = self._sink       # set_sink may race
                if sink is not None:
                    sink((value, at))
                    self._recorded += 1
                self.fired.emit(s.target, s.duration_s, float(value or 0.0))

            slp = self._period - (time.perf_counter() - now)
            if slp > 0:
                time.sleep(slp)
