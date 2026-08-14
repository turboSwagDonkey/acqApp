"""
Test for the camera frame-timestamp path.

The bug this guards: OrcaFireWorker reads frames in BATCHES, and every frame in
a batch used to be stamped when the batch arrived. At 115 fps that made the
recorded timebase a staircase — several frames sharing one timestamp, then a
jump — instead of an even train at the frame rate.

The mock camera delivers one frame at a time and so can never show this. This
drives the REAL worker against a fake camera that behaves the way DCAM does:
frames arrive in bursts, each carrying the camera's own timestamp in its own
arbitrary epoch, and one frame goes missing mid-run (as pylablib's default
`missing_frame="skip"` does).

It then re-runs the identical camera through the OLD arrival-stamping behaviour
as a control. Without that control, the test would pass even if the batching had
stopped happening for some unrelated reason, and would be proving nothing.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_camera_timestamps.py
"""
from __future__ import annotations

import collections
import sys
import time

import numpy as np

from _harness import Report, qt_app

from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
from acqApp.devices.voltage_cam.presets import AcqConfig
from acqApp.acq.clock import SessionClock

# Same shape as pylablib's DCAM.TFrameInfo.
FrameInfo = collections.namedtuple(
    "TFrameInfo",
    ["frame_index", "framestamp", "timestamp_us", "camerastamp",
     "position", "pixeltype"])

FPS = 115.0                 # CoaXPress full-frame rate
PERIOD = 1.0 / FPS
BATCH = 6                   # frames handed over per read
DROP_AFTER = 20             # skip one frame index here, like a real lost frame
CAM_EPOCH = 1_234_567.0     # camera clock epoch: arbitrary, unrelated to ours


class _Timings:
    frame_period = PERIOD


class _Status:
    def __init__(self, n):
        self.acquired, self.skipped, self.unread, self.buffer_size = n, 1, 0, 64


class FakeCam:
    """Just enough DCAMCamera to drive OrcaFireWorker: hands over frames in
    bursts, each stamped on the camera's own clock."""

    def __init__(self, shape):
        self._shape = shape
        self._idx = 0                       # camera's own frame counter
        self._t_us = CAM_EPOCH * 1e6        # camera timestamp, microseconds
        self.reads = 0

    # setup calls the worker makes — all no-ops here
    def set_roi(self, **kw): pass
    def set_exposure(self, s): pass
    def get_all_readout_speeds(self): return ["slow", "fast"]
    def get_readout_speed(self): return "fast"
    def set_readout_speed(self, s): pass
    def set_trigger_mode(self, m): pass
    def setup_ext_trigger(self): pass
    def get_frame_timings(self): return _Timings()
    def start_acquisition(self, nframes=None): pass
    def stop_acquisition(self): pass
    def close(self): pass
    def get_frames_status(self): return _Status(self._idx)
    def read_newest_image(self): return np.zeros(self._shape, dtype=np.uint16)

    def wait_for_frame(self, timeout=None):
        # A batch's worth of frames accumulates between reads — this interval is
        # exactly what the old code was (incorrectly) stamping frames with.
        time.sleep(BATCH * PERIOD)

    def read_multiple_images(self, return_info=False):
        self.reads += 1
        frames, infos = [], []
        for _ in range(BATCH):
            if self._idx == DROP_AFTER:     # one frame lost in the driver
                self._idx += 1
                self._t_us += PERIOD * 1e6
            frames.append(np.full(self._shape, self._idx % 1000, dtype=np.uint16))
            infos.append(FrameInfo(self._idx, self._idx, self._t_us, 0, (0, 0), 1))
            self._idx += 1
            self._t_us += PERIOD * 1e6
        return (frames, infos) if return_info else frames


def main() -> int:
    r = Report("camera-ts")
    qt_app()                                # the worker declares pyqtSignals

    cfg = AcqConfig(preset_key="4432x4", exposure_us=1000.0)
    clock = SessionClock()
    clock.start()
    n_target = BATCH * 8

    # ── the fix ──────────────────────────────────────────────────────────────
    cam = FakeCam(cfg.frame_shape)
    worker = OrcaFireWorker(0, cfg, cam=cam)
    got: list[tuple[float, int]] = []

    def sink(item):
        frame, at, index = item
        # Exactly what adapters.VoltageCamModule hands to Recorder.put(at=...).
        got.append((clock.now() if at is None else clock.at(at), index))
        if len(got) >= n_target:
            worker._stop = True

    worker.set_sink(sink)
    worker._run()                           # drive the loop directly, no QThread

    r.note(f"{len(got)} frames over {cam.reads} reads (batch size {BATCH})")
    r.check(len(got) >= n_target, "worker delivered every frame in each batch")
    r.check(cam.reads >= 2, "more than one batch was read (the bug's precondition)")
    r.check(worker.timestamp_source == "camera",
            f"worker reports the camera timebase (got {worker.timestamp_source!r})")

    ts = np.array([g[0] for g in got])
    idx = np.array([g[1] for g in got])

    # indices: contiguous except for the injected drop
    steps = np.diff(idx)
    r.check(bool(np.all(steps >= 1)), "frame indices never go backwards")
    r.check(int((steps == 2).sum()) == 1,
            f"the dropped frame shows as one index jump "
            f"(jumps={sorted(set(steps.tolist()))})")

    # timestamps: the frame rate, not the read cadence
    dt = np.diff(ts)
    normal = dt[steps == 1]                 # the drop legitimately doubles one
    r.check(bool(np.all(normal > 0)), "no two frames share a timestamp")
    r.check(abs(normal.mean() - PERIOD) < PERIOD * 0.02,
            f"mean interval {normal.mean()*1e3:.3f} ms ≈ frame period "
            f"{PERIOD*1e3:.3f} ms")
    r.check(normal.std() < PERIOD * 0.01,
            f"intervals are even (std {normal.std()*1e6:.1f} µs), not batched")
    gap = dt[steps == 2]
    r.check(len(gap) == 1 and abs(gap[0] - 2 * PERIOD) < PERIOD * 0.05,
            "the dropped frame leaves a double-length gap in the timestamps")
    zero_runs = int((dt < PERIOD * 0.1).sum())
    r.check(zero_runs == 0, "no batch-quantised (near-zero) intervals remain")

    # ── control: the OLD behaviour must fail those checks ────────────────────
    cam2 = FakeCam(cfg.frame_shape)
    w2 = OrcaFireWorker(0, cfg, cam=cam2)
    old: list[float] = []

    def old_sink(item):
        old.append(clock.now())             # arrival time, ignoring item[1]
        if len(old) >= n_target:
            w2._stop = True

    w2.set_sink(old_sink)
    w2._run()
    old_dt = np.diff(np.array(old))
    old_zero = int((old_dt < PERIOD * 0.1).sum())
    r.note(f"control (arrival-stamped): {old_zero} near-zero intervals, "
           f"std {old_dt.std()*1e3:.2f} ms  |  fixed: {zero_runs}, "
           f"std {normal.std()*1e3:.2f} ms")
    r.check(old_zero > n_target // 2,
            "control reproduces the batching bug (so the test can detect it)")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
