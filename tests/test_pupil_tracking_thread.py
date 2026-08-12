"""
Pupil tracking must not run on the GUI thread.

Tracking is unbounded work: `coarse_seed`'s distance transform is ~100-200 ms on
a degenerate mask, and a lost pupil re-seeds on every frame. It used to run
inside the 30 Hz display tick, so a dark eye froze the whole window — including
the *voltage* camera's preview, which shares that tick.

This drives a real MockPupilCameraWorker through a real PupilTrackWorker and
checks the four things that make the move worth having:

  * the pupil is still found, and the result belongs to the frame it is
    published with (they must not drift apart by a frame);
  * the display-side calls stay fast while a deliberately slow tracker is
    grinding — with a CONTROL running the same tracker inline, which is what
    the old code did, to show the check isn't vacuous;
  * a panel edit reaches the tracker across the thread boundary and changes
    what it does;
  * the radius trace keeps one point per frame, not per display tick.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_tracking_thread.py
"""
from __future__ import annotations

import sys
import time

import numpy as np

from _harness import Report, pump, qt_app

SLOW_S = 0.15           # per-frame cost of the deliberately slow tracker
TICKS = 20              # display ticks to time against it


class SlowTracker:
    """A tracker that costs `SLOW_S` a frame — the degenerate-mask case."""

    def __init__(self) -> None:
        self.calls = 0

    def configure(self, **kw) -> None:
        pass

    def process(self, frame):
        self.calls += 1
        time.sleep(SLOW_S)
        from acqApp.pupil_cam.tracking import PupilResult
        return PupilResult(1.0, 2.0, 3.0, 1.0)


def main() -> int:
    r = Report("pupil-thread")
    app = qt_app()      # the workers are real QThreads

    from acqApp.pupil_cam.acquisition import MockPupilCameraWorker
    from acqApp.pupil_cam.track_worker import PupilTrackWorker, track_params
    from acqApp.pupil_cam.settings import PupilSettings

    # ── the settings → tracker mapping ───────────────────────────────────────
    p = track_params(PupilSettings(threshold=42, min_r=7, max_r=99, n_rays=32,
                                   polarity="falling", min_strength=2.5,
                                   fit="ellipse"))
    r.check(p == {"threshold": 42, "min_r": 7, "max_r": 99, "n_rays": 32,
                  "polarity": "falling", "min_strength": 2.5, "fit": "ellipse"},
            f"track_params carries every panel-owned option (got {p})")
    from acqApp.pupil_cam.tracking import PupilTracker
    r.check(all(hasattr(PupilTracker(), k) for k in p),
            "every track_params key is a real PupilTracker option")

    # ── 1. it still tracks ───────────────────────────────────────────────────
    cam = MockPupilCameraWorker(fps=60)
    trk = PupilTrackWorker(cam.get_latest, history=600)
    cam.start()
    trk.start()
    pump(app, 1.5)

    r.check(trk.frames_tracked > 10,
            f"tracker ran on its own thread ({trk.frames_tracked} frames)")
    pair = trk.get_latest()
    if not r.check(pair is not None, "a (frame, result) pair reached the GUI side"):
        return r.finish()
    frame, res = pair
    r.check(isinstance(frame, np.ndarray) and frame.shape == (cam.H, cam.W),
            f"the pair carries the frame itself (shape {getattr(frame, 'shape', None)})")
    r.check(res.found, f"pupil found (r={res.radius}, conf={res.confidence:.2f})")

    # The mock's disc oscillates 20-50 px about the frame centre. A result that
    # belongs to a *different* frame would still be near the centre, so check
    # the stronger thing: the fitted centre lands on a dark pixel of THIS frame.
    if res.found:
        cx, cy = int(round(res.center_x)), int(round(res.center_y))
        r.check(frame[cy, cx] < 60,
                f"the fit's centre is inside this frame's pupil "
                f"(pixel value {frame[cy, cx]})")
        r.check(20.0 <= res.radius <= 50.0,
                f"radius within the mock's dilation range (got {res.radius:.1f})")

    # ── 2. the radius trace is per-frame, not per-tick ───────────────────────
    trk.take_radii()                    # drain what the first 1.5 s queued
    r.check(trk.take_radii() == [], "take_radii drains the queue")
    n0 = trk.frames_tracked
    pump(app, 0.5)
    radii = trk.take_radii()
    n1 = trk.frames_tracked
    r.check(abs(len(radii) - (n1 - n0)) <= 1,
            f"one radius queued per tracked frame ({len(radii)} for "
            f"{n1 - n0} frames)")

    # ── 3. a panel edit crosses the thread boundary ──────────────────────────
    # The mock pupil is grey level 20 on a 180 background. Threshold 5 puts the
    # seed mask below every pixel, so the pupil becomes unfindable — a change
    # only a tracker that actually received the edit could make.
    trk.configure(threshold=5)
    pump(app, 0.6)
    lost = [v for v in trk.take_radii()]
    r.check(len(lost) > 0 and all(np.isnan(v) for v in lost[-5:]),
            f"threshold edit reached the tracker thread (last radii {lost[-5:]})")
    r.check(trk.tracker.threshold == 5, "the queued edit landed on the tracker")
    trk.configure(threshold=60)
    pump(app, 0.6)
    back = trk.take_radii()
    r.check(any(not np.isnan(v) for v in back),
            "and it recovers when the threshold is put back")

    trk.stop()
    cam.stop()
    r.check(not trk.isRunning() and not cam.isRunning(), "both threads joined")

    # ── 4. the display side does not wait for tracking ───────────────────────
    slow = SlowTracker()
    cam2 = MockPupilCameraWorker(fps=30)
    trk2 = PupilTrackWorker(cam2.get_latest, tracker=slow)
    cam2.start()
    trk2.start()
    pump(app, 0.3)                      # make sure it is inside a slow process()

    t0 = time.perf_counter()
    for _ in range(TICKS):
        trk2.get_latest()
        trk2.take_radii()
    dt = time.perf_counter() - t0
    r.check(dt < 0.02,
            f"{TICKS} display-side reads took {dt * 1e3:.2f} ms while a "
            f"{SLOW_S * 1e3:.0f} ms/frame tracker was running")
    r.check(slow.calls > 0, f"the slow tracker really was running ({slow.calls} frames)")

    trk2.stop()
    cam2.stop()

    # CONTROL: the same tracker called inline, which is what update_display()
    # used to do. If this were also fast, the check above would prove nothing.
    ctl_frame = np.full((240, 320), 180, dtype=np.uint8)
    t0 = time.perf_counter()
    for _ in range(3):
        slow.process(ctl_frame)
    inline = (time.perf_counter() - t0) / 3
    r.check(inline >= SLOW_S,
            f"control: tracking inline costs {inline * 1e3:.0f} ms per tick "
            f"— which is what the GUI thread used to pay")
    r.info(f"display-side read is {inline / max(dt / TICKS, 1e-9):.0f}x cheaper "
           f"than the inline call it replaced")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
