"""
Voltage-imaging camera — acquisition workers.

OrcaFireWorker   : captures 16-bit frames from a Hamamatsu ORCA-Fire
                   via pylablib's DCAM wrapper.
MockCameraWorker : synthetic 16-bit frames (noise blob + 0.5 Hz ΔF sine)
                   for dev work.  Matches OrcaFireWorker's pull-based API.

Both share acq.worker.PullWorker:
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

# Map the UI trigger label → pylablib's high-level trigger mode.
# ("int" = internal / free-running, "ext" = external edge.)
_TRIGGER_MODE: dict[str, str] = {
    "Internal (free-running)": "int",
    "External edge":           "ext",
}

# How long to block waiting for the next frame, and how often to repeat the
# complaint when none arrives. Long enough not to busy-poll a free-running
# camera; short enough that Stop is responsive.
_WAIT_TIMEOUT = 0.5
_WAIT_MSG_EVERY = 5.0


class OrcaFireWorker(PullWorker):
    """
    Captures 16-bit frames from a Hamamatsu ORCA-Fire via pylablib.

    The camera is opened and closed inside run() so the worker is safely
    restartable.  AcqConfig is read once at start; exposure can be changed
    hot via set_exposure().
    """
    fps_update = pyqtSignal(int, float)   # (total_frames, fps over recent window)
    # (achievable_fps, is_exposure_limited) — the camera's OWN answer for the
    # configured ROI/binning/exposure, emitted once acquisition is set up.
    timing_update = pyqtSignal(float, bool)
    # (skipped_frames, ring_buffer_size) — frames the camera dropped because we
    # did not drain its buffer fast enough. Nonzero here means real data loss.
    drops_update = pyqtSignal(int, int)

    _STOP_WAIT_MS = 5000
    # Size the DCAM ring buffer to hold this many SECONDS of frames, so a GC
    # pause or a disk stall does not overwrite un-read frames. Bounded by a
    # memory budget: a full ORCA frame is ~21 MB.
    _BUFFER_SECONDS = 2.0
    _BUFFER_BYTES   = 768 << 20
    _BUFFER_MIN     = 16
    _BUFFER_MAX     = 4096
    # Measured uncompressed HDF5Writer throughput on this machine's NVMe
    # (see CAMERA_TRANSFER.md §4b). Used only to warn before a recording that
    # physically cannot be written at the configured rate.
    _WRITER_MBPS    = 1200.0

    def __init__(self, device_index: int = 0, config: AcqConfig | None = None,
                 cam=None):
        super().__init__()
        self._device_index = device_index
        self._config       = config or AcqConfig()
        # An already-open DCAMCamera to reuse (opening is slow, ~7 s). If given,
        # the worker starts/stops acquisition on it but never opens or closes it
        # — the owner does that once. If None, the worker opens/closes its own.
        self._ext_cam      = cam
        self._exp_lock     = threading.Lock()
        self._pending_exp: float | None = None
        self._achievable_fps: float = 0.0
        self._skipped: int = 0
        # Camera-clock → perf_counter offset, anchored on the first frame of the
        # session (see _frame_time). None until that frame arrives.
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
    def _maximise_readout_speed(cam) -> None:
        """
        Force the fastest readout speed.

        The ORCA can sit in the slow (ultra-quiet) readout mode, which costs
        frame rate with no indication in the ROI or exposure settings.
        pylablib's speeds are "fast" / "slow".
        """
        try:
            speeds = cam.get_all_readout_speeds()
            current = cam.get_readout_speed()
            if "fast" in speeds and current != "fast":
                cam.set_readout_speed("fast")
                print(f"[voltage_cam] readout speed: {current} → fast")
        except Exception as e:
            print(f"[voltage_cam] could not set readout speed ({e})")

    # NOTE: pylablib's "chunks" frame format is documented as the fastest read
    # path, but it is NOT safe with a per-frame sink. It returns 3D blocks, so
    # every frame handed on is a *view* that keeps the whole block alive, while
    # RingBuffer.sizeof only sees the view's own nbytes — the byte cap would
    # under-count by the block size and the preview reference alone could pin a
    # multi-GB block. Cutting per-frame overhead needs the sink to take whole
    # blocks (see CAMERA_TRANSFER.md open question 9), not just a format flag.
    # The default "list" format returns independent per-frame copies.

    def _query_timings(self, cam, cfg, verbose: bool = True) -> float:
        """
        Ask the camera what frame period it can actually sustain, instead of
        trusting the datasheet table. Returns fps (falls back to the estimate).

        `verbose=False` for hot exposure changes: dragging the exposure control
        calls this once per loop tick, and printing on every tick would put
        console I/O in the capture path.
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
        """DCAM ring-buffer depth: enough frames to cover _BUFFER_SECONDS of
        acquisition, capped by a memory budget."""
        by_time  = int(max(fps, 1.0) * self._BUFFER_SECONDS)
        by_bytes = self._BUFFER_BYTES // cfg.frame_bytes
        n = int(np.clip(min(by_time, by_bytes), self._BUFFER_MIN, self._BUFFER_MAX))
        print(f"[voltage_cam] buffer: {n} frames "
              f"({n * cfg.frame_bytes / (1 << 20):.0f} MB, "
              f"{n / max(fps, 1.0):.2f} s of slack)")
        return n

    # ── per-frame timing ─────────────────────────────────────────────────────

    def _frame_time(self, info) -> float | None:
        """This frame's acquisition time, in the `time.perf_counter()` domain,
        or None to let the Recorder stamp it on arrival.

        WHY: frames are read in batches, so stamping them as they reach us gives
        every frame in a batch the same timestamp and then a gap — the recorded
        timebase ends up quantised to the read cadence instead of the frame
        rate. The camera stamps each frame itself, which is the real thing.

        Its clock has an arbitrary epoch, so we anchor it to perf_counter on the
        first frame of the session. Frame-to-frame intervals are then exact; the
        whole stream carries one constant offset, the read latency of that first
        frame (well under a frame period when the buffer is being drained).
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
        # A frame cannot have been acquired after we read it. If the camera
        # clock drifts or wraps, trust arrival times rather than write nonsense.
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
        """The camera's own frame counter, recorded alongside each frame so a
        gap caused by a dropped frame is visible in the file rather than just
        implied by a hole in the timestamps."""
        return None if info is None else getattr(info, "frame_index", None)

    def _emit_frames(self, imgs, infos, sink) -> tuple[int, Any]:
        """Feed every frame to the sink as (frame, acquired_at, index).

        Entries are 2D frames in the default "list" format; the 3D branch is
        defensive in case the frame format is ever changed (see the note on
        chunks above). Those carry no per-frame info, so they fall back to being
        stamped on arrival. Returns (frames emitted, newest frame).
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

    def _warn_data_rate(self, cfg, fps: float) -> None:
        """Flag a configuration that produces data faster than it can be written.

        Preview is unaffected (it reads only the newest frame), but a recording
        at this rate will shed frames no matter how the buffers are tuned — the
        disk is the wall. Say so up front rather than after the data is lost.
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

        # Per-step timing so a slow Start can be pinpointed (open vs ROI vs
        # buffer allocation vs first frame).
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
            # Ask the camera what it can actually sustain, then size its ring
            # buffer to hold a couple of seconds of that. pylablib's default of
            # 100 frames is simultaneously too big at full frame (~2 GB) and far
            # too small at the fast presets (100 frames @ 2360 fps = 42 ms of
            # slack — a GC pause loses data).
            fps = self._query_timings(cam, cfg)
            nframes = self._buffer_frames(cfg, fps)
            self._warn_data_rate(cfg, fps)
            self._skipped = 0        # camera clears its own counter on start
            cam.start_acquisition(nframes=nframes)
            mark = _t(f"start_acquisition (nframes={nframes})", mark)

            first = True
            # Windowed fps: a cumulative n/t average converges too slowly to
            # reveal a mid-run slowdown, which is exactly what we watch for.
            # n_acquired tracks the camera's own frame counter between ticks.
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
                        # Two different failures arrive through one exception.
                        # A genuine TIMEOUT is legitimate — an external trigger
                        # that hasn't fired yet — and `timeout` has already
                        # paced it. A call that fails IMMEDIATELY is a device
                        # error, and retrying it with no pause spins a core
                        # silently for as long as the session runs. Tell them
                        # apart by how long the call took, rather than by
                        # vendor exception types.
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

                    # Snapshot once: whether a recording sink is attached decides
                    # how much we read, and it can be set/cleared at any moment.
                    sink = self._sink

                    if sink is None:
                        # PREVIEW ONLY. Reading every frame here would copy the
                        # camera's full output (2+ GB/s on CoaXPress) out of the
                        # driver buffer just to discard all but the newest — the
                        # copy alone cannot keep up, so the buffer fills and the
                        # camera overwrites un-read frames. read_newest_image()
                        # copies ONE frame and advances the read pointer past the
                        # rest, which is exactly what a ~30 Hz preview needs.
                        # Frames skipped here are skipped deliberately.
                        img = cam.read_newest_image()
                        if img is not None:
                            if first:
                                mark = _t("first frame", mark)
                                first = False
                            self._set_latest(img)
                    else:
                        # Record EVERY frame (gap-free), each carrying the time
                        # the CAMERA says it was acquired — not the moment this
                        # batch happened to be read. return_info also gives the
                        # frame index, so frames the driver skipped are visible
                        # in the file instead of silently closing the gap.
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
                            # Frame rate from the camera's own counter, so it is
                            # the true acquisition rate whether or not we are
                            # reading every frame.
                            self.fps_update.emit(
                                st.acquired, (st.acquired - n_acquired) / dt)
                            n_acquired = st.acquired
                            # Only a shortfall while RECORDING is data loss. In
                            # preview we skip frames on purpose, so warning about
                            # it would be crying wolf.
                            if sink is not None and st.skipped != self._skipped:
                                self._skipped = st.skipped
                                print(f"[voltage_cam] DROPPED {st.skipped} frames"
                                      f" (buffer {st.unread}/{st.buffer_size}"
                                      f" unread) — writer cannot keep up")
                                self.drops_update.emit(st.skipped, st.buffer_size)
                        except Exception:
                            # No status support: fall back to counting what we
                            # actually handled.
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
    """
    Synthetic voltage-imaging camera: shot-noise background with a circular
    blob whose mean fluorescence oscillates at 0.5 Hz (simulated ΔF/F).
    Frame dimensions match the configured preset after binning.
    """
    fps_update = pyqtSignal(int, float)
    _FPS = 30.0
    _STOP_WAIT_MS = 2000

    def __init__(self, config: AcqConfig | None = None):
        super().__init__()
        self._config = config or AcqConfig()

    @property
    def timestamp_source(self) -> str:
        """The mock generates each frame at a known instant, so it can report a
        true acquisition time exactly like the real camera does."""
        return "camera"

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
            # Preview gets the bare frame; the recording sink gets the same
            # (frame, acquired_at, index) triple the real worker sends.
            self._publish(frame, record=(frame, acquired, n - 1))
            if n % int(self._FPS) == 0:
                self.fps_update.emit(n, n / max(time.perf_counter() - t0, 1e-9))
            nxt = t0 + n * period
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)
