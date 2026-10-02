"""Voltage camera and recording path: readout, timestamps, .dcimg, losses.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_camera.py [-q] [--part NAME]
"""
from __future__ import annotations

import collections
import math
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from _harness import (Report, qt_app, isolate_user_state, make_window, pump,
                      run_parts)
from acqApp.devices.voltage_cam import presets as P
from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
from acqApp.devices.voltage_cam.presets import AcqConfig
from acqApp.acq.clock import SessionClock
from acqApp.devices.voltage_cam.dcimg import (MIN_FRAMES, DcimgError,
                                              DcimgRecorder, frames_that_fit)
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import Writer


# ═══ readout (was test_readout_hz.py) ═══════════════════════════════════

def _part_readout() -> int:
    r = Report("readout-hz")
    rows_usb = P._ROWS_HZ_USB
    r.note(f"table: {len(rows_usb)} rows, {rows_usb[0][0]}–{rows_usb[-1][0]} "
           f"rows, default link {P.LINK_LABEL[P.DEFAULT_LINK]}")

    # ── every table row comes back exactly ───────────────────────────────────
    exact = True
    for rws, f_usb, f_cxp in P._ROWS_HZ_BOTH:
        exact &= (abs(P.readout_hz(rws, link=P.USB) - f_usb) < 1e-9
                  and abs(P.readout_hz(rws, link=P.CXP) - f_cxp) < 1e-9)
    r.check(exact, f"every one of the {len(P._ROWS_HZ_BOTH)} table rows is "
                   f"returned exactly, on both links")

    # ── the two links are genuinely different columns ────────────────────────
    r.check(abs(P.readout_hz(2368, link=P.USB) - 15.7) < 1e-9,
            f"full frame on USB3 is 15.7 Hz (got "
            f"{P.readout_hz(2368, link=P.USB):.1f})")
    r.check(abs(P.readout_hz(2368, link=P.CXP) - 115.0) < 1e-9,
            f"full frame on CoaXPress is 115 Hz (got "
            f"{P.readout_hz(2368, link=P.CXP):.1f})")
    ratio = P.readout_hz(2368, link=P.CXP) / P.readout_hz(2368, link=P.USB)
    r.check(ratio > 5.0,
            f"the link choice is worth {ratio:.1f}x — losing it (#11) is not a "
            f"rounding error")
    r.check(P.readout_hz(1500, link="nonsense") == P.readout_hz(1500, link=P.USB),
            "an unknown link falls back to the USB column, not to CoaXPress "
            "— an estimate that is too low only oversizes a buffer")

    # ── interpolation ────────────────────────────────────────────────────────
    # Log-log: the geometric midpoint of two rows maps to the geometric mean
    # of their rates. 512→524, 1024→264 on CXP.
    mid = math.sqrt(512 * 1024)
    want = math.sqrt(524.0 * 264.0)
    got = P.readout_hz(round(mid), link=P.CXP)
    r.check(abs(got - want) < 0.5,
            f"interpolates in log-log: {round(mid)} rows -> {got:.1f} Hz, "
            f"geometric mean of the neighbours is {want:.1f}")
    r.check(P.readout_hz(700, link=P.CXP) < P.readout_hz(600, link=P.CXP),
            "fewer rows is never slower")
    mono = all(P.readout_hz(a, link=P.CXP) >= P.readout_hz(b, link=P.CXP)
               for a, b in zip(range(4, 2400, 37), range(41, 2437, 37)))
    r.check(mono, "monotonic across the whole range (65 sample points)")

    # The physical claim the table stands in for.
    k = [rws * P.readout_hz(rws, link=P.CXP) for rws in (2368, 2048, 1024, 512, 256)]
    r.check(max(k) / min(k) < 1.1,
            f"rows x Hz is constant to {100 * (max(k) / min(k) - 1):.0f} % over "
            f"the mid range (readout really is row-by-row)")

    # ── clamps, so a silly ROI cannot extrapolate off the end ────────────────
    r.check(P.readout_hz(999_999, link=P.CXP) == 115.0,
            "more rows than the sensor has clamps to the full-frame rate")
    r.check(P.readout_hz(1, link=P.CXP) == 19500.0,
            "fewer rows than the smallest table entry clamps to its rate")
    r.check(P.readout_hz(0, link=P.CXP) == 19500.0 and
            P.readout_hz(-5, link=P.CXP) == 19500.0,
            "zero or negative rows clamps instead of dividing by zero")

    # ── binning is NOT a rate lever on this camera (2026-09-28: 512 rows at
    # bin 1/2/4 all read out in 1.893 ms) ─────────────────────────────────────
    r.check(all(P.readout_hz(2048, binning=b) == P.readout_hz(2048)
                for b in (0, 1, 2, 4, 8)),
            "binning does not change the readout ceiling")
    r.check(P.readout_hz(512) > P.readout_hz(2048) + 1.0,
            "control: fewer rows really is faster")
    cfg_b1 = P.AcqConfig(preset_key="4432x512", binning=1, exposure_us=500.0)
    cfg_b4 = P.AcqConfig(preset_key="4432x512", binning=4, exposure_us=500.0)
    r.check(cfg_b1.readout_hz == cfg_b4.readout_hz
            and cfg_b4.frame_bytes < cfg_b1.frame_bytes,
            "AcqConfig: binning shrinks the frame, not the readout ceiling")

    # ── the presets the panel actually offers ────────────────────────────────
    bad = [k for k in P.PRESET_KEYS
           if not (0.0 < P.readout_hz(P.PRESETS[k].vsize) < 1e6)]
    r.check(not bad, f"every preset in the dropdown yields a sane rate "
                     f"(bad: {bad})")
    r.check(P.DEFAULT_PRESET in P.PRESETS,
            f"the default preset {P.DEFAULT_PRESET!r} exists")

    # ── the offered list is trimmed; the datasheet table is NOT ──────────────
    small = [k for k in P.PRESET_KEYS if P.PRESETS[k].vsize < P.MIN_PRESET_ROWS]
    r.check(not small, f"no preset is offered below {P.MIN_PRESET_ROWS} rows "
                       f"(offending: {small})")
    r.check(min(P.PRESETS[k].vsize for k in P.PRESET_KEYS) == P.MIN_PRESET_ROWS,
            f"the smallest offered preset is exactly {P.MIN_PRESET_ROWS} rows")

    # Control: vacuous unless the table still HAS smaller rows to exclude.
    r.check(any(rws < P.MIN_PRESET_ROWS for rws, _u, _c in P._ROWS_HZ_BOTH),
            f"the datasheet table still carries rows below "
            f"{P.MIN_PRESET_ROWS} (trimming presets must not trim physics)")

    check_trigger_rate_request(r)
    return r.finish()


