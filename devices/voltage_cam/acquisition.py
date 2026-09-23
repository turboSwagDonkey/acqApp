"""Voltage-imaging camera — acquisition workers.

`OrcaFireWorker` captures 16-bit frames from a Hamamatsu ORCA-Fire via
pylablib's DCAM wrapper; `MockCameraWorker` synthesises them. Both share
`acq.worker.PullWorker`:

    worker.get_latest()  -> np.ndarray | None   (newest frame, for preview)
    worker.set_sink(fn)  -> record every frame
    worker.hz_update      -> pyqtSignal(int, float)
"""

from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker, paced
from .presets import AcqConfig, WRITER_MBPS

# UI trigger label → pylablib's high-level trigger mode. "External edge" is
# DCAM's TRIGGER SOURCE=MASTER PULSE, not its plain EXTERNAL: the pulse here is
# a single START edge, and EXTERNAL captures exactly one frame per edge — one
# static frame and then nothing, the symptom this started as. Capture runs off
# the camera's own master-pulse generator, which that edge starts.
_TRIGGER_MODE: dict[str, str] = {
    "Internal (free-running)": "int",
    "External edge":           "master_pulse",
}

# Master-pulse generator config, set explicitly rather than trusting whatever
# the camera was last left at — the point of arming this from the routine, not
# the operator's memory. MODE=START is load-bearing: CONTINUOUS (the camera's
# own default) free-runs off INTERVAL and never consults the line at all, which
# is what made a TTL routine start itself ~100 ms after Start.
#
# Enums must be written as NUMERIC codes — DCAMAttribute.set_value passes the
# value straight to the C library, so a string raises ValueError, and
# `enum_as_str` is read-only (set_attribute_value doesn't accept it).
_TRIG_SRC_PROP        = "TRIGGER SOURCE"            # 1 INT 2 EXT 3 SW 4 MASTER
_TRIG_POLARITY_PROP   = "TRIGGER POLARITY"          # 1 NEGATIVE 2 POSITIVE
_MP_TRIG_SRC_PROP     = "MASTER PULSE TRIGGER SOURCE"   # 1 EXTERNAL 2 SOFTWARE
_MP_MODE_PROP         = "MASTER PULSE MODE"         # 1 CONTINUOUS 2 START 3 BURST
_MP_INTERVAL_PROP     = "MASTER PULSE INTERVAL"     # seconds
_MP_TRIG_SRC_EXTERNAL = 1
_MP_MODE_CONTINUOUS   = 1
_MP_MODE_START        = 2

# Long enough not to busy-poll a free-running camera, short enough that Stop
# stays responsive; and how often to repeat the complaint when none arrives.
_WAIT_TIMEOUT = 0.5
_WAIT_MSG_EVERY = 5.0

# DCAMERR_NOCAMERA sometimes comes back on a fresh dcamapi_init() with the
# camera plugged in, powered, and otherwise fine — a transient USB-enumeration
# race in Hamamatsu's own driver, not a real absence. A couple of short
# retries clears it; three tries costs ~3 s in the worst case, negligible
# next to the ~7 s the open itself takes.
_NOCAMERA_RETRIES = 3
_NOCAMERA_RETRY_DELAY_S = 1.5


