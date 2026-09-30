"""Rotary wheel encoder — NI DAQ acquisition workers.

    get_latest()  -> (voltage, speed, distance, elapsed_s)
    sink receives    (voltage, speed, distance, acquired_at)

Speed/distance lag the voltage by ~1 s; mm/s and mm with a wheel diameter,
else rev/s.
"""

from __future__ import annotations
import threading
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker, paced


class _EncoderBase(PullWorker):
    """Position -> motion, real and mock.

    The channel is single-turn POSITION: 0 -> volts_per_rev, then a reset that
    smears over a few samples, each too small for a half-turn unwrap to catch.
    Steps implying more than `_MAX_REV_S` are coasted through instead, or the
    distance sawtooths back once per turn.
    """
    hz_update = pyqtSignal(float)      # samples / second

    _MAX_REV_S = 10.0       # faster steps are reset artifacts
    _TAU_S = 0.15           # EMA time constant of the coasting velocity
    _LAG_S = 1.0            # report this far in the past, for a smooth trace
    _SLOPE_WIN_S = 0.25     # half-width of the least-squares speed window
    _HIST_S = _LAG_S + _SLOPE_WIN_S + 0.3
    _SIGN = +1.0            # flip if the wiring inverts
    _DEADBAND_REV_S = 0.05  # below this, speed reads exactly zero

    # Filed; class attributes so ClockedWorker is checkable without an instance.
    timestamp_source: str = "software"  # "hardware" = the board's sample clock
    actual_rate: float = 0.0

    def __init__(self, volts_per_rev: float | None = 5.0,
                 wheel_dia_mm: float | None = 150.0):
        super().__init__()
        self._scale_lock = threading.Lock()
        self._vpr = volts_per_rev
        self._dia = wheel_dia_mm
        self._frac_prev: float | None = None   # previous position in the turn
        self._t_prev = 0.0
        self._pos = 0.0                # cumulative position, rev
        self._vel = 0.0                # rev/s, coasts through resets
        self._buf: deque[tuple[float, float]] = deque()   # (elapsed_s, rev)
        self._dist_rev = 0.0           # reported net distance
        self._t_report: float | None = None
        self._snap: tuple[float, float, float, float] | None = None

    def set_scaling(self, volts_per_rev: float | None,
                    wheel_dia_mm: float | None) -> None:
        with self._scale_lock:
            self._vpr = volts_per_rev
            self._dia = wheel_dia_mm

    def _derive(self, v: float, t: float) -> tuple[float, float]:
        """-> (speed, net_distance). Unscaled, speed is the raw voltage."""
        with self._scale_lock:
            vpr, dia = self._vpr, self._dia
        circ = np.pi * dia if dia else 1.0   # mm per rev, else report in rev

        if not vpr:
            return v, 0.0

        frac = min(max(v / vpr, 0.0), 1.0)
        if self._frac_prev is None:
            self._frac_prev, self._t_prev = frac, t
            self._buf.append((t, 0.0))
            return 0.0, 0.0

        dt = t - self._t_prev
        self._t_prev = t
        if dt > 0:
            step = frac - self._frac_prev
            self._frac_prev = frac
            if   step >  0.5: step -= 1.0    # a clean single-sample reset
            elif step < -0.5: step += 1.0
            if abs(step) / dt > self._MAX_REV_S:     # smeared reset: coast
                step = self._vel * dt
            else:
                a = dt / (self._TAU_S + dt)
                self._vel += a * (step / dt - self._vel)
            self._pos += step
            self._buf.append((t, self._pos))
            while self._buf and t - self._buf[0][0] > self._HIST_S:
                self._buf.popleft()

        return self._report(t, circ)

    def _report(self, t: float, circ: float) -> tuple[float, float]:
        """Speed: least-squares slope over a window. Distance: its integral,
        deadband-gated so ADC noise can't random-walk it at rest."""
        td = t - self._LAG_S
        n = len(self._buf)
        if n < 8 or self._buf[0][0] > td - self._SLOPE_WIN_S:
            return 0.0, self._SIGN * self._dist_rev * circ

        ts = np.fromiter((b[0] for b in self._buf), float, n)
        ps = np.fromiter((b[1] for b in self._buf), float, n)
        lo = int(np.searchsorted(ts, td - self._SLOPE_WIN_S, side="left"))
        hi = int(np.searchsorted(ts, td + self._SLOPE_WIN_S, side="right"))
        tw, pw = ts[lo:hi], ps[lo:hi]
        # Closed-form slope: polyfit's SVD is too heavy per sample at 120 Hz.
        rev_s = 0.0
        if tw.size >= 2:
            dt_ = tw - tw.mean()
            var = float(dt_ @ dt_)
            if var > 0.0:
                rev_s = float(dt_ @ pw / var)
        if abs(rev_s) < self._DEADBAND_REV_S:
            rev_s = 0.0
        elif self._t_report is not None:
            self._dist_rev += rev_s * (td - self._t_report)
        self._t_report = td
        return self._SIGN * rev_s * circ, self._SIGN * self._dist_rev * circ

    # ── watchers (the closed loop) ───────────────────────────────────────────

    def snapshot(self) -> tuple[float, float, float, float] | None:
        """Newest (voltage, speed, live_speed, acquired_at), non-consuming.
        `speed` matches the file but is ~1 s old; `live_speed` is the noisier
        current EMA, for a rule that must act while the animal runs."""
        with self._lock:
            return self._snap

    def _live_speed(self) -> float:
        """Same sign and deadband as `_report`, so thresholds carry over."""
        with self._scale_lock:
            vpr, dia = self._vpr, self._dia
        if not vpr or abs(self._vel) < self._DEADBAND_REV_S:
            return 0.0
        circ = np.pi * dia if dia else 1.0
        return self._SIGN * self._vel * circ

    def _emit_sample(self, v: float, t: float, mono: float | None = None) -> None:
        speed, dist = self._derive(v, t)
        with self._lock:
            self._snap = (v, speed, self._live_speed(),
                          time.perf_counter() if mono is None else mono)
        # The sink gets the acquisition instant (None = stamp on arrival).
        self._publish((v, speed, dist, t), record=(v, speed, dist, mono))


