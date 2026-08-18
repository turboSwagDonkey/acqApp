"""Pupil camera fed from a recorded file instead of the Basler.

A third frame source beside the real camera and the synthetic mock, for tuning
the tracker against real footage with no animal and no hardware — which is the
only way to find parameters that survive fur, lashes and a corneal glint. The
mock's clean disc cannot show whether a setting works on an eye.

Same surface as `PupilCameraWorker`, so the adapter, `PupilTrackWorker` and the
search overlay are unchanged; `set_exposure` is a no-op because a recorded
frame's exposure is already fixed.

A session recorded from one of these is NOT rig data. The adapter files
`pupil_video` in the metadata so the HDF5 says so.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker
from acqApp.devices.pupil_cam.avi import AviReader


class VideoFileCameraWorker(PullWorker):
    """Replays `path` as if it were the pupil camera, looping by default."""

    fps_update = pyqtSignal(int, float)   # (total frames, fps over the run)

    _STOP_WAIT_MS = 2000

    def __init__(self, path: str | Path, fps: float = 20.0,
                 loop: bool = True) -> None:
        super().__init__()
        self._reader = AviReader(path)
        # 0 fps = "the file's own rate"; a still-image AVI reports 0 too, hence
        # the floor.
        self._fps = max(1.0, fps or self._reader.fps or 20.0)
        self._loop = loop
        self._n = 0
        print(f"[pupil_cam] video source {self._reader.describe()} "
              f"— replaying at {self._fps:g} fps{', looping' if loop else ''}")

    # ── the PupilCameraWorker surface ────────────────────────────────────────
    def set_exposure(self, us: float) -> None:
        """No-op: a recorded frame's exposure is fixed."""

    @property
    def frame_shape(self) -> tuple[int, int]:
        return (self._reader.height, self._reader.width)

    # ── video-only ───────────────────────────────────────────────────────────
    @property
    def n_frames(self) -> int:
        return len(self._reader)

    @property
    def source_name(self) -> str:
        return self._reader.path.name

    def _run(self) -> None:
        self._stop = False
        n, t0 = 0, time.perf_counter()
        period = 1.0 / self._fps
        total = len(self._reader)
        while not self._stop:
            i = n % total if self._loop else n
            if i >= total:
                break
            # Copy, not the memmap view: the tracker and the recording sink both
            # outlive this tick and a view would alias the next frame.
            self._publish(np.ascontiguousarray(self._reader.luma(i)))
            n += 1
            self._n = n
            if n % max(1, int(self._fps)) == 0:
                self.fps_update.emit(n, n / (time.perf_counter() - t0))
            slp = t0 + n * period - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
        print(f"[pupil_cam] video source stopped after {n} frames")
