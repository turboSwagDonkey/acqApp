"""
The encoder must be clocked by the DAQ, not by a Python sleep loop.

Speed is a SLOPE — `_derive` divides the change in position by dt — so every
millisecond of scheduler jitter in the old single-sample `task.read()` loop went
straight into the reported wheel speed, on a thread competing with a GUI that
paints two camera previews. The board has a timing engine; the samples should
come off it.

Hardware timing then creates the camera's problem (#1) in a new place: samples
arrive in blocks, so stamping them on arrival would quantise the wheel's
timebase to the read cadence. `EncoderWorker` anchors the first block into the
perf_counter domain and spaces the rest by the board's own rate.

Drives the real worker against a fake NI board that delivers correctly clocked
samples at deliberately irregular times. The CONTROL is the arrival times of the
very same samples — what the old path recorded.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_encoder_timing.py
"""
from __future__ import annotations

import random
import sys
import time
import types

import numpy as np

from _harness import Report, pump, qt_app

RATE = 200.0            # Hz asked for
COERCED = 200.0         # Hz the fake board settles on
VPR = 5.0
RUN_S = 1.5


# ── a fake NI board ───────────────────────────────────────────────────────────

class FakeAiChannels:
    def __init__(self) -> None:
        self.chan = None
        self.kw: dict = {}

    def add_ai_voltage_chan(self, chan, **kw):
        self.chan, self.kw = chan, kw


class FakeTiming:
    """`cfg_samp_clk_timing` + the rate the board coerced it to."""

    def __init__(self, fail: bool = False, coerced: float = COERCED) -> None:
        self.fail = fail
        self._coerced = coerced
        self.samp_clk_rate = 0.0
        self.cfg: dict | None = None

    def cfg_samp_clk_timing(self, rate, sample_mode=None, samps_per_chan=None):
        if self.fail:
            raise RuntimeError("Device does not support hardware timing")
        self.cfg = {"rate": rate, "sample_mode": sample_mode,
                    "samps_per_chan": samps_per_chan}
        self.samp_clk_rate = self._coerced


class FakeTask:
    """A board whose samples are perfectly clocked and irregularly collected.

    Samples become available on a virtual clock at `i / rate`; a read returns as
    soon as the last sample it wants exists, then sometimes dawdles — which is
    what a buffer is for, and what makes arrival time a bad timestamp.
    """

    instances: list["FakeTask"] = []
    fail_timing = False
    stall_p = 0.35              # chance a read is late
    stall_s = (0.01, 0.05)      # how late

    def __init__(self) -> None:
        self.ai_channels = FakeAiChannels()
        self.timing = FakeTiming(fail=FakeTask.fail_timing)
        self.started = False
        self.n = 0              # samples handed out
        self.t0 = 0.0
        self._rng = random.Random(20260812)
        FakeTask.instances.append(self)

    # context manager, as `with Task() as task:` needs
    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False

    def start(self) -> None:
        self.started = True
        self.t0 = time.perf_counter()

    def stop(self) -> None: self.started = False
    def close(self) -> None: self.started = False

    @staticmethod
    def value(i: int) -> float:
        """A descending sawtooth — one turn every 100 samples, like the rig."""
        return float(((-i / 100.0) % 1.0) * VPR)

    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        if number_of_samples_per_channel is None:       # on-demand (fallback)
            v = self.value(self.n)
            self.n += 1
            time.sleep(0.001)
            return v

        n = number_of_samples_per_channel
        rate = self.timing.samp_clk_rate or RATE
        due = self.t0 + (self.n + n - 1) / rate
        wait = due - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        if self._rng.random() < self.stall_p:           # the reader was late
            time.sleep(self._rng.uniform(*self.stall_s))
        out = [self.value(self.n + k) for k in range(n)]
        self.n += n
        return out


def install_fake_nidaqmx() -> None:
    """Put the fake board in front of the real driver for this process."""
    consts = types.ModuleType("nidaqmx.constants")
    consts.TerminalConfiguration = types.SimpleNamespace(RSE="RSE")
    consts.AcquisitionType = types.SimpleNamespace(CONTINUOUS="CONTINUOUS")
    mod = types.ModuleType("nidaqmx")
    mod.Task = FakeTask
    mod.constants = consts
    sys.modules["nidaqmx"] = mod
    sys.modules["nidaqmx.constants"] = consts


def collect(worker, app, seconds: float):
    """Run the worker, recording (arrival, sample) for every sink call."""
    rows: list[tuple[float, tuple]] = []
    worker.set_sink(lambda s: rows.append((time.perf_counter(), s)))
    worker.start()
    pump(app, seconds)
    worker.stop()
    return rows


