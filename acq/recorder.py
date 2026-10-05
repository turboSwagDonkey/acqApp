"""Recorder — one writer thread draining a shared ring buffer to disk.

Workers call `put()`, which stamps the sample on the session clock and
enqueues it; no acquisition thread touches disk.

    rec = Recorder(clock, SessionWriter(), RingBuffer(512))
    rec.start(path, metadata)
    rec.put("wheel", voltage)
    rec.stop()
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Callable

from .clock import AbstractClock
from .ring_buffer import RingBuffer
from .writer import Writer


class Recorder:
    def __init__(
        self,
        clock: AbstractClock,
        writer: Writer,
        ring_buffer: RingBuffer,
    ) -> None:
        self._clock = clock
        self._writer = writer
        self._buf = ring_buffer
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # A worker already inside its sink can put() after close; counted.
        self._gate = threading.Lock()
        self._closed = False
        self._late = 0              # after the file closed
        self._unstamped = 0         # before the session clock started
        self._write_errors = 0      # samples the writer raised on (disk full)
        self._last_write_error = ""
        self._offered: dict[str, int] = {}

    def start(self, path: Path, metadata: dict[str, Any]) -> None:
        self._stop_event.clear()
        self._writer.open(path, metadata)
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="Recorder")
        self._thread.start()

    def put(self, stream: str, data: Any, at: float | None = None) -> None:
        """Enqueue one sample. `at` is a perf_counter() reading of when it was
        ACQUIRED, for batched devices (stamping on arrival would quantise
        them to the read cadence)."""
        try:
            ts = self._clock.now() if at is None else self._clock.at(at)
        except RuntimeError:
            with self._gate:
                self._unstamped += 1
            return
        with self._gate:
            if self._closed:
                self._late += 1
                return
            self._buf.put((stream, ts, data))
            self._offered[stream] = self._offered.get(stream, 0) + 1

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self._writer.update_metadata(metadata)

    def stop(self, drain_timeout: float = 30.0,
             final_metadata: Callable[[], dict[str, Any]] | None = None) -> int:
        """Drain and close; returns samples left un-drained (0 = clean).
        `final_metadata()` runs between the drain and the close."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout)
            self._thread = None
        # Close the gate first, so a straggler is counted either way.
        with self._gate:
            self._closed = True
            remaining = len(self._buf)
        if final_metadata is not None:
            self._writer.update_metadata(final_metadata())
        self._writer.close()
        return remaining

    def offered(self, stream: str) -> int:
        """Samples of `stream` handed to this file (the ring may still shed).

        Read without the gate on purpose: the routine polls this ~70x/s, and
        locking stalled the GUI up to 29 ms behind a busy producer."""
        return self._offered.get(stream, 0)

    @property
    def drop_count(self) -> int:
        """Shed by the ring because the writer fell behind."""
        return self._buf.drop_count

    @property
    def late_count(self) -> int:
        return self._late

    @property
    def unstamped_count(self) -> int:
        return self._unstamped

    @property
    def write_error_count(self) -> int:
        """Samples the writer failed on and the loop moved past."""
        return self._write_errors

    @property
    def last_write_error(self) -> str:
        return self._last_write_error

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or len(self._buf):
            try:
                stream, ts, data = self._buf.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._writer.write(stream, ts, data)
            except Exception as e:                   # noqa: BLE001
                # An escape would end this thread, and the ring would then shed
                # every later sample with nothing said.
                self._write_errors += 1
                msg = f"{type(e).__name__}: {e}"
                if msg != self._last_write_error:
                    print(f"[recorder] write failed ({msg}); data is being "
                          f"lost")
                self._last_write_error = msg
