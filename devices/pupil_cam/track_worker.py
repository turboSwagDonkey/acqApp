"""Pupil tracking on its own thread.

A fit is 1.2-2.4 ms on the rig clips, which fits inside a 33 ms display tick —
offline, single-threaded, with no Qt in the process. On the GUI thread it would
still be the wrong place for it: the pupil preview shares that tick with the
voltage camera's, so any frame that takes longer than expected (a lost pupil, a
degenerate mask, an eye region that just changed size and re-armed the tracker)
stalls the imaging preview too. This is the shape the retired tracker arrived
at for the same reason — see `archive/pupil_tracking/track_worker.py`.

This worker is the **sole consumer** of the camera's `get_latest()` and
republishes the frame *with* its fit, so the ellipse the GUI paints always
belongs to the frame underneath it. It is created whether or not tracking is
on: with `track=False` it is a pass-through, and one code path for the preview
is worth more than the thread it costs.

Frames arriving during a fit are dropped — this drives a live preview, and the
recording of the frames themselves goes through the camera's own sink and is
untouched by anything here. So the pupil trace has its own, sparser sample
times; `frames_seen` vs `fits` is what says how sparse.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from acqApp.acq.worker import PullWorker
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking import PupilTracking


@dataclass(frozen=True)
class Tracked:
    """One frame and everything the display needs to draw over it."""

    frame: np.ndarray
    fit: Any = None                              # PupilFit, or None
    mask: np.ndarray | None = None               # removed pixels, crop-sized
    box: tuple[int, int, int, int] | None = None  # the crop, in frame px


class PupilTrackWorker(PullWorker):
    """Runs `PupilTracking` over whatever the camera worker last produced.

    `source` returns the newest frame or None (normally
    `PupilCameraWorker.get_latest`).

    `get_latest()` -> `Tracked` or None; `take_radii()` drains the radius of
    every frame tracked since the last call, so the trace keeps one point per
    tracked frame even when the display tick is slower than the camera.
    """

    _STOP_WAIT_MS = 3000
    _IDLE_SLEEP_S = 0.004        # shorter than a frame period at any usable fps

    def __init__(self, source: Callable[[], Any], settings: PupilSettings,
                 history: int = 600) -> None:
        super().__init__()
        self._source = source
        self._tracking = PupilTracking()
        # Rebound wholesale from the GUI thread, never mutated in place: one
        # attribute store is atomic under the GIL, so a frame is tracked with
        # the settings as they were at its start or as they became, never with
        # half of each.
        self._settings = settings
        self._radii: deque[float] = deque(maxlen=history)
        self._fit_sink: Callable[[Any, float], None] | None = None
        self._seen = 0
        self._fits = 0

    # ── GUI side ─────────────────────────────────────────────────────────────
    def configure(self, settings: PupilSettings) -> None:
        """Adopt a settings edit, in force from the next frame."""
        self._settings = settings

    def set_fit_sink(self, sink: Callable[[Any, float], None] | None) -> None:
        """Route every tracked frame's fit to the recorder.

        Separate from `PullWorker.set_sink`, which the camera worker owns for
        the frames themselves. Called as `sink(fit_or_None, at)`, where `at` is
        a `perf_counter()` reading taken when the frame was pulled — the
        closest thing to an acquisition time available, since these frames
        carry no camera timestamp.
        """
        self._fit_sink = sink

    def take_radii(self) -> list[float]:
        """Radii tracked since the last call (NaN where there was no fit).
        Drained, not sampled, so the trace keeps one point per frame."""
        with self._lock:
            out = list(self._radii)
            self._radii.clear()
        return out

    @property
    def available(self) -> bool:
        """False only when tracking was asked for and EyeLoop is not there."""
        return self._tracking.available

    @property
    def track_error(self) -> str | None:
        """Why tracking is not running, or None.

        NOT `error`: `PullWorker.error` is the Qt signal that carries a crash
        out of the thread, and shadowing it with a property would break the
        one guard that keeps an exception in `run()` from taking the process
        down.
        """
        return self._tracking.error

    @property
    def frames_seen(self) -> int:
        return self._seen

    @property
    def fits(self) -> int:
        return self._fits

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

            # _set_latest, not _publish: the frames' own recording sink
            # belongs to the camera worker, and the fits go to `_fit_sink`
            # below. This worker has no PullWorker sink of its own.
            self._set_latest(Tracked(frame, fit, self._tracking.last_mask,
                                     self._tracking.last_box))
            if st.track:
                with self._lock:
                    self._radii.append(fit.radius if fit is not None
                                       else float("nan"))
                sink = self._fit_sink       # snapshot: set_fit_sink may race us
                if sink is not None:
                    sink(fit, at)
