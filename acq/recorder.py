"""
Recorder — orchestrates clock + ring buffer + writer thread for one device stream.

Usage pattern:
    rec = Recorder(clock, writer, ring_buffer)
    rec.start(path, metadata)
    # acquisition loop calls:
    rec.put_frame(frame)  or  rec.put_encoder(voltage)
    rec.stop()
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

import numpy as np

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
            target=self._writer_loop, daemon=True, name="RecorderWriter")
        self._thread.start()

    def put_frame(self, frame: np.ndarray) -> None:
        ts = self._clock.now()
        self._buf.put(("frame", ts, frame))

    def put_encoder(self, voltage: float) -> None:
        ts = self._clock.now()
        self._buf.put(("encoder", ts, voltage))

    def stop(self, drain_timeout: float = 5.0) -> None:
        """Signal stop, drain remaining items, then close the writer."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout)
            self._thread = None
        self._writer.close()

    @property
    def drop_count(self) -> int:
        return self._buf.drop_count

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or len(self._buf):
            try:
                item = self._buf.get(timeout=0.05)
            except queue.Empty:
                continue
            kind, ts, data = item
            if kind == "frame":
                self._writer.write_frame(ts, data)
            elif kind == "encoder":
                self._writer.write_encoder(ts, data)
