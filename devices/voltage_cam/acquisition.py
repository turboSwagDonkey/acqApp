"""Voltage-imaging camera workers: `OrcaFireWorker` (Hamamatsu ORCA-Fire via
pylablib DCAM) and `MockCameraWorker`, both on `acq.worker.PullWorker`."""

from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker, paced
from .presets import (AcqConfig, EXTERNAL_EDGE, MIN_EXPOSURE_US, WRITER_MBPS,
                      burst_next_boundary, burst_pulses, fit_exposure)

# "External edge" is MASTER PULSE, not plain EXTERNAL: EXTERNAL takes one
# frame per edge, while here one edge starts the camera's own pulse train.
_TRIGGER_MODE: dict[str, str] = {
    "Internal (free-running)": "int",
    EXTERNAL_EDGE:             "master_pulse",
}

# MODE must be START: CONTINUOUS (the camera default) ignores the line and
# free-runs. Enums are written as numeric codes; strings raise.
_TRIG_SRC_PROP        = "TRIGGER SOURCE"            # 1 INT 2 EXT 3 SW 4 MASTER
_TRIG_POLARITY_PROP   = "TRIGGER POLARITY"          # 1 NEGATIVE 2 POSITIVE
_TRIG_ACTIVE_PROP     = "TRIGGER ACTIVE"            # 1 EDGE 2 LEVEL 3 SYNCREADOUT
_MP_TRIG_SRC_PROP     = "MASTER PULSE TRIGGER SOURCE"   # 1 EXTERNAL 2 SOFTWARE
_MP_MODE_PROP         = "MASTER PULSE MODE"         # 1 CONTINUOUS 2 START 3 BURST
_MP_INTERVAL_PROP     = "MASTER PULSE INTERVAL"     # seconds
_MP_BURST_PROP        = "MASTER PULSE BURST TIMES"
_MP_TRIG_SRC_EXTERNAL = 1
_MP_MODE_CONTINUOUS   = 1
_MP_MODE_START        = 2
_MP_MODE_BURST        = 3
# EDGE leaves exposure and readout back to back (512 rows at 500 us: 401 Hz
# vs a 528 Hz sensor); SYNCREADOUT pipelines them (512.8 Hz at 200/500/1500
# us; 256 rows 998.4, 1024 rows 262.0 — MODE=CONTINUOUS, 2026-09-28).
_TRIG_ACTIVE_SYNCREADOUT = 3

_WAIT_TIMEOUT = 0.5
_WAIT_MSG_EVERY = 5.0

# DCAMERR_NOCAMERA on a fresh open is sometimes a transient USB-enumeration
# race in the driver, not a real absence.
_NOCAMERA_RETRIES = 3
_NOCAMERA_RETRY_DELAY_S = 1.5


def open_camera(device_index: int = 0):
    """`DCAM.DCAMCamera(idx)`, retried past a transient NOCAMERA only."""
    from pylablib.devices import DCAM
    from pylablib.devices.DCAM.dcamapi4_lib import DCAMLibError
    for attempt in range(1, _NOCAMERA_RETRIES + 1):
        try:
            return DCAM.DCAMCamera(idx=device_index)
        except DCAMLibError as e:
            if e.name != "DCAMERR_NOCAMERA" or attempt == _NOCAMERA_RETRIES:
                raise
            print(f"[voltage_cam] DCAM reported NOCAMERA on attempt "
                  f"{attempt}/{_NOCAMERA_RETRIES} — retrying in "
                  f"{_NOCAMERA_RETRY_DELAY_S:g}s (known transient USB-"
                  f"enumeration quirk, not a real absence)")
            time.sleep(_NOCAMERA_RETRY_DELAY_S)


