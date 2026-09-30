"""PullWorker — a QThread that keeps the newest sample for the GUI preview and
feeds every sample to an optional recording sink. Subclasses implement
`_run()` and call `_publish(value[, record=payload])`."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Iterator

from PyQt6.QtCore import QThread, pyqtSignal


def paced(period: float, t0: float) -> Iterator[int]:
    """Yield 1, 2, 3, ... at absolute targets t0 + n*period, so per-iteration
    overhead never accumulates as drift. Use the same `t0` for timestamps."""
    n = 0
    while True:
        n += 1
        yield n
        slp = t0 + n * period - time.perf_counter()
        if slp > 0:
            time.sleep(slp)


class PullWorker(QThread):
    # An exception escaping QThread.run() makes PyQt6 abort the whole process,
    # so run() catches everything and reports it here.
    error = pyqtSignal(str)

    _STOP_WAIT_MS = 3000

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        self._lock = threading.Lock()
        self._latest: Any = None
        self._sink: Callable[[Any], None] | None = None

    # ── thread entry (implement _run, not run) ────────────────────────────────
    def run(self) -> None:
        self._stop = False
        try:
            self._run()
        except Exception as e:                       # noqa: BLE001 — last line of defence
            traceback.print_exc()
            self.error.emit(f"{type(e).__name__}: {e}")

    def _run(self) -> None:
        raise NotImplementedError

    # ── GUI side ────────────────────────────────────────────────────────────
    def get_latest(self) -> Any:
        """The newest value, once; None until the next one."""
        with self._lock:
            v = self._latest
            self._latest = None
        return v

    def set_sink(self, sink: Callable[[Any], None] | None) -> None:
        """Not a barrier: a call already inside the old sink finishes, which is
        why `Recorder` counts late samples."""
        self._sink = sink

    # ── run()-side helpers ──────────────────────────────────────────────────
    def _emit_sink(self, value: Any) -> None:
        sink = self._sink       # snapshot against a racing set_sink
        if sink is not None:
            sink(value)

    def _set_latest(self, value: Any) -> None:
        with self._lock:
            self._latest = value

    def _publish(self, value: Any, record: Any = None) -> None:
        """Sink `record` (or `value`), and keep `value` for the preview."""
        self._emit_sink(value if record is None else record)
        self._set_latest(value)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop = True
        self.wait(self._STOP_WAIT_MS)
