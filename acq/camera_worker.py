"""
CameraWorker — QThread that drives a Hamamatsu Orca via pylablib DCAM.

Acquisition loop:
  - wait_for_frame() blocks until DCAM has at least one frame ready
  - read_multiple_images() drains everything buffered since the last read
  - each frame is timestamped via SessionClock and pushed to the RingBuffer
  - every `preview_every` frames a decimated copy is emitted to the GUI

The GUI never receives raw full-res frames on the Qt signal path — only the
decimated previews.  Full data goes through the RingBuffer -> Recorder path.

Signals
-------
frame_ready(np.ndarray)   — decimated preview frame (for ImageView)
error(str)                — human-readable error message
status(str)               — informational status updates (FPS etc.)
"""
from __future__ import annotations

import time

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from .clock import AbstractClock
from .ring_buffer import RingBuffer


class CameraWorker(QThread):
    frame_ready: pyqtSignal = pyqtSignal(object)   # np.ndarray preview
    error: pyqtSignal = pyqtSignal(str)
    status: pyqtSignal = pyqtSignal(str)

    def __init__(
        self,
        clock: AbstractClock,
        ring_buffer: RingBuffer,
        *,
        exposure: float = 0.01,
        binning: int = 1,
        roi: tuple[int, int, int, int] | None = None,
        preview_every: int = 10,
        cam_index: int = 0,
    ) -> None:
        super().__init__()
        self._clock = clock
        self._buf = ring_buffer
        self.exposure = exposure
        self.binning = binning
        self.roi = roi
        self.preview_every = preview_every
        self._cam_index = cam_index
        self._running = False

    # ------------------------------------------------------------------
    # Public control (called from GUI thread before/after run)
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            from pylablib.devices import DCAM
        except Exception as exc:
            self.error.emit(f"pylablib DCAM import failed: {exc}")
            return

        n = DCAM.get_cameras_number()
        if n == 0:
            self.error.emit(
                "No DCAM camera detected. Check: power, cable, "
                "Hamamatsu DCAM-API runtime installed."
            )
            return
        if self._cam_index >= n:
            self.error.emit(
                f"Camera index {self._cam_index} requested but only {n} camera(s) found."
            )
            return

        cam = DCAM.DCAMCamera(idx=self._cam_index)
        try:
            info = cam.get_device_info()
            self.status.emit(f"Connected: {info}")

            cam.set_exposure(self.exposure)
            try:
                cam.set_attribute_value("BINNING", self.binning)
            except Exception:
                pass  # not all models expose BINNING under this name

            if self.roi is not None:
                cam.set_roi(*self.roi)

            roi_actual = cam.get_roi()
            exp_actual = cam.get_exposure()
            self.status.emit(
                f"ROI={roi_actual}  exposure={exp_actual * 1e3:.3f} ms"
            )

            self._running = True
            frame_count = 0
            t_last_fps = time.perf_counter()
            fps_count = 0

            cam.start_acquisition()
            try:
                while self._running:
                    if not cam.wait_for_frame(timeout=0.1):
                        continue
                    imgs = cam.read_multiple_images()
                    if not imgs:
                        continue

                    for img in imgs:
                        frame = np.asarray(img)
                        self._buf.put(("frame", self._clock.now(), frame))
                        frame_count += 1
                        fps_count += 1

                        if frame_count % self.preview_every == 0:
                            self.frame_ready.emit(frame.copy())

                    # emit FPS every ~2 s
                    now = time.perf_counter()
                    if now - t_last_fps >= 2.0:
                        fps = fps_count / (now - t_last_fps)
                        self.status.emit(
                            f"FPS: {fps:.1f}   drops: {self._buf.drop_count}"
                        )
                        fps_count = 0
                        t_last_fps = now

            finally:
                cam.stop_acquisition()
                self.status.emit(
                    f"Acquisition stopped. Total frames: {frame_count}"
                )

        except Exception as exc:
            self.error.emit(f"Camera error: {exc}")
        finally:
            cam.close()
