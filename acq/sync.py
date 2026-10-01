"""
Master synchronisation controller.

Owns the SessionClock, drives a QTimer for periodic ticks, and schedules named
trigger events (fire the puffer at t+5 s). Every device timestamps against this
clock via the Recorder, so all streams share one origin.

    sync = SyncController(SessionClock())
    sync.tick.connect(...); sync.trigger_fired.connect(puffer.fire)
    sync.schedule_trigger("puffer", delay_s=5.0, duration_s=0.2)
    sync.start_all()      # clock to t=0, tick timer on
    sync.stop_all()
"""

from __future__ import annotations
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from acqApp.acq.clock import AbstractClock, SessionClock

# Import this to convert ticks to seconds (vis_stim does); don't re-hardcode.
DEFAULT_TICK_MS = 100


@dataclass
class _TriggerSpec:
    name:       str
    delay_s:    float
    duration_s: float
    fired:      bool = False


class SyncController(QObject):
    """Central timing and trigger bus. Device workers are started and stopped by
    the owner (MainWindow) around start_all()/stop_all(), which bracket the
    clock."""

    tick           = pyqtSignal(float)      # elapsed seconds, fires every tick_ms
    trigger_fired  = pyqtSignal(str, float) # (trigger_name, duration_s)

    def __init__(self, clock: AbstractClock | None = None,
                 tick_ms: int = DEFAULT_TICK_MS, parent=None):
        super().__init__(parent)
        self._clock: AbstractClock = clock or SessionClock()
        self._triggers: list[_TriggerSpec] = []
        self._running  = False

        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._on_tick)

    # ── The shared clock ────────────────────────────────────────────────────────

    @property
    def clock(self) -> AbstractClock:
        return self._clock

    # ── Triggers ────────────────────────────────────────────────────────────────

    def schedule_trigger(self, name: str,
                         delay_s: float,
                         duration_s: float = 0.0) -> None:
        """Fire trigger `name` at `delay_s` after start_all(). `duration_s`
        rides along in the signal for devices that need it."""
        self._triggers.append(_TriggerSpec(name, delay_s, duration_s))

    def clear_triggers(self) -> None:
        self._triggers.clear()

    def fire(self, name: str, duration_s: float = 0.0) -> None:
        """Fire trigger `name` now, on the same bus as the scheduled ones, so
        a closed-loop puff is indistinguishable downstream (file included)."""
        self.trigger_fired.emit(name, duration_s)

    # ── Control ─────────────────────────────────────────────────────────────────

    def start_all(self) -> None:
        if self._running:
            return
        for t in self._triggers:
            t.fired = False
        self._clock.start()
        self._running = True
        self._timer.start()

    def stop_all(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._running = False
        self._clock.stop()

    # ── Properties ──────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def elapsed(self) -> float:
        return self._clock.now() if self._running else 0.0

    # ── Internal ────────────────────────────────────────────────────────────────

    def _on_tick(self) -> None:
        t = self.elapsed()
        self.tick.emit(t)
        for spec in self._triggers:
            if not spec.fired and t >= spec.delay_s:
                spec.fired = True
                self.trigger_fired.emit(spec.name, spec.duration_s)
