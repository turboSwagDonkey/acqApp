"""DMD settings and controllers; the panel is `panel.py`.

DmdController: the real Vialux ALP-4.2, via `alp.py`.
MockDmdController: renders patterns in memory, no hardware.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from operator import attrgetter
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from acqApp.devices.dmd import alp

# Fallback panel size before (or without) a device: this rig's ALP is XGA.
DEFAULT_W, DEFAULT_H = 1024, 768

FRAME_START = 0
FRAME_STOP = -1

# What the panel is asking the device to show.
MODE_PATTERN, MODE_ALL_ON, MODE_ROI = "pattern", "all_on", "roi"


# Keyed on path, invalidated on mtime: `roi_frame` runs on every preview
# resize/nudge, and re-parsing the JSON was most of its cost (2026-08-27).
_calib_cache: dict[str, tuple[int, object]] = {}


def _emit_frame(sink: Callable[[int], None] | None, signal, idx: int) -> None:
    """Tell the sink, then the Qt signal."""
    if sink is not None:
        sink(idx)
    signal.emit(idx)


def _load_calibration(path):
    from acqApp.devices.dmd.calibration import DmdCalibration
    p = Path(path)
    mtime = p.stat().st_mtime_ns
    cached = _calib_cache.get(str(p))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    calib = DmdCalibration.load(p)
    _calib_cache[str(p)] = (mtime, calib)
    return calib


def orient_calibration(calib, settings):
    """`calib` with the operator's ROI flips applied (Y, then X)."""
    from acqApp.devices.dmd.calibration import flip_x, flip_y
    if settings.roi_flip_y:
        calib = flip_y(calib)
    if settings.roi_flip_x:
        calib = flip_x(calib)
    return calib


def roi_frame(settings, width: int, height: int):
    """The ROI mask as a device-sized frame, or None (reason printed)."""
    if not settings.rois:
        print("[DMD] ROI mode: no ROIs drawn — nothing to project")
        return None
    if not settings.calib_path:
        print("[DMD] ROI mode: no calibration loaded, so camera ROIs can't be "
              "turned into mirrors. Run Calibrate… first.")
        return None
    try:
        from acqApp.devices.dmd.roi import RoiSet
        calib = orient_calibration(_load_calibration(settings.calib_path),
                                   settings)
        frame = RoiSet.from_list(list(settings.rois)).dmd_frame(calib)
    except Exception as e:                        # noqa: BLE001
        print(f"[DMD] ROI mode: couldn't build the mask ({type(e).__name__}: "
              f"{e})")
        return None
    if frame.shape != (height, width):
        print(f"[DMD] ROI mode: calibration is for a "
              f"{frame.shape[1]}x{frame.shape[0]} panel, device is "
              f"{width}x{height} — re-run the calibration")
        return None
    if not int((frame > 0).sum()):
        print("[DMD] ROI mode: ROIs map to no mirrors at all — they may be "
              "outside the DMD's reachable field")
    return np.asarray(frame)


@lru_cache(maxsize=8)
def _subsample_keep(shape: tuple[int, ...], n: int) -> np.ndarray:
    yy, xx = np.indices(shape)
    keep = (yy + xx) % n != 0
    keep.flags.writeable = False        # shared between calls
    return keep


def subsample_frame(frame: np.ndarray, n: int) -> np.ndarray:
    """Zero 1 of every `n` pixels: a coarse ND filter made of missing mirrors,
    illumination time untouched. Diagonal ((row+col) % n) so the removed
    fraction is uniform rather than banded. n <= 1 is a no-op (off)."""
    if n <= 1:
        return frame
    keep = _subsample_keep(tuple(frame.shape), int(n))
    return np.where(keep, frame, 0).astype(frame.dtype, copy=False)


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    on_time_ms:    float       = 100.0  # illumination on-time per pattern (ms)
    # The panel always sets True (hold one image until Stop). The cycling
    # path (`on_time_ms`, `n_repeats`) is code/test-only, kept for the ALP
    # timing rules it encodes.
    static_hold:   bool        = True
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever
    # ── geometry: how the pattern lands on the panel ──
    scale_pct:     float       = 100.0  # per cent of the source image's size
    rotation_deg:  float       = 0.0    # clockwise-positive
    offset_x:      float       = 0.0    # device px from the panel centre
    offset_y:      float       = 0.0
    invert:        bool        = False  # swap on/off mirrors
    sub_sampling:  int         = 1      # `subsample_frame`; 1 = off, UI 1..10
    # `all_on` mirrors display_mode for the session metadata.
    display_mode:  str         = MODE_PATTERN   # pattern | all_on | roi
    all_on:        bool        = False  # turn all mirrors on
    fit:           bool        = False  # scale to fit and centre
    lib_dir:       str         = ""     # ALP API location
    # ── photostimulation ROIs ──
    # Voltage-camera px, as `RoiSet.to_list()`. Projectable only with a
    # `calib_path` registration.
    rois:          tuple       = ()
    calib_path:    str         = ""
    # Mirror only the ROI mask on its way to the panel (`calibration.flip_y`
    # / `flip_x`); pattern images are unaffected.
    roi_flip_y:    bool        = False
    roi_flip_x:    bool        = False


