"""Wheel encoder: position -> speed/distance, and hardware-timed reads.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_encoder.py [-q] [--part NAME]
"""
from __future__ import annotations

import random
import sys
import time
import types

import numpy as np
from _harness import Report, pump, qt_app, run_parts
from acqApp.devices.wheel.acquisition import _EncoderBase


# ═══ derive (was test_encoder_derive.py) ════════════════════════════════

VPR = 4.912                 # volts per revolution
DIA = 150.0                 # wheel diameter, mm
CIRC = np.pi * DIA          # mm per revolution
RATE = 120.0                # samples/s, the rig's encoder rate
REV_S = 1.5                 # simulated wheel speed, rev/s
SMEAR = 3                   # samples the reset smears across


def sim(seconds: float, rev_s: float = REV_S, smear: int = SMEAR,
        noise: float = 0.0, seed: int = 3):
    """(times, voltages) for a wheel turning at `rev_s`, resets smeared.

    A rising ramp: forward running reads positive on this rig (operator,
    2026-08-19); an earlier falling-ramp fixture was wrong about the hardware.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    frac = (rev_s * t) % 1.0
    v = frac * VPR

    # Crossing the dead zone, the output passes through in-between values:
    # sub-steps of ~1/smear turn, each implying tens of rev/s (what _derive
    # rejects) and each under the half-turn a plain unwrap sees. The wrap is
    # DOWNWARD; flip the ramp without flipping this and nothing gets smeared.
    jump = np.nonzero(np.diff(frac) < -0.5)[0] + 1       # sample after each wrap
    for j in jump:
        if smear <= 0 or j + smear >= n:
            continue
        v[j:j + smear] = np.linspace(v[j - 1], v[j + smear], smear + 2)[1:-1]
    if noise:
        v = v + rng.normal(0.0, noise, n)
    return t, np.clip(v, 0.0, VPR)


def naive_unwrap(v: np.ndarray) -> float:
    """Control: the obvious implementation — wrap-correct each step, but keep
    every one (no reset rejection)."""
    frac = v / VPR
    step = np.diff(frac)
    step = np.where(step > 0.5, step - 1.0, np.where(step < -0.5, step + 1.0, step))
    return float(-step.sum())          # _SIGN, so forward reads positive


def raw_sum(v: np.ndarray) -> float:
    """Control: no wrap correction at all — the sawtooth in its purest form."""
    return float(-np.diff(v / VPR).sum())


def run(t, v, vpr=VPR, dia=DIA):
    """Feed a whole trace through one _EncoderBase → (speeds, distances)."""
    enc = _EncoderBase(volts_per_rev=vpr, wheel_dia_mm=dia)
    sp = np.empty(t.size)
    di = np.empty(t.size)
    for i in range(t.size):
        sp[i], di[i] = enc._derive(float(v[i]), float(t[i]))
    return sp, di


def _part_derive() -> int:
    r = Report("encoder-derive")

    # ── a wheel turning steadily, resets smeared ─────────────────────────────
    t, v = sim(6.0)
    sp, di = run(t, v)
    r.note(f"{t.size} samples at {RATE:g} Hz, {REV_S * 6:g} revolutions, "
           f"each reset smeared over {SMEAR} samples "
           f"({np.count_nonzero(np.diff(v / VPR) > 0.5)} steps left big enough "
           f"for a half-turn unwrap to notice)")

    # Speed is for a sample _LAG_S in the past; judge it once the buffer fills.
    settled = t > 2.0
    want = REV_S * CIRC
    med = float(np.median(sp[settled]))
    r.check(abs(med - want) < 0.02 * want,
            f"speed: {med:.1f} mm/s, expected {want:.1f} (within 2 %)")
    r.check(float(np.max(np.abs(sp[settled] - want))) < 0.15 * want,
            f"speed: no reset spikes — worst sample is "
            f"{float(np.max(np.abs(sp[settled] - want))) / want * 100:.1f} % off")

    # Distance is what decays over a long run: check its slope.
    i3 = int(3.0 * RATE)
    i5 = int(5.0 * RATE)
    slope = (di[i5] - di[i3]) / (t[i5] - t[i3])
    r.check(abs(slope - want) < 0.03 * want,
            f"distance: accumulates at {slope:.1f} mm/s over 2 s, "
            f"expected {want:.1f}")
    r.check(bool(np.all(np.diff(di[settled]) >= -1e-9)),
            "distance: never goes backwards while the wheel goes forwards")
    r.check(di[-1] > 3.0 * CIRC,
            f"distance: {di[-1]:.0f} mm — more than the 3 revolutions the "
            f"sawtooth failure would have capped it at")

    # CONTROLS — the two obvious implementations, on the same voltages.
    true_rev = REV_S * 6.0
    naive = naive_unwrap(v)
    raw = raw_sum(v)
    r.check(abs(naive - true_rev) > 0.5,
            f"control: a plain half-turn unwrap reports {naive:.2f} rev of "
            f"{true_rev:.1f} — each smeared reset is sub-steps too small for it "
            f"to see, so it loses the whole revolution")
    r.check(abs(raw - true_rev) > 0.5,
            f"control: no wrap correction at all gives {raw:.2f} rev, not "
            f"{true_rev:.1f}")
    r.check(abs(slope / CIRC - naive / 6.0) > 0.5,
            f"_derive keeps {slope / CIRC:.2f} rev/s where the unwrap control "
            f"averages {naive / 6.0:.2f} — it is not merely agreeing with it")

    # ── a clean reset must still be counted ──────────────────────────────────
    # Rejection is on speed: a good sensor's single-sample wrap must survive.
    t, v = sim(6.0, smear=0)
    _, di_clean = run(t, v)
    r.check(abs(di_clean[-1] - di[-1]) < 0.15 * di[-1],
            f"a clean single-sample reset gives the same distance "
            f"({di_clean[-1]:.0f} vs {di[-1]:.0f} mm)")

    # ── live speed: current (no lag), accurate, and follows a stop at once ───
    enc = _EncoderBase(volts_per_rev=VPR, wheel_dia_mm=DIA)
    t, v = sim(3.0)
    for ti, vi in zip(t, v):
        enc._derive(float(vi), float(ti))
    live = enc._live_speed()
    r.check(abs(live - want) < 0.03 * want,
            f"live speed {live:.1f} mm/s, expected {want:.1f} (within 3 %)")
    for k in range(int(0.6 * RATE)):               # wheel stops dead
        enc._derive(float(v[-1]), float(t[-1] + (k + 1) / RATE))
    r.check(enc._live_speed() == 0.0,
            f"live speed reads zero 0.6 s after a stop ({enc._live_speed():.1f})")

    # ── stationary: the deadband must stop noise from integrating ────────────
    n = int(3.0 * RATE)
    ts = np.arange(n) / RATE
    rng = np.random.default_rng(7)
    vs = np.full(n, 0.5 * VPR) + rng.normal(0.0, 0.004, n)      # ~1 mV of ADC noise
    sp_s, di_s = run(ts, vs)
    r.check(float(np.max(np.abs(sp_s))) == 0.0,
            f"stationary: speed reads exactly zero "
            f"(worst {float(np.max(np.abs(sp_s))):.4f} mm/s)")
    r.check(abs(di_s[-1]) < 1.0,
            f"stationary: distance does not random-walk ({di_s[-1]:.4f} mm "
            f"over 3 s of noise)")

    # ── unscaled: no V/rev configured ────────────────────────────────────────
    enc = _EncoderBase(volts_per_rev=None, wheel_dia_mm=DIA)
    out = enc._derive(2.75, 0.1)
    r.check(out == (2.75, 0.0),
            f"with no V/rev the raw voltage is passed through as 'speed' "
            f"(got {out})")
    t, v = sim(4.0)
    sp_r, di_r = run(t, v, dia=None)
    r.check(abs(float(np.median(sp_r[t > 2.0])) - REV_S) < 0.05,
            f"with no wheel diameter the readout is rev/s "
            f"(got {float(np.median(sp_r[t > 2.0])):.3f}, expected {REV_S})")

    # ── live rescaling ───────────────────────────────────────────────────────
    enc = _EncoderBase(volts_per_rev=VPR, wheel_dia_mm=DIA)
    enc.set_scaling(None, DIA)
    r.check(enc._derive(1.25, 0.0) == (1.25, 0.0),
            "set_scaling(None, ...) takes effect on the next sample")

    return r.finish()


# ═══ timing (was test_encoder_timing.py) ════════════════════════════════

T_RATE = 200.0          # Hz asked for
COERCED = 200.0         # Hz the fake board settles on
T_VPR = 5.0
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
    """Samples perfectly clocked at `i / rate`, collected irregularly: a read
    returns once its last sample exists, then sometimes dawdles."""

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

    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False

    def start(self) -> None:
        self.started = True
        self.t0 = time.perf_counter()

    def stop(self) -> None: self.started = False
    def close(self) -> None: self.started = False

    @staticmethod
    def value(i: int) -> float:
        """A rising sawtooth — one turn every 100 samples, like the rig.
        Forward rotation ramps the voltage UP here (operator, 2026-08-19)."""
        return float(((i / 100.0) % 1.0) * T_VPR)

    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        if number_of_samples_per_channel is None:       # on-demand (fallback)
            v = self.value(self.n)
            self.n += 1
            time.sleep(0.001)
            return v

        n = number_of_samples_per_channel
        rate = self.timing.samp_clk_rate or T_RATE
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


def _part_timing() -> int:
    r = Report("encoder-timing")
    app = qt_app()
    install_fake_nidaqmx()
    from acqApp.devices.wheel.acquisition import EncoderWorker

    # ── the hardware-timed path ──────────────────────────────────────────────
    FakeTask.instances.clear()
    FakeTask.fail_timing = False
    w = EncoderWorker("Dev3/ai2", T_RATE, volts_per_rev=T_VPR, wheel_dia_mm=150.0)
    rows = collect(w, app, RUN_S)

    r.check(w.timestamp_source == "hardware",
            f"worker reports a hardware timebase (got {w.timestamp_source!r})")
    r.check(abs(w.actual_rate - COERCED) < 1e-9,
            f"actual_rate is what the BOARD settled on, not what was asked "
            f"({w.actual_rate} vs {T_RATE} requested)")

    task = FakeTask.instances[0]
    r.check(task.ai_channels.chan == "Dev3/ai2"
            and task.ai_channels.kw.get("terminal_config") == "RSE",
            f"the configured channel reached the board "
            f"({task.ai_channels.chan}, {task.ai_channels.kw})")
    cfg = task.timing.cfg
    r.check(cfg is not None and cfg["rate"] == T_RATE
            and cfg["sample_mode"] == "CONTINUOUS",
            f"sample clock configured continuously at {T_RATE:g} Hz (got {cfg})")
    block = max(1, int(round(T_RATE * EncoderWorker._BLOCK_S)))
    r.check(cfg is not None and cfg["samps_per_chan"] >= 2 * block,
            f"the input buffer holds more than one read "
            f"({cfg['samps_per_chan']} samples, block is {block})")

    if not r.check(len(rows) > 0.5 * T_RATE * RUN_S,
                   f"samples reached the sink ({len(rows)} in {RUN_S:g} s)"):
        return r.finish()
    r.note(f"{len(rows)} samples over {RUN_S:g} s at {COERCED:g} Hz, "
           f"blocks of {block}")

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

    # CONTROL: the same samples' arrival times, which the old software-paced
    # loop recorded. Were these regular too, the above would prove nothing.
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

    # Placed, not just spaced; a constant offset (first read's latency) is ok.
    off = float(np.median(arrival - at))
    r.check(0.0 <= off < 4.0 * EncoderWorker._BLOCK_S,
            f"the stream is anchored to real time: samples are stamped "
            f"{off * 1e3:.1f} ms before they arrived (one block is "
            f"{EncoderWorker._BLOCK_S * 1e3:.0f} ms)")
    r.check(float(np.max(at)) <= float(np.max(arrival)) + 1e-9,
            "no sample is stamped in the future")

    # ── speed: the point of the exercise (exactly rate/100 rev/s) ───────────
    speed = np.array([s[1] for _a, s in rows])          # mm/s
    want_mm_s = (COERCED / 100.0) * np.pi * 150.0
    settled = speed[int(0.75 * len(speed)):]
    med = float(np.median(settled))
    r.check(abs(med - want_mm_s) < 0.01 * want_mm_s,
            f"derived speed {med:.1f} mm/s vs {want_mm_s:.1f} expected "
            f"(within 1 %)")

    # ── the fallback ─────────────────────────────────────────────────────────
    # Losing the wheel is worse than a jittery timebase — but it must say so.
    FakeTask.instances.clear()
    FakeTask.fail_timing = True
    w2 = EncoderWorker("Dev3/ai2", 100.0, volts_per_rev=T_VPR, wheel_dia_mm=150.0)
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


PARTS = {
    "derive": _part_derive,
    "timing": _part_timing,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
