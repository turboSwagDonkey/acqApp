"""Pupil tracking on its own thread.

A fit is 1.2-2.4 ms, which fits in a 33 ms tick — but not reliably: a lost
pupil or a re-armed tracker takes longer, and the pupil preview shares that
tick with the voltage camera's. Same shape the retired tracker arrived at
(`archive/pupil_tracking/track_worker.py`).

**Sole consumer** of the camera's `get_latest()`, republishing each frame *with*
its fit so the ellipse always belongs to the frame under it. Built whether or
not tracking is on; with `track=False` it is a pass-through.

Frames arriving during a fit are dropped, so the trace is sparser than the
recording (which goes through the camera's own sink). `frames_seen` vs `fits`
says how much sparser.
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
    """Rolling mean of the last `window` fits — applied to both the drawn
    outline and what is recorded, so the trace matches what the operator
    looked at. Trades frame-to-frame jitter for lag.

    A lost frame clears the buffer rather than being skipped over: averaging
    a stale fit into a fresh run would smear the outline toward where the
    pupil used to be, which is worse than one un-smoothed point at the seam.
    Never imports the fit type — `type(fit)` builds the next one, so this
    stays usable with no EyeLoop clone installed (a fit just never arrives).
    """

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
    """Circular mean, doubled first: an ellipse's angle is only defined mod
    180 deg (the two ends of the major axis are the same ellipse), and a
    plain mean wraps wrong right at that boundary (e.g. 179 and 1 -> 90, not
    0)."""
    a = np.radians(np.asarray(angles_deg, dtype=float)) * 2.0
    ang = np.degrees(np.arctan2(np.mean(np.sin(a)), np.mean(np.cos(a)))) / 2.0
    return float(ang % 180.0)


class _BlinkDetector:
    """Flags a frame whose radius has dropped suddenly against a rolling
    baseline of recent good ones — a closing eyelid, not the pupil itself,
    shrinks 30%+ frame to frame.

    Runs on the RAW radius, upstream of `_FitSmoother`: averaging exists to
    blur exactly this kind of sudden change, so a smoothed radius would blunt
    or delay the very thing this is meant to catch.

    The baseline only ever absorbs non-blink radii, so a run of blink frames
    cannot drag it down and quietly raise its own bar for what counts as one.
    """

    _WARMUP = 3      # do not judge a session before there is a baseline at all

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
        # A plain-list median, not np.median: the baseline is at most a few
        # dozen floats, and numpy's per-call array-conversion overhead costs
        # more than it saves at that size — this runs on every tracked frame.
        base = statistics.median(self._baseline)
        blink = base > 0.0 and radius <= base * (1.0 - drop_frac)
        if not blink:
            self._baseline.append(radius)
            while len(self._baseline) > window:
                self._baseline.popleft()
        return blink


@dataclass(frozen=True)
class Tracked:
    """One frame and everything the display needs to draw over it."""

    frame: np.ndarray
    fit: Any = None                              # PupilFit, or None
    mask: np.ndarray | None = None               # removed pixels, crop-sized
    box: tuple[int, int, int, int] | None = None  # the crop, in frame px


class PupilTrackWorker(PullWorker):
    """Runs `PupilTracking` over whatever the camera worker last produced.

    `source` is normally `PupilCameraWorker.get_latest`. `get_latest()` gives a
    `Tracked`; `take_tracked()` drains one (radius, is_blink) pair per tracked
    frame.
    """

    _STOP_WAIT_MS = 3000
    _IDLE_SLEEP_S = 0.004        # shorter than a frame period at any usable fps

    def __init__(self, source: Callable[[], Any], settings: PupilSettings,
                 history: int = 600) -> None:
        super().__init__()
        self._source = source
        self._tracking = PupilTracking()
        # Rebound wholesale, never mutated: one store is atomic under the GIL,
        # so no frame is tracked with half of an edit.
        self._settings = settings
        # (radius, is_blink) per tracked frame, one deque so the two always
        # drain in lockstep — two separately-locked deques could desync if the
        # worker appended to one between the reader's two lock acquisitions.
        self._radii: deque[tuple[float, bool]] = deque(maxlen=history)
        self._smoother = _FitSmoother()
        self._blink = _BlinkDetector()
        self._fit_sink: Callable[[Any, bool, float], None] | None = None
        self._seen = 0
        self._fits = 0
        self._blinks = 0

    # ── GUI side ─────────────────────────────────────────────────────────────
    def configure(self, settings: PupilSettings) -> None:
        """Adopt a settings edit, in force from the next frame."""
        self._settings = settings

    def set_fit_sink(self, sink: Callable[[Any, bool, float], None] | None) -> None:
        """`sink(fit_or_None, is_blink, at)` per tracked frame, for the recorder.

        Not `PullWorker.set_sink` — that one is the camera's, for the frames.
        `at` is when the frame was pulled: these frames carry no camera
        timestamp, so it is the closest thing to an acquisition time there is.
        """
        self._fit_sink = sink

    def take_tracked(self) -> list[tuple[float, bool]]:
        """(radius, is_suspected_blink) since the last call, radius NaN where
        there was no fit. Drained, not sampled, so the plot keeps one point
        per frame."""
        with self._lock:
            out = list(self._radii)
            self._radii.clear()
        return out

    @property
    def available(self) -> bool:
        """False when tracking was asked for and EyeLoop is not there."""
        return self._tracking.available

    @property
    def track_error(self) -> str | None:
        """Why tracking is not running, or None.

        NOT named `error`: that is `PullWorker`'s Qt signal, and shadowing it
        breaks the guard that keeps an exception in `run()` from killing the
        process.
        """
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

            # _set_latest, not _publish: this worker has no PullWorker sink —
            # frames go through the camera's, fits through `_fit_sink`.
            self._set_latest(Tracked(frame, fit, self._tracking.last_mask,
                                     self._tracking.last_box))
            if st.track:
                with self._lock:
                    self._radii.append(
                        (fit.radius if fit is not None else float("nan"),
                         is_blink))
                sink = self._fit_sink       # snapshot: set_fit_sink may race us
                if sink is not None:
                    sink(fit, is_blink, at)
