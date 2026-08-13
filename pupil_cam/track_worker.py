"""
Pupil tracking on its own thread.

Tracking is neither cheap nor bounded. `coarse_seed`'s distance transform runs
~100-200 ms on a degenerate mask (lens cap on, illumination off, or simply a
badly set threshold), and a genuinely lost pupil re-seeds on *every* frame. Run
from the 30 Hz display timer, as it used to be, that froze the whole GUI — the
voltage camera's preview pull shares that tick and has nothing to do with the
pupil, so a dark eye stalled the imaging preview too.

So the tracker gets a thread of its own. It is the **sole consumer** of the
camera worker's `get_latest()` (which hands each frame out once) and republishes
the frame together with its own result, so the outline the GUI paints always
belongs to the frame underneath it — pulling the two independently would let
them drift apart by a frame. Radii are queued separately, so the trace stays
complete even when the display tick is slower than the camera.

The tracker object itself stays pure numpy and single-threaded: it is touched
only from this thread, and parameter edits from the settings panel are queued
and applied between frames rather than written into it from the GUI thread.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

from acqApp.acq.worker import PullWorker
from acqApp.pupil_cam.tracking import PupilResult, PupilTracker

# Tracker options the settings panel owns. One list, so the panel, the adapter
# and the worker cannot drift apart about what is configurable.
TRACK_KEYS = ("threshold", "min_r", "max_r",
              "n_rays", "polarity", "min_strength", "fit")


def track_params(settings) -> dict[str, Any]:
    """The tracker kwargs carried by a `PupilSettings`."""
    return {k: getattr(settings, k) for k in TRACK_KEYS}


class PupilTrackWorker(PullWorker):
    """Runs a `PupilTracker` over whatever the camera worker last produced.

    `source` is a callable returning the newest frame or None — normally
    `PupilCameraWorker.get_latest`. Frames that arrive while a slow track is in
    progress are simply skipped: this drives a live preview, so the newest frame
    is the only one worth tracking.

    `get_latest()` returns `(frame, PupilResult)` or None; `take_radii()` drains
    the radius of every frame tracked since the last call.
    """

    _STOP_WAIT_MS = 3000
    _IDLE_SLEEP_S = 0.004        # shorter than a frame period at any usable fps

    def __init__(self, source: Callable[[], Any],
                 tracker: PupilTracker | None = None,
                 history: int = 600) -> None:
        super().__init__()
        self._source = source
        self.tracker = tracker or PupilTracker()
        self._cfg_lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._pending_seed: tuple[float, float, float] | None = None
        self._radii: deque[float] = deque(maxlen=history)
        self._n = 0

    # ── GUI side ─────────────────────────────────────────────────────────────
    def configure(self, **kw: Any) -> None:
        """Queue a tracker-parameter change, applied before the next frame.

        Queued rather than written straight into the tracker because the panel
        edits on the GUI thread while `process()` is running on this one — and
        `PupilTracker.configure` may invalidate the annulus lock mid-fit.
        """
        with self._cfg_lock:
            if self._pending is None:
                self._pending = {}
            self._pending.update(kw)

    def seed(self, cx: float, cy: float, r: float) -> None:
        """Place the annulus by hand — the LabVIEW operator workflow.

        For when the auto-seed latches onto the wrong dark region (a shadow, the
        lash line) and no threshold fixes it. Queued like `configure`, and for
        the same reason: `PupilTracker.seed` rewrites the annulus lock that
        `process()` may be reading on the tracking thread right now.
        """
        with self._cfg_lock:
            self._pending_seed = (cx, cy, r)

    def take_radii(self) -> list[float]:
        """Radii tracked since the last call (NaN where the pupil was lost).

        Drained rather than sampled so the trace keeps one point per *frame*
        even when the display tick runs slower than the camera.
        """
        with self._lock:
            out = list(self._radii)
            self._radii.clear()
        return out

    @property
    def frames_tracked(self) -> int:
        return self._n

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop:
            with self._cfg_lock:
                pending, self._pending = self._pending, None
                seed, self._pending_seed = self._pending_seed, None
            if pending:
                self.tracker.configure(**pending)
            # After configure: `configure` may reset the annulus lock, which
            # would throw away a seed applied before it.
            if seed is not None:
                self.tracker.seed(*seed)

            frame = self._source()
            if frame is None:
                time.sleep(self._IDLE_SLEEP_S)
                continue

            res: PupilResult = self.tracker.process(frame)
            self._n += 1
            self._publish((frame, res))
            with self._lock:
                self._radii.append(res.radius if res.radius is not None
                                   else float("nan"))
