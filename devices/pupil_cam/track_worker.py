"""Pupil tracking on its own thread (a fit is 1-2 ms, a lost pupil longer,
and the preview shares its tick with the voltage camera).

Sole consumer of the camera's `get_latest()`, republishing each frame with its
fit so the ellipse belongs to the frame under it. Frames arriving mid-fit are
dropped.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from acqApp.acq.worker import PullWorker
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking import PupilTracking


class _FitSmoother:
    """Rolling mean of the last `window` fits (drawn and recorded). A lost
    frame clears it: averaging across a gap smears toward the old position.
    `type(fit)` builds the result, so this never imports EyeLoop."""

    def __init__(self) -> None:
        self._buf: deque = deque()

    def reset(self) -> None:
        self._buf.clear()

    def apply(self, fit, window: int):
        if fit is None:
            self._buf.clear()
            return None
        if window <= 1:
            self._buf.clear()
            return fit
        self._buf.append(fit)
        while len(self._buf) > window:
            self._buf.popleft()
        n = len(self._buf)
        cls = type(fit)
        return cls(
            center_x=sum(f.center_x for f in self._buf) / n,
            center_y=sum(f.center_y for f in self._buf) / n,
            semi_major=sum(f.semi_major for f in self._buf) / n,
            semi_minor=sum(f.semi_minor for f in self._buf) / n,
            angle_deg=_mean_angle_deg([f.angle_deg for f in self._buf]),
        )


def _mean_angle_deg(angles_deg: list) -> float:
    """Circular mean of doubled angles: an ellipse's angle is mod 180
    (179 and 1 average to 0, not 90)."""
    a = np.radians(np.asarray(angles_deg, dtype=float)) * 2.0
    ang = np.degrees(np.arctan2(np.mean(np.sin(a)), np.mean(np.cos(a)))) / 2.0
    return float(ang % 180.0)


class _BlinkDetector:
    """A sudden radius drop against a rolling median of recent good frames.
    On the RAW radius (smoothing blurs exactly this). Blink frames never
    enter the baseline, so a run of them can't lower the bar."""

    _WARMUP = 3

    def __init__(self) -> None:
        self._baseline: deque = deque()

    def reset(self) -> None:
        self._baseline.clear()

    def check(self, radius: float | None, drop_frac: float, window: int) -> bool:
        if radius is None:
            return False
        window = max(self._WARMUP, window)
        if len(self._baseline) < self._WARMUP:
            self._baseline.append(radius)
            return False
        # statistics, not numpy: cheaper for a few dozen floats per frame.
        base = statistics.median(self._baseline)
        blink = base > 0.0 and radius <= base * (1.0 - drop_frac)
        if not blink:
            self._baseline.append(radius)
            while len(self._baseline) > window:
                self._baseline.popleft()
        return blink


@dataclass(frozen=True)
class Tracked:
    """One frame and what the display draws over it."""

    frame: np.ndarray
    fit: Any = None                              # PupilFit, or None
    mask: np.ndarray | None = None               # removed pixels, crop-sized
    box: tuple[int, int, int, int] | None = None  # the crop, in frame px


class PupilTrackWorker(PullWorker):
    """`get_latest()` gives a `Tracked`; `take_tracked()` drains
    (radius, is_blink) per tracked frame."""

    _STOP_WAIT_MS = 3000
    _IDLE_SLEEP_S = 0.004

    def __init__(self, source: Callable[[], Any], settings: PupilSettings,
                 history: int = 600) -> None:
        super().__init__()
        self._source = source
        self._tracking = PupilTracking()
        # Rebound whole, never mutated, so no frame sees half an edit.
        self._settings = settings
        # One deque of pairs, so radius and blink flag drain in lockstep.
        self._radii: deque[tuple[float, bool]] = deque(maxlen=history)
        self._smoother = _FitSmoother()
        self._blink = _BlinkDetector()
        self._fit_sink: Callable[[Any, bool, float], None] | None = None
        self._seen = 0
        self._fits = 0
        self._blinks = 0

    # ── GUI side ─────────────────────────────────────────────────────────────
    def configure(self, settings: PupilSettings) -> None:
        self._settings = settings

    def set_fit_sink(self, sink: Callable[[Any, bool, float], None] | None) -> None:
        """`sink(fit_or_None, is_blink, at)` per tracked frame. `at` is when
        the frame was pulled (no camera timestamp)."""
        self._fit_sink = sink

    def take_tracked(self) -> list[tuple[float, bool]]:
        """Drained, so the plot gets one point per frame; NaN = no fit."""
        with self._lock:
            out = list(self._radii)
            self._radii.clear()
        return out

    @property
    def available(self) -> bool:
        return self._tracking.available

    @property
    def track_error(self) -> str | None:
        """Why tracking isn't running. Not `error`: that's PullWorker's
        signal, and shadowing it breaks the crash guard."""
        return self._tracking.error

    @property
    def frames_seen(self) -> int:
        return self._seen

    @property
    def fits(self) -> int:
        return self._fits

    @property
    def blinks(self) -> int:
        return self._blinks

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop:
            frame = self._source()
            if frame is None:
                time.sleep(self._IDLE_SLEEP_S)
                continue
            at = time.perf_counter()
            st = self._settings
            self._seen += 1

            fit = self._tracking.track(frame, st)
            if fit is not None:
                self._fits += 1

            if st.blink_detect:
                is_blink = self._blink.check(
                    fit.radius if fit is not None else None,
                    st.blink_drop_frac, st.blink_baseline_window)
            else:
                self._blink.reset()
                is_blink = False
            if is_blink:
                self._blinks += 1

            if st.smooth:
                fit = self._smoother.apply(fit, max(1, st.smooth_window))
            else:
                self._smoother.reset()

            # Not _publish: frames are recorded by the camera's sink.
            self._set_latest(Tracked(frame, fit, self._tracking.last_mask,
                                     self._tracking.last_box))
            if st.track:
                with self._lock:
                    self._radii.append(
                        (fit.radius if fit is not None else float("nan"),
                         is_blink))
                sink = self._fit_sink
                if sink is not None:
                    sink(fit, is_blink, at)
