"""
Rotary wheel encoder — NI DAQ acquisition worker.

EncoderWorker    : QThread that reads ai2 (or any analog channel) on the DAQ's
                   own sample clock and derives motion from the POSITION voltage.
MockEncoderWorker: synthetic sine wave, no hardware needed.

The ai2 voltage encodes wheel angle (`volts_per_rev` volts per turn). Both
workers turn every sample into a (voltage, speed, distance) triple at the full
acquisition rate, so all three record losslessly:

    get_latest()  -> (voltage, speed, distance, elapsed_s)   # newest, for the GUI
    recording sink receives (voltage, speed, distance, acquired_at)

`acquired_at` is when the DAQ *sampled* the voltage, not when the block carrying
it reached us, so the Recorder can stamp it at its true time.

Distance is NET signed rotation, integrated per sample with wrap correction; see
`_EncoderBase` for why reset transitions are rejected rather than unwrapped.
Speed and distance lag the live voltage by ~1 s (buffered, for a smooth trace).
Units follow the scaling: mm/s and mm with a wheel diameter set, else rev/s and
rev.
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
    over a revolution, then resets. Motion is integrated from the per-sample
    change, wrap-corrected. The reset **smears over a few samples** (sensor
    dead-zone), and those sub-steps are too small for a half-turn unwrap to
    catch — so any step implying an impossible speed (> `_MAX_REV_S`) is
    rejected and coasted at the current velocity instead. That is what stops
    cumulative distance sawtoothing back to zero every revolution.

    Speed/distance are reported for a sample `_LAG_S` in the PAST, buffered, so
    the trace is smooth. `_SIGN` orients forward as positive.
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

    # Which timebase the samples actually carry, recorded into the session file
    # as `wheel_timestamp_source` — the same admission the camera makes about
    # its own frames. "hardware": spaced by the board's sample clock.
    # "software": spaced by a Python sleep loop, so speed carries the
    # scheduler's jitter.
    timestamp_source: str = "software"
    # Declared on the class beside timestamp_source, not only assigned in
    # __init__: the two are read together out of the finished session file
    # (`wheel_timestamp_source` / `wheel_rate_actual_hz`) and are the pair that
    # says whether the recorded speed is a measurement or a scheduler artefact.
    # Being class attributes is also what lets `devices.ClockedWorker` be
    # checked without constructing a worker (tests/test_device_contracts.py).
    actual_rate: float = 0.0            # the rate the device settled on, Hz

    def __init__(self, volts_per_rev: float | None = 5.0,
                 wheel_dia_mm: float | None = 150.0):
        super().__init__()
        self.actual_rate = 0.0
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
        # Newest sample, kept for watchers rather than consumers — see snapshot().
        self._snap: tuple[float, float, float, float] | None = None

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

    # ── watchers (the closed loop) ───────────────────────────────────────────

    def snapshot(self) -> tuple[float, float, float, float] | None:
        """Newest `(voltage, speed, live_speed, acquired_at)` WITHOUT consuming it.

        `get_latest()` hands each sample out once and the display tick is
        already that consumer, so a watcher (the closed loop) reads this instead
        of draining the plot. None until the first sample.

        The two speeds are not interchangeable, and choosing between them is an
        experimental decision, not an implementation detail:

          `speed`       the reported one — a least-squares slope centred
                        `_LAG_S` in the past, so it matches `wheel_speed` in the
                        file but is ~1 s old. A rule on it acts a second late.
          `live_speed`  the EMA behind it (`_TAU_S` = 0.15 s): noisier, current.
                        A rule that must act *while* the animal runs wants this.

        `acquired_at` is `perf_counter` at the instant the DAQ sampled, so a
        decision can be recorded at the true time of its cause.
        """
        with self._lock:
            return self._snap

    def _live_speed(self) -> float:
        """Current EMA velocity in the reported units (mm/s, or rev/s with no
        wheel diameter set). Carries the same sign convention and deadband as
        `_report`, so a resting wheel reads exactly 0 on both and a threshold
        read off one readout means the same thing on the other."""
        with self._scale_lock:
            vpr, dia = self._vpr, self._dia
        if not vpr or abs(self._vel) < self._DEADBAND_REV_S:
            return 0.0
        circ = np.pi * dia if dia else 1.0
        return self._SIGN * self._vel * circ

    def _emit_sample(self, v: float, t: float, mono: float | None = None) -> None:
        speed, dist = self._derive(v, t)
        # Watchers get the acquisition instant, not `t`: `t` is elapsed-since-
        # worker-start, which is not the domain Recorder.put(at=) stamps in.
        with self._lock:
            self._snap = (v, speed, self._live_speed(),
                          time.perf_counter() if mono is None else mono)
        # get_latest → 4-tuple for the GUI; the sink gets the same three values
        # plus the perf_counter instant the sample was ACQUIRED. `None` there
        # means "stamp it on arrival", which is all a device without its own
        # timebase can offer.
        self._publish((v, speed, dist, t), record=(v, speed, dist, mono))


class EncoderWorker(_EncoderBase):
    """Analog input clocked by the DAQ board, not by a Python sleep loop.

    `cfg_samp_clk_timing` + continuous block reads. Speed is a SLOPE, so every
    millisecond of scheduler jitter in the old single-sample `task.read()` loop
    went straight into the wheel speed — and that loop competed with a GUI
    thread painting two camera previews. The board's intervals are exact.

    Sample TIMES are reconstructed the way the camera's are (#1): the device
    knows the spacing but not our epoch, so the first block anchors index 0 into
    the perf_counter domain and every sample after is `anchor + i/rate`. The
    stream then carries one constant offset — that first read's driver latency.
    `timestamp_source` records whether this worked.

    If the board won't take the timing configuration, acquisition falls back to
    the software-paced loop rather than losing the wheel for the session — but
    it says so, loudly and in the session file.
    """

    _BLOCK_S = 0.05         # seconds of samples per read: 20 GUI updates/s
    _BUFFER_S = 5.0         # DAQmx input buffer depth — a stall this long loses data

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
        """Acquire on the board's sample clock. False if it wouldn't configure.

        A fresh Task is opened for each path rather than reconfiguring a failed
        one: a task whose timing was half-set is not a task whose behaviour is
        worth reasoning about.
        """
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
                # The divider can only hit certain rates exactly; the file
                # records what was used, not what was asked for.
                print(f"[wheel] board coerced {self._rate:g} Hz to {rate:.4f} Hz")
            print(f"[wheel] hardware-timed: {rate:g} Hz, "
                  f"{block} samples per read")

            timeout = 4.0 * self._BLOCK_S + 1.0
            anchor: float | None = None
            i = 0                                     # global sample index
            n_win, t_win = 0, time.perf_counter()

            while not self._stop:
                try:
                    data = task.read(number_of_samples_per_channel=block,
                                     timeout=timeout)
                except Exception as e:                # noqa: BLE001
                    # Usually -200279: the input buffer overflowed because we
                    # didn't read in time, which at these rates means the
                    # process was stalled for seconds. Those samples are gone —
                    # better to fail visibly than to hand back a stream with an
                    # invisible hole whose timestamps still look continuous.
                    print(f"[wheel] read failed after {i} samples "
                          f"({type(e).__name__}: {e})")
                    raise
                if not isinstance(data, list):        # a block of 1 comes back bare
                    data = [data]
                now = time.perf_counter()
                if anchor is None:
                    # The last sample of this first block was acquired at ~now,
                    # so index 0 sits (len-1)/rate earlier.
                    anchor = now - (len(data) - 1) / rate

                for v in data:
                    self._emit_sample(float(v), i / rate, anchor + i / rate)
                    i += 1

                n_win += len(data)
                if now - t_win >= 1.0:
                    self.fps_update.emit(n_win / (now - t_win))
                    n_win, t_win = 0, now
        return True

    # ── software-paced (fallback only) ───────────────────────────────────────
    def _run_software(self) -> None:
        from nidaqmx import Task

        self.timestamp_source = "software"
        self.actual_rate = self._rate
        period = 1.0 / self._rate
        t0 = time.perf_counter()
        n = 0

        with Task() as task:
            self._add_channel(task)
            while not self._stop:
                voltage: float = task.read()  # type: ignore[assignment]
                now = time.perf_counter()
                n += 1
                # Arrival IS the best estimate available on this path — there is
                # no device timebase to anchor to.
                self._emit_sample(float(voltage), now - t0, now)
                if n % max(1, int(self._rate)) == 0 and now > t0:
                    self.fps_update.emit(n / (now - t0))
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
        self.actual_rate = self.RATE        # software-paced, like the fallback
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
            self._emit_sample(voltage, t, t0 + t)
            if n % int(self.RATE) == 0 and t > 0:
                self.fps_update.emit(n / t)
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
