"""Closed loop — the rule and what it watches. Pure, no Qt."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

COMPARISONS = ("above", "below")

# Module keys, so firing is `sync.fire(key, duration)`.
TARGETS: dict[str, str] = {"puffer": "Puffer", "dmd": "DMD"}

# Already outruns the wheel's 120 Hz; faster only re-reads a sample.
POLL_HZ = 200.0


@dataclass(frozen=True)
class SignalSource:
    """A live scalar a rule can watch. `read()` -> (value, acquired_at) or
    None while not running; must not consume. `acquired_at` is perf_counter
    at acquisition, so a fire is filed at its cause."""
    key:   str
    label: str
    units: str
    read:  Callable[[], tuple[float, float] | None]


@dataclass
class LoopSettings:
    """No `armed` field: nothing may restore a rig into it."""
    source:       str   = ""          # SignalSource.key; "" = first on offer
    comparison:   str   = "above"
    threshold:    float = 50.0        # source units
    hold_s:       float = 0.25        # must hold this long
    refractory_s: float = 5.0         # minimum gap between fires
    retrigger:    bool  = True        # False = the condition must clear first
    target:       str   = "puffer"
    duration_s:   float = 0.100
    max_fires:    int   = 0           # 0 = no limit


class LoopRule:
    """`update()` per sample, True on exactly the samples that should fire.

      hold_s       noise crosses a threshold many times a second
      refractory_s a true condition would otherwise fire every sample
      retrigger    False: one fire per bout
      max_fires    a wrong rule is wrong a bounded number of times

    `update(None, t)` never fires, so a stopped source can't satisfy `below`.
    """

    def __init__(self, settings: LoopSettings | None = None) -> None:
        self._s = settings or LoopSettings()
        self.n_fires = 0
        self.reset()

    def reset(self) -> None:
        """Forget everything, including the count. Per session."""
        self._since: float | None = None
        self._last_fire: float | None = None
        self._cleared = True
        self.n_fires = 0
        self.last_satisfied = False     # from the last update(), for readouts

    def configure(self, settings: LoopSettings) -> None:
        """Keeps the fire history: a nudged threshold mustn't reset the budget
        or the refractory window."""
        self._s = settings
        self._since = None

    def idle(self) -> None:
        """While disarmed: the hold restarts at arming."""
        self._since = None
        self._cleared = True

    @property
    def settings(self) -> LoopSettings:
        return self._s

    def satisfied(self, value: float | None) -> bool:
        """The bare condition, no gates (shown live while disarmed)."""
        if value is None:
            return False
        return (value > self._s.threshold if self._s.comparison == "above"
                else value < self._s.threshold)

    def update(self, value: float | None, t: float) -> bool:
        s = self._s
        self.last_satisfied = self.satisfied(value)
        if not self.last_satisfied:
            self._since = None
            self._cleared = True
            return False
        if self._since is None:
            self._since = t
        if t - self._since < s.hold_s:
            return False
        if not s.retrigger and not self._cleared:
            return False
        if self._last_fire is not None and t - self._last_fire < s.refractory_s:
            return False
        if s.max_fires and self.n_fires >= s.max_fires:
            return False
        self._last_fire = t
        self._cleared = False
        self.n_fires += 1
        return True
