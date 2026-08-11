"""
Recorder — one background writer thread draining a shared ring buffer to disk.

All device workers call recorder.put(stream, data) from their acquisition
threads. put() stamps the sample with the single session-wide SessionClock at
acquisition time (NOT at write time), then enqueues it. A dedicated thread
drains the ring buffer into the Writer, so no acquisition thread ever touches
disk I/O.

Usage:
    rec = Recorder(clock, HDF5Writer(), RingBuffer(512))
    rec.start(path, metadata)
    # from each worker thread:
    rec.put("voltage_cam", frame)
    rec.put("wheel", voltage)
    rec.stop()
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, path: Path, metadata: dict[str, Any]) -> None:
        self._stop_event.clear()
        self._writer.open(path, metadata)
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="Recorder")
        self._thread.start()

    def put(self, stream: str, data: Any, at: float | None = None) -> None:
        """Enqueue one sample, stamped on the shared clock at acquisition time.

        `at` is a `time.perf_counter()` reading of when the sample was actually
        ACQUIRED, for devices that carry their own timestamps. Pass it whenever
        the device knows: the camera hands over frames in batches, so stamping
        on arrival would give every frame in a batch the same time and quantise
        that stream's timebase to the read cadence rather than the frame rate.
        Omit it and the sample is stamped on arrival, which is right for the
        devices we poll one sample at a time.
        """
        try:
            ts = self._clock.now() if at is None else self._clock.at(at)
        except RuntimeError:
            return  # session clock not started yet — drop silently
        self._buf.put((stream, ts, data))

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Record facts only known once the run is under way or finished (which
        timebase a device gave us, how much was dropped). Call before stop()."""
        self._writer.update_metadata(metadata)

    def stop(self, drain_timeout: float = 30.0) -> int:
        """
        Signal stop, drain the ring buffer to disk, then close the writer.

        The writer thread exits on its own once the buffer is empty, so with a
        generous timeout every queued sample is written. Returns the number of
        samples still un-drained if the timeout was hit (0 on a clean drain) so
        the caller can surface it rather than losing data silently.
        """
        self._stop_event.set()
        remaining = 0
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout)
            remaining = len(self._buf)
            self._thread = None
        self._writer.close()
        return remaining

    @property
    def drop_count(self) -> int:
        return self._buf.drop_count

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or len(self._buf):
            try:
                stream, ts, data = self._buf.get(timeout=0.05)
            except queue.Empty:
                continue
            self._writer.write(stream, ts, data)
