"""
DCAM's own recorder — the .dcimg path, everything short of the DLL.

The ctypes calls themselves need the camera and are verified at the rig; what
is testable here is the part that got them wrong twice: the frame cap (0 is
REJECTED, not "unlimited", and the cap is bounded by free disk space, not by
memory), the file naming, and the wiring that decides when this path is used
at all — including where a routine's frames-unit Wait gets its count from,
since DCAM-written frames never reach the Recorder that normally supplies it.

Cheap and hardware-free: no camera, no DLL call, ~1 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dcimg.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from _harness import Report, isolate_user_state, make_window, pump, qt_app

from acqApp.devices.voltage_cam.dcimg import (MIN_FRAMES, DcimgError,
                                              DcimgRecorder, frames_that_fit)


def check_frame_cap(r: Report, tmp: Path) -> None:
    """`maxframepersession` is bounded by the DRIVE, not by memory: at the rig
    1e6 frames of 128 KB bound on D: (678 GB free) and failed on C:, and 1e7
    failed on both with DCAMERR_FAILEDWRITEDATA."""
    free = shutil.disk_usage(tmp).free
    small = frames_that_fit(tmp, 1 << 20)           # 1 MB frames
    r.check(small > 0, f"some frames fit on a drive with {free/1e9:.0f} GB free")
    r.check(small <= free // (1 << 20),
            f"the cap never exceeds free space ({small} frames of 1 MB)")

    # Halving the frame size doubles the cap — the cap is bytes, not frames.
    # Within one frame: free space moves under us, and the division truncates.
    r.check(abs(frames_that_fit(tmp, 1 << 19) - small * 2) <= 2,
            "the cap scales inversely with frame size")

    # A frame nothing could hold.
    r.check(frames_that_fit(tmp, free * 2) == 0,
            "a frame larger than the drive fits zero of them")

    for bad in (0, -1):
        try:
            frames_that_fit(tmp, bad)
            r.check(False, f"frame_bytes={bad} must raise")
        except ValueError:
            r.check(True, f"frame_bytes={bad} raises rather than dividing by it")

    # A path that doesn't exist yet is normal — the session folder is made by
    # the writer, and the recorder is sized before it opens.
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

    # close() runs on the failure path, so it must never raise or need open().
    fit.close()
    fit.close()
    r.check(True, "close() on an unopened recorder is a no-op, twice over")


def check_host_wiring(r: Report, app, tmp: Path) -> None:
    """When the .dcimg path is chosen, and what the file is called."""
    win = make_window({"voltage_cam", "routines"})
    sp = win._save_panel

    sp._chk_split.setChecked(False)
    sp._cmb_orca_format.setCurrentIndex(
        sp._cmb_orca_format.findData("dcimg"))
    sp._on_edited()
    r.check(not win.dcimg_enabled(),
            "DCIMG off in composite .h5 mode — it cannot live inside one")

    sp._chk_split.setChecked(True)
    sp._on_edited()
    r.check(win.dcimg_enabled(), "…on once the session is a folder of files")

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

    # A worker that IS writing one, at a count no sink could have produced.
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

    # 0 frames written so far is NOT "no .dcimg": the difference decides
    # whether a Wait counts from the recorder or from a Recorder that will
    # never move.
    FakeWorker.dcimg_frames = 0
    r.check(hooks.frames() == 0,
            "a .dcimg with nothing in it yet still counts 0, not None")

    FakeWorker.dcimg_active = False
    r.check(hooks.frames() is None or hooks.frames() == 0,
            "with no .dcimg open it falls back to the Recorder")

    # The routine itself must now START on DCIMG — it used to be refused.
    cam.worker = None
    sp = win._save_panel
    sp._chk_split.setChecked(True)
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

    # The setting survives a save/load round trip, and defaults on for a file
    # written before it existed.
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

    # rearm_step restarts the clock without re-issuing anything.
    pump(app, 0.15)
    before = eng.progress()
    r.check(before > 0, f"fixture: the wait has started ({before:.3f})")
    r.check(eng.rearm_step() is True, "rearm_step() re-arms a Wait step")
    r.check(eng.progress() < before,
            f"…and its clock restarts ({eng.progress():.3f} < {before:.3f})")

    # A held routine doesn't tick, so nothing touches the step's clock.
    # (`progress()` is computed from wall time, so it keeps climbing either
    # way — the clock's ORIGIN is what says whether the gap was counted.)
    win.camera_ready = lambda _k: False
    adapter._hold_t0 = time.monotonic()
    t_armed = eng._wait_t0
    pump(app, 0.2)
    r.check(eng._wait_t0 == t_armed,
            "while held, the step's clock origin is left alone")

    # Releasing restarts the step rather than resuming mid-way through it.
    win.camera_ready = lambda _k: True
    pump(app, 0.1)
    r.check(adapter._hold_t0 is None, "the hold releases once frames resume")
    r.check(eng._wait_t0 > t_armed,
            "…and the step's clock restarts from the release, so the gap "
            "is not counted against the trial")

    # A step that must NOT be re-armed: re-issuing a move, or resetting a
    # trigger's baseline, would each undo the thing the step is there for.
    adapter._abort()
    adapter.panel._r.steps = [Step(kind="move", x_um=0.0)]
    adapter.panel._reload_table()
    adapter._start()
    r.check(adapter._engine.rearm_step() is False,
            "rearm_step() refuses a non-Wait step")
    adapter._abort()


def main() -> int:
    r = Report("dcimg")
    isolate_user_state()
    app = qt_app()
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dcimg_"))
    try:
        check_frame_cap(r, tmp)
        check_naming(r, tmp)
        check_cap_guards(r, tmp)
        check_host_wiring(r, app, tmp)
        check_routine_counts_recorder(r, app)
        check_wait_for_camera(r, app)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
