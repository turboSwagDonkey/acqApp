"""Closed loop — the rule and what it watches. Pure, and no Qt.

`LoopRule` is the whole decision, as a function of (value, time): the semantics
live here, and `tests/test_closed_loop.py` drives this file directly.
`LoopSettings` is what persists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

COMPARISONS = ("above", "below")

# Outputs a rule can drive, key → label. The key is a module key, so firing is
# `sync.fire(key, duration)` and the module's `on_trigger` does the rest.
TARGETS: dict[str, str] = {"puffer": "Puffer", "dmd": "DMD"}

# A latency floor, not an experimental parameter, so not a panel setting —
# 200 Hz already outruns the wheel's 120 Hz, so faster only re-reads a sample.
POLL_HZ = 200.0


@dataclass(frozen=True)
class SignalSource:
    """A live scalar a rule can watch.

    `read()` -> `(value, acquired_at)`, or None while the signal isn't running
    (normal, not an error). `acquired_at` is the `perf_counter()` instant of
    ACQUISITION — `Recorder.put(at=)`'s domain — so a fire lands in the file at
    its cause, not at the GUI hop after. Called from the loop thread; must not
    consume.
    """
    key:   str                                       # stable id: config + file
    label: str                                       # what the combo shows
    units: str
    read:  Callable[[], tuple[float, float] | None]


@dataclass
class LoopSettings:
    """The rule, as persisted. All of this reaches `acqapp_local.json` and the
    session file — hence no `armed`: nothing can restore a rig into it."""
    source:       str   = ""          # SignalSource.key; "" = first on offer
    comparison:   str   = "above"     # one of COMPARISONS
    threshold:    float = 50.0        # in the source's units
    hold_s:       float = 0.25        # condition must hold this long to count
    refractory_s: float = 5.0         # minimum gap between two fires
    retrigger:    bool  = True        # False = the condition must clear first
    target:       str   = "puffer"    # a TARGETS key
    duration_s:   float = 0.100       # passed to the target
    max_fires:    int   = 0           # 0 = no limit


class LoopRule:
    """Should this fire, given the newest value? Pure, Qt-free, testable.

    One `update()` per sample, True on exactly the samples that should actuate.
    Each gate covers a way a bare threshold misbehaves on a real signal:

      `hold_s`       noise crosses a threshold many times a second
      `refractory_s` a condition that stays true would fire on every sample —
                     200 puffs a second
      `retrigger`    True: re-fire each refractory while it holds. False: one
                     fire per bout, the signal must fall back first
      `max_fires`    session ceiling: a wrong rule is wrong a bounded number of
                     times

    `update(None, t)` never fires — which matters for `below`, where a source
    that isn't running must not read as zero and satisfy it.
    """

    def __init__(self, settings: LoopSettings | None = None) -> None:
        self._s = settings or LoopSettings()
        self.n_fires = 0
        self.reset()

    def reset(self) -> None:
        """Forget everything, including the fire count. Per session."""
        self._since: float | None = None      # when the condition became true
        self._last_fire: float | None = None
        self._cleared = True                  # false since the last fire?
        self.n_fires = 0

    def configure(self, settings: LoopSettings) -> None:
        """Adopt new settings mid-session, keeping the fire history: nudging a
        threshold must not hand back a fresh `max_fires` budget or bypass the
        refractory window."""
        self._s = settings
        self._since = None

    def idle(self) -> None:
        """Called while disarmed. Drops the in-progress hold, so `hold_s` starts
        at arming and not from whenever the animal began running. Count and
        refractory survive."""
        self._since = None
        self._cleared = True

    @property
    def settings(self) -> LoopSettings:
        return self._s

    def satisfied(self, value: float | None) -> bool:
        """Is the condition true right now? Ignores every gate — the panel shows
        it, so a threshold can be set against a live animal while disarmed."""
        if value is None:
            return False
        return (value > self._s.threshold if self._s.comparison == "above"
                else value < self._s.threshold)

    def update(self, value: float | None, t: float) -> bool:
        s = self._s
        if not self.satisfied(value):
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


