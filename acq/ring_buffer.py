"""
Bounded ring buffer for acquisition frames.

Producer (acquisition thread) puts items; consumer (writer thread) gets them.
When full, oldest item is dropped and the drop counter is incremented — the GUI
can poll drop_count to show a warning. Thread-safe via a single Lock.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any


class RingBuffer:
    def __init__(self, maxlen: int) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._q: deque[Any] = deque()
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self.drop_count: int = 0

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------
    def put(self, item: Any) -> None:
        with self._lock:
            if len(self._q) >= self._maxlen:
                self._q.popleft()
                self.drop_count += 1
            self._q.append(item)
            self._not_empty.notify()

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------
    def get(self, timeout: float | None = None) -> Any:
        """Block until an item is available or timeout (raises queue.Empty)."""
        import queue
        with self._not_empty:
            if not self._q:
                self._not_empty.wait(timeout)
            if not self._q:
                raise queue.Empty
            return self._q.popleft()

    def get_nowait(self) -> Any:
        import queue
        with self._lock:
            if not self._q:
                raise queue.Empty
            return self._q.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)
