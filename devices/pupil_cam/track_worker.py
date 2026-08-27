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

    `source` is normally `PupilCameraWorker.get_latest`. `get_latest()` gives a
    `Tracked`; `take_radii()` drains one radius per tracked frame.
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
        self._radii: deque[float] = deque(maxlen=history)
        self._fit_sink: Callable[[Any, float], None] | None = None
        self._seen = 0
        self._fits = 0

    # ── GUI side ─────────────────────────────────────────────────────────────
    def configure(self, settings: PupilSettings) -> None:
        """Adopt a settings edit, in force from the next frame."""
        self._settings = settings

    def set_fit_sink(self, sink: Callable[[Any, float], None] | None) -> None:
        """`sink(fit_or_None, at)` per tracked frame, for the recorder.

        Not `PullWorker.set_sink` — that one is the camera's, for the frames.
        `at` is when the frame was pulled: these frames carry no camera
        timestamp, so it is the closest thing to an acquisition time there is.
        """
        self._fit_sink = sink

    def take_radii(self) -> list[float]:
        """Radii since the last call, NaN where there was no fit. Drained, not
        sampled, so the plot keeps one point per frame."""
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

            # _set_latest, not _publish: this worker has no PullWorker sink —
            # frames go through the camera's, fits through `_fit_sink`.
            self._set_latest(Tracked(frame, fit, self._tracking.last_mask,
                                     self._tracking.last_box))
            if st.track:
                with self._lock:
                    self._radii.append(fit.radius if fit is not None
                                       else float("nan"))
                sink = self._fit_sink       # snapshot: set_fit_sink may race us
                if sink is not None:
                    sink(fit, at)
