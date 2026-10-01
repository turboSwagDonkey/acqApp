"""SessionClock: seconds since session start, on time.perf_counter().

Device code only calls clock.now()/at(), so a DAQ-backed clock can replace
it without touching them.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod


class AbstractClock(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def now(self) -> float:
        """Seconds since session start."""

    @abstractmethod
    def at(self, mono: float) -> float:
        """Seconds since session start for a `time.perf_counter()` reading:
        how a hardware-timestamped sample lands at its acquisition time, not
        its arrival."""

    @abstractmethod
    def stop(self) -> None: ...


class SessionClock(AbstractClock):
    """Software clock backed by time.perf_counter()."""

    def __init__(self) -> None:
        self._origin: float | None = None

    def start(self) -> None:
        self._origin = time.perf_counter()

    def now(self) -> float:
        return self.at(time.perf_counter())

    def at(self, mono: float) -> float:
        if self._origin is None:
            raise RuntimeError("SessionClock.start() not called")
        return mono - self._origin

    def stop(self) -> None:
        self._origin = None
