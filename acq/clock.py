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
    def stop(self) -> None: ...


class SessionClock(AbstractClock):
    """Software clock backed by time.perf_counter()."""

    def __init__(self) -> None:
        self._origin: float | None = None

    def start(self) -> None:
        self._origin = time.perf_counter()

    def now(self) -> float:
        if self._origin is None:
            raise RuntimeError("SessionClock.start() not called")
        return time.perf_counter() - self._origin

    def stop(self) -> None:
        self._origin = None