def check_trigger_rate_request(r: Report) -> None:
    """External edge under SYNCREADOUT, and a requested rate. Rig 2026-09-28,
    4432x512 / 500 us: EDGE 401 Hz, SYNCREADOUT 513 Hz."""
    edge = P.EXTERNAL_EDGE
    cfg = P.AcqConfig(preset_key="4432x512", binning=4, exposure_us=500.0,
                      trigger_mode=edge)
    r.check(cfg.trigger_hz < cfg.expected_hz,
            "trigger ceiling sits below free-running (the pad)")
    r.check(480.0 < cfg.trigger_hz < 530.0,
            f"512 rows estimates near the measured 513 Hz ({cfg.trigger_hz:.1f})")
    long_exp = P.AcqConfig(preset_key="4432x512", exposure_us=1500.0,
                           trigger_mode=edge)
    r.check(abs(long_exp.trigger_hz - cfg.trigger_hz) < 1e-9,
            "exposure doesn't move the SYNCREADOUT ceiling")
    r.check(not cfg.rate_unreachable, "no request: nothing unreachable")
    cfg500 = P.AcqConfig(preset_key="4432x512", exposure_us=500.0,
                         trigger_mode=edge, target_hz=500.0)
    r.check(not cfg500.rate_unreachable and abs(cfg500.rate_hz - 500.0) < 1e-9,
            "500 Hz at 4432x512 is reachable and is the rate")
    over = P.AcqConfig(preset_key="4432x512", exposure_us=500.0,
                       trigger_mode=edge, target_hz=900.0)
    r.check(over.rate_unreachable
            and abs(over.rate_hz - over.trigger_hz) < 1e-9,
            "900 Hz is unreachable and falls back to the ceiling")
    internal = P.AcqConfig(preset_key="4432x512", target_hz=900.0)
    r.check(internal.rate_unreachable
            and abs(internal.rate_hz - internal.readout_hz) < 1e-9,
            "Internal: beyond readout is unreachable too, ceiling = readout")

    # The interval written to the camera, from the MEASURED readout period.
    mpi = P.master_pulse_interval
    readout_s = 0.001893
    sync_floor = readout_s + P.MP_INTERVAL_PAD_S
    edge_floor = sync_floor + 500e-6
    r.check(1.0 / 500.0 > sync_floor + 1e-6,
            "control: a 500 Hz ask is slower than the floor")
    r.check(abs(mpi(readout_s, 500.0, True, 500.0) - 1.0 / 500.0) < 1e-9,
            "a reachable request is honoured exactly")
    r.check(abs(mpi(readout_s, 500.0, True, 5000.0) - sync_floor) < 1e-9,
            "an unreachable request clamps to the SYNCREADOUT floor")
    r.check(abs(mpi(readout_s, 500.0, True) - sync_floor) < 1e-9,
            "target 0 means the floor")
    r.check(abs(mpi(readout_s, 500.0) - edge_floor) < 1e-9
            and edge_floor > sync_floor,
            "default is the longer EDGE floor — can't bring back the half rate")

    # Exposure is always the longest the rate allows.
    fit = P.fit_exposure
    close = lambda a, b: abs(a[0] - b[0]) < 1e-12 and abs(a[1] - b[1]) < 1e-12
    r.check(close(fit(readout_s, 500.0, True, True), (0.002, 0.002)),
            "SYNCREADOUT at 500 Hz: exposure = the whole 2 ms interval")
    r.check(close(fit(readout_s, 5000.0, True, True), (sync_floor, sync_floor)),
            "SYNCREADOUT, unreachable: clamped to the floor, exposure fills it")
    r.check(close(fit(readout_s, 0.0, True, True), (sync_floor, sync_floor)),
            "SYNCREADOUT, Max: the floor")
    r.check(close(fit(readout_s, 500.0, True, False),
                  (0.002 - sync_floor, 0.002)),
            "EDGE at 500 Hz: exposure = interval - readout - pad, rate held")
    r.check(close(fit(readout_s, 0.0, True, False),
                  (P.MIN_EXPOSURE_US * 1e-6,
                   sync_floor + P.MIN_EXPOSURE_US * 1e-6)),
            "EDGE, Max: minimum exposure, fastest interval")
    r.check(close(fit(readout_s, 30.0, False), (1 / 30.0, 1 / 30.0)),
            "Internal at 30 Hz: exposure = 1/30 s")
    r.check(close(fit(readout_s, 5000.0, False), (readout_s, readout_s)),
            "Internal beyond readout: exposure = the readout period")
    slow = P.AcqConfig(preset_key="4432x512", trigger_mode=edge,
                       target_hz=250.0).fit_exposure()
    r.check(abs(slow.exposure_us - 4000.0) < 1e-6 and slow.rate_hz == 250.0,
            f"AcqConfig.fit_exposure: 250 Hz -> 4000 us ({slow.exposure_us:.1f})")


# ═══ timestamps (was test_camera_timestamps.py) ═════════════════════════

# Same shape as pylablib's DCAM.TFrameInfo.
FrameInfo = collections.namedtuple(
    "TFrameInfo",
    ["frame_index", "framestamp", "timestamp_us", "camerastamp",
     "position", "pixeltype"])

FPS = 115.0                 # CoaXPress full-frame rate
PERIOD = 1.0 / FPS
BATCH = 6                   # frames handed over per read
DROP_AFTER = 20             # skip one frame index here, like a real lost frame
CAM_EPOCH = 1_234_567.0     # camera clock epoch: arbitrary, unrelated to ours


class _Timings:
    frame_period = PERIOD


class _Status:
    def __init__(self, n):
        self.acquired, self.skipped, self.unread, self.buffer_size = n, 1, 0, 64


class FakeCam:
    """Just enough DCAMCamera to drive OrcaFireWorker: hands over frames in
    bursts, each stamped on the camera's own clock."""

    def __init__(self, shape):
        self._shape = shape
        self._idx = 0                       # camera's own frame counter
        self._t_us = CAM_EPOCH * 1e6        # camera timestamp, microseconds
        self.reads = 0

    # setup calls the worker makes — all no-ops here
    def set_roi(self, **kw): pass
    def set_exposure(self, s): pass
    def get_all_readout_speeds(self): return ["slow", "fast"]
    def get_readout_speed(self): return "fast"
    def set_readout_speed(self, s): pass
    def get_frame_timings(self): return _Timings()
    def start_acquisition(self, nframes=None): pass
    def stop_acquisition(self): pass
    def close(self): pass
    def get_frames_status(self): return _Status(self._idx)
    def read_newest_image(self): return np.zeros(self._shape, dtype=np.uint16)

    def wait_for_frame(self, timeout=None):
        # The interval the old code wrongly stamped frames with.
        time.sleep(BATCH * PERIOD)

    def read_multiple_images(self, return_info=False):
        self.reads += 1
        frames, infos = [], []
        for _ in range(BATCH):
            if self._idx == DROP_AFTER:     # one frame lost in the driver
                self._idx += 1
                self._t_us += PERIOD * 1e6
            frames.append(np.full(self._shape, self._idx % 1000, dtype=np.uint16))
            infos.append(FrameInfo(self._idx, self._idx, self._t_us, 0, (0, 0), 1))
            self._idx += 1
            self._t_us += PERIOD * 1e6
        return (frames, infos) if return_info else frames


