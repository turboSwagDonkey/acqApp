"""
Bounded ring buffer for acquisition samples.

Producer (acquisition thread) puts, consumer (writer thread) gets. On overflow
the oldest goes and `drop_count` rises, which the GUI polls for a warning.

Two bounds: `maxlen` (items) and `maxbytes` (payload, so a handful of 20 MB
frames can't balloon RAM inside any sane item count).

Under either, it sheds the oldest *sized* item — a frame — before a zero-byte
one: frames are plentiful and redundant with the preview, a sparse
stimulus/behaviour event isn't. One item is always kept, so an item larger
than `maxbytes` is buffered rather than dropped.
"""
from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Callable


class RingBuffer:
    def __init__(self, maxlen: int, maxbytes: int | None = None,
                 sizeof: Callable[[Any], int] | None = None) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._q: deque[tuple[Any, int]] = deque()   # (item, payload_bytes)
        self._maxlen = maxlen
        self._maxbytes = maxbytes
        self._sizeof = sizeof or (lambda _item: 0)
        self._bytes = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self.drop_count: int = 0

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------
    def put(self, item: Any) -> None:
        size = self._sizeof(item)
        with self._lock:
            self._q.append((item, size))
            self._bytes += size
            # Count cap: frames first too, or it would discard the events the
            # byte cap protects. It bites first: 512 items is about a second
            # of writer stall at full frame rate.
            while len(self._q) > self._maxlen and len(self._q) > 1:
                if self._evict_oldest_sized():
                    continue
                _old, osize = self._q.popleft()     # backlog really is events
                self._bytes -= osize
                self.drop_count += 1
            while (self._maxbytes is not None and self._bytes > self._maxbytes
                   and len(self._q) > 1 and self._evict_oldest_sized()):
                pass
            self._not_empty.notify()

    def _evict_oldest_sized(self) -> bool:
        """Drop the oldest frame (nonzero payload); False if there is none."""
        for i, (_it, sz) in enumerate(self._q):
            if sz > 0:
                del self._q[i]
                self._bytes -= sz
                self.drop_count += 1
                return True
        return False

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------
    def get(self, timeout: float | None = None) -> Any:
        """Block until an item is available or timeout (raises queue.Empty)."""
        with self._not_empty:
            if not self._q:
                self._not_empty.wait(timeout)
            return self._pop()

    def get_nowait(self) -> Any:
        with self._lock:
            return self._pop()

    def _pop(self) -> Any:
        """Caller holds the lock."""
        if not self._q:
            raise queue.Empty
        item, size = self._q.popleft()
        self._bytes -= size
        return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)
