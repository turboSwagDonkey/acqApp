"""
Stage: the Z (focus) axis.

Most rigs have no Z motor at all, and the one that does must not get it for
free: `StageSettings.z` is None unless the calibration file BOTH names a Z
axis (`xy_pad.z_axis`) AND marks that axis `"active": true` — a config with
Z merely present-but-inactive (the pre-2026-09-13 shape of every stage_config
on record) must keep behaving exactly as before. Three more properties, all
consequences of Z being a FOCUS axis rather than a third planar axis:

  - `frame_rotation_deg` (the camera-alignment jog rotation) must never touch
    Z — rotating a focus move by the XY mounting angle would send it sideways.
  - Z calibrates independently: `has_frame` gates X/Y absolute go-to as a
    pair (unchanged), but Z's own `has_frame` must gate Z's Go-to on its own,
    since `establish_frame()`/Calibrate… never touches it.
  - A rig with no Z stage must be able to call every Z-shaped method
    (`jog_um("z", ...)`, `read_z_um()`) and get a clean refusal, not an
    AttributeError from indexing a None axis.

`config_path()` points at the real shared calibration; every check here
redirects it at a temp file first, same as test_stage_state.py.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_stage_z.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report

from acqApp.devices.stage import settings as S
from acqApp.devices.stage.control import (
    MockStageController, StageController, StageControllerError, _pick_axis,
)

CFG_NO_Z = {
    "port": "COM10", "controller": "auto",
    "axes": [{"index": 4, "name": "X", "active": True, "counts_per_um": 2.0},
             {"index": 5, "name": "Y", "active": True, "counts_per_um": 2.0}],
    "xy_pad": {"x_axis": 4, "y_axis": 5},
}
CFG_Z_PRESENT_INACTIVE = {
    **CFG_NO_Z,
    "axes": CFG_NO_Z["axes"] + [
        {"index": 6, "name": "Z / Focus", "active": False,
         "counts_per_um": 4.7254}],
    "xy_pad": {"x_axis": 4, "y_axis": 5, "z_axis": 6},
}
CFG_Z_ACTIVE = {
    **CFG_NO_Z,
    "axes": CFG_NO_Z["axes"] + [
        {"index": 6, "name": "Z / Focus", "active": True,
         "counts_per_um": 4.7254, "step_um": 20}],
    "xy_pad": {"x_axis": 4, "y_axis": 5, "z_axis": 6},
}


def _load(tmp: Path, cfg: dict) -> S.StageSettings:
    (tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return S.load_settings()


def check_gating(r: Report, tmp: Path) -> None:
    s = _load(tmp, CFG_NO_Z)
    r.check(s.z is None and s.has_z is False,
            "no z_axis pointer at all -> no Z")

    s = _load(tmp, CFG_Z_PRESENT_INACTIVE)
    r.check(s.z is None and s.has_z is False,
            "z_axis named but active:false -> still no Z (today's real "
            "stage_config, pre-2026-09-13, must not regress)")

    s = _load(tmp, CFG_Z_ACTIVE)
    r.check(s.has_z is True and s.z is not None and s.z.index == 6,
            "z_axis named AND active -> Z built from that axis's own config")
    r.check(s.z.step_um == 20.0, "Z's own fields (step_um) come through")
    r.check(s.x.index == 4 and s.y.index == 5,
            "enabling Z leaves X/Y exactly as before")


def check_pick_axis_refuses(r: Report) -> None:
    s = S.StageSettings()   # defaults: no z
    r.check(s.has_z is False, "default StageSettings has no Z")
    try:
        _pick_axis(s, "z")
        r.check(False, "_pick_axis('z') on a no-Z rig should raise")
    except StageControllerError:
        r.check(True, "_pick_axis('z') on a no-Z rig raises cleanly")


def check_mock_controller(r: Report) -> None:
    s = S.StageSettings(z=S.StageAxis(6, "Z", 4.7254))
    c = MockStageController(s)
    c.connect()
    r.check(c.has_z is True, "controller.has_z reflects settings.has_z")

    c.jog_um("z", 50.0)
    z = c.read_z_um()
    r.check(z == 50.0, f"Z jog moves Z (got {z})")
    x, y = c.read_xy_um()
    r.check((x, y) == (0.0, 0.0), "…and leaves X/Y untouched")

    # frame_rotation_deg is an X/Y camera-alignment setting; Z must ignore it.
    s2 = S.StageSettings(z=S.StageAxis(6, "Z", 4.7254), frame_rotation_deg=45.0)
    c2 = MockStageController(s2)
    c2.connect()
    c2.jog_um("z", 50.0)
    zz = c2.read_z_um()
    xx, yy = c2.read_xy_um()
    r.check(zz == 50.0 and (xx, yy) == (0.0, 0.0),
            "a 45° frame_rotation_deg does not deflect a Z jog into X/Y")

    c2.stop_all()
    r.check(c2._target["z"] == c2._pos["z"],
            "stop_all() also freezes Z's target, not just X/Y's")


def check_no_z_controller_refuses(r: Report) -> None:
    """A rig with no Z stage must refuse cleanly, not crash, on every Z entry
    point — this is the case that matters most: most rigs have no Z."""
    s = S.StageSettings()
    c = MockStageController(s)
    c.connect()
    try:
        c.read_z_um()
        r.check(False, "read_z_um() on a no-Z mock should raise")
    except StageControllerError:
        r.check(True, "read_z_um() on a no-Z mock raises StageControllerError")
    try:
        c.jog_um("z", 10.0)
        r.check(False, "jog_um('z', ...) on a no-Z mock should raise")
    except (StageControllerError, KeyError):
        r.check(True, "jog_um('z', ...) on a no-Z mock raises, doesn't crash "
                "silently into a None axis")


def check_real_controller_shape(r: Report) -> None:
    """No hardware here, so only the parts that don't touch a device: object
    construction and the has_z passthrough — connect() would need a real
    port."""
    s = S.StageSettings(z=S.StageAxis(6, "Z", 4.7254))
    c = StageController(s)
    r.check(c.has_z is True, "StageController.has_z reflects settings.has_z")
    s2 = S.StageSettings()
    r.check(StageController(s2).has_z is False,
            "…and is False with no Z axis configured")


def check_poll_worker_publishes_z(r: Report) -> None:
    from acqApp.devices.stage.acquisition import StagePollWorker

    s = S.StageSettings(z=S.StageAxis(6, "Z", 4.7254))
    c = MockStageController(s)
    c.connect()
    c.jog_um("z", 12.0)
    w = StagePollWorker(c, poll_hz=4.0)
    r.check(w._has_z is True, "worker snapshots has_z at construction")

    # Exercise one iteration of the read the worker's _run loop performs,
    # without spinning up the actual QThread (there is no Qt app here).
    xy = c.read_xy_um()
    pos = (*xy, c.read_z_um()) if w._has_z else xy
    r.check(len(pos) == 3, "a has_z worker's published sample carries Z, too")
    r.check(pos[2] == 12.0, "…and it's the real Z reading, not a placeholder")

    s2 = S.StageSettings()
    c2 = MockStageController(s2)
    c2.connect()
    w2 = StagePollWorker(c2, poll_hz=4.0)
    xy2 = c2.read_xy_um()
    pos2 = (*xy2, c2.read_z_um()) if w2._has_z else xy2
    r.check(len(pos2) == 2,
            "a no-Z worker's sample stays a 2-tuple (every existing caller "
            "indexes [0]/[1] only, so this must not change shape for them)")


def main() -> int:
    r = Report("stage-z")
    real_config_path = S.config_path
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_stagez_"))
    S.config_path = lambda: tmp / "config.json"
    try:
        check_gating(r, tmp)
        check_pick_axis_refuses(r)
        check_mock_controller(r)
        check_no_z_controller_refuses(r)
        check_real_controller_shape(r)
        check_poll_worker_publishes_z(r)
    finally:
        S.config_path = real_config_path
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