def _part_timestamps() -> int:
    r = Report("camera-ts")
    qt_app()                                # the worker declares pyqtSignals

    # Smallest offered preset, binned hard: a cheap frame shape.
    cfg = AcqConfig(preset_key="4432x512", binning=4, exposure_us=1000.0)
    clock = SessionClock()
    clock.start()
    n_target = BATCH * 8

    # ── the fix ──────────────────────────────────────────────────────────────
    cam = FakeCam(cfg.frame_shape)
    worker = OrcaFireWorker(0, cfg, cam=cam)
    got: list[tuple[float, int]] = []

    def sink(item):
        frame, at, index = item
        # Exactly what adapters.VoltageCamModule hands to Recorder.put(at=...).
        got.append((clock.now() if at is None else clock.at(at), index))
        if len(got) >= n_target:
            worker._stop = True

    worker.set_sink(sink)
    worker._run()                           # drive the loop directly, no QThread

    r.note(f"{len(got)} frames over {cam.reads} reads (batch size {BATCH})")
    r.check(len(got) >= n_target, "worker delivered every frame in each batch")
    r.check(cam.reads >= 2, "more than one batch was read (the bug's precondition)")
    r.check(worker.timestamp_source == "camera",
            f"worker reports the camera timebase (got {worker.timestamp_source!r})")

    ts = np.array([g[0] for g in got])
    idx = np.array([g[1] for g in got])

    steps = np.diff(idx)
    r.check(bool(np.all(steps >= 1)), "frame indices never go backwards")
    r.check(int((steps == 2).sum()) == 1,
            f"the dropped frame shows as one index jump "
            f"(jumps={sorted(set(steps.tolist()))})")

    dt = np.diff(ts)
    normal = dt[steps == 1]                 # the drop legitimately doubles one
    r.check(bool(np.all(normal > 0)), "no two frames share a timestamp")
    r.check(abs(normal.mean() - PERIOD) < PERIOD * 0.02,
            f"mean interval {normal.mean()*1e3:.3f} ms ≈ frame period "
            f"{PERIOD*1e3:.3f} ms")
    r.check(normal.std() < PERIOD * 0.01,
            f"intervals are even (std {normal.std()*1e6:.1f} µs), not batched")
    gap = dt[steps == 2]
    r.check(len(gap) == 1 and abs(gap[0] - 2 * PERIOD) < PERIOD * 0.05,
            "the dropped frame leaves a double-length gap in the timestamps")
    zero_runs = int((dt < PERIOD * 0.1).sum())
    r.check(zero_runs == 0, "no batch-quantised (near-zero) intervals remain")

    # ── control: the OLD behaviour must fail those checks ────────────────────
    cam2 = FakeCam(cfg.frame_shape)
    w2 = OrcaFireWorker(0, cfg, cam=cam2)
    old: list[float] = []

    def old_sink(item):
        old.append(clock.now())             # arrival time, ignoring item[1]
        if len(old) >= n_target:
            w2._stop = True

    w2.set_sink(old_sink)
    w2._run()
    old_dt = np.diff(np.array(old))
    old_zero = int((old_dt < PERIOD * 0.1).sum())
    r.note(f"control (arrival-stamped): {old_zero} near-zero intervals, "
           f"std {old_dt.std()*1e3:.2f} ms  |  fixed: {zero_runs}, "
           f"std {normal.std()*1e3:.2f} ms")
    r.check(old_zero > n_target // 2,
            "control reproduces the batching bug (so the test can detect it)")

    return r.finish()


# ═══ dcimg (was test_dcimg.py) ══════════════════════════════════════════

def check_frame_cap(r: Report, tmp: Path) -> None:
    """`maxframepersession` is bounded by the DRIVE, not by memory: at the rig
    1e6 frames of 128 KB bound on D: (678 GB free) and failed on C:, and 1e7
    failed on both with DCAMERR_FAILEDWRITEDATA."""
    free = shutil.disk_usage(tmp).free
    small = frames_that_fit(tmp, 1 << 20)           # 1 MB frames
    r.check(small > 0, f"some frames fit on a drive with {free/1e9:.0f} GB free")
    r.check(small <= free // (1 << 20),
            f"the cap never exceeds free space ({small} frames of 1 MB)")

    # Within one frame: free space moves under us, and the division truncates.
    r.check(abs(frames_that_fit(tmp, 1 << 19) - small * 2) <= 2,
            "the cap scales inversely with frame size")

    r.check(frames_that_fit(tmp, free * 2) == 0,
            "a frame larger than the drive fits zero of them")

    for bad in (0, -1):
        try:
            frames_that_fit(tmp, bad)
            r.check(False, f"frame_bytes={bad} must raise")
        except ValueError:
            r.check(True, f"frame_bytes={bad} raises rather than dividing by it")

    # Normal: the recorder is sized before the writer makes the folder.
    deep = tmp / "not" / "made" / "yet" / "x.dcimg"
    r.check(frames_that_fit(deep, 1 << 20) == small,
            "an unmade path measures the nearest existing parent's drive")


def check_naming(r: Report, tmp: Path) -> None:
    """DCAM is handed the STEM plus ext="dcimg"; it appends the extension."""
    rec = DcimgRecorder(tmp / "s_voltage_cam", max_frames=100)
    r.check(rec.path.name == "s_voltage_cam.dcimg",
            f"a stem gains the extension (got {rec.path.name})")
    r.check(rec._stem.endswith("s_voltage_cam")
            and not rec._stem.endswith(".dcimg"),
            f"…but DCAM is handed the stem, not the name (got {rec._stem})")

    rec = DcimgRecorder(tmp / "s.dcimg", max_frames=100)
    r.check(rec.path.name == "s.dcimg", "a full name is left alone")
    r.check(not rec.is_open, "a recorder is not open until open() is called")


def check_cap_guards(r: Report, tmp: Path) -> None:
    """0 is DCAMERR_INVALIDVALUE, not "unlimited" — the bug that cost the
    first two spike runs. Caught before the DLL sees it."""
    rec = DcimgRecorder(tmp / "zero", max_frames=0)
    try:
        rec.open()
        r.check(False, "max_frames=0 must be refused before the DLL is called")
    except DcimgError as e:
        r.check("unlimited" in str(e),
                f"…and the refusal says why (got {e})")

    try:
        DcimgRecorder.for_frames(tmp / "huge", frame_bytes=shutil.disk_usage(tmp).free)
        r.check(False, "for_frames must refuse a frame the drive can't hold")
    except DcimgError as e:
        r.check("no room" in str(e), f"…naming the drive (got {e})")

    fit = DcimgRecorder.for_frames(tmp / "ok", frame_bytes=1 << 20)
    r.check(fit.max_frames >= MIN_FRAMES,
            f"a sane frame size gets a usable cap ({fit.max_frames:,})")
    capped = DcimgRecorder.for_frames(tmp / "ok", frame_bytes=1 << 10, cap=100)
    r.check(capped.max_frames == 100,
            f"a known length caps it below the drive ({capped.max_frames})")
    r.check(DcimgRecorder.for_frames(tmp / "ok", frame_bytes=1 << 20,
                                     cap=1 << 40).max_frames == fit.max_frames,
            "...but never above what the drive holds")

    # close() runs on the failure path.
    fit.close()
    fit.close()
    r.check(True, "close() on an unopened recorder is a no-op, twice over")


def check_host_wiring(r: Report, app, tmp: Path) -> None:
    """When the .dcimg path is chosen, and what the file is called."""
    win = make_window({"voltage_cam", "routines"})
    sp = win._save_panel

    sp._cmb_orca_format.setCurrentIndex(
        sp._cmb_orca_format.findData("dcimg"))
    sp._on_edited()
    r.check(win.dcimg_enabled(), "DCIMG on: every session is a folder of files")

    r.check(win.dcimg_target("voltage_cam") is None,
            "no target before a recording opens, even when enabled")

    win._rec_path = tmp / "M1_20260923_120000"
    got = win.dcimg_target("voltage_cam")
    r.check(got == win._rec_path / "M1_20260923_120000_voltage_cam.dcimg",
            f"named like SplitWriter's other per-stream files (got {got})")

    sp._cmb_orca_format.setCurrentIndex(sp._cmb_orca_format.findData("tiff"))
    sp._on_edited()
    r.check(win.dcimg_target("voltage_cam") is None,
            "TIFF asks for no target, so the sink path runs unchanged")


