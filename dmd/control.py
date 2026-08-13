"""
DMD (Digital Micromirror Device) controller + settings panel.

DmdController      : the real Vialux ALP-4.2 (1024x768 on this rig), via dmd/alp.py.
MockDmdController  : renders patterns locally, no hardware needed.
SettingsPanel      : QWidget for pattern, geometry, timing and trigger settings.
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

# Only QObject and pyqtSignal: the controllers are devices, not widgets. The
# panel and everything it draws with live in `panel.py`.
from acqApp.dmd import alp

# Fallback panel size before (or without) a device: this rig's ALP is XGA.
DEFAULT_W, DEFAULT_H = 1024, 768

FRAME_START = 0
FRAME_STOP = -1


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    on_time_ms:    float       = 100.0  # illumination on-time per pattern (ms)
    # The panel no longer exposes the timing controls and hardcodes this True:
    # the DMD holds one image until Stop. `on_time_ms` / `n_repeats` below are
    # only read on the cycling path, which is now reachable from code (and the
    # tests) but not from the UI — kept because the ALP timing rules it encodes
    # were expensive to establish, not because anything drives it today.
    static_hold:   bool        = True   # True = project one image, held
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever
    # ── geometry: how the pattern lands on the panel ──
    scale_pct:     float       = 100.0  # per cent of the source image's size
    rotation_deg:  float       = 0.0    # clockwise-positive
    offset_x:      float       = 0.0    # device px from the panel centre
    offset_y:      float       = 0.0
    invert:        bool        = False  # swap on/off mirrors
    all_on:        bool        = False  # turn all mirrors on
    fit:           bool        = False  # scale to fit and centre
    lib_dir:       str         = ""     # ALP API location


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
        if self._s.pattern_path or self._s.all_on:
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
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def apply_settings(self, settings: DmdSettings) -> None:
        geometry_changed = (
            (settings.scale_pct, settings.rotation_deg, settings.offset_x,
             settings.offset_y, settings.invert, settings.fit, settings.all_on)
            != (self._s.scale_pct, self._s.rotation_deg, self._s.offset_x,
                self._s.offset_y, self._s.invert, self._s.fit, self._s.all_on))
        self._s = settings
        if geometry_changed and (settings.pattern_path or settings.all_on):
            self.load_pattern(settings.pattern_path)

    def load_pattern(self, path: Path | None = None) -> None:
        w, h = self.resolution

        if self._s.all_on:
            self._pattern = np.full((h, w), 255, dtype=np.uint8)
            print(f"[DMD] all mirrors ON -> {w}x{h}, {self.on_pixels} mirrors on")
            return

        p = Path(path or self._s.pattern_path or "")
        if not p.is_file():
            print(f"[DMD] load_pattern: no such file ({p})")
            self._pattern = None
            return

        self._pattern = alp.build_frame(
            p, w, h, scale_pct=self._s.scale_pct,
            rotation_deg=self._s.rotation_deg, offset_x=self._s.offset_x,
            offset_y=self._s.offset_y, invert=self._s.invert, fit=self._s.fit)
        print(f"[DMD] {p.name} -> {w}x{h}, {self.on_pixels} mirrors on")

    def display(self) -> None:
        if self._pattern is None:
            print("[DMD] display: no pattern loaded — nothing to project")
            return
        if self.on_pixels == 0:
            # Every mirror off is a legal frame and a projector showing nothing.
            # It is also what a bad scale/offset produces, so say it out loud.
            print("[DMD] display: the frame is entirely dark — check scale, "
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

    def software_trigger(self) -> None:
        self._frame(FRAME_START)


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
        reload = ((settings.pattern_path or settings.all_on) and settings != self._s)
        self._s = settings
        if reload:
            self.load_pattern(settings.pattern_path)

    def _frame(self, idx: int) -> None:
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def load_pattern(self, path: Path | None = None) -> None:
        if self._s.all_on:
            self._pattern = np.full((DEFAULT_H, DEFAULT_W), 255, dtype=np.uint8)
            print(f"[DMD mock] all mirrors ON -> {DEFAULT_W}x{DEFAULT_H}, "
                  f"{self.on_pixels} mirrors on")
            return

        p = Path(path or self._s.pattern_path or "")
        if p.is_file():
            try:
                self._pattern = alp.build_frame(
                    p, DEFAULT_W, DEFAULT_H, scale_pct=self._s.scale_pct,
                    rotation_deg=self._s.rotation_deg,
                    offset_x=self._s.offset_x, offset_y=self._s.offset_y,
                    invert=self._s.invert, fit=self._s.fit)
                return
            except Exception as e:
                print(f"[DMD mock] could not render {p.name}: {e}")

        # Fallback checkerboard
        tile = np.kron([[0, 255] * 8, [255, 0] * 8] * 8,
                       np.ones((4, 4), dtype=np.uint8)).astype(np.uint8)
        reps = (DEFAULT_H // tile.shape[0] + 1, DEFAULT_W // tile.shape[1] + 1)
        self._pattern = np.tile(tile, reps)[:DEFAULT_H, :DEFAULT_W]

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

    def software_trigger(self) -> None:
        self._frame(FRAME_START)
