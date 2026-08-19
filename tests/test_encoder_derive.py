"""
The wheel encoder's position → speed/distance derivation.

The channel carries single-turn POSITION: the voltage ramps 0 → volts_per_rev
over one revolution and then resets. Every revolution therefore contains a step
that is not motion, and the reset is not clean — it smears over a few samples as
the sensor crosses its dead zone. `_EncoderBase._derive` rejects any step
implying an impossible speed and coasts through it at the current velocity;
without that, cumulative distance sawtooths back towards zero once per turn and
a 20-minute run under-reports by every revolution it contains.

Nothing about this is visible on the rig: a distance readout that is quietly a
third short still looks like a distance readout. So it is checked here against a
synthetic wheel turning at a known rate, with two CONTROLS — a raw integrator
and a plain half-turn unwrap — that reproduce the failures the real rule exists
to avoid.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_encoder_derive.py
"""
from __future__ import annotations

import sys

import numpy as np

from _harness import Report      # noqa: F401  (also puts acqApp on sys.path)

from acqApp.devices.wheel.acquisition import _EncoderBase       # noqa: E402

VPR = 4.912                 # volts per revolution
DIA = 150.0                 # wheel diameter, mm
CIRC = np.pi * DIA          # mm per revolution
RATE = 120.0                # samples/s, the rig's encoder rate
REV_S = 1.5                 # simulated wheel speed, rev/s
SMEAR = 3                   # samples the reset smears across


def sim(seconds: float, rev_s: float = REV_S, smear: int = SMEAR,
        noise: float = 0.0, seed: int = 3):
    """(times, voltages) for a wheel turning at `rev_s`, resets smeared.

    Position runs *upward* through the ramp, because that is what this rig
    does: the operator confirmed 2026-08-19 that a mouse running forward reads
    positive with `_SIGN = +1.0`, which is only true of a rising ramp. This
    fixture previously asserted the opposite ("the forward direction is the
    falling one") and was simply wrong about the hardware — see §7 (ad).
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    frac = (rev_s * t) % 1.0
    v = frac * VPR

    # Smear each reset: while the sensor crosses its dead zone the output is
    # neither the old position nor the new one, but somewhere in between. The
    # transition is then several sub-steps of ~1/smear of a turn each — every
    # one implying tens of rev/s, which is the signature _derive rejects, and
    # every one under the half-turn a plain unwrap needs to see.
    # A rising ramp wraps DOWNWARD (…0.99 → 0.00), so the reset is a large
    # negative step. Flipping the ramp without flipping this would find no
    # wraps at all and quietly stop smearing them — the whole point of `sim`.
    jump = np.nonzero(np.diff(frac) < -0.5)[0] + 1       # sample after each wrap
    for j in jump:
        if smear <= 0 or j + smear >= n:
            continue
        v[j:j + smear] = np.linspace(v[j - 1], v[j + smear], smear + 2)[1:-1]
    if noise:
        v = v + rng.normal(0.0, noise, n)
    return t, np.clip(v, 0.0, VPR)


def naive_unwrap(v: np.ndarray) -> float:
    """Control: wrap-correct each step, but keep every one — no reset rejection.

    This is the obvious implementation, and the one the comment in
    `_EncoderBase` is warning about.
    """
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


def main() -> int:
    r = Report("encoder-derive")

    # ── a wheel turning steadily, resets smeared ─────────────────────────────
    t, v = sim(6.0)
    sp, di = run(t, v)
    r.note(f"{t.size} samples at {RATE:g} Hz, {REV_S * 6:g} revolutions, "
           f"each reset smeared over {SMEAR} samples "
           f"({np.count_nonzero(np.diff(v / VPR) > 0.5)} steps left big enough "
           f"for a half-turn unwrap to notice)")

    # Speed is reported for a sample _LAG_S in the past, so judge it after the
    # buffer has filled.
    settled = t > 2.0
    want = REV_S * CIRC
    med = float(np.median(sp[settled]))
    r.check(abs(med - want) < 0.02 * want,
            f"speed: {med:.1f} mm/s, expected {want:.1f} (within 2 %)")
    r.check(float(np.max(np.abs(sp[settled] - want))) < 0.15 * want,
            f"speed: no reset spikes — worst sample is "
            f"{float(np.max(np.abs(sp[settled] - want))) / want * 100:.1f} % off")

    # Distance is the number that decays over a long run, so check its slope
    # rather than a single value: it must keep pace with the true speed.
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
    # Rejection is on speed, not on "is this a wrap", so a single-sample wrap —
    # which is what a good sensor gives — has to survive it.
    t, v = sim(6.0, smear=0)
    _, di_clean = run(t, v)
    r.check(abs(di_clean[-1] - di[-1]) < 0.15 * di[-1],
            f"a clean single-sample reset gives the same distance "
            f"({di_clean[-1]:.0f} vs {di[-1]:.0f} mm)")

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
    enc = _EncoderBase(volts_per_rev=VPR, wheel_dia_mm=None)
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


if __name__ == "__main__":
    sys.exit(main())
