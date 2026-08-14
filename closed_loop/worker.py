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

    `fired` is emitted from this thread; Qt queues it, so actuation happens on
    the GUI thread with every other one.

    Disarmed, the rule still evaluates and the panel still shows whether the
    condition is met — it just does not fire, which is what makes a threshold
    settable against a live animal without actuating anything.
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
        """Queue a settings change, applied before the next evaluation — queued
        because the panel edits on the GUI thread while `update()` runs here."""
        with self._cfg_lock:
            self._pending = settings

    @property
    def n_fires(self) -> int:
        """Fires this SESSION — the loop runs under Live view too, so this can
        exceed what is in the file."""
        return self._rule.n_fires

    @property
    def recorded_fires(self) -> int:
        """Fires that reached the sink, i.e. that are in `/closed_loop`.

        Separate from `n_fires` because they genuinely differ — the rule runs all
        session but the sink is attached only while recording. Filing `n_fires`
        would leave an attribute disagreeing with the stream beside it.
        """
        return self._recorded

    @property
    def source_key(self) -> str:
        return self._source.key

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop:
            now = time.perf_counter()

            with self._cfg_lock:
                pending, self._pending = self._pending, None
            if pending is not None:
                self._rule.configure(pending)

            sample = self._source.read()
            if sample is None:
                value, at = None, now
            else:
                value, at = float(sample[0]), float(sample[1])

            if self._armed:
                hit = self._rule.update(value, at)
            else:
                self._rule.idle()
                hit = False

            # Readout for the display tick, fired or not. Not via _publish():
            # the sink carries fires only, not a 200 Hz copy of the wheel.
            self._set_latest((value, self._rule.satisfied(value),
                              self._rule.n_fires, self._armed))

            if hit:
                s = self._rule.settings
                sink = self._sink           # snapshot: set_sink(None) is safe
                if sink is not None:
                    sink((value, at))
                    self._recorded += 1
                self.fired.emit(s.target, s.duration_s, float(value or 0.0))

            slp = self._period - (time.perf_counter() - now)
            if slp > 0:
                time.sleep(slp)


