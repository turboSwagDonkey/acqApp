"""MOTION: measure the stage -> camera axes (`cam_axes.CamAxes`).

Nudges stage +X, back, +Y, back, by one step each, and correlates the
settled frame after each nudge with the one before it. Every wait is a timer
tick, never a sleep, so the GUI (and Esc) stay live. A fault or `cancel()`
stops the stage where it is and says how far that is from the start: after
Esc nothing moves on its own.
"""
from __future__ import annotations

import time
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .cam_axes import MIN_PEAK, describe, fit_axes, image_shift, implausible

# (axis to nudge or None, step sign, what the settled frame after it is for)
_PLAN = ((None, 0, "ref_x"), ("x", +1, "shot_x"), ("x", -1, "ref_y"),
         ("y", +1, "shot_y"), ("y", -1, None))


class AxesMeasurement(QObject):
    finished = pyqtSignal(object)   # CamAxes
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    _TICK_MS = 100
    _GRACE_S = 0.3          # the controller's "moving" bit lags the command
    _TIMEOUT_S = 15.0       # per step
    _SETTLE_FRAMES = 2      # the first new frame may have been exposed moving

    def __init__(self, controller, grab: Callable[[], object], step_um: float,
                 binning: int = 1, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._grab = grab
        self._step = step_um
        self._binning = binning
        self._frames: dict[str, object] = {}
        self._start_xy: tuple[float, float] | None = None
        self._i = 0
        self._t0 = 0.0
        self._settled = False
        self._seen = 0
        self._last = None
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        self._start_xy = self._ctrl.read_xy_um()
        self._begin(0)
        self._timer.start()

    def cancel(self) -> None:
        if self.running:
            self._fail("Cancelled")

    # ── steps ───────────────────────────────────────────────────────────────
    def _begin(self, i: int) -> None:
        self._i = i
        axis, sign, _ = _PLAN[i]
        if axis is not None:
            self.progress.emit(f"Measuring camera axes: nudging {axis.upper()} "
                               f"{'+' if sign > 0 else '−'}{self._step:g} µm…")
            self._ctrl.jog_um(axis, sign * self._step)
        self._t0 = time.monotonic()
        self._settled = False

    def _tick(self) -> None:
        # A QTimer slot: nothing may escape (it would abort the process).
        try:
            self._advance()
        except Exception as e:                      # noqa: BLE001
            self._fail(f"{type(e).__name__}: {e}")

    def _advance(self) -> None:
        now = time.monotonic()
        if now - self._t0 > self._TIMEOUT_S:
            self._fail("Timed out waiting for the stage to stop and the camera "
                       "to deliver frames (is Live view running?)")
            return
        if not self._settled:
            if now - self._t0 < self._GRACE_S or self._ctrl.is_moving():
                return
            self._settled, self._seen, self._last = True, 0, self._grab()
            return
        f = self._grab()
        if f is None:
            self._fail("The camera stopped delivering frames")
            return
        if f is not self._last:
            self._last = f
            self._seen += 1
        if self._seen < self._SETTLE_FRAMES:
            return
        kind = _PLAN[self._i][2]
        if kind is not None:
            self._frames[kind] = f
        if self._i + 1 < len(_PLAN):
            self._begin(self._i + 1)
        else:
            self._timer.stop()
            self._finish()

    def _finish(self) -> None:
        shifts = {}
        for axis in ("x", "y"):
            dx, dy, peak = image_shift(self._frames[f"ref_{axis}"],
                                       self._frames[f"shot_{axis}"])
            if peak < MIN_PEAK:
                self.failed.emit(
                    f"No clear image shift for {axis.upper()} (match "
                    f"{peak:.2f}). Needs a textured sample in focus; or try a "
                    f"bigger step.")
                return
            shifts[axis] = (dx, dy)
        axes = fit_axes(shifts["x"], shifts["y"], self._step, self._binning)
        why = implausible(axes)
        if why:
            self.failed.emit(f"Measurement looks wrong: {why}. Try again with "
                             f"a different step or a more textured field.")
            return
        self.progress.emit(f"Camera axes measured: {describe(axes)}")
        self.finished.emit(axes)

    def _fail(self, msg: str) -> None:
        self._timer.stop()
        self.failed.emit(f"{msg}. {self._stop_and_locate()}")

    def _stop_and_locate(self) -> str:
        """Best effort, on a link that may be dead: -> where the stage is."""
        try:
            self._ctrl.stop_all()
            x, y = self._ctrl.read_xy_um()
        except Exception as e:                      # noqa: BLE001
            return f"Stage state unknown ({type(e).__name__}: {e})."
        if self._start_xy is None:
            return "Stage stopped."
        dx, dy = x - self._start_xy[0], y - self._start_xy[1]
        return (f"Stage stopped {dx:+.1f}, {dy:+.1f} µm from where it started "
                f"({self._start_xy[0]:.1f}, {self._start_xy[1]:.1f}).")
