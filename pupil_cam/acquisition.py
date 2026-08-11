"""
Pupil-tracking camera — acquisition worker (Basler via pypylon).

The rig camera is a **Basler acA1920-40umMED — USB3 Vision**, not GigE. It
*must* be on a USB 3.0 port: pylon refuses to open it otherwise ("The device
cannot be operated on an USB 2.0 port"), and there is no reduced-speed
fallback. Watch for USB2 Micro-B cables and intermediate hubs — a USB3 Micro-B
socket accepts a USB2 cable that only carries the USB2 pins.

Pre-init pattern: open the camera BEFORE importing PyQt6/pyqtgraph to avoid
DLL-path conflicts on Windows (same reason as DCAM in voltage_cam).

open_camera()        : enumerate + open the first Basler, or None.
PupilCameraWorker    : grabs Mono8 frames in free-running mode.
MockPupilCameraWorker: synthetic oscillating pupil, no hardware needed.

Both workers share acq.worker.PullWorker: get_latest() returns the newest frame
and the recording sink receives every frame.
"""

from __future__ import annotations

import threading
import time

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker


# ══════════════════════════════════════════════════════════════════════════════
#  GenICam node helpers
#
#  Node names differ across Basler families (USB ace uses the SFNC 2.0 names,
#  GigE ace classic the older *Abs ones), and a missing node must never be fatal
#  — the camera still grabs fine on its defaults.
# ══════════════════════════════════════════════════════════════════════════════

def _node(cam, *names):
    """First available node among `names`, or None."""
    from pypylon import genicam
    for name in names:
        try:
            n = getattr(cam, name)
            if genicam.IsAvailable(n.Node):
                return n
        except Exception:
            continue
    return None


def _set_clamped(cam, value: float, *names, label: str = "") -> float | None:
    """Set a numeric node, clamped to its own range. Returns the value set."""
    n = _node(cam, *names)
    if n is None:
        print(f"[pupil_cam] {label or names[0]}: node unavailable, left at default")
        return None
    try:
        lo, hi = n.GetMin(), n.GetMax()
        v = min(max(value, lo), hi)
        n.SetValue(v)
        if abs(v - value) > 1e-6:
            print(f"[pupil_cam] {label or names[0]}: {value:g} out of range "
                  f"[{lo:g}, {hi:g}] — clamped to {v:g}")
        return float(n.GetValue())
    except Exception as e:
        print(f"[pupil_cam] {label or names[0]}: could not set ({e})")
        return None


def _set_enum(cam, value: str, *names, label: str = "") -> bool:
    n = _node(cam, *names)
    if n is None:
        return False
    try:
        n.SetValue(value)
        return True
    except Exception as e:
        print(f"[pupil_cam] {label or names[0]} = {value!r} failed ({e})")
        return False


def open_camera(index: int = 0):
    """
    Enumerate and open the first Basler camera; return the InstantCamera, or
    None if there is no camera (or it cannot be opened).

    Call this BEFORE importing PyQt6/pyqtgraph — see the module docstring.
    Never raises: the caller falls back to the mock worker.
    """
    try:
        from pypylon import pylon
        factory = pylon.TlFactory.GetInstance()
        devices = factory.EnumerateDevices()
        if not devices:
            print("[pupil_cam] no Basler cameras found")
            return None
        if index >= len(devices):
            print(f"[pupil_cam] camera index {index} out of range "
                  f"({len(devices)} found)")
            return None
        cam = pylon.InstantCamera(factory.CreateDevice(devices[index]))
        cam.Open()
        info = cam.GetDeviceInfo()
        print(f"[pupil_cam] opened {info.GetModelName()} "
              f"(SN {info.GetSerialNumber()}, {info.GetDeviceClass()})")
        return cam
    except Exception as e:
        msg = str(e)
        if "USB 2.0" in msg or "USB 3.0" in msg:
            print("[pupil_cam] camera is on a USB 2.0 port — it needs USB 3.0. "
                  "Plug it directly into a blue/SS port (not through a hub) "
                  "and use the Basler USB3 Micro-B cable.")
        print(f"[pupil_cam] camera open failed: {e}")
        return None


