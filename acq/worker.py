"""
PullWorker — shared scaffolding for pull-based acquisition workers.

Each worker runs on its own QThread, keeps the newest sample for a ~30 Hz GUI
preview, and optionally feeds every sample to a recording sink. The snapshot,
the sink handle and stop()/wait() live here; a worker implements only _run().

Subclass contract:
    class FooWorker(PullWorker):
        some_signal = pyqtSignal(...)          # declare device-specific signals
        def run(self):
            ...
            self._publish(value)               # newest sample (+ feed the sink)
            # or, when the recorded payload differs from the preview value:
            self._publish(preview_value, record=payload)

The GUI calls get_latest() (returns the newest value once, else None) and
set_sink(fn) / set_sink(None). stop() flips the flag and joins the thread.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Iterator

from PyQt6.QtCore import QThread, pyqtSignal


def paced(period: float, t0: float) -> Iterator[int]:
    """Yield 1, 2, 3, ... at a fixed rate relative to `t0` (drift-free).

    Recomputes the ABSOLUTE target time for iteration n from the fixed start
    `t0`, rather than sleeping a flat `period` each time, so the loop
    free-runs at the true average rate with no cumulative drift from
    per-iteration overhead. Capture `t0` once, before the loop starts, and
    pass the SAME value here and into any of the caller's own `now - t0`
    timestamp math, so pacing and timestamps agree on the origin.

    Usage (replaces the old duplicated idiom `nxt = t0 + n * period; slp =
    nxt - time.perf_counter(); if slp > 0: time.sleep(slp)`):

        t0 = time.perf_counter()
        for n in paced(period, t0):
            if self._stop:
                break
            ...do one iteration's work...

    *** NOT YET VALIDATED ON REAL HARDWARE ***
    This is verified equivalent to the old per-callsite inline idiom by a
    deterministic replay test (byte-identical sleep durations and elapsed
    time vs. the old code, across randomized work/overrun patterns on a
    simulated clock) and by empirical jitter measurement on a dev machine —
    but it has NOT been exercised against actual hardware. Any caller pacing
    a REAL device poll/sample loop (as opposed to a mock or file-replay
    source) must be run on the physical rig — confirming cadence and that no
    reads/samples are dropped under real scheduling — before being trusted
    in an experiment.
    """
    n = 0
    while True:
        n += 1
        yield n
        slp = t0 + n * period - time.perf_counter()
        if slp > 0:
            time.sleep(slp)


class PullWorker(QThread):
    # "TypeName: message" if _run() raises. An exception escaping QThread.run()
    # makes PyQt6 qFatal() and take the WHOLE process down, so catch everything
    # here and surface it as a signal the GUI can show.
    error = pyqtSignal(str)

    _STOP_WAIT_MS = 3000        # subclasses may override

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        self._lock = threading.Lock()
        self._latest: Any = None
        self._sink: Callable[[Any], None] | None = None

    # ── thread entry (do not override — implement _run instead) ──────────────
    def run(self) -> None:
        self._stop = False
        try:
            self._run()
        except Exception as e:                       # noqa: BLE001 — last line of defence
            traceback.print_exc()
            self.error.emit(f"{type(e).__name__}: {e}")

    def _run(self) -> None:
        """Subclasses implement the acquisition loop here (not run())."""
        raise NotImplementedError

    # ── GUI side ────────────────────────────────────────────────────────────
    def get_latest(self) -> Any:
        with self._lock:
            v = self._latest
            self._latest = None
        return v

    def set_sink(self, sink: Callable[[Any], None] | None) -> None:
        """Attach (or clear) a per-sample recording sink.

        The store is atomic under the GIL, so a reader sees the old sink or the
        new one. It does **not** stop a worker already inside `sink(value)`:
        that call runs to completion and can land after the file closed, which
        is why `Recorder` counts those rather than dropping them silently
        (`late_count`). Detaching is not a barrier.
        """
        self._sink = sink

    # ── run()-side helpers ──────────────────────────────────────────────────
    def _emit_sink(self, value: Any) -> None:
        sink = self._sink       # snapshot: no None deref if set_sink races us
        if sink is not None:
            sink(value)

    def _set_latest(self, value: Any) -> None:
        with self._lock:
            self._latest = value

    def _publish(self, value: Any, record: Any = None) -> None:
        """Feed the sink (with `record` if given, else `value`) and update the
        newest-sample snapshot the GUI pulls."""
        self._emit_sink(value if record is None else record)
        self._set_latest(value)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop = True
        self.wait(self._STOP_WAIT_MS)