def open_camera(device_index: int = 0):
    """`DCAM.DCAMCamera(idx=device_index)`, retried past a transient
    DCAMERR_NOCAMERA. Shared by the startup pre-open (main.py) and this
    worker's own-open fallback (_run(), below) — the same flakiness can hit
    either one, and it should only be handled in one place.

    Any other DCAMLibError (camera genuinely absent, held by another
    process, real hardware fault) is raised immediately — only this one
    named, known-transient code is worth a retry."""
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
    """Opened and closed inside run() so the worker is restartable. AcqConfig
    is read once at start; exposure can change hot via set_exposure().
    """
    hz_update = pyqtSignal(int, float)   # (total_frames, Hz over recent window)
    # The camera's OWN answer for the configured ROI/binning/exposure.
    timing_update = pyqtSignal(float, bool)   # (achievable_hz, exposure_limited)
    # Frames the camera dropped because we didn't drain its buffer fast enough.
    # Nonzero means real data loss.
    drops_update = pyqtSignal(int, int)       # (skipped_frames, buffer_size)

    _STOP_WAIT_MS = 5000
    # Ring buffer holds this many seconds of frames, so a GC pause or disk stall
    # doesn't overwrite un-read ones. Bounded by memory: a full frame is ~21 MB.
    # 768 MB (0.38 s at full frame) was too tight with headroom to spare: the
    # rig has 64 GiB RAM, ~51 GiB free (2026-08-27), and 768 MB was shedding
    # ~6% of frames on real hardware even after the writer stopped being the
    # bottleneck (PLAN.md sec 6 item 1). 6 GiB covers the full 2.0 s at full
    # frame (115 Hz) with margin, and is still <12% of free RAM.
    _BUFFER_SECONDS = 2.0
    _BUFFER_BYTES   = 6 << 30
    _BUFFER_MIN     = 16
    _BUFFER_MAX     = 4096
    # One number, shared with the settings panel — see presets.WRITER_MBPS.
    _WRITER_MBPS    = WRITER_MBPS

    def __init__(self, device_index: int = 0, config: AcqConfig | None = None,
                 cam=None):
        super().__init__()
        self._device_index = device_index
        self._config       = config or AcqConfig()
        # An already-open DCAMCamera to reuse (opening is slow, ~7 s). The
        # worker never opens or closes a handle it was given.
        self._ext_cam      = cam
        self._exp_lock     = threading.Lock()
        self._pending_exp: float | None = None
        self._pending_rearm = False     # see rearm_trigger()
        # DCAM's own recorder (set_record_file). The swap needs a capture
        # stop/start, so the loop performs it; this only requests one.
        self._rec_want: Path | None = None
        self._rec_change = False
        self._rec_busy = False          # a swap is underway — see dcimg_ready
        self._dcimg = None              # the live DcimgRecorder, while recording
        self._dcimg_total = 0           # frames it reported at the last stop
        self._dcimg_missing = 0
        # perf_counter at attach/close — the .dcimg's only tie to the shared
        # clock, see dcimg_span.
        self._dcimg_t0: float | None = None
        self._dcimg_t1: float | None = None
        self._dcimg_full = False        # hit the frame cap — see the loop
        self._achievable_hz: float = 0.0
        self._skipped: int = 0
        self._last_exp_error: str | None = None
        # Camera-clock → perf_counter offset, anchored on the session's first
        # frame (see _frame_time). None until it arrives.
        self._t_offset: float | None = None
        self._use_cam_time = True

    @property
    def timestamp_source(self) -> str:
        """Where the recorded frame times come from: the camera's own per-frame
        stamps ("camera"), or the moment we read them ("arrival"). "unknown"
        until the first frame decides it."""
        if not self._use_cam_time:
            return "arrival"
        return "camera" if self._t_offset is not None else "unknown"

    def set_exposure(self, us: float) -> None:
        """Queue an exposure change; applied on the next frame loop tick."""
        self._config.exposure_us = us
        with self._exp_lock:
            self._pending_exp = us

    @staticmethod
    def _trigger_readback(cam) -> str:
        """What the camera says its trigger state actually IS, for the log.

        Only the properties that decide whether capture is gated; anything
        unreadable degrades to `?` rather than costing the caller a frame.
        """
        def g(prop: str) -> str:
            try:
                return str(cam.get_attribute_value(prop, enum_as_str=True))
            except Exception:       # noqa: BLE001 — a log line is not worth a raise
                return "?"
        src = g(_TRIG_SRC_PROP)
        if src != "MASTER PULSE":
            return f"{_TRIG_SRC_PROP}={src}"
        return (f"{_TRIG_SRC_PROP}={src}, {_MP_MODE_PROP}={g(_MP_MODE_PROP)}, "
                f"{_MP_TRIG_SRC_PROP}={g(_MP_TRIG_SRC_PROP)}, "
                f"{_TRIG_POLARITY_PROP}={g(_TRIG_POLARITY_PROP)}, "
                f"{_MP_INTERVAL_PROP}={g(_MP_INTERVAL_PROP)}")

    @staticmethod
    def _do_rearm(cam, nframes: int) -> None:
        """The actual re-arm, confirmed live against the real camera
        (2026-09-17): a bare `stop_acquisition()`/`start_acquisition()` is NOT
        enough. That pair pauses reading frames, but MASTER PULSE MODE=START's
        own "already got my edge" latch survives it untouched, so the very
        next `start_acquisition()` free-runs with no edge at all — the actual
        rig bug (one edge worked, the second recording began on its own).

        What resets the latch is writing `MASTER PULSE MODE` itself: away
        from START to CONTINUOUS, then back to START, around the stop/start.
        It's the property WRITE that clears it, not the acquisition state —
        tested by cycling the mode with acquisition already stopped and
        restarted, and confirmed gated (zero frames) until a real external
        edge arrived. Only `MASTER PULSE MODE` needs rewriting; `TRIGGER
        SOURCE`/`MASTER PULSE TRIGGER SOURCE`/polarity survive the cycle.
        """
        cam.stop_acquisition()
        cam.set_attribute_value(
            _MP_MODE_PROP, _MP_MODE_CONTINUOUS, error_on_missing=False)
        cam.set_attribute_value(
            _MP_MODE_PROP, _MP_MODE_START, error_on_missing=False)
        cam.start_acquisition(nframes=nframes)

    supports_dcimg = True

    def set_record_file(self, path: Path | None) -> None:
        """Record straight to `path` as a .dcimg (None stops). The driver
        writes the frames, so none reach the sink — preview still works, but
        `read_multiple_images` yields nothing while a recorder is attached.

        Takes effect on the next loop pass, which stops and restarts capture:
        DCAM binds a recorder only to a camera whose capture is stopped.
        """
        with self._exp_lock:
            self._rec_want = path
            self._rec_change = True

    @property
    def dcimg_ready(self) -> bool:
        """Whether capture is actually running for whatever was last asked
        for. False between `set_record_file()` and the frame that proves the
        camera came back — a `.dcimg` swap stops capture for ~0.9 s, and a
        routine that kept counting through that would file a short trial.

        True when nothing is pending and either no recorder is attached or
        the attached one has produced a frame.
        """
        with self._exp_lock:
            if self._rec_change or self._rec_busy:
                return False
        return self._dcimg is None or self._dcimg_total > 0

    @property
    def dcimg_active(self) -> bool:
        """Whether a recorder is attached RIGHT NOW. Distinguishes "not
        recording a .dcimg" from "recording one that has 0 frames so far" —
        the routine engine needs to tell those apart."""
        return self._dcimg is not None

    @property
    def dcimg_frames(self) -> int:
        """Frames the recorder wrote — the count no Python sink ever saw.
        Refreshed once per captured frame, so a routine can count on it."""
        return self._dcimg_total

    @property
    def dcimg_missing(self) -> int:
        """Frames the recorder never received. Real data loss."""
        return self._dcimg_missing

    @property
    def dcimg_span(self) -> tuple[float, float] | None:
        """(attached_at, closed_at) as `perf_counter` readings, or None.

        The ONLY bridge between a .dcimg and the rest of the session: its
        frames carry DCAM's own timebase, and nothing in the file ties them
        to the shared clock. `Recorder.put(at=…)` already takes readings on
        this timebase, so the adapter can hand both ends to `clock.at()` and
        file real session times. `closed_at` is 0.0 while still recording.
        """
        if self._dcimg_t0 is None:
            return None
        return (self._dcimg_t0, self._dcimg_t1 or 0.0)

    def _close_dcimg(self) -> None:
        """Latch the final counts before the handle goes away."""
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

    def _swap_dcimg(self, cam, path: Path | None) -> None:
        """Stop capture, change recorder, start again. Raises only if the NEW
        recording can't open; the old one is closed either way."""
        from .dcimg import DcimgRecorder

        cam.stop_acquisition()
        self._close_dcimg()
        try:
            if path is not None:
                w, h = self._frame_shape(cam)
                rec = DcimgRecorder.for_frames(path, w * h * 2)   # 16-bit
                rec.open()
                rec.attach(cam.handle)
                self._dcimg = rec
                self._dcimg_total = self._dcimg_missing = 0
                self._dcimg_full = False
                # Stamped after attach, before capture restarts: the first
                # frame cannot precede this.
                self._dcimg_t0, self._dcimg_t1 = time.perf_counter(), None
                print(f"[voltage_cam] recording to {rec.path.name} "
                      f"(cap {rec.max_frames:,} frames)")
        finally:
            # Capture restarts either way: a camera left stopped is a frozen
            # preview and no error anywhere the operator is looking.
            cam.start_acquisition()

    @staticmethod
    def _frame_shape(cam) -> tuple[int, int]:
        hstart, hend, vstart, vend, hbin, vbin = cam.get_roi()
        return ((hend - hstart) // hbin, (vend - vstart) // vbin)

    def rearm_trigger(self) -> None:
        """Queue a re-arm, so the NEXT external edge is detectable again — in
        MASTER PULSE/START mode the first edge starts the stream and further
        edges do nothing on their own, so a routine recording once per edge
        has to re-arm between recordings. `_do_rearm` is the actual sequence
        and the reasoning behind it; this just queues that call.

        Queued, not done here: this is called from the Qt thread, and the DCAM
        calls belong to the capture thread that owns the handle. So the caller
        can't treat it as complete on return — the loop acts on it when its
        own frame wait next expires, and residual frames keep arriving for a
        while after that, which is what `routines/engine.py`'s
        `TRIGGER_SETTLE_S` accounts for.
        """
        with self._exp_lock:
            self._pending_rearm = True

    @property
    def achievable_hz(self) -> float:
        """Frame rate the camera reported for the running configuration
        (0.0 until acquisition has been set up)."""
        return self._achievable_hz

    @property
    def skipped_frames(self) -> int:
        """Frames the camera discarded because we read too slowly."""
        return self._skipped

    # ── setup helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _maximise_readout_speed(cam) -> str:
        """Force the fastest readout: the ORCA can sit in slow (ultra-quiet)
        mode, which costs frame rate with no sign of it in the ROI or exposure.

        Returns what happened, including "absent" — on this C16240
        `get_all_readout_speeds()` returns `[]`, so this did nothing and looked
        like it had worked.
        """
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

    # pylablib's "chunks" format is the fastest read path but is unsafe with a
    # per-frame sink: each frame is a view pinning a whole 3D block, while
    # RingBuffer.sizeof sees only the view's nbytes. Cutting per-frame overhead
    # needs the sink to take blocks (CAMERA_TRANSFER.md Q9), not a format flag.

    def _query_timings(self, cam, cfg, verbose: bool = True) -> float:
        """The camera's own sustainable frame period, else the datasheet
        estimate. `verbose=False` for hot exposure changes: dragging the control
        calls this every tick, and printing would put console I/O in the capture
        path.
        """
        hz = cfg.expected_hz
        try:
            timings = cam.get_frame_timings()      # (exposure, frame_period)
            period = float(getattr(timings, "frame_period", 0.0) or 0.0)
            if period > 0:
                hz = 1.0 / period
        except Exception as e:
            if verbose:
                print(f"[voltage_cam] get_frame_timings unavailable ({e}); "
                      f"using datasheet estimate")
        limited = cfg.exposure_limited
        if verbose:
            print(f"[voltage_cam] achievable: {hz:.1f} Hz "
                  f"(readout ceiling {cfg.readout_hz:.1f}, "
                  f"exposure ceiling {cfg.exposure_hz:.1f}"
                  f"{' — EXPOSURE LIMITED' if limited else ''})")
            if limited:
                print(f"[voltage_cam] shorten exposure to "
                      f"≤{cfg.max_exposure_us:.0f} µs to reach the readout ceiling")
        self._achievable_hz = hz
        self.timing_update.emit(hz, limited)
        return hz

    def _buffer_frames(self, cfg, hz: float) -> int:
        """DCAM ring depth: _BUFFER_SECONDS of frames, capped by memory.

        Prints which bound won. At full frame the byte cap wins hard — 38
        frames, 0.33 s, not 2 s — and that shortfall is the difference between
        absorbing a GC pause and dropping through it.
        """
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
        """This frame's acquisition time in the `perf_counter()` domain, or
        None to let the Recorder stamp it on arrival.

        Frames arrive in batches, so arrival stamping quantises the timebase to
        the read cadence. The camera's own stamps have an arbitrary epoch, so
        they are anchored to perf_counter on the first frame: intervals exact,
        one constant offset.
        """
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
        # A frame can't have been acquired after we read it — if the clock
        # drifts or wraps, trust arrival rather than write nonsense.
        if t > now + 1.0:
            self._fallback("camera frame timestamps are inconsistent")
            return None
        return t

    def _fallback(self, why: str) -> None:
        """Give up on camera timestamps for the rest of the session."""
        if self._use_cam_time:
            self._use_cam_time = False
            print(f"[voltage_cam] {why} — frames will be stamped on arrival "
                  f"(their timing is then quantised to the read cadence)")

    @staticmethod
    def _frame_index(info) -> int | None:
        """The camera's frame counter — a drop shows in the file directly,
        not as a hole in the timestamps."""
        return None if info is None else getattr(info, "frame_index", None)

    def _emit_frames(self, imgs, infos, sink) -> tuple[int, Any]:
        """-> (frames emitted, newest frame), feeding the sink
        (frame, acquired_at, index).

        The 3D branch is for the chunks format (see the note above): no
        per-frame info, so arrival stamping.
        """
        n, last = 0, None
        for i, block in enumerate(imgs):
            if block is None:
                continue
            if block.ndim == 3:
                for img in block:
                    sink((img, None, None))
                n += block.shape[0]
                last = block[-1]
            else:
                info = infos[i] if infos is not None and i < len(infos) else None
                sink((block, self._frame_time(info), self._frame_index(info)))
                n += 1
                last = block
        return n, last

    @staticmethod
    def _skip_report(st) -> str:
        """A camera-side skip is NOT the writer, and the old message said it
        was. The sink only enqueues (`Recorder.put` → ring, no disk I/O), so a
        slow writer sheds in the ring and is counted there; a skip here is this
        loop not draining the driver buffer.
        """
        return (f"[voltage_cam] DROPPED {st.skipped} frames (driver buffer "
                f"{st.unread}/{st.buffer_size} unread) — the read loop is not "
                f"draining in time. Suspects: the per-frame copy at this frame "
                f"size, or a sink that blocks. A slow WRITER is a separate "
                f"count (recorder drops), not this one.")

    def _warn_data_rate(self, cfg, hz: float) -> None:
        """Say before the run, not after, that this rate sheds frames however
        the buffers are tuned. Preview is unaffected.

        Not the disk: D: writes 2700 MB/s, the writer 2464 (2026-08-25). The
        wall is `WRITER_MBPS`, and it has moved once.
        """
        mbps = cfg.frame_bytes * hz / (1 << 20)
        print(f"[voltage_cam] data rate: {mbps:.0f} MB/s "
              f"({cfg.frame_bytes / (1 << 20):.2f} MB/frame × {hz:.0f} Hz)")
        if mbps > self._WRITER_MBPS:
            keep = self._WRITER_MBPS / mbps
            cap_hz = self._WRITER_MBPS / (cfg.frame_bytes / (1 << 20))
            print(f"[voltage_cam] ⚠ RECORDING CANNOT KEEP UP: the writer sustains"
                  f" ~{self._WRITER_MBPS:.0f} MB/s, so ~{(1 - keep) * 100:.0f}% of"
                  f" frames would be dropped.")
            print(f"[voltage_cam]   To record gap-free, cap the rate near "
                  f"{cap_hz:.0f} Hz (exposure ≥ {1e6 / cap_hz:.0f} µs), "
                  f"or use a smaller ROI/binning. Live preview is unaffected.")

    def _run(self) -> None:
        cfg    = self._config
        preset = cfg.preset

        # Per-step timing so a slow Start can be pinpointed.
        def _t(label, since):
            dt = time.perf_counter() - since
            print(f"[voltage_cam] {label}: {dt:.2f}s")
            return time.perf_counter()

        own_cam = self._ext_cam is None
        mark = time.perf_counter()
        if own_cam:
            cam = open_camera(self._device_index)
            mark = _t("open", mark)
        else:
            cam = self._ext_cam       # reuse the already-open handle (no 7 s open)
        try:
            # --- ROI / binning ---
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

            # --- exposure (pylablib uses seconds) ---
            cam.set_exposure(cfg.exposure_us * 1e-6)

            # --- speed-critical camera settings ---
            self._maximise_readout_speed(cam)

            # --- trigger (pylablib high-level API) ---
            mode = _TRIGGER_MODE.get(cfg.trigger_mode, "int")
            try:
                cam.set_trigger_mode(mode)
                if mode == "master_pulse":
                    # invert=True is TRIGGER POLARITY=POSITIVE, i.e. start on
                    # the RISING edge. pylablib's default (invert=False) is
                    # POLARITY=NEGATIVE, which starts on the falling edge
                    # instead, so the recording only began when the trigger
                    # was switched OFF.
                    cam.setup_ext_trigger(invert=True)
                    cam.set_attribute_value(
                        _MP_TRIG_SRC_PROP, _MP_TRIG_SRC_EXTERNAL,
                        error_on_missing=False)
                    cam.set_attribute_value(
                        _MP_MODE_PROP, _MP_MODE_START,
                        error_on_missing=False)
                    # Leave the generator free to tick as fast as the sensor can
                    # be read: in START mode its INTERVAL caps the frame rate,
                    # and the camera's own default (0.1 s) would pin the whole
                    # recording to 10 Hz regardless of the preset. Too short is
                    # safe — the camera captures as fast as it can.
                    cam.set_attribute_value(
                        _MP_INTERVAL_PROP, cam.get_frame_period(),
                        error_on_missing=False)
                # Read back rather than restate what was asked for: every one of
                # these was wrong at some point, and a log that echoed the
                # intent would have hidden all of it. This is the line that says
                # whether the camera is really gated or free-running.
                print(f"[voltage_cam] trigger: {self._trigger_readback(cam)}")
            except Exception as e:
                # Deliberately not "falling back to internal": set_trigger_mode
                # may already have taken effect above, and claiming otherwise
                # sent the last round of debugging in the wrong direction.
                print(f"[voltage_cam] trigger setup failed ({e}); camera left "
                      f"at {self._trigger_readback(cam)}")

            # --- capture loop ---
            # pylablib's default 100 frames is at once too big at full frame
            # (~2 GB) and far too small at the fast presets (42 ms of slack at
            # 2360 Hz — a GC pause loses data).
            hz = self._query_timings(cam, cfg)
            nframes = self._buffer_frames(cfg, hz)
            self._warn_data_rate(cfg, hz)
            self._skipped = 0        # camera clears its own counter on start
            cam.start_acquisition(nframes=nframes)
            mark = _t(f"start_acquisition (nframes={nframes})", mark)

            first = True
            # Windowed, not cumulative: an n/t average converges too slowly to
            # reveal the mid-run slowdown we watch for.
            n_acquired, win_n = 0, 0
            status_t0 = time.perf_counter()
            wait_fails, wait_msg_t0, wait_seq_t0 = 0, 0.0, 0.0

            try:
                while not self._stop:
                    with self._exp_lock:
                        pending = self._pending_exp
                        self._pending_exp = None
                        rearm = self._pending_rearm
                        self._pending_rearm = False
                        rec_change = self._rec_change
                        rec_path = self._rec_want
                        self._rec_change = False
                        # Hand the "not ready" baton over INSIDE the lock.
                        # Clearing _rec_change first and only then swapping
                        # left a ~900 ms window reading ready — measured on
                        # real hardware as a 3 ms blip, which would release a
                        # waiting routine before a single frame existed.
                        self._rec_busy = rec_change
                    if rec_change:
                        try:
                            self._swap_dcimg(cam, rec_path)
                        except Exception as e:      # noqa: BLE001
                            # Report and carry on, like the re-arm below: the
                            # capture thread dying takes the session with it,
                            # and the operator sees an empty file either way.
                            print(f"[voltage_cam] .dcimg recording failed "
                                  f"({type(e).__name__}: {e})")
                            self.error.emit(f"DCIMG recording failed: {e}")
                        finally:
                            # Ready is still False past here until the new
                            # recorder's own first frame lands — see
                            # dcimg_ready. A failed swap clears it too, or a
                            # held routine would wait out its whole timeout.
                            self._rec_busy = False
                    if rearm:
                        try:
                            self._do_rearm(cam, nframes)
                            # Both mirror camera counters that restart from 0
                            # here. Leaving them would make the next status
                            # tick report a large NEGATIVE rate (acquired minus
                            # a pre-restart total) and a phantom drop.
                            self._skipped = 0
                            n_acquired = 0
                            print(f"[voltage_cam] re-armed: "
                                  f"{self._trigger_readback(cam)}")
                        except Exception as e:      # noqa: BLE001
                            # Report and carry on: the routine's own trigger
                            # timeout is what turns "never re-armed" into a
                            # pause, and killing the capture thread here would
                            # take the whole session down with it.
                            print(f"[voltage_cam] trigger re-arm failed "
                                  f"({type(e).__name__}: {e})")
                    if pending is not None:
                        try:
                            cam.set_exposure(pending * 1e-6)
                            self._query_timings(cam, cfg, verbose=False)
                        except Exception as e:      # noqa: BLE001
                            # Say it once per distinct reason: this is the
                            # capture loop, and the operator dragging a slider
                            # that silently does nothing is worse than a line
                            # of console. Printing every tick would be its own
                            # kind of failure.
                            why = f"{type(e).__name__}: {e}"
                            if why != self._last_exp_error:
                                self._last_exp_error = why
                                print(f"[voltage_cam] exposure change to "
                                      f"{pending:.0f} us refused ({why})")

                    t_wait = time.perf_counter()
                    try:
                        cam.wait_for_frame(timeout=_WAIT_TIMEOUT)
                        wait_fails = 0
                    except Exception as e:
                        # Two failures share one exception. A real TIMEOUT is
                        # legitimate (an external trigger that hasn't fired) and
                        # already paced; an IMMEDIATE failure is a device error,
                        # and retrying it unpaced spins a core all session. Tell
                        # them apart by elapsed time, not by exception type.
                        waited = time.perf_counter() - t_wait
                        full_timeout = waited >= _WAIT_TIMEOUT * 0.5
                        if wait_fails == 0:
                            wait_seq_t0 = t_wait
                        wait_fails += 1
                        if not full_timeout:
                            time.sleep(min(0.02 * wait_fails, _WAIT_TIMEOUT))
                        now = time.perf_counter()
                        if wait_fails == 1 or now - wait_msg_t0 >= _WAIT_MSG_EVERY:
                            wait_msg_t0 = now
                            # A full-length timeout in External edge mode is the
                            # camera doing its job — gated, no edge yet — and
                            # printing it as `DCAMTimeoutError` read as a fault
                            # for exactly as long as it took someone to ask.
                            # An IMMEDIATE failure is still a real error, in any
                            # mode, so keep the exception for that.
                            if mode == "master_pulse" and full_timeout:
                                msg = (f"[voltage_cam] waiting for an external "
                                       f"trigger — no frame for "
                                       f"{now - wait_seq_t0:.1f} s (normal "
                                       f"while the line is idle)")
                            else:
                                msg = (f"[voltage_cam] no frame "
                                       f"({wait_fails} consecutive, "
                                       f"{waited * 1e3:.0f} ms): "
                                       f"{type(e).__name__}: {e}")
                            print(msg)
                        continue

                    # Snapshot once: the sink decides how much we read, and it
                    # can be set or cleared at any moment.
                    sink = self._sink

                    if sink is None:
                        # PREVIEW ONLY. Reading every frame would copy 2+ GB/s
                        # out of the driver buffer to discard all but the
                        # newest — the copy alone can't keep up, so the buffer
                        # fills anyway. Skips here are deliberate.
                        img = cam.read_newest_image()
                        if img is not None:
                            if first:
                                mark = _t("first frame", mark)
                                first = False
                            self._set_latest(img)
                    else:
                        # Record EVERY frame at the time the CAMERA says it was
                        # acquired. `return_info` also gives the frame index, so
                        # driver-skipped frames stay visible in the file.
                        res = cam.read_multiple_images(return_info=True)
                        imgs, infos = res if res else (None, None)
                        if imgs:
                            if first:
                                mark = _t("first frame", mark)
                                first = False
                            n_new, last = self._emit_frames(imgs, infos, sink)
                            win_n += n_new
                            if last is not None:
                                self._set_latest(last)   # newest, for preview

                    # EVERY pass, not on the 1 s status tick: this is what a
                    # routine's frames-unit Wait counts, and at 115 Hz a
                    # once-a-second count would quantise "wait 100 frames" to
                    # the nearest second. One ctypes call per frame is nothing.
                    # It also keeps the number fresh for final_metadata(),
                    # which can be read before the loop closes the file.
                    if self._dcimg is not None:
                        try:
                            st_rec = self._dcimg.status()
                            self._dcimg_total = st_rec.total
                            self._dcimg_missing = st_rec.missing
                            # The frame cap is a HARD ceiling and hitting it is
                            # SILENT: the recorder just stops, `missing` stays
                            # 0, and every later frame is discarded with no
                            # error anywhere (measured 2026-09-23). This flag
                            # going False is the only evidence, so say it once
                            # and loudly rather than file a short recording
                            # that looks complete.
                            if not st_rec.recording and not self._dcimg_full:
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
                            # From the camera's own counter, so it's the true
                            # acquisition rate whether or not we read every frame.
                            self.hz_update.emit(
                                st.acquired, (st.acquired - n_acquired) / dt)
                            n_acquired = st.acquired
                            # Only a shortfall while RECORDING is data loss —
                            # preview skips on purpose. A .dcimg recording has
                            # no sink and still counts.
                            recording = sink is not None or self._dcimg is not None
                            if recording and st.skipped != self._skipped:
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
                # After the stop, never before: closing the file while the
                # driver is still writing to it truncates the recording.
                self._close_dcimg()

        finally:
            if own_cam:                # only close a camera we opened ourselves
                cam.close()


class MockCameraWorker(PullWorker):
    """Synthetic camera: shot-noise background with a circular blob whose mean
    fluorescence oscillates at 0.5 Hz. Frame size follows the preset."""
    hz_update = pyqtSignal(int, float)
    _FPS = 30.0
    _STOP_WAIT_MS = 2000

    # How long the mock stays dark after a re-arm before its own "edge"
    # arrives. Must outlast the engine's settle window
    # (`routines/engine.py`'s TRIGGER_SETTLE_S) or a mock routine could never
    # tell the gate apart from a camera that ignores the trigger entirely.
    _GATE_S = 1.2

    def __init__(self, config: AcqConfig | None = None):
        super().__init__()
        self._config = config or AcqConfig()
        self._gated_until = 0.0

    @property
    def timestamp_source(self) -> str:
        """The mock generates each frame at a known instant, so it reports a
        true acquisition time like the real camera."""
        return "camera"

    @property
    def skipped_frames(self) -> int:
        """Always 0 — frames are generated on demand. Exists so
        `cam_dropped_frames` reads off the worker instead of a `getattr(…, 0)`
        default that would keep filing 0 if the real property were renamed
        (§5b A1)."""
        return 0

    # ── the .dcimg path, which only a real DCAM camera has ──
    # Declared, not omitted: the adapter branches on `supports_dcimg`, so a
    # missing attribute here would be a crash in Emulate rather than a
    # fallback, and `test_device_contracts` holds the two twins to one API.
    supports_dcimg = False

    def set_record_file(self, path) -> None:
        """No-op: there is no DCAM recorder behind a synthetic camera, so
        Emulate records through the normal sink (a TIFF) instead."""

    @property
    def dcimg_ready(self) -> bool:
        """Always: nothing here ever stops capture to change a file."""
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
        """No-op on the mock worker (kept for API parity with OrcaFireWorker)."""
        self._config.exposure_us = us

    def rearm_trigger(self) -> None:
        """Go dark for `_GATE_S`, then resume — the mock's stand-in for "gated,
        until an edge arrives".

        Not a no-op, deliberately. A free-running mock would look exactly like
        a camera that ignores the trigger line, which the engine now (rightly)
        faults on rather than treating the next frame as an edge. Emulating the
        gate is what keeps a `trigger` routine drivable end to end without the
        rig, and makes the mock exercise the real waiting path instead of
        skipping past it.
        """
        self._gated_until = time.perf_counter() + self._GATE_S

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
            if acquired < self._gated_until:
                continue         # gated by a re-arm — see rearm_trigger()
            t     = acquired - t0
            frame = rng.integers(1500, 2500, (H, W), dtype=np.uint16)
            sig   = int(300 * np.sin(2 * np.pi * 0.5 * t))
            frame[blob] = np.clip(
                frame[blob].astype(np.int32) + sig, 0, 65535
            ).astype(np.uint16)
            # Preview gets the bare frame; the sink gets the same triple the
            # real worker sends.
            self._publish(frame, record=(frame, acquired, n - 1))
            if n % int(self._FPS) == 0:
                self.hz_update.emit(n, n / max(time.perf_counter() - t0, 1e-9))