def check_routine_counts_recorder(r: Report, app) -> None:
    """A routine's frames-unit Wait counts what reached the FILE. When DCAM
    is writing it, `Recorder.offered()` never moves — the recorder's own
    total is the same quantity, and is what the engine must read instead."""
    from acqApp.routines.settings import Step

    win = make_window({"voltage_cam", "routines", "stage"})
    mods = {m.key: m for m in win._modules}
    adapter, cam = mods["routines"], mods["voltage_cam"]

    r.check(win.dcimg_frames("voltage_cam") is None,
            "no .dcimg open — the host says so with None, not 0")

    # Writing one, at a count no sink could have produced.
    class FakeWorker:
        dcimg_active = True
        dcimg_frames = 41
        dcimg_missing = 0
        dcimg_span = None
    cam.worker = FakeWorker()
    r.check(win.dcimg_frames("voltage_cam") == 41,
            f"…and reports the recorder's own total while one is "
            f"(got {win.dcimg_frames('voltage_cam')})")

    adapter.panel._r.steps = [Step(kind="wait", length=5, unit="frames")]
    adapter.panel._reload_table()
    hooks = adapter._hooks()
    r.check(hooks.frames() == 41,
            f"the engine's frames() hook reads it, not offered() "
            f"(got {hooks.frames()})")

    # 0 is not "no .dcimg": None would count from a Recorder that never moves.
    FakeWorker.dcimg_frames = 0
    r.check(hooks.frames() == 0,
            "a .dcimg with nothing in it yet still counts 0, not None")

    FakeWorker.dcimg_active = False
    r.check(hooks.frames() is None or hooks.frames() == 0,
            "with no .dcimg open it falls back to the Recorder")

    # It used to be refused.
    cam.worker = None
    sp = win._save_panel
    sp._cmb_orca_format.setCurrentIndex(sp._cmb_orca_format.findData("dcimg"))
    sp._on_edited()
    adapter._start()
    r.check(adapter._engine is not None,
            "a routine starts with ORCA format set to DCIMG")
    adapter._abort()


def check_wait_for_camera(r: Report, app) -> None:
    """A .dcimg roll stops the camera for ~0.9 s. The step that roll belongs
    to armed its clock BEFORE the roll, so ticking through the gap files a
    trial short by exactly that much — hold, then restart the step's clock."""
    from acqApp.routines.settings import Routine, Step

    rt = Routine(wait_for_camera=False)
    r.check(Routine.from_dict(rt.to_dict()).wait_for_camera is False,
            "wait_for_camera round-trips through to_dict/from_dict")
    r.check(Routine.from_dict({"name": "old"}).wait_for_camera is True,
            "…and defaults ON when the key is absent (a pre-existing file)")

    win = make_window({"voltage_cam", "routines", "stage"})
    adapter = {m.key: m for m in win._modules}["routines"]
    adapter.panel._r.steps = [Step(kind="wait", length=30, unit="seconds")]
    adapter.panel._reload_table()
    adapter._start()
    eng = adapter._engine
    r.check(eng is not None, "fixture: the routine is running")

    pump(app, 0.15)
    before = eng.progress()
    r.check(before > 0, f"fixture: the wait has started ({before:.3f})")
    r.check(eng.rearm_step() is True, "rearm_step() re-arms a Wait step")
    r.check(eng.progress() < before,
            f"…and its clock restarts ({eng.progress():.3f} < {before:.3f})")

    # The clock's ORIGIN, not `progress()` (wall time, climbs regardless).
    win.camera_ready = lambda _k: False
    adapter._hold_t0 = time.monotonic()
    t_armed = eng._wait_t0
    pump(app, 0.2)
    r.check(eng._wait_t0 == t_armed,
            "while held, the step's clock origin is left alone")

    win.camera_ready = lambda _k: True
    pump(app, 0.1)
    r.check(adapter._hold_t0 is None, "the hold releases once frames resume")
    r.check(eng._wait_t0 > t_armed,
            "…and the step's clock restarts from the release, so the gap "
            "is not counted against the trial")

    # Re-issuing a move or resetting a trigger's baseline would undo the step.
    adapter._abort()
    adapter.panel._r.steps = [Step(kind="move", x_um=0.0)]
    adapter.panel._reload_table()
    adapter._start()
    r.check(adapter._engine.rearm_step() is False,
            "rearm_step() refuses a non-Wait step")
    adapter._abort()