def main() -> int:
    r = Report("encoder-timing")
    app = qt_app()
    install_fake_nidaqmx()
    from acqApp.devices.wheel.acquisition import EncoderWorker

    # ── the hardware-timed path ──────────────────────────────────────────────
    FakeTask.instances.clear()
    FakeTask.fail_timing = False
    w = EncoderWorker("Dev3/ai2", RATE, volts_per_rev=VPR, wheel_dia_mm=150.0)
    rows = collect(w, app, RUN_S)

    r.check(w.timestamp_source == "hardware",
            f"worker reports a hardware timebase (got {w.timestamp_source!r})")
    r.check(abs(w.actual_rate - COERCED) < 1e-9,
            f"actual_rate is what the BOARD settled on, not what was asked "
            f"({w.actual_rate} vs {RATE} requested)")

    task = FakeTask.instances[0]
    r.check(task.ai_channels.chan == "Dev3/ai2"
            and task.ai_channels.kw.get("terminal_config") == "RSE",
            f"the configured channel reached the board "
            f"({task.ai_channels.chan}, {task.ai_channels.kw})")
    cfg = task.timing.cfg
    r.check(cfg is not None and cfg["rate"] == RATE
            and cfg["sample_mode"] == "CONTINUOUS",
            f"sample clock configured continuously at {RATE:g} Hz (got {cfg})")
    block = max(1, int(round(RATE * EncoderWorker._BLOCK_S)))
    r.check(cfg is not None and cfg["samps_per_chan"] >= 2 * block,
            f"the input buffer holds more than one read "
            f"({cfg['samps_per_chan']} samples, block is {block})")

    if not r.check(len(rows) > 0.5 * RATE * RUN_S,
                   f"samples reached the sink ({len(rows)} in {RUN_S:g} s)"):
        return r.finish()
    r.note(f"{len(rows)} samples over {RUN_S:g} s at {COERCED:g} Hz, "
           f"blocks of {block}")

    # Every sample exactly once, in order: a block read must not drop, repeat
    # or reorder anything.
    volts = np.array([s[0] for _a, s in rows])
    want = np.array([FakeTask.value(i) for i in range(len(rows))])
    r.check(bool(np.allclose(volts, want)),
            "every sample delivered exactly once, in order")

    # ── the timestamps ───────────────────────────────────────────────────────
    at = np.array([s[3] for _a, s in rows])
    arrival = np.array([a for a, _s in rows])
    r.check(bool(np.all(np.isfinite(at))),
            "every sample carries an acquisition time for the recorder")

    d_at = np.diff(at)
    period = 1.0 / COERCED
    r.check(float(np.max(np.abs(d_at - period))) < 1e-9,
            f"recorded intervals are the board's, exactly: "
            f"max deviation {float(np.max(np.abs(d_at - period))) * 1e9:.1f} ns "
            f"from {period * 1e3:.3f} ms")
    r.check(bool(np.all(d_at > 0)), "no two samples share a timestamp")

    # CONTROL: the arrival times of these very same samples — what the old
    # software-paced loop recorded. If these were also regular, the check
    # above would be proving nothing about the code.
    d_ar = np.diff(arrival)
    r.check(float(np.std(d_ar)) > 20.0 * float(np.std(d_at)) + 1e-4,
            f"control: arrival intervals are irregular "
            f"(sd {np.std(d_ar) * 1e3:.2f} ms vs {np.std(d_at) * 1e3:.6f} ms "
            f"for the recorded ones)")
    r.check(float(np.max(d_ar)) > 3.0 * period,
            f"control: samples really do arrive in batches "
            f"(worst gap {float(np.max(d_ar)) * 1e3:.1f} ms, "
            f"one sample period is {period * 1e3:.1f} ms)")
    r.info(f"arrival jitter that no longer reaches the file: "
           f"±{float(np.max(np.abs(d_ar - period))) * 1e3:.1f} ms")

    # The anchor must put the stream in the right place, not just space it
    # evenly: a constant offset is acceptable (it is the first read's latency),
    # a drifting or wildly wrong one is not.
    off = float(np.median(arrival - at))
    r.check(0.0 <= off < 4.0 * EncoderWorker._BLOCK_S,
            f"the stream is anchored to real time: samples are stamped "
            f"{off * 1e3:.1f} ms before they arrived (one block is "
            f"{EncoderWorker._BLOCK_S * 1e3:.0f} ms)")
    r.check(float(np.max(at)) <= float(np.max(arrival)) + 1e-9,
            "no sample is stamped in the future")

    # ── speed: the point of the exercise ─────────────────────────────────────
    # The fake wheel turns at exactly rate/100 rev/s. Hardware spacing means the
    # derived speed should be that, to well under a percent.
    speed = np.array([s[1] for _a, s in rows])          # mm/s
    want_mm_s = (COERCED / 100.0) * np.pi * 150.0
    settled = speed[int(0.75 * len(speed)):]
    med = float(np.median(settled))
    r.check(abs(med - want_mm_s) < 0.01 * want_mm_s,
            f"derived speed {med:.1f} mm/s vs {want_mm_s:.1f} expected "
            f"(within 1 %)")

    # ── the fallback ─────────────────────────────────────────────────────────
    # A board with no timing engine must keep acquiring — losing the wheel for
    # a session is worse than a jittery timebase — but must say which it gave.
    FakeTask.instances.clear()
    FakeTask.fail_timing = True
    w2 = EncoderWorker("Dev3/ai2", 100.0, volts_per_rev=VPR, wheel_dia_mm=150.0)
    rows2 = collect(w2, app, 0.6)

    r.check(w2.timestamp_source == "software",
            f"a board that refuses hardware timing is reported as software-"
            f"timed (got {w2.timestamp_source!r})")
    r.check(len(rows2) > 10,
            f"and it keeps acquiring anyway ({len(rows2)} samples)")
    r.check(len(FakeTask.instances) == 2,
            f"the failed task is discarded and a fresh one opened for the "
            f"fallback ({len(FakeTask.instances)} tasks)")
    at2 = [s[3] for _a, s in rows2]
    r.check(all(a is not None for a in at2)
            and all(abs(a - b) < 0.05 for (b, _s), a in zip(rows2, at2)),
            "on the fallback the acquisition time IS the arrival time — the "
            "only honest answer without a device timebase")
    FakeTask.fail_timing = False

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
