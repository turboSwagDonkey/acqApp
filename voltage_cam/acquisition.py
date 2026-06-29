"""
Voltage-imaging camera — acquisition workers.

OrcaFireWorker   : captures 16-bit frames from a Hamamatsu ORCA-Fire
                   via pylablib's DCAM wrapper.
MockCameraWorker : synthetic 16-bit frames (noise blob + 0.5 Hz ΔF sine)
                   for dev work.  Matches OrcaFireWorker's pull-based API.

Both expose:
    worker.get_latest()  -> np.ndarray | None
    worker.total_frames  -> int
    worker.fps_update    -> pyqtSignal(int, float)
"""

from __future__ import annotations
import threading
import time

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from .presets import AcqConfig

# Raw DCAM trigger-mode property values
_TRIGGER_DCAM: dict[str, int] = {
    "Internal (free-running)": 1,
    "External edge":           2,
}


class OrcaFireWorker(QThread):
    """
    Captures 16-bit frames from a Hamamatsu ORCA-Fire via pylablib.

    The camera is opened and closed inside run() so the worker is safely
    restartable.  AcqConfig is read once at start; exposure can be changed
    hot via set_exposure().
    """
    fps_update = pyqtSignal(int, float)   # (total_frames, fps)

    def __init__(self, device_index: int = 0, config: AcqConfig | None = None):
        super().__init__()
        self._device_index  = device_index
        self._config        = config or AcqConfig()
        self._stop          = False
        self._lock          = threading.Lock()
        self._exp_lock      = threading.Lock()
        self._pending_exp: float | None = None
        self.latest_frame: np.ndarray | None = None
        self.total_frames: int = 0

    def get_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def set_exposure(self, us: float) -> None:
        """Queue an exposure change; applied on the next frame loop tick."""
        self._config.exposure_us = us
        with self._exp_lock:
            self._pending_exp = us

    def run(self) -> None:
        from pylablib.devices import DCAM

        cfg    = self._config
        preset = cfg.preset

        cam = DCAM.DCAMCamera(idx=self._device_index)
        try:
            # --- ROI / binning ---
            if preset.is_full_frame:
                cam.set_roi(hbin=cfg.binning, vbin=cfg.binning)
            else:
                cam.set_roi(
                    hstart = preset.hpos,
                    hend   = preset.hpos + preset.hsize,
                    vstart = preset.vpos,
                    vend   = preset.vpos + preset.vsize,
                    hbin   = cfg.binning,
                    vbin   = cfg.binning,
                )

            # --- exposure (pylablib uses seconds) ---
            cam.set_exposure(cfg.exposure_us * 1e-6)

            # --- trigger ---
            trig_val = _TRIGGER_DCAM.get(cfg.trigger_mode, 1)
            cam.set_attribute_value("TRIGGERMODE", trig_val)

            # --- capture loop ---
            cam.start_acquisition()
            n, t0 = 0, time.perf_counter()

            try:
                while not self._stop:
                    with self._exp_lock:
                        pending = self._pending_exp
                        self._pending_exp = None
                    if pending is not None:
                        try:
                            cam.set_exposure(pending * 1e-6)
                        except Exception:
                            pass

                    try:
                        cam.wait_for_frame(timeout=0.5)
                    except Exception:
                        continue

                    imgs = cam.read_multiple_images()
                    if imgs:
                        n += 1
                        t = time.perf_counter() - t0
                        with self._lock:
                            self.latest_frame = imgs[-1]
                            self.total_frames = n
                        if n % 20 == 0:
                            self.fps_update.emit(n, n / t if t > 0 else 0.0)
            finally:
                cam.stop_acquisition()

        finally:
            cam.close()

    def stop(self) -> None:
        self._stop = True
        self.wait(5000)


class MockCameraWorker(QThread):
    """
    Synthetic voltage-imaging camera: shot-noise background with a circular
    blob whose mean fluorescence oscillates at 0.5 Hz (simulated ΔF/F).
    Frame dimensions match the configured preset after binning.
    """
    fps_update = pyqtSignal(int, float)
    _FPS = 30.0

    def __init__(self, config: AcqConfig | None = None):
        super().__init__()
        self._config      = config or AcqConfig()
        self._stop        = False
        self._lock        = threading.Lock()
        self.latest_frame: np.ndarray | None = None
        self.total_frames: int = 0

    def get_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self.latest_frame
            self.latest_frame = None
        return f

    def run(self) -> None:
        self._stop = False
        H, W   = self._config.frame_shape
        cy, cx = H // 2, W // 2
        y, x   = np.ogrid[-cy:H - cy, -cx:W - cx]
        r_blob = min(H, W) * 0.12
        blob   = (x * x + y * y) < r_blob ** 2

        rng    = np.random.default_rng(0)
        period = 1.0 / self._FPS
        n, t0  = 0, time.perf_counter()

        while not self._stop:
            t     = time.perf_counter() - t0
            frame = rng.integers(1500, 2500, (H, W), dtype=np.uint16)
            sig   = int(300 * np.sin(2 * np.pi * 0.5 * t))
            frame[blob] = np.clip(
                frame[blob].astype(np.int32) + sig, 0, 65535
            ).astype(np.uint16)
            n += 1
            with self._lock:
                self.latest_frame = frame
                self.total_frames = n
            if n % int(self._FPS) == 0:
                self.fps_update.emit(n, n / max(time.perf_counter() - t0, 1e-9))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)

    def stop(self) -> None:
        self._stop = True
        self.wait(2000)