class PupilCameraWorker(PullWorker):
    """
    Grabs Mono8 frames from a Basler camera.

    Normally handed an already-open pylon.InstantCamera so the caller can
    pre-init before Qt imports (avoids Windows DLL conflicts); it then never
    opens or closes that handle — the owner does. Pass `cam=None` to have the
    worker open and close its own, which is the simpler path when there is no
    Qt-import ordering problem to work around.
    """
    fps_update = pyqtSignal(int, float)   # (total_frames, fps over recent window)

    _STOP_WAIT_MS = 5000
    _FPS_WINDOW_S = 1.0
    _GRAB_TIMEOUT_MS = 500      # short, so stop() stays responsive

    def __init__(self, cam=None, exposure_us: float = 8000.0, fps: float = 20.0,
                 device_index: int = 0):
        super().__init__()
        self._cam          = cam
        self._device_index = device_index
        self._exposure_us  = exposure_us
        self._fps          = fps
        self._exp_lock     = threading.Lock()
        self._pending_exp: float | None = None
        self._frame_shape: tuple[int, int] | None = None

    def set_exposure(self, us: float) -> None:
        """Queue an exposure change; applied on the next grab-loop tick.

        Queued rather than written directly because the camera is opened on the
        GUI thread and grabbed on this one — see the cross-thread note in
        PUPIL_CAMERA_TRANSFER.md.
        """
        self._exposure_us = us
        with self._exp_lock:
            self._pending_exp = us

    @property
    def frame_shape(self) -> tuple[int, int] | None:
        """(H, W) of the frames actually delivered, once the first has arrived."""
        return self._frame_shape

    def _configure(self, cam) -> None:
        """Put the camera into free-running Mono8 at the requested exposure/fps."""
        # Mono8 explicitly: the camera can persist Mono12 from a previous
        # session, which would hand us uint16 frames and silently break both
        # the tracker's threshold units and the (0, 255) display levels.
        pf = _node(cam, "PixelFormat")
        if pf is not None:
            current = "?"
            try:
                current = pf.GetValue()
                if current != "Mono8":
                    pf.SetValue("Mono8")
                    print(f"[pupil_cam] pixel format: {current} → Mono8")
            except Exception as e:
                print(f"[pupil_cam] could not force Mono8 ({e}) — "
                      f"still {current!r}")

        # Auto-exposure would fight every set_exposure() call.
        _set_enum(cam, "Off", "ExposureAuto")
        _set_enum(cam, "Off", "GainAuto")
        _set_enum(cam, "Off", "TriggerMode")           # free-running

        exp = _set_clamped(cam, self._exposure_us,
                           "ExposureTime", "ExposureTimeAbs", label="exposure")

        # Frame rate is only honoured when the enable node is on; without it the
        # camera free-runs as fast as exposure and bandwidth allow.
        rate_on = _node(cam, "AcquisitionFrameRateEnable")
        if rate_on is not None:
            try:
                rate_on.SetValue(True)
            except Exception:
                pass
        rate = _set_clamped(cam, self._fps, "AcquisitionFrameRate",
                            "AcquisitionFrameRateAbs", label="frame rate")

        res = _node(cam, "ResultingFrameRate", "ResultingFrameRateAbs")
        actual = None
        try:
            actual = float(res.GetValue()) if res is not None else None
        except Exception:
            pass
        print(f"[pupil_cam] exposure={exp if exp is None else f'{exp:.0f}'} us, "
              f"requested {self._fps:g} fps"
              + (f", set {rate:.2f}" if rate is not None else "")
              + (f", camera reports {actual:.2f} achievable" if actual else ""))
        if actual is not None and rate is not None and actual < rate - 0.5:
            print(f"[pupil_cam] frame rate limited to {actual:.2f} fps — "
                  f"exposure or USB bandwidth is the ceiling")

    def _run(self) -> None:
        from pypylon import pylon

        self._stop = False
        own_cam = self._cam is None
        cam = open_camera(self._device_index) if own_cam else self._cam
        if cam is None:
            print("[pupil_cam] no camera — worker exiting")
            return

        try:
            self._configure(cam)
            cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

            n = 0
            win_n, win_t0 = 0, time.perf_counter()
            try:
                while not self._stop and cam.IsGrabbing():
                    with self._exp_lock:
                        pending, self._pending_exp = self._pending_exp, None
                    if pending is not None:
                        _set_clamped(cam, pending, "ExposureTime",
                                     "ExposureTimeAbs", label="exposure")

                    # Return (not Throw) on timeout: a missed frame should spin
                    # the loop so the stop flag is seen, not raise out of it.
                    grab = cam.RetrieveResult(self._GRAB_TIMEOUT_MS,
                                              pylon.TimeoutHandling_Return)
                    if grab is None:
                        continue
                    try:
                        if not grab.GrabSucceeded():
                            print(f"[pupil_cam] grab failed: "
                                  f"{grab.GetErrorDescription()}")
                            continue
                        frame = grab.Array.copy()      # Mono8 → (H, W) uint8
                        if self._frame_shape is None:
                            self._frame_shape = frame.shape
                            print(f"[pupil_cam] first frame: shape={frame.shape} "
                                  f"dtype={frame.dtype} "
                                  f"range=[{frame.min()}, {frame.max()}]")
                            if frame.dtype != np.uint8:
                                print(f"[pupil_cam] WARNING: expected uint8 "
                                      f"(Mono8), got {frame.dtype} — the "
                                      f"tracker's threshold assumes 0-255")
                        n += 1
                        win_n += 1
                        self._publish(frame)

                        now = time.perf_counter()
                        dt = now - win_t0
                        if dt >= self._FPS_WINDOW_S:
                            self.fps_update.emit(n, win_n / dt)
                            win_n, win_t0 = 0, now
                    finally:
                        grab.Release()
            finally:
                try:
                    cam.StopGrabbing()
                except Exception:
                    pass
        finally:
            if own_cam:                # only close a camera we opened ourselves
                try:
                    cam.Close()
                except Exception:
                    pass


