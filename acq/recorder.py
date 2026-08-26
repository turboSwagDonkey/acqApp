"""Recorder — one background writer thread draining a shared ring buffer to disk.

Device workers call `put()` from their acquisition threads; it stamps the sample
on the session-wide clock at acquisition time (not write time) and enqueues it,
so no acquisition thread ever touches disk I/O.

    rec = Recorder(clock, HDF5Writer(), RingBuffer(512))
    rec.start(path, metadata)
    rec.put("wheel", voltage)       # from each worker thread
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
        # Detaching the sinks doesn't stop a worker already inside its callback,
        # so a put() can land after the file closed. Those used to return
        # silently, undercounting drops exactly when the number mattered.
        self._gate = threading.Lock()
        self._closed = False
        self._late = 0              # arrived after the file was closed
        self._unstamped = 0         # arrived before the session clock started
        # Samples offered per stream. Under the same lock as the enqueue, so a
        # reader gets a count that never leads the buffer.
        self._offered: dict[str, int] = {}

    def start(self, path: Path, metadata: dict[str, Any]) -> None:
        self._stop_event.clear()
        self._writer.open(path, metadata)
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="Recorder")
        self._thread.start()

    def put(self, stream: str, data: Any, at: float | None = None) -> None:
        """Enqueue one sample, stamped on the shared clock at acquisition time.

        `at` is a `perf_counter()` reading of when the sample was ACQUIRED, for
        devices carrying their own timestamps — the camera hands over frames in
        batches, so stamping on arrival would quantise that stream to the read
        cadence. Omit it for devices polled one sample at a time.
        """
        try:
            ts = self._clock.now() if at is None else self._clock.at(at)
        except RuntimeError:
            with self._gate:            # clock not started — nothing to stamp with
                self._unstamped += 1
            return
        with self._gate:
            if self._closed:
                self._late += 1
                return
            self._buf.put((stream, ts, data))
            self._offered[stream] = self._offered.get(stream, 0) + 1

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Facts known only once the run is under way. Call before stop()."""
        self._writer.update_metadata(metadata)

    def stop(self, drain_timeout: float = 30.0,
             final_metadata: Callable[[], dict[str, Any]] | None = None) -> int:
        """Stop, drain to disk, close. Returns samples still un-drained if the
        timeout was hit (0 on a clean drain), so the caller can surface it.

        `final_metadata()` runs after the drain and before the close: its counts
        are only final once the writer thread stopped, and there is no way to
        write them afterwards.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout)
            self._thread = None
        # Close the gate before measuring, so a straggler is either in the
        # buffer (counted in `remaining`) or counted late — never discarded.
        with self._gate:
            self._closed = True
            remaining = len(self._buf)
        if final_metadata is not None:
            self._writer.update_metadata(final_metadata())
        self._writer.close()
        return remaining

    def offered(self, stream: str) -> int:
        """Samples of `stream` handed to this file so far.

        "Offered", not written: the ring can still shed one, and that is
        counted in `drop_count` rather than here. It is what an experiment
        routine measures a "100 frames" step by — the frames that reached the
        file, not the frames the camera produced, which differ exactly when the
        write path is the thing falling behind.
        """
        with self._gate:
            return self._offered.get(stream, 0)

    @property
    def drop_count(self) -> int:
        """Shed by the ring buffer because the writer fell behind."""
        return self._buf.drop_count

    @property
    def late_count(self) -> int:
        """Arrived after the file was closed."""
        return self._late

    @property
    def unstamped_count(self) -> int:
        """Offered before the session clock started, so they had no timebase."""
        return self._unstamped

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or len(self._buf):
            try:
                stream, ts, data = self._buf.get(timeout=0.05)
            except queue.Empty:
                continue
            self._writer.write(stream, ts, data)