class OrcaFireWorker(PullWorker):
    """AcqConfig is read once at start; capture rate can change hot."""
    hz_update = pyqtSignal(int, float)        # (total_frames, recent Hz)
    timing_update = pyqtSignal(float, bool)   # (achievable_hz, unused: False)
    drops_update = pyqtSignal(int, int)       # (skipped_frames, buffer_size)

    _STOP_WAIT_MS = 5000
    # 768 MB still shed ~6% at full frame; 6 GiB covers 2 s at 115 Hz.
    _BUFFER_SECONDS = 2.0
    _BUFFER_BYTES   = 6 << 30
    _BUFFER_MIN     = 16
    _BUFFER_MAX     = 4096
    _WRITER_MBPS    = WRITER_MBPS
    # A routine's per-trial .dcimg holds one burst; room for a few strays
    # keeps its cap small, which is what makes the roll fast (dcimg.py).
    _TRIAL_FILE_BURSTS = 8

    def __init__(self, device_index: int = 0, config: AcqConfig | None = None,
                 cam=None):
        super().__init__()
        self._device_index = device_index
        self._config       = config or AcqConfig()
        # A handle we were given is never opened or closed here (~7 s open).
        self._ext_cam      = cam
        self._exp_lock     = threading.Lock()
        self._pending_rate: float | None = None
        self._syncreadout = False       # camera-confirmed; decides the floor
        self._readout_s = 1.0 / max(self._config.readout_hz, 1e-9)
        self._interval_s = 0.0          # frame period fit_exposure chose
        self._pending_rearm = False
        self._rearm_with_file = False
        self._mp_mode = _MP_MODE_START
        self._burst_n = 0               # 0 = START mode
        # Burst: the frame count when the re-arm was asked for; the gate goes
        # to the end of that burst. Taken then, not when the loop gets to it
        # (up to a wait later), or an edge in between reads as mid-burst.
        self._soft_gate: int | None = None
        self._gate_acq0 = 0             # acquired count the gate sits at
        self._gate_stale = False        # the gated burst starts with a leftover
        self._acquired = 0
        self._rearm_from = 0
        # (re-arms completed, frames since). Capture thread writes, replaced
        # whole so a reader never sees half an update.
        self._gate = (0, 0)
        self._gate_t = 0.0
        self._rec_want: Path | None = None
        self._rec_change = False
        self._rec_busy = False
        self._dcimg = None
        self._dcimg_total = 0
        self._dcimg_missing = 0
        self._dcimg_t0: float | None = None
        self._dcimg_t1: float | None = None
        self._dcimg_full = False
        self._achievable_hz: float = 0.0
        self._master_pulse = False
        self._skipped: int = 0
        self._last_exp_error: str | None = None
        self._t_offset: float | None = None    # camera clock -> perf_counter
        self._use_cam_time = True

    @property
    def timestamp_source(self) -> str:
        """"camera", "arrival", or "unknown" before the first frame."""
        if not self._use_cam_time:
            return "arrival"
        return "camera" if self._t_offset is not None else "unknown"

    def set_rate(self, hz: float) -> None:
        """Hot capture-rate change; exposure follows (fit_exposure)."""
        with self._exp_lock:
            self._pending_rate = float(hz)

    def set_exposure(self, us: float) -> None:
        """Device protocol: the longest exposure `us` is the rate 1/`us`."""
        self.set_rate(1e6 / us if us > 0 else 0.0)

    @staticmethod
    def _trigger_readback(cam) -> str:
        """The trigger state the camera reports, for the log."""
        def g(prop: str) -> str:
            try:
                return str(cam.get_attribute_value(prop, enum_as_str=True))
            except Exception:       # noqa: BLE001
                return "?"
        src = g(_TRIG_SRC_PROP)
        if src != "MASTER PULSE":
            return f"{_TRIG_SRC_PROP}={src}"
        return (f"{_TRIG_SRC_PROP}={src}, {_MP_MODE_PROP}={g(_MP_MODE_PROP)}, "
                f"{_MP_TRIG_SRC_PROP}={g(_MP_TRIG_SRC_PROP)}, "
                f"{_TRIG_POLARITY_PROP}={g(_TRIG_POLARITY_PROP)}, "
                f"{_TRIG_ACTIVE_PROP}={g(_TRIG_ACTIVE_PROP)}, "
                f"{_MP_INTERVAL_PROP}={g(_MP_INTERVAL_PROP)}")

    @staticmethod
    def _do_rearm(cam, nframes: int, mode: int = _MP_MODE_START) -> None:
        """A bare stop/start leaves START mode's "already triggered" latch set
        and the camera free-runs; rewriting MODE clears it (rig, 2026-09-17).

        Kills an attached .dcimg for good (a recorder is single-use per
        capture session), so with one open a routine re-arms through
        `arm_with_next_file` instead."""
        cam.stop_acquisition()
        OrcaFireWorker._cycle_master_pulse(cam, mode)
        cam.start_acquisition(nframes=nframes)

    @staticmethod
    def _cycle_master_pulse(cam, mode: int = _MP_MODE_START) -> None:
        cam.set_attribute_value(
            _MP_MODE_PROP, _MP_MODE_CONTINUOUS, error_on_missing=False)
        cam.set_attribute_value(_MP_MODE_PROP, mode, error_on_missing=False)

    supports_dcimg = True

    def set_record_file(self, path: Path | None) -> None:
        """Record straight to `path` as a .dcimg (None stops). Applied by the
        loop, which must stop capture to bind it. No frames reach the sink
        while one is attached."""
        with self._exp_lock:
            self._rec_want = path
            self._rec_change = True

    @property
    def dcimg_ready(self) -> bool:
        """False from `set_record_file()` until the new recorder's first
        frame, so a routine doesn't count through the stopped gap."""
        with self._exp_lock:
            if self._rec_change or self._rec_busy:
                return False
        return self._dcimg is None or self._dcimg_total > 0

    @property
    def dcimg_active(self) -> bool:
        return self._dcimg is not None

    @property
    def dcimg_frames(self) -> int:
        return self._dcimg_total

    @property
    def dcimg_missing(self) -> int:
        return self._dcimg_missing

    @property
    def dcimg_span(self) -> tuple[float, float] | None:
        """(attached_at, closed_at) in perf_counter, the .dcimg's only tie to
        the shared clock. closed_at is 0.0 while recording."""
        if self._dcimg_t0 is None:
            return None
        return (self._dcimg_t0, self._dcimg_t1 or 0.0)

    @staticmethod
    def _hit_frame_cap(st_rec, max_frames: int) -> bool:
        """DCAM also clears RECORDING while gated, so the flag alone would call
        an empty recorder full; the count tells them apart."""
        return not st_rec.recording and st_rec.total >= max_frames

    def _close_dcimg(self) -> None:
        if self._dcimg is None:
            return
        try:
            st = self._dcimg.status()
            self._dcimg_total, self._dcimg_missing = st.total, st.missing
        except Exception as e:                       # noqa: BLE001
            print(f"[voltage_cam] dcimg status at close failed: {e}")
        self._dcimg_t1 = time.perf_counter()
        self._dcimg.close()
        self._dcimg = None

    def arm_with_next_file(self) -> None:
        """Make the next .dcimg swap re-arm the trigger too. One flag, not two
        requests: the loop could otherwise run a plain re-arm in between and
        kill the new file."""
        with self._exp_lock:
            self._rearm_with_file = True

    def _swap_dcimg(self, cam, path: Path | None, *,
                    rearm_nframes: int | None = None,
                    cap: int | None = None) -> None:
        """Stop capture, change recorder, restart (gated if `rearm_nframes`).
        `cap`: frames the new file needs at most. Raises only if the new
        recording can't open."""
        from .dcimg import DcimgRecorder

        # Open the file before stopping the camera, to keep the gap short.
        t0 = time.perf_counter()
        rec, err, name = None, None, ""
        if path is not None:
            try:
                w, h = self._frame_shape(cam)
                rec = DcimgRecorder.for_frames(path, w * h * 2, cap)   # 16-bit
                rec.open()
            except Exception as e:                   # noqa: BLE001
                rec, err = None, e
        t1 = time.perf_counter()
        cam.stop_acquisition()
        self._close_dcimg()
        t_stop = time.perf_counter()
        try:
            if err is not None:
                raise err
            if rec is not None:
                rec.attach(cam.handle)
                self._dcimg = rec
                self._dcimg_total = self._dcimg_missing = 0
                self._dcimg_full = False
                self._dcimg_t0, self._dcimg_t1 = time.perf_counter(), None
                name = rec.path.name
        finally:
            t_start = time.perf_counter()
            if rearm_nframes is not None and self._dcimg is not None:
                self._cycle_master_pulse(cam, self._mp_mode)
                cam.start_acquisition(nframes=rearm_nframes)
            else:
                cam.start_acquisition()
        # Split: the attach dominates when the cap is drive-sized (dcimg.py).
        t2 = time.perf_counter()
        print(f"[voltage_cam] dcimg -> {name or 'closed'}: prep {t1 - t0:.2f} s, "
              f"camera stopped {t2 - t1:.2f} s (stop+close {t_stop - t1:.2f}, "
              f"attach {t_start - t_stop:.2f}, start {t2 - t_start:.2f})")

    def _file_cap(self, rearm_file: bool) -> int | None:
        """Frames a new .dcimg may need: a routine's per-trial file in burst
        mode holds a few bursts at most; anything else, the drive."""
        if not (rearm_file and self._burst_n):
            return None
        return self._TRIAL_FILE_BURSTS * burst_pulses(self._burst_n,
                                                      self._syncreadout)

    @staticmethod
    def _frame_shape(cam) -> tuple[int, int]:
        hstart, hend, vstart, vend, hbin, vbin = cam.get_roi()
        return ((hend - hstart) // hbin, (vend - vstart) // vbin)

    def rearm_trigger(self) -> None:
        """Queue a re-arm; the capture thread owns the handle. In burst mode
        the camera re-arms itself, so this only moves the gate — no stop."""
        with self._exp_lock:
            self._pending_rearm = True
            self._rearm_from = self._acquired

    @property
    def trigger_gate(self) -> tuple[int, int]:
        """(re-arms completed, frames since the last). The first frame after
        the count moves is the edge."""
        return self._gate

    @property
    def burst_rule(self) -> tuple[int, bool] | None:
        """(frames per edge, leftover leads each non-first burst), or None
        outside burst mode."""
        return (self._burst_n, self._syncreadout) if self._burst_n else None

    @property
    def burst_frames_since_gate(self) -> int | None:
        """Real frames of the burst the last gate caught; None outside burst."""
        if not self._burst_n:
            return None
        return max(0, self._acquired - self._gate_acq0 - int(self._gate_stale))

    def _gated(self, acquired: int = 0) -> None:
        """`acquired`: the frame count the gate sits at (0 after a restart)."""
        self._gate_acq0 = acquired
        self._gate_stale = self._syncreadout and acquired > 0
        if acquired == 0:
            self._acquired = 0
        self._gate = (self._gate[0] + 1, 0)
        self._gate_t = time.perf_counter()

    def _poll_burst(self, cam) -> None:
        """Burst mode: frames counted from the camera, not from wake-ups, so a
        gate can sit at a boundary the loop never saw quiet."""
        try:
            acquired = int(cam.get_frames_status().acquired)
        except Exception:                            # noqa: BLE001
            return
        self._acquired = acquired
        if self._soft_gate is not None:
            asked, self._soft_gate = min(self._soft_gate, acquired), None
            self._gated(burst_next_boundary(asked, self._burst_n,
                                            self._syncreadout))
            print(f"[voltage_cam] gated at frame {self._gate_acq0} "
                  f"(burst, no stop)")
        seq, _ = self._gate
        self._gate = (seq, max(0, acquired - self._gate_acq0))

    def _count_gate_frame(self) -> None:
        # An edge ~0 s after every re-arm means the camera isn't gating.
        seq, n = self._gate
        if n == 0 and seq > 0:
            print(f"[voltage_cam] trigger edge "
                  f"{time.perf_counter() - self._gate_t:.2f} s after re-arm")
        self._gate = (seq, n + 1)

    @property
    def achievable_hz(self) -> float:
        return self._achievable_hz

    @property
    def skipped_frames(self) -> int:
        return self._skipped

    # ── setup helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _maximise_readout_speed(cam) -> str:
        """Force fast readout. The C16240 reports no selectable speeds, which
        once looked like success; returns what actually happened."""
        try:
            speeds = cam.get_all_readout_speeds()
            current = cam.get_readout_speed()
        except Exception as e:
            print(f"[voltage_cam] could not read readout speed ({e})")
            return "error"
        if not speeds:
            print(f"[voltage_cam] readout speed: not selectable on this model "
                  f"(reports {current!r}) — left as found")
            return "absent"
        if "fast" not in speeds:
            print(f"[voltage_cam] readout speed: no 'fast' among {speeds} "
                  f"— left at {current!r}")
            return "absent"
        if current == "fast":
            return "already"
        try:
            cam.set_readout_speed("fast")
        except Exception as e:
            print(f"[voltage_cam] could not set readout speed ({e})")
            return "error"
        print(f"[voltage_cam] readout speed: {current} → fast")
        return "set"

    def _query_timings(self, cam, cfg, verbose: bool = True) -> float:
        """The rate the camera runs at: 1/interval under External edge (the
        interval IS the rate), else the camera's own frame period, else the
        datasheet. Quiet on the hot path."""
        hz = cfg.expected_hz
        if self._master_pulse and self._interval_s > 0:
            hz = 1.0 / self._interval_s
        else:
            try:
                timings = cam.get_frame_timings()      # (exposure, frame_period)
                period = float(getattr(timings, "frame_period", 0.0) or 0.0)
                if period > 0:
                    hz = 1.0 / period
            except Exception as e:
                if verbose:
                    print(f"[voltage_cam] get_frame_timings unavailable ({e}); "
                          f"using datasheet estimate")
        if verbose:
            how = ""
            if self._master_pulse:
                how = (" — External edge, SYNCREADOUT" if self._syncreadout
                       else " — External edge, EDGE: exposure and readout "
                            "back to back")
            print(f"[voltage_cam] achievable: {hz:.1f} Hz{how}")
        self._achievable_hz = hz
        self.timing_update.emit(hz, False)
        return hz

    def _measure_readout(self, cam, cfg) -> float:
        """Readout period with a negligible exposure, so it's readout alone."""
        try:
            cam.set_exposure(MIN_EXPOSURE_US * 1e-6)
            period = float(cam.get_frame_period())
            if period > 0:
                return period
        except Exception as e:                       # noqa: BLE001
            print(f"[voltage_cam] readout period unavailable ({e}); "
                  f"using datasheet estimate")
        return 1.0 / max(cfg.readout_hz, 1e-9)

    def _apply_rate(self, cam, cfg, verbose: bool = True) -> None:
        """Longest exposure for `cfg.target_hz`, and under External edge the
        matching MASTER PULSE INTERVAL."""
        exp_s, period_s = fit_exposure(self._readout_s, cfg.target_hz,
                                       self._master_pulse, self._syncreadout)
        cam.set_exposure(exp_s)
        cfg.exposure_us = exp_s * 1e6
        if self._master_pulse:
            cam.set_attribute_value(_MP_INTERVAL_PROP, period_s,
                                    error_on_missing=False)
        self._interval_s = period_s
        if verbose:
            self._report_rate_request(period_s, cfg)

    @staticmethod
    def _enable_syncreadout(cam) -> bool:
        """Ask for SYNCREADOUT; True only if the camera reads it back —
        trusting an unwritten property would halve the rate."""
        try:
            cam.set_attribute_value(_TRIG_ACTIVE_PROP, _TRIG_ACTIVE_SYNCREADOUT,
                                    error_on_missing=False)
            got = str(cam.get_attribute_value(_TRIG_ACTIVE_PROP,
                                              enum_as_str=True))
        except Exception as e:                       # noqa: BLE001
            print(f"[voltage_cam] TRIGGER ACTIVE=SYNCREADOUT failed "
                  f"({type(e).__name__}: {e}) — staying on the EDGE interval "
                  f"floor, which costs the exposure time in rate")
            return False
        if got.upper().replace(" ", "") != "SYNCREADOUT":
            print(f"[voltage_cam] TRIGGER ACTIVE reads {got!r}, not SYNCREADOUT "
                  f"— using the EDGE interval floor (rate will be lower by the "
                  f"exposure time)")
            return False
        return True

    @staticmethod
    def _report_rate_request(interval_s: float, cfg) -> None:
        """Say the rate it will run at, loudly when a request was clamped."""
        got = 1.0 / interval_s if interval_s > 0 else 0.0
        want = cfg.target_hz
        exp = f"exposure {cfg.exposure_us:.0f} µs"
        if want <= 0:
            print(f"[voltage_cam] capture rate: {got:.1f} Hz (Max), {exp}")
        elif got < want - 0.5:
            print(f"[voltage_cam] CANNOT REACH {want:.1f} Hz: this "
                  f"configuration tops out at {got:.1f} Hz, running there "
                  f"instead ({exp}). Fewer ROWS is the lever — binning is "
                  f"not one.")
        else:
            print(f"[voltage_cam] capture rate: {got:.1f} Hz "
                  f"(requested {want:.1f}), {exp}")

    def _buffer_frames(self, cfg, hz: float) -> int:
        """DCAM ring depth: `_BUFFER_SECONDS` of frames, capped by memory."""
        by_time  = int(max(hz, 1.0) * self._BUFFER_SECONDS)
        by_bytes = self._BUFFER_BYTES // cfg.frame_bytes
        n = int(np.clip(min(by_time, by_bytes), self._BUFFER_MIN, self._BUFFER_MAX))
        slack = n / max(hz, 1.0)
        print(f"[voltage_cam] buffer: {n} frames "
              f"({n * cfg.frame_bytes / (1 << 20):.0f} MB, "
              f"{slack:.2f} s of slack)")
        if by_bytes < by_time:
            want_gib = by_time * cfg.frame_bytes / (1 << 30)
            print(f"[voltage_cam] buffer is MEMORY-capped at "
                  f"{self._BUFFER_BYTES / (1 << 20):.0f} MB: {slack:.2f} s of "
                  f"slack, not the {self._BUFFER_SECONDS:.1f} s intended. A "
                  f"stall longer than {slack:.2f} s sheds frames; the full "
                  f"{self._BUFFER_SECONDS:.1f} s would need {want_gib:.1f} GiB.")
        return n

    # ── per-frame timing ─────────────────────────────────────────────────────

    def _frame_time(self, info) -> float | None:
        """The camera's own stamp, anchored to perf_counter on the first
        frame; None to stamp on arrival (quantised to the read cadence)."""
        if not self._use_cam_time or info is None:
            return None
        us = getattr(info, "timestamp_us", 0) or 0
        if us <= 0:
            self._fallback("camera does not report frame timestamps")
            return None
        t_cam = us * 1e-6
        now = time.perf_counter()
        if self._t_offset is None:
            self._t_offset = now - t_cam
            print(f"[voltage_cam] using the camera's own frame timestamps "
                  f"(offset {self._t_offset:.3f} s)")
        t = t_cam + self._t_offset
        if t > now + 1.0:        # acquired after we read it: clock drift/wrap
            self._fallback("camera frame timestamps are inconsistent")
            return None
        return t

    def _fallback(self, why: str) -> None:
        if self._use_cam_time:
            self._use_cam_time = False
            print(f"[voltage_cam] {why} — frames will be stamped on arrival "
                  f"(their timing is then quantised to the read cadence)")

    def _emit_frames(self, imgs, infos, sink) -> tuple[int, Any]:
        """Feed the sink (frame, acquired_at, index) -> (count, newest)."""
        n, last = 0, None
        n_info = len(infos) if infos is not None else 0
        for i, img in enumerate(imgs):
            if img is None:
                continue
            info = infos[i] if i < n_info else None
            sink((img, self._frame_time(info),
                  None if info is None else getattr(info, "frame_index", None)))
            n += 1
            last = img
        return n, last

    @staticmethod
    def _skip_report(st) -> str:
        # The sink only enqueues, so a camera-side skip is this loop, never
        # the writer (whose drops are counted separately).
        return (f"[voltage_cam] DROPPED {st.skipped} frames (driver buffer "
                f"{st.unread}/{st.buffer_size} unread) — the read loop is not "
                f"draining in time. Suspects: the per-frame copy at this frame "
                f"size, or a sink that blocks. A slow WRITER is a separate "
                f"count (recorder drops), not this one.")

    def _warn_data_rate(self, cfg, hz: float) -> None:
        """Warn up front when the rate exceeds what the writer sustains."""
        mbps = cfg.frame_bytes * hz / (1 << 20)
        print(f"[voltage_cam] data rate: {mbps:.0f} MB/s "
              f"({cfg.frame_bytes / (1 << 20):.2f} MB/frame × {hz:.0f} Hz)")
        if mbps > self._WRITER_MBPS:
            keep = self._WRITER_MBPS / mbps
            cap_hz = self._WRITER_MBPS / (cfg.frame_bytes / (1 << 20))
            print(f"[voltage_cam] ⚠ RECORDING CANNOT KEEP UP: the writer sustains"
                  f" ~{self._WRITER_MBPS:.0f} MB/s, so ~{(1 - keep) * 100:.0f}% of"
                  f" frames would be dropped.")
            print(f"[voltage_cam]   To record gap-free, set the capture rate "
                  f"≤ {cap_hz:.0f} Hz, or use a smaller ROI/binning. Live "
                  f"preview is unaffected.")

    def _run(self) -> None:
        cfg    = self._config
        preset = cfg.preset

        def _t(label, since):
            dt = time.perf_counter() - since
            print(f"[voltage_cam] {label}: {dt:.2f}s")
            return time.perf_counter()

        own_cam = self._ext_cam is None
        mark = time.perf_counter()
        cam = self._ext_cam
        if own_cam:
            cam = open_camera(self._device_index)
            mark = _t("open", mark)
        try:
            if preset.is_full_frame:
                cam.set_roi(hbin=cfg.binning, vbin=cfg.binning)
            else:
                cam.set_roi(
                    hstart = preset.hpos,
                    hend   = preset.hpos + preset.hsize,
                    vstart = preset.vpos,
                    vend   = preset.vpos + preset.vsize,
                    hbin   = cfg.binning,
                    vbin   = cfg.binning,
                )
            mark = _t("set_roi", mark)

            self._maximise_readout_speed(cam)
            self._readout_s = self._measure_readout(cam, cfg)

            mode = _TRIGGER_MODE.get(cfg.trigger_mode, "int")
            self._master_pulse = mode == "master_pulse"
            try:
                cam.set_trigger_mode(mode)
                if mode == "master_pulse":
                    # invert=True is POLARITY=POSITIVE (rising edge); the
                    # default started recordings when the line went OFF.
                    cam.setup_ext_trigger(invert=True)
                    cam.set_attribute_value(
                        _MP_TRIG_SRC_PROP, _MP_TRIG_SRC_EXTERNAL,
                        error_on_missing=False)
                    self._mp_mode = (_MP_MODE_BURST if cfg.burst
                                     else _MP_MODE_START)
                    cam.set_attribute_value(
                        _MP_MODE_PROP, self._mp_mode,
                        error_on_missing=False)
                    # setup_ext_trigger leaves ACTIVE at EDGE.
                    self._syncreadout = self._enable_syncreadout(cam)
                    if cfg.burst:
                        cam.set_attribute_value(
                            _MP_BURST_PROP,
                            burst_pulses(cfg.burst_frames, self._syncreadout))
                        self._burst_n = cfg.burst_frames
                # Read back, not restated: every one of these was wrong once.
                print(f"[voltage_cam] trigger: {self._trigger_readback(cam)}")
            except Exception as e:
                # Not "fell back to internal": set_trigger_mode may have
                # already taken effect.
                print(f"[voltage_cam] trigger setup failed ({e}); camera left "
                      f"at {self._trigger_readback(cam)}")
            # Also replaces the camera's default interval (0.1 s = 10 Hz).
            try:
                self._apply_rate(cam, cfg)
            except Exception as e:                   # noqa: BLE001
                print(f"[voltage_cam] capture rate not applied "
                      f"({type(e).__name__}: {e})")

            hz = self._query_timings(cam, cfg)
            nframes = self._buffer_frames(cfg, hz)
            self._warn_data_rate(cfg, hz)
            self._skipped = 0        # camera clears its own counter on start
            cam.start_acquisition(nframes=nframes)
            mark = _t(f"start_acquisition (nframes={nframes})", mark)

            first = True
            n_acquired, win_n = 0, 0
            status_t0 = time.perf_counter()
            wait_fails, wait_msg_t0 = 0, 0.0

            try:
                while not self._stop:
                    with self._exp_lock:
                        pending = self._pending_rate
                        self._pending_rate = None
                        rearm = self._pending_rearm
                        self._pending_rearm = False
                        rearm_from = self._rearm_from
                        rec_change = self._rec_change
                        rec_path = self._rec_want
                        self._rec_change = False
                        # A roll is close-then-open; consuming the flag on the
                        # close (path None) would leave the open ungated.
                        rearm_file = (rec_change and rec_path is not None
                                      and self._rearm_with_file)
                        if rearm_file:
                            self._rearm_with_file = False
                        # Set inside the lock, or dcimg_ready blips True
                        # between the two (seen on the rig).
                        self._rec_busy = rec_change
                    if rec_change:
                        try:
                            self._swap_dcimg(
                                cam, rec_path,
                                rearm_nframes=nframes if rearm_file else None,
                                cap=self._file_cap(rearm_file))
                            if rearm_file and self._dcimg is not None:
                                self._gated()
                                self._skipped = 0
                                n_acquired = 0
                                print("[voltage_cam] re-armed with the new file")
                        except Exception as e:      # noqa: BLE001
                            print(f"[voltage_cam] .dcimg recording failed "
                                  f"({type(e).__name__}: {e})")
                            self.error.emit(f"DCIMG recording failed: {e}")
                        finally:
                            self._rec_busy = False
                    if rearm and not (rearm_file and self._dcimg is not None):
                        if self._burst_n:
                            self._soft_gate = rearm_from
                        else:
                            try:
                                self._do_rearm(cam, nframes, self._mp_mode)
                                self._gated()
                                # Camera counters restart at 0; stale totals
                                # would read as a negative rate and a drop.
                                self._skipped = 0
                                n_acquired = 0
                                print("[voltage_cam] re-armed")
                            except Exception as e:  # noqa: BLE001
                                print(f"[voltage_cam] trigger re-arm failed "
                                      f"({type(e).__name__}: {e})")
                    if pending is not None:
                        try:
                            cfg.target_hz = pending
                            self._apply_rate(cam, cfg)
                            self._query_timings(cam, cfg, verbose=False)
                        except Exception as e:      # noqa: BLE001
                            why = f"{type(e).__name__}: {e}"
                            if why != self._last_exp_error:
                                self._last_exp_error = why
                                print(f"[voltage_cam] capture rate change to "
                                      f"{pending:.1f} Hz refused ({why})")

                    if self._burst_n:
                        self._poll_burst(cam)
                    t_wait = time.perf_counter()
                    try:
                        cam.wait_for_frame(timeout=_WAIT_TIMEOUT)
                        wait_fails = 0
                        if not self._burst_n:
                            self._count_gate_frame()
                    except Exception as e:
                        # A full timeout is legitimate (gated, no edge yet); an
                        # immediate failure is a device error and must be
                        # paced, or it spins a core. Told apart by elapsed time.
                        waited = time.perf_counter() - t_wait
                        full_timeout = waited >= _WAIT_TIMEOUT * 0.5
                        wait_fails += 1
                        if not full_timeout:
                            time.sleep(min(0.02 * wait_fails, _WAIT_TIMEOUT))
                        now = time.perf_counter()
                        if wait_fails == 1 or now - wait_msg_t0 >= _WAIT_MSG_EVERY:
                            wait_msg_t0 = now
                            if not (mode == "master_pulse" and full_timeout):
                                print(f"[voltage_cam] no frame "
                                      f"({wait_fails} consecutive, "
                                      f"{waited * 1e3:.0f} ms): "
                                      f"{type(e).__name__}: {e}")
                        continue

                    sink = self._sink
                    if sink is None:
                        # Preview only: copying every frame can't keep up, so
                        # skips here are deliberate.
                        img = cam.read_newest_image()
                        if img is not None:
                            if first:
                                mark = _t("first frame", mark)
                                first = False
                            self._set_latest(img)
                    else:
                        res = cam.read_multiple_images(return_info=True)
                        imgs, infos = res if res else (None, None)
                        if imgs:
                            if first:
                                mark = _t("first frame", mark)
                                first = False
                            n_new, last = self._emit_frames(imgs, infos, sink)
                            win_n += n_new
                            if last is not None:
                                self._set_latest(last)

                    # Every pass: a frames-unit Wait counts this.
                    if self._dcimg is not None:
                        try:
                            st_rec = self._dcimg.status()
                            self._dcimg_total = st_rec.total
                            self._dcimg_missing = st_rec.missing
                            # Hitting the cap is otherwise silent: later frames
                            # are discarded with missing still 0.
                            if (not self._dcimg_full and self._hit_frame_cap(
                                    st_rec, self._dcimg.max_frames)):
                                self._dcimg_full = True
                                why = (f"the .dcimg stopped at its "
                                       f"{self._dcimg.max_frames:,}-frame cap "
                                       f"— every later frame is being "
                                       f"discarded")
                                print(f"[voltage_cam] {why}")
                                self.error.emit(why)
                        except Exception:           # noqa: BLE001
                            pass

                    now = time.perf_counter()
                    if now - status_t0 >= 1.0:
                        dt = now - status_t0
                        status_t0 = now
                        try:
                            st = cam.get_frames_status()
                            self.hz_update.emit(
                                st.acquired, (st.acquired - n_acquired) / dt)
                            n_acquired = st.acquired
                            # Only a sink reads every frame. The .dcimg writes
                            # behind the preview's skips; its own `missing` is
                            # its loss (rig 2026-09-30: 146 "drops", 0 gaps).
                            if sink is not None and st.skipped != self._skipped:
                                self._skipped = st.skipped
                                print(self._skip_report(st))
                                self.drops_update.emit(st.skipped, st.buffer_size)
                        except Exception:      # no status support — count our own
                            self.hz_update.emit(win_n, win_n / dt)
                        win_n = 0
            finally:
                try:
                    cam.stop_acquisition()
                except Exception:
                    pass
                # Only after the stop: closing first truncates the file.
                self._close_dcimg()

        finally:
            if own_cam:
                cam.close()


class MockCameraWorker(PullWorker):
    """Noise plus a blob oscillating at 0.5 Hz; frame size follows the preset."""
    hz_update = pyqtSignal(int, float)
    _FPS = 30.0
    _STOP_WAIT_MS = 2000

    # Dark time after a re-arm before the mock's own "edge". Longer than the
    # engine's settle window so the fallback path works too.
    _GATE_S = 1.2

    def __init__(self, config: AcqConfig | None = None):
        super().__init__()
        self._config = config or AcqConfig()
        self._gated_until = 0.0
        self._rearm_req = False
        self._gate = (0, 0)

    @property
    def trigger_gate(self) -> tuple[int, int]:
        return self._gate

    @property
    def burst_rule(self) -> tuple[int, bool] | None:
        n = self._config.burst_frames if self._config.burst else 0
        return (n, False) if n else None

    @property
    def burst_frames_since_gate(self) -> int | None:
        """Burst mode: N frames per edge, then quiet until the next re-arm."""
        return self._gate[1] if self.burst_rule else None

    @property
    def timestamp_source(self) -> str:
        return "camera"

    @property
    def skipped_frames(self) -> int:
        return 0

    # The .dcimg API, inert: test_device_contracts holds both workers to one API.
    supports_dcimg = False

    def set_record_file(self, path) -> None:
        """No-op: Emulate records a TIFF through the sink."""

    @property
    def dcimg_ready(self) -> bool:
        return True

    @property
    def dcimg_active(self) -> bool:
        return False

    @property
    def dcimg_frames(self) -> int:
        return 0

    @property
    def dcimg_missing(self) -> int:
        return 0

    @property
    def dcimg_span(self) -> tuple[float, float] | None:
        return None

    def set_exposure(self, us: float) -> None:
        self._config.exposure_us = us

    def set_rate(self, hz: float) -> None:
        self._config.target_hz = hz
        self._config.fit_exposure()

    def arm_with_next_file(self) -> None:
        pass

    def rearm_trigger(self) -> None:
        """Go dark for `_GATE_S`, standing in for "gated until an edge"."""
        self._rearm_req = True

    def _run(self) -> None:
        self._stop = False
        H, W   = self._config.frame_shape
        cy, cx = H // 2, W // 2
        y, x   = np.ogrid[-cy:H - cy, -cx:W - cx]
        r_blob = min(H, W) * 0.12
        blob   = (x * x + y * y) < r_blob ** 2

        rng    = np.random.default_rng(0)
        period = 1.0 / self._FPS
        t0     = time.perf_counter()

        for n in paced(period, t0):
            if self._stop:
                break
            acquired = time.perf_counter()
            if self._rearm_req:
                self._rearm_req = False
                self._gated_until = acquired + self._GATE_S
                self._gate = (self._gate[0] + 1, 0)
            if acquired < self._gated_until:
                continue
            rule = self.burst_rule
            if rule and self._gate[1] >= rule[0]:
                continue
            self._gate = (self._gate[0], self._gate[1] + 1)
            t     = acquired - t0
            frame = rng.integers(1500, 2500, (H, W), dtype=np.uint16)
            sig   = int(300 * np.sin(2 * np.pi * 0.5 * t))
            frame[blob] = np.clip(
                frame[blob].astype(np.int32) + sig, 0, 65535
            ).astype(np.uint16)
            self._publish(frame, record=(frame, acquired, n - 1))
            if n % int(self._FPS) == 0:
                self.hz_update.emit(n, n / max(time.perf_counter() - t0, 1e-9))
