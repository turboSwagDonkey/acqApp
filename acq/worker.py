"""
PullWorker — shared scaffolding for pull-based acquisition workers.

Every device worker (camera, encoder, pupil cam, stage poller) runs on its own
QThread, keeps only the newest sample for a ~30 Hz GUI preview, and optionally
feeds every sample to a recording sink. That boilerplate — the lock-guarded
latest-value snapshot, the thread-safe sink handle, and the stop()/wait() — lives
here so each worker only has to implement run() and declare its own rate signal.

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
import traceback
from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal


class PullWorker(QThread):
    # Emitted (with "TypeName: message") if _run() raises. A Python exception
    # that escapes QThread.run() makes PyQt6 call qFatal()/abort() and take the
    # WHOLE process down — so we catch everything here and surface it as a signal
    # the GUI can show, instead of the app vanishing.
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
        """Attach (or clear) a per-sample recording sink. Thread-safe."""
        self._sink = sink

    # ── run()-side helpers ──────────────────────────────────────────────────
    def _emit_sink(self, value: Any) -> None:
        sink = self._sink                       # snapshot: set_sink(None) is safe
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