# Everything that changes the built frame; the real controller reloads only
# when one of these differs.
_GEOMETRY = attrgetter(
    "scale_pct", "rotation_deg", "offset_x", "offset_y", "invert", "fit",
    "display_mode", "rois", "calib_path", "roi_flip_y", "roi_flip_x",
    "sub_sampling")


class DmdController(QObject):
    """The real ALP-4.2 device."""
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._sink: Callable[[int], None] | None = None
        self._pattern: np.ndarray | None = None
        self._running = False
        lib_dir, source = alp.resolve_lib_dir(self._s.lib_dir)
        self._dev = alp.AlpDevice(lib_dir)
        self._dev.open()
        print(f"[DMD] ALP {self._dev.width}x{self._dev.height} (API from {source})")
        if self._s.pattern_path or self._s.display_mode != MODE_PATTERN:
            self.load_pattern(self._s.pattern_path)

    @property
    def device_name(self) -> str:
        return f"ALP-4.2 {self._dev.width}x{self._dev.height}"

    @property
    def resolution(self) -> tuple[int, int]:
        return self._dev.width, self._dev.height

    @property
    def on_pixels(self) -> int:
        return 0 if self._pattern is None else int((self._pattern > 0).sum())

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        self._sink = sink

    def _frame(self, idx: int) -> None:
        _emit_frame(self._sink, self.frame_displayed, idx)

    def apply_settings(self, settings: DmdSettings) -> None:
        changed = _GEOMETRY(settings) != _GEOMETRY(self._s)
        self._s = settings
        # Reload even in MODE_PATTERN with no file: that clears a stale
        # ALL_ON/ROI frame, which a skipped reload would project.
        if changed:
            self.load_pattern(settings.pattern_path)

    def load_pattern(self, path: Path | None = None) -> None:
        w, h = self.resolution
        mode = self._s.display_mode

        if mode == MODE_ALL_ON:
            self._pattern = subsample_frame(
                np.full((h, w), 255, dtype=np.uint8), self._s.sub_sampling)
            print(f"[DMD] all mirrors ON -> {w}x{h}, {self.on_pixels} mirrors on")
            return
        if mode == MODE_ROI:
            self._pattern = roi_frame(self._s, w, h)
            if self._pattern is not None:
                self._pattern = subsample_frame(self._pattern, self._s.sub_sampling)
                print(f"[DMD] ROIs -> {w}x{h}, {self.on_pixels} mirrors on")
            return

        p = Path(path or self._s.pattern_path or "")
        if not p.is_file():
            print(f"[DMD] load_pattern: no such file ({p})")
            self._pattern = None
            return

        self._pattern = subsample_frame(alp.build_frame(
            p, w, h, scale_pct=self._s.scale_pct,
            rotation_deg=self._s.rotation_deg, offset_x=self._s.offset_x,
            offset_y=self._s.offset_y, invert=self._s.invert, fit=self._s.fit),
            self._s.sub_sampling)
        print(f"[DMD] {p.name} -> {w}x{h}, {self.on_pixels} mirrors on")

    def project_frame(self, frame: np.ndarray) -> None:
        """Project a device-sized frame as-is, skipping `build_frame` (the
        sweep measures that geometry). Settings are untouched."""
        w, h = self.resolution
        if frame.shape != (h, w):
            raise ValueError(f"frame is {frame.shape}, device is {(h, w)}")
        self._pattern = np.ascontiguousarray(frame, dtype=np.uint8)
        self._dev.project(self._pattern, illumination_us=None, loop=True)
        self._running = True
        self._frame(FRAME_START)

    def display(self) -> None:
        if self._pattern is None:
            print("[DMD] display: no pattern loaded — nothing to project")
            return
        if self.on_pixels == 0:
            # Legal, but also what a bad scale/offset produces.
            print("[DMD] display: frame is entirely dark — check scale, "
                  "offset and invert")

        if self._s.static_hold:
            illum, loop, repeats = None, True, 0
        else:
            illum = int(round(self._s.on_time_ms * 1000.0))
            if illum > alp.MAX_PICTURE_US:
                print(f"[DMD] on-time {self._s.on_time_ms:g} ms exceeds the "
                      f"ALP's {alp.MAX_PICTURE_US / 1000:g} ms limit — "
                      f"clamped. Use static hold for a longer exposure.")
                illum = alp.MAX_PICTURE_US
            repeats = max(0, int(self._s.n_repeats))
            loop = repeats == 0

        self._dev.project(self._pattern, illumination_us=illum,
                          loop=loop, repeats=repeats)
        self._running = True
        self.pattern_started.emit()
        self._frame(FRAME_START)
        if self._s.static_hold:
            how = "static hold"
        else:
            how = (f"{illum / 1000:g} ms on-time, "
                   + ("looping" if loop else f"{repeats} repeats"))
        print(f"[DMD] projecting — {how}, {self.on_pixels} mirrors on")

    def stop(self) -> None:
        if not self._running:
            return
        self._dev.halt()
        self._running = False
        self._frame(FRAME_STOP)
        self.pattern_stopped.emit()

    def close(self) -> None:
        self.stop()
        self._dev.close()


