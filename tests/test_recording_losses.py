"""
Every way a sample can fail to reach the file (audit #14, #10, #8).

Three separate blind spots in the recording path, all of which lose data
quietly — which is the part that matters, because a recording that says
nothing went wrong is trusted:

  #14 The ring buffer's COUNT cap dropped the oldest item outright, so it
      discarded exactly the zero-byte event samples (puffs, DMD frames) that
      the byte cap goes out of its way to protect. 512 items is about a second
      of writer stall, so the count cap is the one that bites first.

  #10 Detaching the sinks does not stop a worker already inside its callback:
      it captured the Recorder, so its put() can land after the file closed.
      Those returned silently, making `recorder_dropped_samples` an undercount
      exactly when a run was in trouble.

  #8  A failing `wait_for_frame` hit `except Exception: continue` with no pause
      and no message — a camera that stops delivering spins a core silently for
      the rest of the session.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_recording_losses.py
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

from _harness import Report, qt_app

from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import Writer


def frame(i: int):
    """A sized sample, shaped like the real (stream, ts, data) tuple."""
    return ("voltage_cam", float(i), np.zeros((64, 64), dtype=np.uint16))


def event(i: int):
    """A zero-byte sample: a puff, a DMD frame — sparse and irreplaceable."""
    return ("puffer", float(i), 0.1)


def sizeof(item) -> int:
    data = item[2]
    return int(data.nbytes) if isinstance(data, np.ndarray) else 0


# ── #14 ───────────────────────────────────────────────────────────────────────

def check_count_cap(r: Report) -> None:
    buf = RingBuffer(maxlen=4, maxbytes=None, sizeof=sizeof)
    buf.put(event(0))
    for i in range(1, 11):
        buf.put(frame(i))

    kept = [buf.get_nowait() for _ in range(len(buf))]
    streams = [k[0] for k in kept]
    r.check("puffer" in streams,
            f"the event survived 10 frames of overflow (kept {streams})")
    r.check(len(kept) == 4, f"count cap held at maxlen (kept {len(kept)})")
    r.check(buf.drop_count == 7, f"every eviction counted (got {buf.drop_count})")

    # Control: the old policy on the identical sequence. Without this the test
    # would keep passing if events simply stopped reaching the buffer.
    old: deque = deque()
    old_drops = 0
    for item in [event(0)] + [frame(i) for i in range(1, 11)]:
        old.append(item)
        while len(old) > 4 and len(old) > 1:
            old.popleft()
            old_drops += 1
    r.check("puffer" not in [o[0] for o in old],
            "control: the old drop-oldest rule loses the event")

    # A backlog of nothing but events still has to be bounded.
    buf2 = RingBuffer(maxlen=3, maxbytes=None, sizeof=sizeof)
    for i in range(5):
        buf2.put(event(i))
    kept2 = [buf2.get_nowait() for _ in range(len(buf2))]
    r.check(len(kept2) == 3 and buf2.drop_count == 2,
            "with only events buffered, the oldest events are shed")
    r.check([k[1] for k in kept2] == [2.0, 3.0, 4.0],
            f"and it is the OLDEST that go (kept {[k[1] for k in kept2]})")

    # The byte cap must still shed frames and spare events.
    buf3 = RingBuffer(maxlen=1000, maxbytes=3 * 64 * 64 * 2, sizeof=sizeof)
    buf3.put(event(0))
    for i in range(1, 9):
        buf3.put(frame(i))
    kept3 = [buf3.get_nowait() for _ in range(len(buf3))]
    r.check("puffer" in [k[0] for k in kept3],
            "byte cap still spares the event")
    r.check(sum(sizeof(k) for k in kept3) <= 3 * 64 * 64 * 2,
            "byte cap still bounds the buffered payload")


# ── #10 ───────────────────────────────────────────────────────────────────────

class SpyWriter(Writer):
    """Records what happened to it, in order."""

    def __init__(self) -> None:
        self.events: list = []
        self.written: list = []
        self.meta: dict = {}

    def open(self, path: Path, metadata: dict) -> None:
        self.events.append("open")
        self.meta.update(metadata)

    def write(self, stream: str, timestamp: float, data) -> None:
        self.written.append((stream, timestamp, data))

    def update_metadata(self, metadata: dict) -> None:
        self.events.append(("meta", dict(metadata)))
        self.meta.update(metadata)

    def close(self) -> None:
        self.events.append("close")


def new_recorder(clock=None, maxlen=512):
    clock = clock or SessionClock()
    w = SpyWriter()
    return Recorder(clock, w, RingBuffer(maxlen, sizeof=sizeof)), w, clock


def check_late_samples(r: Report) -> None:
    rec, w, clock = new_recorder()
    clock.start()
    rec.start(Path("unused.h5"), {"subject": "m17"})
    for i in range(5):
        rec.put("wheel", float(i))

    seen: dict = {}

    def final() -> dict:
        # Called after the drain, before close — so it can report final counts.
        seen["at_call"] = list(w.events)
        return {"drops": rec.drop_count, "late": rec.late_count}

    remaining = rec.stop(final_metadata=final)
    r.check(remaining == 0, f"clean drain (remaining {remaining})")
    r.check(len(w.written) == 5, f"all 5 samples written (got {len(w.written)})")
    r.check("close" not in seen.get("at_call", ["close"]),
            "final metadata is gathered while the file is still open")
    r.check(w.events[-2:] == [("meta", {"drops": 0, "late": 0}), "close"],
            f"metadata written immediately before close (got {w.events[-2:]})")

    # A worker still inside its callback when the sinks were detached.
    for _ in range(3):
        rec.put("wheel", 99.0)
    r.check(rec.late_count == 3, f"late samples counted (got {rec.late_count})")
    r.check(len(w.written) == 5, "and none of them reached the closed writer")

    # Un-drained samples must not ALSO be counted as late: each lost sample
    # belongs to exactly one bucket, or the total is meaningless.
    r.check(rec.drop_count == 0 and rec.unstamped_count == 0,
            "the other counters stayed at zero")


def check_unstamped(r: Report) -> None:
    """A sample offered before the clock started has no timebase to land on."""
    rec, w, clock = new_recorder()                 # clock NOT started
    rec.start(Path("unused.h5"), {})
    rec.put("wheel", 1.0)
    rec.put("wheel", 2.0)
    r.check(rec.unstamped_count == 2,
            f"pre-start samples counted (got {rec.unstamped_count})")
    r.check(rec.late_count == 0, "not miscounted as late")
    clock.start()
    rec.put("wheel", 3.0)
    rec.stop()
    r.check(len(w.written) == 1,
            f"only the stamped sample was written (got {len(w.written)})")


def check_drops_counted(r: Report) -> None:
    """An overflowing buffer must show up in drop_count, not vanish."""
    rec, w, clock = new_recorder(maxlen=4)
    clock.start()
    # No start() → no writer thread draining, so the buffer is forced to shed.
    for i in range(20):
        rec.put("voltage_cam", np.zeros((8, 8), dtype=np.uint16))
    r.check(rec.drop_count == 16,
            f"shed samples are counted (got {rec.drop_count})")


# ── #8 ────────────────────────────────────────────────────────────────────────

def check_no_hot_spin(r: Report) -> None:
    from test_camera_timestamps import FakeCam
    from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
    from acqApp.devices.voltage_cam.presets import AcqConfig

    RUN_S = 1.0

    class BrokenCam(FakeCam):
        """A camera whose link has gone: every wait fails instantly."""

        def __init__(self, shape):
            super().__init__(shape)
            self.waits = 0
            self.t0 = 0.0           # set on the first wait: the worker's setup
            self.deadline = 0.0     # (pylablib import, ROI, timings) is not free
            self.worker = None

        def wait_for_frame(self, timeout=None):
            if not self.waits:
                self.t0 = time.perf_counter()
                self.deadline = self.t0 + RUN_S
            self.waits += 1
            if time.perf_counter() >= self.deadline:
                self.worker._stop = True
            raise RuntimeError("link down")

    # Smallest offered preset, binned hard — BrokenCam never yields a frame, so
    # the shape only has to be cheap.
    cfg = AcqConfig(preset_key="4432x512", binning=4, exposure_us=1000.0)
    cam = BrokenCam(cfg.frame_shape)
    worker = OrcaFireWorker(0, cfg, cam=cam)
    cam.worker = worker

    worker._run()                       # must return, not raise, not spin
    dt = time.perf_counter() - cam.t0   # time spent in the retry loop only

    # How fast the loop would turn with no pause at all, for scale.
    t1 = time.perf_counter()
    unpaced = 0
    while time.perf_counter() - t1 < 0.05:
        try:
            raise RuntimeError("link down")
        except RuntimeError:
            unpaced += 1
    unpaced_rate = unpaced / 0.05

    r.info(f"{cam.waits} retries in {dt:.2f} s "
           f"({cam.waits / dt:.0f}/s); unpaced would be ~{unpaced_rate:,.0f}/s")
    r.check(cam.waits > 1, "the worker kept retrying (a late trigger is normal)")
    r.check(cam.waits / dt < 100,
            f"retries are paced, not spinning ({cam.waits / dt:.0f}/s)")
    r.check(dt >= RUN_S, f"the loop ran for the full window ({dt:.2f} s)")


def main() -> int:
    r = Report("losses")
    qt_app()                            # the camera worker declares pyqtSignals
    check_count_cap(r)
    check_late_samples(r)
    check_unstamped(r)
    check_drops_counted(r)
    check_no_hot_spin(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
