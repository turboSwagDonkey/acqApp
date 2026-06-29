"""
Pupil-tracking camera — acquisition worker (Basler GigE via pypylon).

Pre-init pattern: open the camera BEFORE importing PyQt5/pyqtgraph to avoid
DLL-path conflicts on Windows (same reason as DCAM in voltage_cam).

PupilCameraWorker   : grabs Mono8 frames in free-running mode.
MockPupilCameraWorker: synthetic oscillating pupil, no hardware needed.
"""

from __future__ import annotations
import threading
import time

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal


class PupilCameraWorker(QThread):
    """
    Grabs frames from a Basler camera opened externally via pypylon.

    Pass the already-opened pylon.InstantCamera object so the caller can
    pre-init before Qt imports (avoids Windows DLL conflicts).
    """
    fps_update = pyqtSignal(int, float)   # (total_frames, fps)

    def __init__(
        self,
        cam,
        exposure_us: float = 8000.0,
        fps: float = 20.0,
    ):
        super().__init__()
        self._cam         = cam
        self._exposure_us = exposure_us
        self._fps         = fps
        self._stop        = False
        self._lock        = threading.Lock()
        self.latest_frame: np.ndarray | None = None
        self.total_frames = 0

    def get_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def set_exposure(self, us: float) -> None:
        """Hot-change exposure while grabbing."""
        self._exposure_us = us
        try:
            self._cam.ExposureTime.SetValue(us)
        except Exception:
            pass

    def run(self) -> None:
        from pypylon import pylon

        cam = self._cam
        cam.ExposureTime.SetValue(self._exposure_us)
        cam.AcquisitionFrameRateEnable.SetValue(True)
        cam.AcquisitionFrameRate.SetValue(self._fps)
        cam.TriggerMode.SetValue("Off")   # free-running

        cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        t0 = time.perf_counter()
        n  = 0

        try:
            while not self._stop and cam.IsGrabbing():
                grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                try:
                    if grab.GrabSucceeded():
                        frame = grab.Array.copy()   # Mono8 → (H, W) uint8
                        n += 1
                        t = time.perf_counter() - t0
                        with self._lock:
                            self.latest_frame = frame
                            self.total_frames = n
                        if n % 20 == 0:
                            self.fps_update.emit(n, n / t if t > 0 else 0.0)
                finally:
                    grab.Release()
        finally:
            cam.StopGrabbing()

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class MockPupilCameraWorker(QThread):
    """
    Synthetic pupil camera: grey frame with a dark ellipse whose axes
    vary sinusoidally (simulated pupil dilation).
    """
    fps_update = pyqtSignal(int, float)
    H, W, FPS = 240, 320, 30.0

    def __init__(self):
        super().__init__()
        self._stop        = False
        self._lock        = threading.Lock()
        self.latest_frame: np.ndarray | None = None
        self.total_frames = 0

    def get_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def run(self) -> None:
        self._stop = False
        n, t0 = 0, time.perf_counter()
        period = 1.0 / self.FPS
        cy, cx = self.H // 2, self.W // 2
        Y, X = np.ogrid[:self.H, :self.W]

        while not self._stop:
            t = time.perf_counter() - t0
            r = 35 + 15 * np.sin(2 * np.pi * 0.1 * t)
            frame = np.full((self.H, self.W), 180, dtype=np.uint8)
            frame[((X - cx) ** 2 + (Y - cy) ** 2) < r ** 2] = 20
            n += 1
            with self._lock:
                self.latest_frame = frame
                self.total_frames = n
            if n % 30 == 0:
                self.fps_update.emit(n, n / (time.perf_counter() - t0))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)

    def stop(self) -> None:
        self._stop = True
        self.wait(2000)
