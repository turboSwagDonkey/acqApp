"""
Bounded ring buffer for acquisition samples.

Producer (acquisition thread) puts, consumer (writer thread) gets. On overflow
the oldest item goes and `drop_count` rises, which the GUI polls for a warning.

Two independent bounds: `maxlen` (item count) and `maxbytes` (buffered payload,
so a handful of 20 MB frames cannot balloon RAM inside any sane item count).

Under EITHER, the buffer sheds the oldest *sized* item — a frame — before a
zero-byte one. Frames are plentiful and redundant with the preview; a sparse
stimulus/behaviour event is not, so events survive a frame backlog. One item is
always kept, so an item larger than `maxbytes` is buffered rather than dropped.
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
            # Count cap: frames first, same rule as the byte cap. Dropping the
            # oldest outright would discard exactly the events the byte cap
            # protects — and this cap bites first, 512 items being about a
            # second of writer stall at full frame rate.
            while len(self._q) > self._maxlen and len(self._q) > 1:
                if self._evict_oldest_sized():
                    continue
                _old, osize = self._q.popleft()     # backlog really is events
                self._bytes -= osize
                self.drop_count += 1
            # Byte cap: shed the oldest *sized* item (a frame); leave scalar
            # events in place. Stops if nothing sized remains to drop.
            while (self._maxbytes is not None and self._bytes > self._maxbytes
                   and len(self._q) > 1 and self._evict_oldest_sized()):
                pass
            self._not_empty.notify()

    def _evict_oldest_sized(self) -> bool:
        """Remove the oldest item with a nonzero payload (an image frame).
        Returns False if every buffered item is zero-byte (nothing to shed)."""
        for i, (_it, sz) in enumerate(self._q):
            if sz > 0:
                self._q.rotate(-i)          # bring index i to the front
                _it2, sz2 = self._q.popleft()
                self._q.rotate(i)           # restore original order (minus it)
                self._bytes -= sz2
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
            if not self._q:
                raise queue.Empty
            item, size = self._q.popleft()
            self._bytes -= size
            return item

    def get_nowait(self) -> Any:
        with self._lock:
            if not self._q:
                raise queue.Empty
            item, size = self._q.popleft()
            self._bytes -= size
            return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)
