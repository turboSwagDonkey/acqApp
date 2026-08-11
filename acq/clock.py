"""
SessionClock — monotonic wall-clock for a single acquisition session.

v1 uses time.perf_counter() (software). The NI PCIe-6363 timing engine can
replace this later by swapping in a DaqClock subclass; device code never
calls time.perf_counter() directly — it only calls clock.now().
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
        ...

    @abstractmethod
    def at(self, mono: float) -> float:
        """Seconds since session start for a `time.perf_counter()` reading.

        Devices that carry their own hardware timestamps (the camera, and later
        the DAQ) know when a sample was really acquired, which is not when it
        reached us. They convert that to the perf_counter domain and pass it
        here, so their samples land on the shared timebase at their true
        acquisition time rather than at their arrival time.
        """
        ...

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
