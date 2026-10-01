"""Pupil camera fed from a recorded AVI: tunes the tracker on real footage
(fur, lashes, glints) with no animal or hardware. Same surface as
`PupilCameraWorker`. A session recorded from one is NOT rig data; the adapter
files `pupil_video` in the metadata.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker, paced
from acqApp.devices.pupil_cam.avi import AviReader


class VideoFileCameraWorker(PullWorker):
    """Replays `path` as if it were the pupil camera, looping by default."""

    hz_update = pyqtSignal(int, float)   # (total frames, Hz over the run)

    _STOP_WAIT_MS = 2000

    def __init__(self, path: str | Path, rate_hz: float = 20.0,
                 loop: bool = True) -> None:
        super().__init__()
        self._reader = AviReader(path)
        # 0 Hz = the file's own rate; a still-image AVI reports 0 too.
        self._hz = max(1.0, rate_hz or self._reader.hz or 20.0)
        self._loop = loop
        self._n = 0
        print(f"[pupil_cam] video source {self._reader.describe()} "
              f"— replaying at {self._hz:g} Hz{', looping' if loop else ''}")

    def set_exposure(self, us: float) -> None:
        """No-op."""

    @property
    def frame_shape(self) -> tuple[int, int]:
        return (self._reader.height, self._reader.width)

    @property
    def n_frames(self) -> int:
        return len(self._reader)

    @property
    def source_name(self) -> str:
        return self._reader.path.name

    def _run(self) -> None:
        self._stop = False
        t0 = time.perf_counter()
        period = 1.0 / self._hz
        total = len(self._reader)
        for n in paced(period, t0):     # counts from 1
            if self._stop:
                break
            i = (n - 1) % total if self._loop else (n - 1)
            if i >= total:
                break
            # Planar frames stay read-only views of the (unchanging) file;
            # padded or flipped DIB frames get copied contiguous.
            self._publish(np.ascontiguousarray(self._reader.luma(i)))
            self._n = n
            if n % max(1, int(self._hz)) == 0:
                self.hz_update.emit(n, n / (time.perf_counter() - t0))
        print(f"[pupil_cam] video source stopped after {self._n} frames")
