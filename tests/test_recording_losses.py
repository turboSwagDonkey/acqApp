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
import threading
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


def check_offered_never_blocks(r: Report) -> None:
    """`offered()` is read from the GUI thread; it must not wait on the gate.

    An experiment routine measures a "100 frames" step by this count, reading it
    ~70×/s (its own tick plus the display tick) while every device worker is
    enqueueing through `put()`. Taking the gate to read one int bought a count
    that never leads the buffer — which no caller can tell apart — and cost a
    stall behind the producers: 6.1 ms mean, 28.7 ms worst against a saturating
    one, against 1.3 us unlocked.

    Deterministic, not a timing measurement: hold the gate from another thread
    and require the read to come back anyway. A locked read hangs here.
    """
    rec, _w, clock = new_recorder()
    clock.start()
    rec.start(Path("unused.h5"), {})
    for i in range(7):
        rec.put("wheel", float(i))
    r.check(rec.offered("wheel") == 7,
            f"offered() counts what was handed over (got {rec.offered('wheel')})")
    r.check(rec.offered("nothing_here") == 0,
            "…and 0 for a stream nothing has written")

    got: list = []
    held = threading.Event()

    def reader() -> None:
        held.wait(2.0)
        got.append(rec.offered("wheel"))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    with rec._gate:                     # the lock every put() takes
        held.set()
        th.join(timeout=1.0)
    r.check(not th.is_alive() and got == [7],
            f"offered() returns while the enqueue gate is held (got {got})")

    # CONTROL: the same read WITH the gate is what would hang — so the check
    # above is not passing for free.
    stuck: list = []
    go = threading.Event()

    def locked_reader() -> None:
        go.wait(2.0)
        with rec._gate:
            stuck.append(rec.offered("wheel"))

    th2 = threading.Thread(target=locked_reader, daemon=True)
    th2.start()
    with rec._gate:
        go.set()
        th2.join(timeout=0.3)
        blocked = th2.is_alive()
    th2.join(timeout=1.0)
    r.check(blocked, "control: a read that does take the gate blocks there")

    rec.stop()


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


# ── the diagnostics themselves (2026-08-17) ──────────────────────────────────
# A loss that is reported with the WRONG CAUSE costs as much as a silent one:
# it sends the next session after the wrong fix. These three all misreported.

def check_skip_report_blames_the_loop(r: Report) -> None:
    """A camera skip is a read-loop shortfall, never the writer's.

    The sink only enqueues (Recorder.put -> ring, no disk I/O), so a slow
    writer sheds in the ring and is counted there instead.
    """
    from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker

    class St:
        skipped, unread, buffer_size = 143, 38, 38

    msg = OrcaFireWorker._skip_report(St())
    r.check("143" in msg and "38/38" in msg,
            "the skip report carries the counts")
    r.check("writer cannot keep up" not in msg,
            "it no longer blames the writer for a driver-buffer overflow")
    r.check("read loop" in msg,
            f"it names the read loop as the cause (got: {msg[:60]}...)")
    # Control: it must still mention the writer, to say it is NOT this count —
    # a message that simply deleted the word would also pass the check above.
    r.check("WRITER" in msg,
            "it still distinguishes the writer's separate count")


def check_memory_capped_buffer_is_announced(r: Report) -> None:
    """The 2 s of slack the constant promises silently becomes 0.33 s at full
    frame. Announcing which bound won is the whole fix."""
    import io
    from contextlib import redirect_stdout
    from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
    from acqApp.devices.voltage_cam.presets import AcqConfig

    w = OrcaFireWorker(0, AcqConfig())

    def sizing(cfg, fps):
        buf = io.StringIO()
        with redirect_stdout(buf):
            n = w._buffer_frames(cfg, fps)
        return n, buf.getvalue()

    big = AcqConfig()                                   # full frame, 21 MB
    n_big, out_big = sizing(big, 115.0)
    r.check(n_big < 115.0 * OrcaFireWorker._BUFFER_SECONDS,
            f"full frame really is memory-capped ({n_big} frames)")
    r.check("MEMORY-capped" in out_big,
            "and the shortfall is announced, not left in the arithmetic")
    r.check("GiB" in out_big,
            "the announcement says what the full slack would cost")

    # Control: a small frame is NOT capped, and must stay quiet — otherwise the
    # check above would pass on a warning that always fires.
    small = AcqConfig(preset_key="4432x512", binning=4)  # ~0.28 MB
    n_small, out_small = sizing(small, 115.0)
    r.check(n_small == int(115.0 * OrcaFireWorker._BUFFER_SECONDS),
            f"a small frame gets the full {OrcaFireWorker._BUFFER_SECONDS} s "
            f"({n_small} frames)")
    r.check("MEMORY-capped" not in out_small,
            "and stays quiet — the warning is not unconditional")


def check_readout_speed_absence_is_reported(r: Report) -> None:
    """`get_all_readout_speeds() == []` on this model, so the 'fast' path never
    ran and silently looked like it had."""
    from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker

    class NoSpeeds:
        def get_all_readout_speeds(self): return []
        def get_readout_speed(self): return 1

    class HasSpeeds:
        def __init__(self): self.set_to = None
        def get_all_readout_speeds(self): return ["slow", "fast"]
        def get_readout_speed(self): return "slow"
        def set_readout_speed(self, v): self.set_to = v

    class Broken:
        def get_all_readout_speeds(self): raise RuntimeError("no such property")
        def get_readout_speed(self): raise RuntimeError("no such property")

    r.check(OrcaFireWorker._maximise_readout_speed(NoSpeeds()) == "absent",
            "a camera with no selectable speeds reports 'absent', not success")
    # Control: where the control DOES exist it must still be used, or the fix
    # would just be a way of never setting the speed.
    cam = HasSpeeds()
    r.check(OrcaFireWorker._maximise_readout_speed(cam) == "set"
            and cam.set_to == "fast",
            "a camera that offers 'fast' is still switched to it")
    r.check(OrcaFireWorker._maximise_readout_speed(Broken()) == "error",
            "a camera that raises reports 'error', and does not propagate")


def main() -> int:
    r = Report("losses")
    qt_app()                            # the camera worker declares pyqtSignals
    check_count_cap(r)
    check_late_samples(r)
    check_unstamped(r)
    check_drops_counted(r)
    check_offered_never_blocks(r)
    check_no_hot_spin(r)
    check_skip_report_blames_the_loop(r)
    check_memory_capped_buffer_is_announced(r)
    check_readout_speed_absence_is_reported(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