def check_cap_vs_gated(r: Report) -> None:
    """FILLED vs merely not capturing: DCAM clears RECORDING for a camera
    gated on a trigger step's edge too, and a routine aborted on a 0-frame
    file."""
    from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker as W
    from acqApp.devices.voltage_cam.dcimg import RecStatus

    def st(total, recording):
        return RecStatus(total=total, index=0, missing=0, recording=recording)

    cap = 156_250
    r.check(W._hit_frame_cap(st(cap, False), cap) is True,
            "a stopped recorder AT the cap really is full")
    r.check(W._hit_frame_cap(st(cap + 10, False), cap) is True,
            "…and so is one past it")
    r.check(W._hit_frame_cap(st(0, False), cap) is False,
            "a gated 0-frame recorder is NOT full (the rig abort)")
    r.check(W._hit_frame_cap(st(cap // 2, False), cap) is False,
            "nor is a half-full one paused between trigger steps")
    r.check(W._hit_frame_cap(st(cap, True), cap) is False,
            "still recording is never 'stopped at the cap'")


def check_swap_rearms_in_order(r: Report, tmp: Path) -> None:
    """A trigger step's file swap must bind the NEW recorder while capture is
    stopped, cycle the master pulse, and only then start - so capture comes
    back gated with the file already open. The plain swap must not cycle."""
    from types import SimpleNamespace

    from acqApp.devices.voltage_cam import acquisition as acq
    from acqApp.devices.voltage_cam import dcimg as dc

    calls: list = []

    class FakeCam:
        handle = "H"

        def stop_acquisition(self):
            calls.append("stop")

        def start_acquisition(self, nframes=None):
            calls.append(("start", nframes))

        def set_attribute_value(self, name, value, error_on_missing=True):
            calls.append(("set", value))

        def get_roi(self):
            return (0, 16, 0, 8, 1, 1)

    class FakeRec:
        path = tmp / "f.dcimg"
        max_frames = 10

        @classmethod
        def for_frames(cls, path, bpf, cap=None):
            calls.append(("cap", cap))
            return cls()

        def open(self):
            calls.append("open")

        def attach(self, h):
            calls.append(("attach", h))

        def close(self):
            calls.append("close")

        def status(self):
            return SimpleNamespace(total=0, missing=0, recording=True,
                                   session=0)

    real = dc.DcimgRecorder
    dc.DcimgRecorder = FakeRec
    try:
        w = acq.OrcaFireWorker.__new__(acq.OrcaFireWorker)
        w._dcimg = None
        w._dcimg_total = w._dcimg_missing = 0
        w._dcimg_t0 = w._dcimg_t1 = None
        w._dcimg_full = False
        w._exp_lock = threading.Lock()
        w._rearm_with_file = False
        w._mp_mode = acq._MP_MODE_START

        w._swap_dcimg(FakeCam(), tmp / "a.dcimg", rearm_nframes=59, cap=800)
        want = [("cap", 800), "open", "stop", ("attach", "H"),
                ("set", acq._MP_MODE_CONTINUOUS), ("set", acq._MP_MODE_START),
                ("start", 59)]
        r.check(calls == want,
                f"swap with re-arm: open (at its cap), stop, bind, cycle the "
                f"pulse, then start ({calls})")

        calls.clear()
        w._swap_dcimg(FakeCam(), tmp / "b.dcimg")
        r.check(calls == [("cap", None), "open", "stop", "close",
                          ("attach", "H"), ("start", None)],
                f"control: a plain swap never touches the master pulse "
                f"({calls})")

        w._burst_n, w._syncreadout = 2500, True
        r.check(w._file_cap(True) == 8 * 2501,
                f"a routine's trial file in burst mode is capped at a few "
                f"bursts ({w._file_cap(True)})")
        r.check(w._file_cap(False) is None,
                "an operator's recording keeps the drive-sized cap")
        w._burst_n = 0
        r.check(w._file_cap(True) is None,
                "control: outside burst mode the length is unknown, no cap")

        w.arm_with_next_file()
        r.check(w._rearm_with_file, "arm_with_next_file sets the latch")

        class BadAttach(FakeRec):
            def attach(self, h):
                calls.append("attach-fails")
                raise RuntimeError("DCAMERR_INVALIDHANDLE")
        dc.DcimgRecorder = BadAttach
        calls.clear()
        try:
            w._swap_dcimg(FakeCam(), tmp / "c.dcimg")
            r.check(False, "a failed attach must raise")
        except RuntimeError:
            r.check(calls == [("cap", None), "open", "stop", "close",
                              "attach-fails", "close", ("start", None)]
                    and w._dcimg is None,
                    f"a recorder that never bound is closed, and capture "
                    f"still restarts ({calls})")
    finally:
        dc.DcimgRecorder = real


def _part_dcimg() -> int:
    r = Report("dcimg")
    isolate_user_state()
    app = qt_app()
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dcimg_"))
    try:
        check_frame_cap(r, tmp)
        check_naming(r, tmp)
        check_cap_guards(r, tmp)
        check_cap_vs_gated(r)
        check_host_wiring(r, app, tmp)
        check_routine_counts_recorder(r, app)
        check_wait_for_camera(r, app)
        check_swap_rearms_in_order(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ losses (was test_recording_losses.py) ══════════════════════════════

def frame(i: int):
    """A sized sample, shaped like the real (stream, ts, data) tuple."""
    return ("voltage_cam", float(i), np.zeros((64, 64), dtype=np.uint16))


def event(i: int):
    """A zero-byte sample: a puff, a DMD frame — sparse and irreplaceable."""
    return ("puffer", float(i), 0.1)


def sizeof(item) -> int:
    data = item[2]
    return int(data.nbytes) if isinstance(data, np.ndarray) else 0


# ── #14 ───────────────────────────────────────────────────────────────────────

def check_count_cap(r: Report) -> None:
    buf = RingBuffer(maxlen=4, maxbytes=None, sizeof=sizeof)
    buf.put(event(0))
    for i in range(1, 11):
        buf.put(frame(i))

    kept = [buf.get_nowait() for _ in range(len(buf))]
    streams = [k[0] for k in kept]
    r.check("puffer" in streams,
            f"the event survived 10 frames of overflow (kept {streams})")
    r.check(len(kept) == 4, f"count cap held at maxlen (kept {len(kept)})")
    r.check(buf.drop_count == 7, f"every eviction counted (got {buf.drop_count})")

    # Control: the old policy; else this passes if events stop arriving.
    old: deque = deque()
    for item in [event(0)] + [frame(i) for i in range(1, 11)]:
        old.append(item)
        while len(old) > 4:
            old.popleft()
    r.check("puffer" not in [o[0] for o in old],
            "control: the old drop-oldest rule loses the event")

    buf2 = RingBuffer(maxlen=3, maxbytes=None, sizeof=sizeof)
    for i in range(5):
        buf2.put(event(i))
    kept2 = [buf2.get_nowait() for _ in range(len(buf2))]
    r.check(len(kept2) == 3 and buf2.drop_count == 2,
            "with only events buffered, the oldest events are shed")
    r.check([k[1] for k in kept2] == [2.0, 3.0, 4.0],
            f"and it is the OLDEST that go (kept {[k[1] for k in kept2]})")

    buf3 = RingBuffer(maxlen=1000, maxbytes=3 * 64 * 64 * 2, sizeof=sizeof)
    buf3.put(event(0))
    for i in range(1, 9):
        buf3.put(frame(i))
    kept3 = [buf3.get_nowait() for _ in range(len(buf3))]
    r.check("puffer" in [k[0] for k in kept3],
            "byte cap still spares the event")
    r.check(sum(sizeof(k) for k in kept3) <= 3 * 64 * 64 * 2,
            "byte cap still bounds the buffered payload")


# ── #10 ───────────────────────────────────────────────────────────────────────

class SpyWriter(Writer):
    """Records what happened to it, in order."""

    def __init__(self) -> None:
        self.events: list = []
        self.written: list = []
        self.meta: dict = {}

    def open(self, path: Path, metadata: dict) -> None:
        self.events.append("open")
        self.meta.update(metadata)

    def write(self, stream: str, timestamp: float, data) -> None:
        self.written.append((stream, timestamp, data))

    def update_metadata(self, metadata: dict) -> None:
        self.events.append(("meta", dict(metadata)))
        self.meta.update(metadata)

    def close(self) -> None:
        self.events.append("close")


def new_recorder(clock=None, maxlen=512):
    clock = clock or SessionClock()
    w = SpyWriter()
    return Recorder(clock, w, RingBuffer(maxlen, sizeof=sizeof)), w, clock


def check_late_samples(r: Report) -> None:
    rec, w, clock = new_recorder()
    clock.start()
    rec.start(Path("unused.h5"), {"subject": "m17"})
    for i in range(5):
        rec.put("wheel", float(i))

    seen: dict = {}

    def final() -> dict:
        seen["at_call"] = list(w.events)
        return {"drops": rec.drop_count, "late": rec.late_count}

    remaining = rec.stop(final_metadata=final)
    r.check(remaining == 0, f"clean drain (remaining {remaining})")
    r.check(len(w.written) == 5, f"all 5 samples written (got {len(w.written)})")
    r.check("close" not in seen.get("at_call", ["close"]),
            "final metadata is gathered while the file is still open")
    r.check(w.events[-2:] == [("meta", {"drops": 0, "late": 0}), "close"],
            f"metadata written immediately before close (got {w.events[-2:]})")

    # A worker still inside its callback when the sinks were detached.
    for _ in range(3):
        rec.put("wheel", 99.0)
    r.check(rec.late_count == 3, f"late samples counted (got {rec.late_count})")
    r.check(len(w.written) == 5, "and none of them reached the closed writer")

    # Each lost sample belongs to exactly one bucket.
    r.check(rec.drop_count == 0 and rec.unstamped_count == 0,
            "the other counters stayed at zero")


def check_unstamped(r: Report) -> None:
    """A sample offered before the clock started has no timebase to land on."""
    rec, w, clock = new_recorder()                 # clock NOT started
    rec.start(Path("unused.h5"), {})
    rec.put("wheel", 1.0)
    rec.put("wheel", 2.0)
    r.check(rec.unstamped_count == 2,
            f"pre-start samples counted (got {rec.unstamped_count})")
    r.check(rec.late_count == 0, "not miscounted as late")
    clock.start()
    rec.put("wheel", 3.0)
    rec.stop()
    r.check(len(w.written) == 1,
            f"only the stamped sample was written (got {len(w.written)})")


def check_drops_counted(r: Report) -> None:
    """An overflowing buffer must show up in drop_count, not vanish."""
    rec, w, clock = new_recorder(maxlen=4)
    clock.start()
    # No start(): no writer thread draining.
    for i in range(20):
        rec.put("voltage_cam", np.zeros((8, 8), dtype=np.uint16))
    r.check(rec.drop_count == 16,
            f"shed samples are counted (got {rec.drop_count})")


def check_offered_never_blocks(r: Report) -> None:
    """`offered()`, read ~70×/s from the GUI thread by a routine, must not
    wait on the enqueue gate (locked: 6.1 ms mean, 28.7 ms worst stall).
    Deterministic: hold the gate and require the read to return."""
    rec, _w, clock = new_recorder()
    clock.start()
    rec.start(Path("unused.h5"), {})
    for i in range(7):
        rec.put("wheel", float(i))
    r.check(rec.offered("wheel") == 7,
            f"offered() counts what was handed over (got {rec.offered('wheel')})")
    r.check(rec.offered("nothing_here") == 0,
            "…and 0 for a stream nothing has written")

    got: list = []
    held = threading.Event()

    def reader() -> None:
        held.wait(2.0)
        got.append(rec.offered("wheel"))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    with rec._gate:                     # the lock every put() takes
        held.set()
        th.join(timeout=1.0)
    r.check(not th.is_alive() and got == [7],
            f"offered() returns while the enqueue gate is held (got {got})")

    # CONTROL: the same read WITH the gate hangs.
    stuck: list = []
    go = threading.Event()

    def locked_reader() -> None:
        go.wait(2.0)
        with rec._gate:
            stuck.append(rec.offered("wheel"))

    th2 = threading.Thread(target=locked_reader, daemon=True)
    th2.start()
    with rec._gate:
        go.set()
        th2.join(timeout=0.3)
        blocked = th2.is_alive()
    th2.join(timeout=1.0)
    r.check(blocked, "control: a read that does take the gate blocks there")

    rec.stop()


# ── #8 ────────────────────────────────────────────────────────────────────────

def check_no_hot_spin(r: Report) -> None:
    RUN_S = 1.0

    class BrokenCam(FakeCam):
        """A camera whose link has gone: every wait fails instantly."""

        def __init__(self, shape):
            super().__init__(shape)
            self.waits = 0
            self.t0 = 0.0           # set on the first wait: the worker's setup
            self.deadline = 0.0     # (pylablib import, ROI, timings) is not free
            self.worker = None

        def wait_for_frame(self, timeout=None):
            if not self.waits:
                self.t0 = time.perf_counter()
                self.deadline = self.t0 + RUN_S
            self.waits += 1
            if time.perf_counter() >= self.deadline:
                self.worker._stop = True
            raise RuntimeError("link down")

    cfg = AcqConfig(preset_key="4432x512", binning=4, exposure_us=1000.0)
    cam = BrokenCam(cfg.frame_shape)
    worker = OrcaFireWorker(0, cfg, cam=cam)
    cam.worker = worker

    worker._run()                       # must return, not raise, not spin
    dt = time.perf_counter() - cam.t0   # time spent in the retry loop only

    # For scale: the loop with no pause at all.
    t1 = time.perf_counter()
    unpaced = 0
    while time.perf_counter() - t1 < 0.05:
        try:
            raise RuntimeError("link down")
        except RuntimeError:
            unpaced += 1
    unpaced_rate = unpaced / 0.05

    r.info(f"{cam.waits} retries in {dt:.2f} s "
           f"({cam.waits / dt:.0f}/s); unpaced would be ~{unpaced_rate:,.0f}/s")
    r.check(cam.waits > 1, "the worker kept retrying (a late trigger is normal)")
    r.check(cam.waits / dt < 100,
            f"retries are paced, not spinning ({cam.waits / dt:.0f}/s)")
    r.check(dt >= RUN_S, f"the loop ran for the full window ({dt:.2f} s)")


# ── the diagnostics themselves (2026-08-17) ──────────────────────────────────
# A loss reported with the WRONG CAUSE sends the next session after the wrong
# fix. These three all misreported.

def check_skip_report_blames_the_loop(r: Report) -> None:
    """A camera skip is a read-loop shortfall, never the writer's: the sink
    only enqueues, so a slow writer sheds (and is counted) in the ring."""

    class St:
        skipped, unread, buffer_size = 143, 38, 38

    msg = OrcaFireWorker._skip_report(St())
    r.check("143" in msg and "38/38" in msg,
            "the skip report carries the counts")
    r.check("writer cannot keep up" not in msg,
            "it no longer blames the writer for a driver-buffer overflow")
    r.check("read loop" in msg,
            f"it names the read loop as the cause (got: {msg[:60]}...)")
    # Control: a message that just deleted the word would pass the above.
    r.check("WRITER" in msg,
            "it still distinguishes the writer's separate count")


def check_memory_capped_buffer_is_announced(r: Report) -> None:
    """A 768 MB budget silently cut full frame from 2 s of slack to 0.33 s
    (~6% real frame loss). The memory-cap branch is exercised with a shrunk
    budget rather than this machine's headroom."""
    import io
    from contextlib import redirect_stdout

    def sizing(worker, cfg, hz):
        buf = io.StringIO()
        with redirect_stdout(buf):
            n = worker._buffer_frames(cfg, hz)
        return n, buf.getvalue()

    full = AcqConfig()                                  # full frame, ~21 MB

    w = OrcaFireWorker(0, full)
    n_full, out_full = sizing(w, full, 115.0)
    r.check(n_full == int(115.0 * OrcaFireWorker._BUFFER_SECONDS),
            f"full frame at the real CXP rate now gets the full "
            f"{OrcaFireWorker._BUFFER_SECONDS} s ({n_full} frames), not "
            f"memory-capped")
    r.check("MEMORY-capped" not in out_full,
            "and stays quiet now that the budget covers it")

    w_tight = OrcaFireWorker(0, full)
    w_tight._BUFFER_BYTES = 768 << 20
    n_tight, out_tight = sizing(w_tight, full, 115.0)
    r.check(n_tight < 115.0 * OrcaFireWorker._BUFFER_SECONDS,
            f"a tight budget really does cap full frame ({n_tight} frames)")
    r.check("MEMORY-capped" in out_tight,
            "and the shortfall is announced, not left in the arithmetic")
    r.check("GiB" in out_tight,
            "the announcement says what the full slack would cost")

    # Control: or the above would pass on a warning that always fires.
    small = AcqConfig(preset_key="4432x512", binning=4)  # ~0.28 MB
    n_small, out_small = sizing(w_tight, small, 115.0)
    r.check(n_small == int(115.0 * OrcaFireWorker._BUFFER_SECONDS),
            f"a small frame gets the full {OrcaFireWorker._BUFFER_SECONDS} s "
            f"({n_small} frames)")
    r.check("MEMORY-capped" not in out_small,
            "and stays quiet — the warning is not unconditional")


def check_readout_speed_absence_is_reported(r: Report) -> None:
    """`get_all_readout_speeds() == []` on this model, so the 'fast' path never
    ran and silently looked like it had."""

    class NoSpeeds:
        def get_all_readout_speeds(self): return []
        def get_readout_speed(self): return 1

    class HasSpeeds:
        def __init__(self): self.set_to = None
        def get_all_readout_speeds(self): return ["slow", "fast"]
        def get_readout_speed(self): return "slow"
        def set_readout_speed(self, v): self.set_to = v

    class Broken:
        def get_all_readout_speeds(self): raise RuntimeError("no such property")
        def get_readout_speed(self): raise RuntimeError("no such property")

    r.check(OrcaFireWorker._maximise_readout_speed(NoSpeeds()) == "absent",
            "a camera with no selectable speeds reports 'absent', not success")
    # Control: or the fix could be a way of never setting the speed.
    cam = HasSpeeds()
    r.check(OrcaFireWorker._maximise_readout_speed(cam) == "set"
            and cam.set_to == "fast",
            "a camera that offers 'fast' is still switched to it")
    r.check(OrcaFireWorker._maximise_readout_speed(Broken()) == "error",
            "a camera that raises reports 'error', and does not propagate")


def check_nocamera_retry(r: Report) -> None:
    """DCAMERR_NOCAMERA is sometimes a transient USB-enumeration race in the
    driver; open_camera() retries that code alone, a bounded number of times.
    """
    import acqApp.devices.voltage_cam.acquisition as ACQ
    import pylablib.devices.DCAM as real_dcam
    from pylablib.devices.DCAM.dcamapi4_defs import DCAMERR
    from pylablib.devices.DCAM.dcamapi4_lib import DCAMLibError

    real_ctor = real_dcam.DCAMCamera
    real_delay = ACQ._NOCAMERA_RETRY_DELAY_S
    ACQ._NOCAMERA_RETRY_DELAY_S = 0.0

    calls = {"n": 0}

    def flaky_then_ok(idx=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise DCAMLibError("dcamapi_init", DCAMERR.DCAMERR_NOCAMERA)
        return "THE_HANDLE"

    real_dcam.DCAMCamera = flaky_then_ok
    try:
        got = ACQ.open_camera(0)
    finally:
        real_dcam.DCAMCamera = real_ctor
        ACQ._NOCAMERA_RETRY_DELAY_S = real_delay
    r.check(got == "THE_HANDLE" and calls["n"] == 3,
            f"two NOCAMERA failures are retried past, landing the real open "
            f"on attempt {calls['n']}")

    always_calls = {"n": 0}

    def always_nocamera(idx=0):
        always_calls["n"] += 1
        raise DCAMLibError("dcamapi_init", DCAMERR.DCAMERR_NOCAMERA)

    real_dcam.DCAMCamera = always_nocamera
    ACQ._NOCAMERA_RETRY_DELAY_S = 0.0
    try:
        try:
            ACQ.open_camera(0)
            r.check(False, "a camera that never appears must still raise")
        except DCAMLibError as e:
            r.check(e.name == "DCAMERR_NOCAMERA"
                    and always_calls["n"] == ACQ._NOCAMERA_RETRIES,
                    f"…with the real error after exactly "
                    f"{ACQ._NOCAMERA_RETRIES} attempts, not fewer, not "
                    f"forever ({always_calls['n']})")
    finally:
        real_dcam.DCAMCamera = real_ctor
        ACQ._NOCAMERA_RETRY_DELAY_S = real_delay

    other_calls = {"n": 0}

    def other_error(idx=0):
        other_calls["n"] += 1
        raise DCAMLibError("dcamapi_init", DCAMERR.DCAMERR_BUSY)

    real_dcam.DCAMCamera = other_error
    try:
        try:
            ACQ.open_camera(0)
            r.check(False, "a non-NOCAMERA DCAM error must still raise")
        except DCAMLibError as e:
            r.check(e.name != "DCAMERR_NOCAMERA" and other_calls["n"] == 1,
                    f"…on the first attempt, not retried ({e.name}, "
                    f"{other_calls['n']} call(s))")
    finally:
        real_dcam.DCAMCamera = real_ctor


def check_edge_rate_reported(r: Report) -> None:
    """External edge reports the rate its interval sets, not Internal's: the
    panel said 528 Hz while files came out at 446 (rig, 2026-09-30), which
    read as lost frames. And exposure fills whatever the rate leaves."""
    import io
    from contextlib import redirect_stdout
    from acqApp.devices.voltage_cam.presets import (
        EXTERNAL_EDGE, master_pulse_interval)

    class Cam(FakeCam):
        def __init__(self, shape):
            super().__init__(shape)
            self.props: dict = {}
            self.exposure = None

        def set_exposure(self, s):
            self.exposure = s

        def set_attribute_value(self, name, value, error_on_missing=True):
            self.props[name] = value

    readout = 1.8940e-3                       # rig, 4432x512
    for sync in (True, False):
        cfg = AcqConfig(preset_key="4432x512", binning=4,
                        trigger_mode=EXTERNAL_EDGE, target_hz=400.0)
        cam = Cam(cfg.frame_shape)
        w = OrcaFireWorker(0, cfg, cam=cam)
        w._master_pulse, w._syncreadout, w._readout_s = True, sync, readout
        with redirect_stdout(io.StringIO()):
            w._apply_rate(cam, cfg)
            got = w._query_timings(cam, cfg)
        name = "SYNCREADOUT" if sync else "EDGE"
        r.check(abs(got - 400.0) < 1e-6 and abs(w.achievable_hz - 400.0) < 1e-6,
                f"{name}: reports the interval's rate, 400 Hz ({got:.2f})")
        r.check(abs(cam.props.get("MASTER PULSE INTERVAL", 0) - 2.5e-3) < 1e-12,
                f"{name}: interval written = 1/400 s")
        want_exp = 2.5e-3 if sync else 2.5e-3 - readout - P.MP_INTERVAL_PAD_S
        r.check(abs(cam.exposure - want_exp) < 1e-12
                and abs(cfg.exposure_us - want_exp * 1e6) < 1e-6,
                f"{name}: exposure is the longest that holds it "
                f"({cam.exposure * 1e6:.0f} us)")

    ctrl_cfg = AcqConfig(preset_key="4432x512", binning=4)
    ctrl = OrcaFireWorker(0, ctrl_cfg)
    with redirect_stdout(io.StringIO()):
        ctrl._query_timings(FakeCam(ctrl_cfg.frame_shape), ctrl_cfg)
    r.check(abs(ctrl.achievable_hz - FPS) < 1e-6,
            "control: Internal still reports the camera's frame period")
    # The rig's measured EDGE spacing: 2.2440 ms at 250 us, 2.4929 ms at
    # 500 us, same 1.894 ms readout — with the old 100 us pad.
    r.check(abs((master_pulse_interval(1.8940e-3, 500.0)
                 - master_pulse_interval(1.8940e-3, 250.0)) - 0.25e-3) < 1e-9,
            "the EDGE interval grows 1:1 with exposure, as measured")


def check_dcimg_preview_skips_not_drops(r: Report) -> None:
    """With a .dcimg attached the loop only previews the newest frame, so the
    ring's skip count grows by design; the recorder writes every frame. Files
    carried 26-183 "dropped" with 0 gaps in their frame counters (2026-09-30)."""
    import io
    from contextlib import redirect_stdout
    from types import SimpleNamespace

    cfg = AcqConfig(preset_key="4432x512", binning=4, exposure_us=1000.0)

    class Rec:
        max_frames = 10 ** 6

        def status(self):
            return SimpleNamespace(total=5, missing=0, recording=True)

        def close(self):
            pass

    def run(with_sink: bool) -> int:
        cam = FakeCam(cfg.frame_shape)
        w = OrcaFireWorker(0, cfg, cam=cam)
        t0 = time.perf_counter()

        def newest():
            cam._idx += BATCH
            if time.perf_counter() - t0 > 1.3:
                w._stop = True
            return np.zeros(cfg.frame_shape, dtype=np.uint16)

        cam.read_newest_image = newest
        if with_sink:
            w.set_sink(lambda item: setattr(w, "_stop",
                                            time.perf_counter() - t0 > 1.3))
        else:
            w._dcimg = Rec()
        with redirect_stdout(io.StringIO()):
            w._run()
        return w.skipped_frames

    r.check(run(False) == 0,
            "a .dcimg under preview reports no drops from the ring's skips")
    r.check(run(True) == 1,
            "control: with a sink the same skip count is a real drop")


def _part_losses() -> int:
    r = Report("losses")
    qt_app()                            # the camera worker declares pyqtSignals
    check_count_cap(r)
    check_late_samples(r)
    check_unstamped(r)
    check_drops_counted(r)
    check_offered_never_blocks(r)
    check_no_hot_spin(r)
    check_skip_report_blames_the_loop(r)
    check_memory_capped_buffer_is_announced(r)
    check_readout_speed_absence_is_reported(r)
    check_nocamera_retry(r)
    check_edge_rate_reported(r)
    check_dcimg_preview_skips_not_drops(r)
    return r.finish()


# ═══ burst (MASTER PULSE MODE=BURST) ═════════════════════════════════════

def check_burst_arithmetic(r: Report) -> None:
    """Pinned to the rig probe (2026-09-30, SYNCREADOUT, 900 pulses per edge):
    bursts of 899, then 900 — framestamps 0..898, 899..1798, 1799.. . So 900
    pulses is N=899, and each later burst leads with one leftover frame."""
    n = 899
    r.check(P.burst_pulses(n, True) == 900 and P.burst_pulses(n, False) == n,
            "SYNCREADOUT asks one pulse more than the frames wanted")
    r.check([P.burst_next_boundary(a, n, True) for a in (0, 1, 899, 900, 1799,
                                                          1800)]
            == [0, 899, 899, 1799, 1799, 2699],
            "boundaries fall where the probe's bursts ended (899, 1799, ...)")
    r.check(P.burst_stale_indices(2699, n, True) == [899, 1799],
            "the leftovers are each later burst's first framestamp (899, 1799)")
    r.check(P.burst_stale(899, n, True) and not P.burst_stale(900, n, True)
            and not P.burst_stale(898, n, True),
            "only the first frame of a later burst is stale")
    r.check(P.burst_stale_indices(5000, n, False) == []
            and P.burst_next_boundary(1000, n, False) == 1798,
            "control: EDGE never leaves one open, bursts are exactly N")


def check_burst_gate(r: Report) -> None:
    """In burst mode a re-arm doesn't stop the camera: the gate moves to the
    end of the burst the count was in when the re-arm was ASKED for."""
    from types import SimpleNamespace
    w = OrcaFireWorker(0, AcqConfig())
    w._burst_n, w._syncreadout = 900, True

    class Cam:
        acquired = 0

        def get_frames_status(self):
            return SimpleNamespace(acquired=self.acquired)

    cam = Cam()
    w._gated()                                   # a capture start
    cam.acquired = 500
    w._poll_burst(cam)
    r.check(w.trigger_gate == (1, 500) and w.burst_frames_since_gate == 500,
            f"after a start, every frame is real ({w.trigger_gate}, "
            f"{w.burst_frames_since_gate})")
    cam.acquired = 900
    w._poll_burst(cam)
    w.rearm_trigger()                            # asked at the boundary
    w._soft_gate = w._rearm_from                 # what the loop does
    cam.acquired = 905                           # next edge before the loop ran
    w._poll_burst(cam)
    r.check(w.trigger_gate == (2, 5) and w.burst_frames_since_gate == 4,
            f"an edge between the ask and the loop still counts as the edge, "
            f"less its leftover frame ({w.trigger_gate}, "
            f"{w.burst_frames_since_gate})")
    cam.acquired = 1801
    w._poll_burst(cam)
    r.check(w.burst_frames_since_gate == 900,
            f"a later burst: N real frames after the leftover "
            f"({w.burst_frames_since_gate})")

    w._soft_gate = 1850                          # asked mid-burst
    cam.acquired = 1850
    w._poll_burst(cam)
    r.check(w.trigger_gate == (3, 0) and w._gate_acq0 == 2702,
            f"asked mid-burst: the gate waits for that burst's end "
            f"({w.trigger_gate}, at {w._gate_acq0})")
    cam.acquired = 2702
    w._poll_burst(cam)
    r.check(w.trigger_gate[1] == 0,
            "control: the burst's own tail is not an edge")


def check_seal_dcimg(r: Report) -> None:
    """A sealed .dcimg is closed but still this recording's: its count stays
    readable and the next roll may re-arm with it, until the sink changes."""
    from types import SimpleNamespace

    from acqApp.adapters.voltage_cam import VoltageCamModule

    class Worker:
        supports_dcimg = True
        dcimg_active = True
        dcimg_frames = 2500

        def __init__(self):
            self.files, self.armed = [], 0

        def set_record_file(self, path):
            self.files.append(path)

        def arm_with_next_file(self):
            self.armed += 1

    m = VoltageCamModule.__new__(VoltageCamModule)
    m._dcimg_sealed = False
    m.worker = w = Worker()
    r.check(m.seal_dcimg() and w.files == [None],
            f"sealing asks the worker to close the file ({w.files})")
    w.dcimg_active = False                       # the worker has closed it
    r.check(m.dcimg_frames() == 2500,
            f"a sealed file's count stays readable ({m.dcimg_frames()})")
    r.check(m.arm_with_next_file() and w.armed == 1,
            "the next roll can still re-arm with its new file")
    r.check(not m.seal_dcimg() and w.files == [None],
            "sealing twice closes nothing more")
    m.win = SimpleNamespace(dcimg_target=lambda key: None)
    m.worker.set_sink = lambda sink: None
    m.attach_sink(None)
    r.check(m.dcimg_frames() is None and not m.arm_with_next_file(),
            "control: once the sink changes, no .dcimg is this recording's")


def check_burst_panel(r: Report) -> None:
    from acqApp.devices.voltage_cam.panel import SettingsPanel
    pnl = SettingsPanel(AcqConfig(trigger_mode=P.TRIGGER_MODES[0],
                                  burst_frames=1500))
    r.check(not pnl._spn_burst.isEnabled() and not pnl.get_config().burst,
            "Internal: Frames per edge is off, whatever it holds")
    pnl.set_trigger_mode(P.EXTERNAL_EDGE)
    cfg = pnl.get_config()
    r.check(pnl._spn_burst.isEnabled() and cfg.burst
            and cfg.burst_frames == 1500,
            f"External edge: it applies ({cfg.burst_frames})")
    pnl.set_running(True)
    r.check(not pnl._spn_burst.isEnabled(), "locked while running")
    pnl.set_running(False)
    pnl.set_burst_frames(0)
    r.check(not pnl.get_config().burst, "0 is Off")


def _part_burst() -> int:
    r = Report("burst")
    isolate_user_state()
    app = qt_app()                      # unassigned, it's collected (REFERENCE gotchas)
    check_burst_arithmetic(r)
    check_burst_gate(r)
    check_seal_dcimg(r)
    check_burst_panel(r)
    del app
    return r.finish()


PARTS = {
    "readout": _part_readout,
    "timestamps": _part_timestamps,
    "dcimg": _part_dcimg,
    "losses": _part_losses,
    "burst": _part_burst,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