class EncoderWorker(_EncoderBase):
    """Analog input on the board's sample clock. Speed is a slope, so loop
    jitter went straight into it. Times are anchor + i/rate, the anchor set
    from the first block. Falls back to a software loop, loudly, if timing
    won't configure."""

    _BLOCK_S = 0.05         # per read: 20 GUI updates/s
    _BUFFER_S = 5.0         # a stall longer than this loses data

    def __init__(self, chan: str = "Dev3/ai2", rate: float = 120.0,
                 volts_per_rev: float | None = 4.912,
                 wheel_dia_mm: float | None = 150.0):
        super().__init__(volts_per_rev, wheel_dia_mm)
        self._chan = chan
        self._rate = rate

    def _add_channel(self, task) -> None:
        from nidaqmx.constants import TerminalConfiguration
        task.ai_channels.add_ai_voltage_chan(
            self._chan,
            terminal_config=TerminalConfiguration.RSE,
            min_val=-10.0, max_val=10.0,
        )

    def _run(self) -> None:
        self._stop = False
        if not self._run_hardware():
            self._run_software()

    # ── hardware-timed (the normal path) ─────────────────────────────────────
    def _run_hardware(self) -> bool:
        """False if the board refused the timing configuration."""
        from nidaqmx import Task
        from nidaqmx.constants import AcquisitionType

        block = max(1, int(round(self._rate * self._BLOCK_S)))
        with Task() as task:
            self._add_channel(task)
            try:
                task.timing.cfg_samp_clk_timing(
                    self._rate, sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=max(2 * block,
                                       int(self._rate * self._BUFFER_S)))
                rate = float(task.timing.samp_clk_rate)
                task.start()
            except Exception as e:                    # noqa: BLE001
                print(f"[wheel] hardware timing unavailable "
                      f"({type(e).__name__}: {e}) — falling back to a "
                      f"software-paced loop at {self._rate:g} Hz. Wheel speed "
                      f"will carry the scheduler's jitter.")
                return False

            self.actual_rate = rate
            self.timestamp_source = "hardware"
            if abs(rate - self._rate) > 1e-3 * self._rate:
                print(f"[wheel] board coerced {self._rate:g} Hz to {rate:.4f} Hz")
            print(f"[wheel] hardware-timed: {rate:g} Hz, "
                  f"{block} samples per read")

            timeout = 4.0 * self._BLOCK_S + 1.0
            anchor: float | None = None
            i = 0
            n_win, t_win = 0, time.perf_counter()

            while not self._stop:
                try:
                    data = task.read(number_of_samples_per_channel=block,
                                     timeout=timeout)
                except Exception as e:                # noqa: BLE001
                    # Usually a buffer overflow after a stall: fail rather than
                    # hand back a hole with continuous-looking timestamps.
                    print(f"[wheel] read failed after {i} samples "
                          f"({type(e).__name__}: {e})")
                    raise
                if not isinstance(data, list):        # a block of 1 comes back bare
                    data = [data]
                now = time.perf_counter()
                if anchor is None:      # the block's last sample was ~now
                    anchor = now - (len(data) - 1) / rate

                for v in data:
                    t = i / rate
                    self._emit_sample(float(v), t, anchor + t)
                    i += 1

                n_win += len(data)
                if now - t_win >= 1.0:
                    self.hz_update.emit(n_win / (now - t_win))
                    n_win, t_win = 0, now
        return True

    # ── software-paced (fallback only; not yet run on the physical encoder) ──
    def _run_software(self) -> None:
        from nidaqmx import Task

        self.timestamp_source = "software"
        self.actual_rate = self._rate
        period = 1.0 / self._rate
        t0 = time.perf_counter()

        with Task() as task:
            self._add_channel(task)
            for n in paced(period, t0):
                if self._stop:
                    break
                voltage: float = task.read()  # type: ignore[assignment]
                now = time.perf_counter()
                self._emit_sample(float(voltage), now - t0, now)
                if n % max(1, int(self._rate)) == 0 and now > t0:
                    self.hz_update.emit(n / (now - t0))


class MockEncoderWorker(_EncoderBase):
    """A 0 -> Vfs sawtooth with noise, so the reset handling is exercised:
    forward, pause, reverse."""
    RATE = 120.0
    _STOP_WAIT_MS = 2000

    def _run(self) -> None:
        self._stop = False
        self.actual_rate = self.RATE
        period = 1.0 / self.RATE
        vfs = self._vpr or 5.0
        rng = np.random.default_rng()
        t0 = time.perf_counter()
        rev = 0.0
        for n in paced(period, t0):
            if self._stop:
                break
            t = time.perf_counter() - t0
            # 0.4 rev/s forward 6 s, still 3 s, 0.25 rev/s back.
            phase = t % 12.0
            spin = 0.4 if phase < 6 else (0.0 if phase < 9 else -0.25)
            rev += spin * period
            voltage = float((rev % 1.0) * vfs + rng.normal(0, 0.045))
            voltage = min(max(voltage, 0.0), vfs)
            self._emit_sample(voltage, t, t0 + t)
            if n % int(self.RATE) == 0 and t > 0:
                self.hz_update.emit(n / t)