class MockDmdController(QObject):
    """Renders patterns in memory and logs events — no hardware."""
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s       = settings or DmdSettings()
        self._pattern: np.ndarray | None = None
        self._running = False
        self._thread:  threading.Thread | None = None
        self._sink: Callable[[int], None] | None = None

    device_name = "mock (no DMD attached)"

    @property
    def resolution(self) -> tuple[int, int]:
        return DEFAULT_W, DEFAULT_H

    @property
    def on_pixels(self) -> int:
        return 0 if self._pattern is None else int((self._pattern > 0).sum())

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        self._sink = sink

    def apply_settings(self, settings: DmdSettings) -> None:
        reload = settings != self._s
        self._s = settings
        if reload:
            self.load_pattern(settings.pattern_path)

    def _frame(self, idx: int) -> None:
        _emit_frame(self._sink, self.frame_displayed, idx)

    def load_pattern(self, path: Path | None = None) -> None:
        if self._s.display_mode == MODE_ALL_ON:
            self._pattern = subsample_frame(
                np.full((DEFAULT_H, DEFAULT_W), 255, dtype=np.uint8),
                self._s.sub_sampling)
            print(f"[DMD mock] all mirrors ON -> {DEFAULT_W}x{DEFAULT_H}, "
                  f"{self.on_pixels} mirrors on")
            return
        if self._s.display_mode == MODE_ROI:
            self._pattern = roi_frame(self._s, DEFAULT_W, DEFAULT_H)
            if self._pattern is not None:
                self._pattern = subsample_frame(self._pattern, self._s.sub_sampling)
            return

        p = Path(path or self._s.pattern_path or "")
        if p.is_file():
            try:
                self._pattern = subsample_frame(alp.build_frame(
                    p, DEFAULT_W, DEFAULT_H, scale_pct=self._s.scale_pct,
                    rotation_deg=self._s.rotation_deg,
                    offset_x=self._s.offset_x, offset_y=self._s.offset_y,
                    invert=self._s.invert, fit=self._s.fit), self._s.sub_sampling)
                return
            except Exception as e:
                print(f"[DMD mock] couldn't render {p.name}: {e}")

        # Placeholder for no pattern at all: not a display mode, so no
        # sub-sampling.
        tile = np.kron([[0, 255] * 8, [255, 0] * 8] * 8,
                       np.ones((4, 4), dtype=np.uint8)).astype(np.uint8)
        reps = (DEFAULT_H // tile.shape[0] + 1, DEFAULT_W // tile.shape[1] + 1)
        self._pattern = np.tile(tile, reps)[:DEFAULT_H, :DEFAULT_W]

    def project_frame(self, frame: np.ndarray) -> None:
        """Hold the frame so `on_pixels` is truthful; nothing is emitted."""
        h, w = DEFAULT_H, DEFAULT_W
        if frame.shape != (h, w):
            raise ValueError(f"frame is {frame.shape}, device is {(h, w)}")
        self._pattern = np.ascontiguousarray(frame, dtype=np.uint8)
        self._running = True
        self._frame(FRAME_START)

    def display(self) -> None:
        if self._running:
            return
        self._running = True
        self.pattern_started.emit()
        self._frame(FRAME_START)
        if self._s.static_hold:
            return

        on_time = max(0.001, self._s.on_time_ms / 1000.0)

        def _loop():
            idx = 1
            while self._running:
                time.sleep(on_time)
                if self._running:
                    self._frame(idx)
                    idx += 1

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        was_running = self._running
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if was_running:
            self._frame(FRAME_STOP)
        self.pattern_stopped.emit()

    def close(self) -> None:
        self.stop()
