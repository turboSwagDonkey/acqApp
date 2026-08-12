"""
Bounded ring buffer for acquisition samples.

Producer (acquisition thread) puts items; consumer (writer thread) gets them.
When full, the oldest item is dropped and drop_count is incremented — the GUI
can poll drop_count to show a warning. Thread-safe via a single Lock.

Two independent bounds:
  • maxlen   — item count (protects scalar-dominated streams).
  • maxbytes — total buffered payload bytes (protects against full-frame images
               ballooning RAM: a handful of 20 MB frames would blow past any
               sane count long before maxlen). Requires `sizeof` to measure an
               item's payload.

Under EITHER overload the buffer sheds the oldest *sized* item (an image frame)
in preference to a zero-byte one (a scalar/event sample). Frames are redundant
with the live preview and plentiful; a sparse stimulus/behaviour event is
critical to keep, so events survive a frame backlog. A zero-byte item is only
dropped once nothing sized remains. At least one item is always kept, so a
single item larger than maxbytes is buffered rather than dropped on arrival.
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
            # Count cap: shed frames first, same rule as the byte cap below.
            # Dropping the oldest item outright would discard exactly the
            # zero-byte event samples the byte cap exists to protect — and it
            # is the cap that bites first, since 512 items is about a second
            # of writer stall at full frame rate.
            while len(self._q) > self._maxlen and len(self._q) > 1:
                if self._evict_oldest_sized():
                    continue
                # Nothing sized left: the backlog really is events, so the
                # oldest one has to go.
                _old, osize = self._q.popleft()
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
