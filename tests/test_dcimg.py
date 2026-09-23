"""
DCAM's own recorder — the .dcimg path, everything short of the DLL.

The ctypes calls themselves need the camera and are verified at the rig; what
is testable here is the part that got them wrong twice: the frame cap (0 is
REJECTED, not "unlimited", and the cap is bounded by free disk space, not by
memory), the file naming, and the wiring that decides when this path is used
at all — including that a routine refuses to run on it, since DCAM-written
frames never reach the Recorder the routine counts.

Cheap and hardware-free: no camera, no DLL call, ~1 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dcimg.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report, isolate_user_state, make_window, qt_app

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


def check_routine_refuses(r: Report, app) -> None:
    """DCAM-written frames never reach the Recorder, so `_frames()` would sit
    at 0 — a frames-unit Wait would hang forever. Refuse at Start."""
    from acqApp.routines.settings import Step

    win = make_window({"voltage_cam", "routines", "stage"})
    adapter = {m.key: m for m in win._modules}["routines"]
    adapter.panel._r.steps = [Step(kind="wait", length=5, unit="frames")]
    adapter.panel._reload_table()

    sp = win._save_panel
    sp._chk_split.setChecked(True)
    sp._cmb_orca_format.setCurrentIndex(sp._cmb_orca_format.findData("dcimg"))
    sp._on_edited()

    adapter._start()
    r.check(adapter._engine is None,
            "Start is refused while ORCA format is DCIMG")
    r.check(not win._btn_rec.isChecked(),
            "…and no recording is left open behind the refusal")

    sp._cmb_orca_format.setCurrentIndex(sp._cmb_orca_format.findData("tiff"))
    sp._on_edited()
    adapter._start()
    r.check(adapter._engine is not None,
            "control: the same routine starts on TIFF")
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
        check_routine_refuses(r, app)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
