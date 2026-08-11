"""
Rotary wheel encoder — NI DAQ acquisition worker.

EncoderWorker    : QThread that polls ai2 (or any analog channel) and derives
                   motion from the POSITION voltage.
MockEncoderWorker: synthetic sine wave, no hardware needed.

The ai2 voltage encodes wheel angle (volts_per_rev volts per turn). Both workers
turn each sample into a (voltage, speed, distance) triple at the full acquisition
rate, so all three are recorded losslessly on the shared clock:

    get_latest()  -> (voltage, speed, distance, elapsed_s)   # newest, for the GUI
    recording sink receives (voltage, speed, distance)

Distance is NET (signed) cumulative rotation — forward minus backward. Motion is
integrated per sample with wrap-correction; reset transitions that smear over
several samples are rejected and coasted so distance accumulates instead of
sawtoothing back each turn. Speed/distance are reported for a sample ~1 s in the
PAST (buffered) so the trace is smooth and lags the live voltage. `_SIGN` orients
"forward" as positive. Units follow the scaling: mm/s and mm when a wheel diameter
is set, else rev/s and rev.
"""

from __future__ import annotations
import threading
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker


class _EncoderBase(PullWorker):
    """Shared position→motion derivation for the real and mock encoders.

    The channel carries single-turn POSITION: the voltage ramps 0→volts_per_rev
    over one revolution, then resets. We integrate wheel motion from the per-sample
    change in position, wrap-correcting each step. The reset can smear across a few
    samples (sensor dead-zone / slew), and those sub-steps are too small for a
    plain half-turn unwrap to catch — so instead we REJECT any step implying an
    impossible speed (> _MAX_REV_S) and coast through it at the wheel's current
    velocity (it keeps spinning during the sensor's blind spot). That's what keeps
    cumulative distance from sawtoothing back to zero every revolution.

    Speed/distance are reported for a sample ~_LAG_S in the PAST (buffered) so the
    trace is smooth and lags the live voltage, as requested. Distance is net
    (signed) rotation; both carry _SIGN so forward reads positive.
    """
    fps_update = pyqtSignal(float)      # samples / second

    # Any per-sample step implying a speed above this (rev/s) is a reset/dead-zone
    # artifact, not real motion (a wheel spins at most a few rev/s), so we drop it
    # and coast at the current velocity. Clean single-sample wraps survive — their
    # wrap-corrected step is small — only the smeared-reset sub-steps are caught.
    _MAX_REV_S = 10.0
    # EMA time constant (s) for the velocity used to coast through resets / show speed.
    _TAU_S = 0.15
    # Report speed/distance for a sample this many seconds in the past: smooths the
    # trace and lags it behind the live voltage. Voltage itself stays live.
    _LAG_S = 1.0
    # Half-width (s) of the least-squares window the reported speed slope is
    # measured over. Wider = more noise rejection (better slow-speed SNR) but more
    # smoothing; 0.25 → a 0.5 s fit, well inside the 1 s lag.
    _SLOPE_WIN_S = 0.25
    # Buffered (t, position) history retained: lag + slope window + margin.
    _HIST_S = _LAG_S + _SLOPE_WIN_S + 0.3
    # Direction sign. Forward should read positive; flip to +1.0 if the wiring or
    # wheel mounting makes forward come out negative.
    _SIGN = -1.0
    # Below this speed (rev/s) the readout reads zero, so a resting wheel shows 0.
    _DEADBAND_REV_S = 0.05

    def __init__(self, volts_per_rev: float | None = 5.0,
                 wheel_dia_mm: float | None = 150.0):
        super().__init__()
        self._scale_lock = threading.Lock()
        self._vpr = volts_per_rev
        self._dia = wheel_dia_mm
        self._frac_prev: float | None = None   # previous position fraction (0..1)
        self._t_prev = 0.0
        self._pos = 0.0                # absolute cumulative position, rev (signed)
        self._vel = 0.0                # velocity estimate, rev/s (coasts thru resets)
        self._buf: deque[tuple[float, float]] = deque()   # (elapsed_s, position_rev)
        self._dist_rev = 0.0           # reported net distance, rev (gated integral)
        self._t_report: float | None = None    # last reported (delayed) sample time

    def set_scaling(self, volts_per_rev: float | None,
                    wheel_dia_mm: float | None) -> None:
        """Update V/rev and wheel diameter live (thread-safe)."""
        with self._scale_lock:
            self._vpr = volts_per_rev
            self._dia = wheel_dia_mm

    def _derive(self, v: float, t: float) -> tuple[float, float]:
        """Integrate wheel motion from the position voltage and return a
        (speed, net_distance) reported for a sample _LAG_S in the past. Resets that
        smear over several samples are rejected (coasted) so distance accumulates
        instead of sawtoothing. With no V/rev configured, speed falls back to the
        live voltage."""
        with self._scale_lock:
            vpr, dia = self._vpr, self._dia
        circ = np.pi * dia if dia else 1.0   # mm per rev, else 1 → report in rev

        if not vpr:                          # unscaled → live voltage as "speed"
            return v, 0.0

        frac = min(max(v / vpr, 0.0), 1.0)   # position within one turn, 0..1
        if self._frac_prev is None:          # first sample → seed, no motion yet
            self._frac_prev, self._t_prev = frac, t
            self._buf.append((t, 0.0))
            return 0.0, 0.0

        dt = t - self._t_prev
        self._t_prev = t
        if dt > 0:
            step = frac - self._frac_prev
            self._frac_prev = frac
            if   step >  0.5: step -= 1.0    # wrap-correct a clean single-sample reset
            elif step < -0.5: step += 1.0
            if abs(step) / dt > self._MAX_REV_S:     # smeared-reset / glitch sample
                step = self._vel * dt                # coast at current velocity
            else:                                    # good sample → update velocity
                a = dt / (self._TAU_S + dt)
                self._vel += a * (step / dt - self._vel)
            self._pos += step                        # cleaned cumulative position
            self._buf.append((t, self._pos))
            while self._buf and t - self._buf[0][0] > self._HIST_S:
                self._buf.popleft()

        return self._report(t, circ)

    def _report(self, t: float, circ: float) -> tuple[float, float]:
        """Speed + net distance for a sample _LAG_S in the past. Speed is a
        least-squares slope over a window (real SNR on a noisy signal); distance
        is the integral of that speed, gated by a deadband so per-sample ADC noise
        can neither random-walk nor over-count it while ~stationary."""
        td = t - self._LAG_S
        # Until the buffer brackets td with a full slope window, report live pos.
        if len(self._buf) < 8 or self._buf[0][0] > td - self._SLOPE_WIN_S:
            return 0.0, self._SIGN * self._dist_rev * circ

        ts = np.fromiter((b[0] for b in self._buf), float, len(self._buf))
        ps = np.fromiter((b[1] for b in self._buf), float, len(self._buf))
        win = (ts >= td - self._SLOPE_WIN_S) & (ts <= td + self._SLOPE_WIN_S)
        if win.sum() >= 2:                           # LSQ slope = velocity, rev/s
            rev_s = float(np.polyfit(ts[win], ps[win], 1)[0])
        else:
            rev_s = 0.0
        if abs(rev_s) < self._DEADBAND_REV_S:        # ~stationary → freeze distance
            rev_s = 0.0
        elif self._t_report is not None:             # integrate velocity → distance
            self._dist_rev += rev_s * (td - self._t_report)
        self._t_report = td
        return self._SIGN * rev_s * circ, self._SIGN * self._dist_rev * circ

    def _emit_sample(self, v: float, t: float) -> None:
        speed, dist = self._derive(v, t)
        # get_latest → 4-tuple for the GUI; sink → 3-tuple for recording.
        self._publish((v, speed, dist, t), record=(v, speed, dist))


