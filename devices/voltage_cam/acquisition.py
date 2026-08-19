"""Voltage-imaging camera — acquisition workers.

`OrcaFireWorker` captures 16-bit frames from a Hamamatsu ORCA-Fire via
pylablib's DCAM wrapper; `MockCameraWorker` synthesises them. Both share
`acq.worker.PullWorker`:

    worker.get_latest()  -> np.ndarray | None   (newest frame, for preview)
    worker.set_sink(fn)  -> record every frame
    worker.fps_update    -> pyqtSignal(int, float)
"""

from __future__ import annotations
import threading
import time
from typing import Any

import numpy as np
from PyQt6.QtCore import pyqtSignal

from acqApp.acq.worker import PullWorker
from .presets import AcqConfig

# UI trigger label → pylablib's high-level trigger mode.
_TRIGGER_MODE: dict[str, str] = {
    "Internal (free-running)": "int",
    "External edge":           "ext",
}

# Long enough not to busy-poll a free-running camera, short enough that Stop
# stays responsive; and how often to repeat the complaint when none arrives.
_WAIT_TIMEOUT = 0.5
_WAIT_MSG_EVERY = 5.0


class OrcaFireWorker(PullWorker):
    """Captures 16-bit frames from a Hamamatsu ORCA-Fire via pylablib.

    Opened and closed inside run() so the worker is restartable. AcqConfig is
    read once at start; exposure can change hot via set_exposure().
    """
    fps_update = pyqtSignal(int, float)   # (total_frames, fps over recent window)
    # The camera's OWN answer for the configured ROI/binning/exposure.
    timing_update = pyqtSignal(float, bool)   # (achievable_fps, exposure_limited)
    # Frames the camera dropped because we didn't drain its buffer fast enough.
    # Nonzero means real data loss.
    drops_update = pyqtSignal(int, int)       # (skipped_frames, buffer_size)

    _STOP_WAIT_MS = 5000
    # Ring buffer holds this many seconds of frames, so a GC pause or disk stall
    # doesn't overwrite un-read ones. Bounded by memory: a full frame is ~21 MB.
    _BUFFER_SECONDS = 2.0
    _BUFFER_BYTES   = 768 << 20
    _BUFFER_MIN     = 16
    _BUFFER_MAX     = 4096
    # Sustained end-to-end write rate, to warn before a recording that cannot
    # physically be written. Measured 2026-08-17 through the real path (worker →
    # Recorder → HDF5Writer → D:), NOT the bench: 1004 MB/s over a 10 s full-frame
    # run against 1165 benched. The old 1200 advised a 60 fps cap; the truth is 48.
    _WRITER_MBPS    = 1000.0

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
        self._achievable_fps: float = 0.0
        self._skipped: int = 0
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

    @property
    def achievable_fps(self) -> float:
        """Frame rate the camera reported for the running configuration
        (0.0 until acquisition has been set up)."""
        return self._achievable_fps

    @property
    def skipped_frames(self) -> int:
        """Frames the camera discarded because we read too slowly."""
        return self._skipped

    # ── setup helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _maximise_readout_speed(cam) -> str:
        """Force the fastest readout speed — the ORCA can sit in slow
        (ultra-quiet) mode, which costs frame rate with no sign of it in the ROI
        or exposure settings.

        Returns what happened, including when the control does not exist: on
        this C16240 `get_all_readout_speeds()` returns `[]`, so this did nothing
        and looked like it had worked.
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

    # NOTE: pylablib's "chunks" format is the fastest read path but is NOT safe
    # with a per-frame sink: it returns 3D blocks, so each frame handed on is a
    # view pinning the whole block while RingBuffer.sizeof sees only the view's
    # nbytes. Cutting per-frame overhead needs the sink to take whole blocks
    # (CAMERA_TRANSFER.md open question 9), not a format flag.

    def _query_timings(self, cam, cfg, verbose: bool = True) -> float:
        """The camera's own sustainable frame period, falling back to the
        datasheet estimate. `verbose=False` for hot exposure changes — dragging
        the control calls this every tick, and printing would put console I/O in
        the capture path.
        """
        fps = cfg.expected_fps
        try:
            timings = cam.get_frame_timings()      # (exposure, frame_period)
            period = float(getattr(timings, "frame_period", 0.0) or 0.0)
            if period > 0:
                fps = 1.0 / period
        except Exception as e:
            if verbose:
                print(f"[voltage_cam] get_frame_timings unavailable ({e}); "
                      f"using datasheet estimate")
        limited = cfg.exposure_limited
        if verbose:
            print(f"[voltage_cam] achievable: {fps:.1f} fps "
                  f"(readout ceiling {cfg.readout_fps:.1f}, "
                  f"exposure ceiling {cfg.exposure_fps:.1f}"
                  f"{' — EXPOSURE LIMITED' if limited else ''})")
            if limited:
                print(f"[voltage_cam] shorten exposure to "
                      f"≤{cfg.max_exposure_us:.0f} µs to reach the readout ceiling")
        self._achievable_fps = fps
        self.timing_update.emit(fps, limited)
        return fps

    def _buffer_frames(self, cfg, fps: float) -> int:
        """DCAM ring depth: _BUFFER_SECONDS of frames, capped by a memory budget.

        Says which bound won. At full frame the byte cap wins by a wide margin —
        38 frames, 0.33 s, not the 2 s advertised — and that silent shortfall is
        the difference between absorbing a GC pause and dropping through it.
        """
        by_time  = int(max(fps, 1.0) * self._BUFFER_SECONDS)
        by_bytes = self._BUFFER_BYTES // cfg.frame_bytes
        n = int(np.clip(min(by_time, by_bytes), self._BUFFER_MIN, self._BUFFER_MAX))
        slack = n / max(fps, 1.0)
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
        """This frame's acquisition time in the `perf_counter()` domain, or None
        to let the Recorder stamp it on arrival.

        Frames are read in batches, so arrival stamping gives a whole batch one
        timestamp and then a gap, quantising the timebase to the read cadence.
        The camera stamps each frame itself; its epoch is arbitrary, so it is
        anchored to perf_counter on the session's first frame — intervals exact,
        one constant offset on the stream.
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
        # A frame cannot have been acquired after we read it — if the clock
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
        """The camera's own frame counter, so a dropped frame is visible in the
        file rather than implied by a hole in the timestamps."""
        return None if info is None else getattr(info, "frame_index", None)

    def _emit_frames(self, imgs, infos, sink) -> tuple[int, Any]:
        """Feed every frame to the sink as (frame, acquired_at, index).
        -> (frames emitted, newest frame).

        The 3D branch is defensive in case the format is ever changed to chunks
        (see the note above); those carry no per-frame info, so they fall back
        to arrival stamping.
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
        """What a camera-side skip means — which is NOT "the writer".

        The sink only enqueues (`Recorder.put` → ring, no disk I/O), so a slow
        writer sheds in the Recorder's ring and is counted there. A skip here
        means *this* loop did not drain the driver buffer in time. Different
        fixes, and the old message named the wrong one.
        """
        return (f"[voltage_cam] DROPPED {st.skipped} frames (driver buffer "
                f"{st.unread}/{st.buffer_size} unread) — the read loop is not "
                f"draining in time. Suspects: the per-frame copy at this frame "
                f"size, or a sink that blocks. A slow WRITER is a separate "
                f"count (recorder drops), not this one.")

    def _warn_data_rate(self, cfg, fps: float) -> None:
        """Flag a configuration producing data faster than it can be written.

        Preview is unaffected, but a recording at this rate sheds frames however
        the buffers are tuned — the disk is the wall. Say so before, not after.
        """
        mbps = cfg.frame_bytes * fps / (1 << 20)
        print(f"[voltage_cam] data rate: {mbps:.0f} MB/s "
              f"({cfg.frame_bytes / (1 << 20):.2f} MB/frame × {fps:.0f} fps)")
        if mbps > self._WRITER_MBPS:
            keep = self._WRITER_MBPS / mbps
            print(f"[voltage_cam] ⚠ RECORDING CANNOT KEEP UP: the writer sustains"
                  f" ~{self._WRITER_MBPS:.0f} MB/s, so ~{(1 - keep) * 100:.0f}% of"
                  f" frames would be dropped.")
            print(f"[voltage_cam]   To record gap-free, cap the rate near "
                  f"{self._WRITER_MBPS / (cfg.frame_bytes / (1 << 20)):.0f} fps "
                  f"(exposure ≥ {1e6 / (self._WRITER_MBPS / (cfg.frame_bytes / (1 << 20))):.0f} µs), "
                  f"or use a smaller ROI/binning. Live preview is unaffected.")

    def _run(self) -> None:
        from pylablib.devices import DCAM

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
            cam = DCAM.DCAMCamera(idx=self._device_index)
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
                if mode == "ext":
                    cam.setup_ext_trigger()
            except Exception as e:
                print(f"[voltage_cam] trigger setup failed ({e}); "
                      "using camera default (internal)")

            # --- capture loop ---
            # pylablib's default 100 frames is at once too big at full frame
            # (~2 GB) and far too small at the fast presets (42 ms of slack at
            # 2360 fps — a GC pause loses data).
            fps = self._query_timings(cam, cfg)
            nframes = self._buffer_frames(cfg, fps)
            self._warn_data_rate(cfg, fps)
            self._skipped = 0        # camera clears its own counter on start
            cam.start_acquisition(nframes=nframes)
            mark = _t(f"start_acquisition (nframes={nframes})", mark)

            first = True
            # Windowed, not cumulative: an n/t average converges too slowly to
            # reveal the mid-run slowdown we watch for.
            n_acquired, win_n = 0, 0
            status_t0 = time.perf_counter()
            wait_fails, wait_msg_t0 = 0, 0.0

            try:
                while not self._stop:
                    with self._exp_lock:
                        pending = self._pending_exp
                        self._pending_exp = None
                    if pending is not None:
                        try:
                            cam.set_exposure(pending * 1e-6)
                            self._query_timings(cam, cfg, verbose=False)
                        except Exception:
                            pass

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
                        wait_fails += 1
                        if waited < _WAIT_TIMEOUT * 0.5:
                            time.sleep(min(0.02 * wait_fails, _WAIT_TIMEOUT))
                        now = time.perf_counter()
                        if wait_fails == 1 or now - wait_msg_t0 >= _WAIT_MSG_EVERY:
                            wait_msg_t0 = now
                            print(f"[voltage_cam] no frame "
                                  f"({wait_fails} consecutive, "
                                  f"{waited * 1e3:.0f} ms): "
                                  f"{type(e).__name__}: {e}")
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

                    now = time.perf_counter()
                    if now - status_t0 >= 1.0:
                        dt = now - status_t0
                        status_t0 = now
                        try:
                            st = cam.get_frames_status()
                            # From the camera's own counter, so it is the true
                            # acquisition rate whether or not we read every frame.
                            self.fps_update.emit(
                                st.acquired, (st.acquired - n_acquired) / dt)
                            n_acquired = st.acquired
                            # Only a shortfall while RECORDING is data loss —
                            # preview skips on purpose.
                            if sink is not None and st.skipped != self._skipped:
                                self._skipped = st.skipped
                                print(self._skip_report(st))
                                self.drops_update.emit(st.skipped, st.buffer_size)
                        except Exception:      # no status support — count our own
                            self.fps_update.emit(win_n, win_n / dt)
                        win_n = 0
            finally:
                try:
                    cam.stop_acquisition()
                except Exception:
                    pass

        finally:
            if own_cam:                # only close a camera we opened ourselves
                cam.close()


class MockCameraWorker(PullWorker):
    """Synthetic camera: shot-noise background with a circular blob whose mean
    fluorescence oscillates at 0.5 Hz. Frame size follows the preset."""
    fps_update = pyqtSignal(int, float)
    _FPS = 30.0
    _STOP_WAIT_MS = 2000

    def __init__(self, config: AcqConfig | None = None):
        super().__init__()
        self._config = config or AcqConfig()

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

    def set_exposure(self, us: float) -> None:
        """No-op on the mock worker (kept for API parity with OrcaFireWorker)."""
        self._config.exposure_us = us

    def _run(self) -> None:
        self._stop = False
        H, W   = self._config.frame_shape
        cy, cx = H // 2, W // 2
        y, x   = np.ogrid[-cy:H - cy, -cx:W - cx]
        r_blob = min(H, W) * 0.12
        blob   = (x * x + y * y) < r_blob ** 2

        rng    = np.random.default_rng(0)
        period = 1.0 / self._FPS
        n, t0  = 0, time.perf_counter()

        while not self._stop:
            acquired = time.perf_counter()
            t     = acquired - t0
            frame = rng.integers(1500, 2500, (H, W), dtype=np.uint16)
            sig   = int(300 * np.sin(2 * np.pi * 0.5 * t))
            frame[blob] = np.clip(
                frame[blob].astype(np.int32) + sig, 0, 65535
            ).astype(np.uint16)
            n += 1
            # Preview gets the bare frame; the sink gets the same triple the
            # real worker sends.
            self._publish(frame, record=(frame, acquired, n - 1))
            if n % int(self._FPS) == 0:
                self.fps_update.emit(n, n / max(time.perf_counter() - t0, 1e-9))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
