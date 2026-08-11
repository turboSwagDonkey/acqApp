"""
Standalone recording backend for the voltage-cam bring-up harness
(voltage_cam/_toy.py).

NOT used by the main app — that records every stream through acq/ (Recorder +
HDF5Writer) on the shared clock. This streams a fixed number of frames to an
HDF5 file; write() returns True once the target frame count is reached.

write() is designed to be attached as the worker's sink (worker.set_sink), so
EVERY captured frame is recorded rather than only the ~30/s the GUI preview
happens to pull. It is called from the capture thread, so it only enqueues:
a background thread does the HDF5 I/O, and the queue is bounded by bytes so a
slow disk sheds frames instead of exhausting RAM.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import numpy as np

from acqApp.acq.ring_buffer import RingBuffer


class RecordingManager:
    _QUEUE_BYTES = 512 << 20     # bounded backlog between capture and disk

    def __init__(self) -> None:
        self._file = None
        self._dset = None
        self._n = 0              # frames accepted (enqueued)
        self._written = 0        # frames actually on disk
        self._n_target = 0
        self._buf: RingBuffer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._dropped = 0        # survives stop(), unlike the buffer's counter

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    @property
    def dropped(self) -> int:
        """Frames shed because the disk could not keep up.

        Readable before *or* after stop() — the count is latched on stop.
        """
        return self._buf.drop_count if self._buf is not None else self._dropped

    def start(self, folder: Path, name: str, n_frames: int = 300) -> Path:
        import h5py
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.h5"
        self._file = h5py.File(path, "w")
        self._dset = None
        self._n = 0
        self._written = 0
        self._n_target = max(1, int(n_frames))
        self._buf = RingBuffer(self._n_target + 64,
                               maxbytes=self._QUEUE_BYTES,
                               sizeof=lambda f: getattr(f, "nbytes", 0))
        self._dropped = 0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True,
                                        name="ToyRecorder")
        self._thread.start()
        return path

    def write(self, frame: np.ndarray) -> bool:
        """Queue one frame; returns True when the target count is reached.

        Safe to call from the capture thread — it never touches the file.
        """
        if self._buf is None:
            return True
        if self._n < self._n_target:
            self._buf.put(frame)
            self._n += 1
        return self._n >= self._n_target

    # ── writer thread ────────────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        # Snapshot the buffer: stop() may replace it if a later start() races a
        # writer thread that outlived its drain timeout.
        buf = self._buf
        if buf is None:
            return
        while not self._stop_event.is_set() or len(buf):
            try:
                frame = buf.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._lock:
                if self._file is None:
                    break
                if self._dset is None:
                    # Uncompressed: a full ORCA frame is ~21 MB and gzip runs
                    # well under the ~330 MB/s the camera delivers, so
                    # compressing here would throttle acquisition. Pre-allocated
                    # to n_target with one frame per chunk, so each write is a
                    # single whole-chunk append with no resize.
                    self._dset = self._file.create_dataset(
                        "frames", shape=(self._n_target,) + frame.shape,
                        maxshape=(self._n_target,) + frame.shape,
                        dtype=frame.dtype, chunks=(1,) + frame.shape)
                if self._written < self._n_target:
                    self._dset[self._written] = frame
                    self._written += 1

    def stop(self, drain_timeout: float = 30.0) -> int:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout)
            if self._thread.is_alive():
                print("[voltage_cam] recorder did not drain within "
                      f"{drain_timeout:.0f}s — file may be short")
            self._thread = None
        if self._buf is not None:
            self._dropped = self._buf.drop_count      # latch before releasing
        with self._lock:
            if self._file is not None:
                # trim to what we actually wrote if stopped early
                if self._dset is not None and self._written < self._n_target:
                    self._dset.resize((self._written,) + self._dset.shape[1:])
                self._file.close()
                self._file = None
                self._dset = None
        # NOT cleared: a writer thread that outlived its drain timeout still
        # holds this buffer, and nulling it here would crash that thread.
        # start() replaces it, so nothing leaks across recordings.
        return self._written