class EncoderWorker(_EncoderBase):
    def __init__(self, chan: str = "Dev3/ai2", rate: float = 120.0,
                 volts_per_rev: float | None = 4.912,
                 wheel_dia_mm: float | None = 150.0):
        super().__init__(volts_per_rev, wheel_dia_mm)
        self._chan = chan
        self._rate = rate

    def _run(self) -> None:
        from nidaqmx import Task
        from nidaqmx.constants import TerminalConfiguration

        self._stop = False
        period = 1.0 / self._rate
        t0 = time.perf_counter()
        n = 0

        with Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                self._chan,
                terminal_config=TerminalConfiguration.RSE,
                min_val=-10.0, max_val=10.0,
            )
            while not self._stop:
                voltage: float = task.read()  # type: ignore[assignment]
                elapsed = time.perf_counter() - t0
                n += 1
                self._emit_sample(voltage, elapsed)
                if n % int(self._rate) == 0 and elapsed > 0:
                    self.fps_update.emit(n / elapsed)
                nxt = t0 + n * period
                slp = nxt - time.perf_counter()
                if slp > 0:
                    time.sleep(slp)


class MockEncoderWorker(_EncoderBase):
    """Synthetic encoder — a real 0→Vfs single-turn SAWTOOTH so the toy exercises
    the reset handling. Spins forward, pauses, then reverses, with ADC noise."""
    RATE = 120.0
    _STOP_WAIT_MS = 2000

    def _run(self) -> None:
        self._stop = False
        period = 1.0 / self.RATE
        vfs = self._vpr or 5.0
        rng = np.random.default_rng()
        t0 = time.perf_counter()
        n = 0
        rev = 0.0
        while not self._stop:
            t = time.perf_counter() - t0
            # 0.4 rev/s forward for 6 s, still for 3 s, 0.25 rev/s back — repeat.
            phase = t % 12.0
            spin = 0.4 if phase < 6 else (0.0 if phase < 9 else -0.25)
            rev += spin * period
            # Descending sawtooth (like the rig) so forward spin → positive distance.
            voltage = float(((-rev) % 1.0) * vfs + rng.normal(0, 0.045))
            voltage = min(max(voltage, 0.0), vfs)
            n += 1
            self._emit_sample(voltage, t)
            if n % int(self.RATE) == 0 and t > 0:
                self.fps_update.emit(n / t)
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
