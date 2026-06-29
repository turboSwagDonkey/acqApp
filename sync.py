"""
Master synchronisation controller.

Owns a wall-clock QTimer, lets subsystems register start/stop callables,
and can schedule named trigger events (e.g. fire the puffer at t+5 s).

Usage
-----
    sync = SyncController()
    sync.register("voltage_cam", cam_acq.start, cam_acq.stop)
    sync.register("wheel",       enc_acq.start, enc_acq.stop)
    sync.register("puffer",      None,          None)          # trigger-only

    sync.all_started.connect(on_run_begin)
    sync.tick.connect(lambda t: status_bar.showMessage(f"{t:.1f} s"))
    sync.trigger_fired.connect(puffer.fire)

    sync.schedule_trigger("puffer", delay_s=5.0, duration_s=0.2)
    sync.start_all()
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable

from PyQt5.QtCore import QObject, QTimer, pyqtSignal


@dataclass
class _TriggerSpec:
    name:       str
    delay_s:    float
    duration_s: float
    fired:      bool = False


@dataclass
class _Device:
    name:     str
    start_fn: Callable | None
    stop_fn:  Callable | None


class SyncController(QObject):
    """Central timing and trigger bus for all acquisition devices."""

    all_started    = pyqtSignal()           # emitted once all devices have started
    all_stopped    = pyqtSignal()           # emitted once all devices have stopped
    tick           = pyqtSignal(float)      # elapsed seconds, fires every tick_ms
    trigger_fired  = pyqtSignal(str, float) # (trigger_name, elapsed_s)

    def __init__(self, tick_ms: int = 100, parent=None):
        super().__init__(parent)
        self._devices:  list[_Device]       = []
        self._triggers: list[_TriggerSpec]  = []
        self._running   = False
        self._t0:       float               = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._on_tick)

    # ── Registration ───────────────────────────────────────────────────────────

    def register(self, name: str,
                 start_fn: Callable | None,
                 stop_fn:  Callable | None) -> None:
        """Register a device's start/stop callables with the controller."""
        self._devices.append(_Device(name, start_fn, stop_fn))

    def schedule_trigger(self, name: str,
                         delay_s: float,
                         duration_s: float = 0.0) -> None:
        """
        Fire trigger `name` at `delay_s` seconds after start_all().
        `duration_s` is passed along in the signal for devices that need it
        (e.g. the puffer needs to know how long to stay open).
        """
        self._triggers.append(_TriggerSpec(name, delay_s, duration_s))

    def clear_triggers(self) -> None:
        self._triggers.clear()

    # ── Control ────────────────────────────────────────────────────────────────

    def start_all(self) -> None:
        if self._running:
            return
        for t in self._triggers:
            t.fired = False
        self._t0 = time.perf_counter()
        for dev in self._devices:
            if dev.start_fn is not None:
                dev.start_fn()
        self._running = True
        self._timer.start()
        self.all_started.emit()

    def stop_all(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._running = False
        for dev in self._devices:
            if dev.stop_fn is not None:
                dev.stop_fn()
        self.all_stopped.emit()

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0 if self._running else 0.0

    # ── Internal ───────────────────────────────────────────────────────────────

    def _on_tick(self) -> None:
        t = self.elapsed()
        self.tick.emit(t)
        for spec in self._triggers:
            if not spec.fired and t >= spec.delay_s:
                spec.fired = True
                self.trigger_fired.emit(spec.name, spec.duration_s)