class MockPupilCameraWorker(PullWorker):
    """
    Synthetic pupil camera: grey frame with a dark disc whose radius varies
    sinusoidally (simulated pupil dilation). Frame rate is configurable so the
    settings-panel fps actually takes effect on the mock.

    Includes a bright specular dot standing in for the IR corneal glint, since
    that is the artefact the tracker's outlier rejection exists to handle —
    a perfectly clean disc would not exercise it.
    """
    fps_update = pyqtSignal(int, float)
    H, W = 240, 320
    _STOP_WAIT_MS = 2000

    def __init__(self, fps: float = 30.0):
        super().__init__()
        self._fps = max(1.0, fps)

    def set_exposure(self, us: float) -> None:
        """No-op (kept for API parity with PupilCameraWorker)."""

    @property
    def frame_shape(self) -> tuple[int, int]:
        return (self.H, self.W)

    def _run(self) -> None:
        self._stop = False
        n, t0 = 0, time.perf_counter()
        period = 1.0 / self._fps
        cy, cx = self.H // 2, self.W // 2
        Y, X = np.ogrid[:self.H, :self.W]

        while not self._stop:
            t = time.perf_counter() - t0
            r = 35 + 15 * np.sin(2 * np.pi * 0.1 * t)
            frame = np.full((self.H, self.W), 180, dtype=np.uint8)
            frame[((X - cx) ** 2 + (Y - cy) ** 2) < r ** 2] = 20
            gr = max(2.0, 0.16 * r)     # corneal glint, offset inside the pupil
            frame[((X - (cx + 0.35 * r)) ** 2
                   + (Y - (cy - 0.3 * r)) ** 2) < gr ** 2] = 245
            n += 1
            self._publish(frame)
            if n % max(1, int(self._fps)) == 0:
                self.fps_update.emit(n, n / (time.perf_counter() - t0))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
