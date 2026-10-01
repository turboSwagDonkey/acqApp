"""Stage model and controller: persisted calibration, the Z axis.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_stage.py [-q] [--part NAME]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report, run_parts
from acqApp.devices.stage import settings as S
from acqApp.devices.stage.control import (StageController,
                                          StageControllerError,
                                          MockStageController, _pick_axis)


# ═══ state (was test_stage_state.py) ════════════════════════════════════

UPDATES = {1: {"true_center": 12345, "soft_min": -1000, "soft_max": 1000},
           2: {"true_center": 6789}}


def redirect_config(tmp: Path, name: str) -> Path:
    """Point config_path() at a temp file — never the operator's calibration."""
    path = tmp / name
    S._SHARED_CONFIG = path
    S._LOCAL_CONFIG = path
    return path


def check_guards(r: Report) -> None:
    """#6 — motion on a disconnected controller is a stage error, not a crash."""
    ctl = StageController(S.StageSettings())          # never connect()ed
    for label, call in (
        ("move_to_um", lambda: ctl.move_to_um("x", 100.0)),
        ("jog_um",     lambda: ctl.jog_um("x", 10.0)),
        ("read_xy_um", lambda: ctl.read_xy_um()),
        # "not connected" is checked before "no Z stage".
        ("read_z_um",  lambda: ctl.read_z_um()),
    ):
        try:
            call()
        except StageControllerError:
            r.check(True, f"{label}() while disconnected raises StageControllerError")
        except Exception as e:                        # noqa: BLE001 - report it
            r.check(False, f"{label}() raised {type(e).__name__}: {e}")
        else:
            r.check(False, f"{label}() while disconnected did not raise")

    # Teardown paths call these.
    try:
        ctl.stop("x")
        ctl.stop_all()
        r.check(True, "stop()/stop_all() while disconnected are no-ops")
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"stop() while disconnected raised {type(e).__name__}: {e}")


def check_persist_missing(r: Report, tmp: Path) -> None:
    """#7 — no config file yet: create one instead of raising."""
    path = redirect_config(tmp, "missing.json")
    r.check(not path.exists(), "config file absent to begin with")
    try:
        S.save_axis_updates(UPDATES)
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"save_axis_updates raised {type(e).__name__}: {e}")
        return
    r.check(path.is_file(), "a config file was created")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    by_index = {a["index"]: a for a in cfg["axes"]}
    r.check(by_index[1]["true_center"] == 12345 and by_index[2]["true_center"] == 6789,
            "both axes' calibration survived to disk")


def check_persist_merges(r: Report, tmp: Path) -> None:
    """An existing config keeps its other keys, and is backed up first."""
    path = redirect_config(tmp, "existing.json")
    original = {"port": "COM54", "margin_um": 50,
                "axes": [{"index": 1, "counts_per_um": 20.0, "slope": 1.5},
                         {"index": 2, "counts_per_um": 20.0}]}
    path.write_text(json.dumps(original), encoding="utf-8")

    S.save_axis_updates({1: {"true_center": 999}})
    cfg = json.loads(path.read_text(encoding="utf-8"))
    ax1 = {a["index"]: a for a in cfg["axes"]}[1]
    r.check(cfg.get("port") == "COM54", "unrelated top-level keys untouched")
    r.check(ax1.get("counts_per_um") == 20.0 and ax1.get("slope") == 1.5,
            "unrelated axis keys untouched")
    r.check(ax1.get("true_center") == 999, "the update landed")

    bak = path.with_suffix(path.suffix + ".bak")
    r.check(bak.is_file() and json.loads(bak.read_text(encoding="utf-8")) == original,
            "previous contents kept as .bak (one step undoable)")


def check_persist_corrupt(r: Report, tmp: Path) -> None:
    """A truncated config must not swallow a just-measured calibration."""
    path = redirect_config(tmp, "corrupt.json")
    # Killed mid-write.
    path.write_text('{"axes": [{"index": 1,', encoding="utf-8")

    try:
        S.save_axis_updates({1: {"true_center": 42}})
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"save over a corrupt config raised {type(e).__name__}: {e}")
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    r.check({a["index"]: a for a in cfg["axes"]}[1]["true_center"] == 42,
            "calibration written over a corrupt config")
    bak = path.with_suffix(path.suffix + ".bak")
    r.check(bak.is_file() and bak.read_text(encoding="utf-8").startswith('{"axes"'),
            "the corrupt original is preserved in .bak, not discarded")


def _part_state() -> int:
    r = Report("stage-state")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_stage_"))
    real = S.config_path()
    before = real.stat().st_mtime_ns if real.is_file() else None
    try:
        check_guards(r)
        check_persist_missing(r, tmp)
        check_persist_merges(r, tmp)
        check_persist_corrupt(r, tmp)
        after = real.stat().st_mtime_ns if real.is_file() else None
        r.check(after == before,
                f"the operator's real calibration ({real.name}) was not written")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ z (was test_stage_z.py) ════════════════════════════════════════════

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

    # Dropping Z from the target made every later Z read a KeyError.
    for ax in (s.x, s.y):
        ax.slope, ax.offset, ax.origin_set = 1.0, 0.0, True
    c.go_to_center()
    r.check(c._target["z"] == 50.0 and c.read_z_um() == 50.0,
            "go_to_center moves X/Y only; Z keeps its target")
    try:
        c.go_home()
        r.check(False, "go_home with no home set must raise")
    except StageControllerError:
        r.check(True, "the mock refuses go_home with the real controller's "
                      "error type")


def check_no_z_controller_refuses(r: Report) -> None:
    """Most rigs have no Z: every Z entry point refuses cleanly."""
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
    """Construction and has_z only; connect() would need a real port."""
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

    # One iteration of the worker's _run read, without a QThread (no Qt app).
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
            "a no-Z worker's sample stays a 2-tuple (callers index [0]/[1])")


def _calibrated_axis(**over) -> S.StageAxis:
    """A Z axis as "Set Z = 0" + Calibrate… leaves it (the bare StageAxis
    used above was never calibrated)."""
    kw = dict(ref_counts=1000.0, origin_set=True, slope=17.78, offset=0.0,
              soft_min=-9000, soft_max=11000)
    kw.update(over)
    return S.StageAxis(6, "Z", 4.7254, **kw)


def check_axis_frame_stale(r: Report) -> None:
    """A hard-limit hit re-references the command origin, silently
    invalidating `slope`/`offset`; `frame_stale` is how the app notices."""
    z = _calibrated_axis()
    r.check(z.has_frame, "fixture: a calibrated axis has a frame")

    z.frame_stale = True
    r.check(not z.has_frame,
            "frame_stale alone drops has_frame, even with slope/offset/"
            "origin_set all otherwise intact")

    # Only a remeasured command map ("slope") clears it; "Set Z = 0" must not
    # un-flag a still-broken one.
    z.apply_updates({"true_center": 2000, "soft_min": -8000, "soft_max": 12000})
    r.check(z.frame_stale,
            "a center_updates()-shaped update (no 'slope' key) leaves "
            "frame_stale untouched")
    z.apply_updates({"slope": 17.9, "offset": 5.0})
    r.check(not z.frame_stale,
            "only a fresh slope/offset measurement (establish_frame's own "
            "update shape) clears it")
    r.check(z.has_frame, "…restoring has_frame")


def check_clamp_counts_refuses_when_uncalibrated(r: Report) -> None:
    """clamp_counts() never passes an unbounded target through — except on a
    never-calibrated axis, which must be jogged to declare its first zero."""
    fresh = S.StageAxis(6, "Z", 4.7254)      # never touched — origin_set=False
    r.check(fresh.clamp_counts(999_999) == 999_999,
            "a never-calibrated axis's clamp is a no-op (bootstrap jog)")

    # Calibrated but no soft limits: only a hand-edited config does this.
    tampered = S.StageAxis(6, "Z", 4.7254, ref_counts=1000.0, origin_set=True)
    try:
        tampered.clamp_counts(999_999)
        r.check(False, "a claimed-calibrated axis with no soft limits should "
                "refuse to clamp, not pass the target through unbounded")
    except ValueError:
        r.check(True, "…and it does")

    real = _calibrated_axis()
    r.check(real.clamp_counts(999_999) == real.soft_max
            and real.clamp_counts(-999_999) == real.soft_min,
            "a properly calibrated axis clamps normally either direction")


def check_mock_refuses_uncalibrated_and_stale(r: Report) -> None:
    """The mock mirrors StageController's guards: goto_fov and a routine's
    Move step rely on `move_to_um` as their only calibration check."""
    s = S.StageSettings(z=_calibrated_axis())
    c = MockStageController(s)
    c.connect()

    # X is uncalibrated.
    try:
        c.move_to_um("x", 100.0)
        r.check(False, "move_to_um on an uncalibrated axis should refuse")
    except StageControllerError:
        r.check(True, "…and it does (mirrors StageController.move_to_um)")

    # Calibrated Z, but a hard limit was observed since: everything refuses.
    s.z.frame_stale = True
    for label, call in (
            ("move_to_um", lambda: c.move_to_um("z", 100.0)),
            ("jog_um",     lambda: c.jog_um("z", 10.0)),
    ):
        try:
            call()
            r.check(False, f"{label}('z', ...) after a hard-limit hit "
                    f"should refuse")
        except StageControllerError:
            r.check(True, f"{label}('z', ...) refuses while frame_stale")

    s.z.apply_updates({"slope": 17.9, "offset": 1.0})
    c.move_to_um("z", 100.0)
    r.check(c._target["z"] == 100.0,
            "…and moves normally again once frame_stale clears")


class _FakeDevNoReframe:
    """MCM301-like: no `establish_frame` at all — `supports_reframe` is
    `hasattr(dev, 'establish_frame')`, so it must be absent, not None."""

    def __init__(self) -> None:
        self.statuses: dict[int, object] = {}
        self.moves: list[tuple[int, int]] = []

    def get_status(self, axis: int):
        return self.statuses[axis]

    def move_to_readout(self, axis: int, target_readout: int) -> None:
        self.moves.append((axis, target_readout))


class _FakeDev(_FakeDevNoReframe):
    """MCM6101-like: `establish_frame` exists, so `supports_reframe`."""

    def establish_frame(self, axis: int, span: int) -> dict:
        raise NotImplementedError("not exercised by these checks")


def check_real_controller_detects_hard_limit(r: Report) -> None:
    """A hard-limit hit used to be invisible: the poll kept only `.position`,
    the label said "Frame OK", and the next move went through a command map
    the hit had invalidated."""
    from acqApp.devices.stage.driver import AxisStatus, STATUS_REV_HWLIMIT

    z = _calibrated_axis()
    s = S.StageSettings(z=z)
    ctrl = StageController(s)
    ctrl._dev = _FakeDev()
    ctrl.backend_kind = "mcm6101"
    r.check(ctrl.supports_reframe,
            "fixture: this fake reports drift-prone, like the real MCM6101")

    ctrl._dev.statuses[6] = AxisStatus(6, 1500, 1500, 0)   # no limit bit
    ctrl.read_z_um()
    r.check(not z.frame_stale,
            "control: an ordinary read with no limit bit changes nothing")

    ctrl._dev.statuses[6] = AxisStatus(6, 1500, 1500, STATUS_REV_HWLIMIT)
    ctrl.read_z_um()
    r.check(z.frame_stale and not z.has_frame,
            "a hard-limit status bit latches frame_stale on the very next "
            "read — the same read path the 4 Hz poll worker already uses")

    # Sticky: the origin re-referenced the instant the switch was touched.
    ctrl._dev.statuses[6] = AxisStatus(6, 1490, 1490, 0)
    ctrl.read_z_um()
    r.check(z.frame_stale, "backing off the limit does not clear frame_stale")

    for label, call in (
            ("move_to_um", lambda: ctrl.move_to_um("z", 100.0)),
            ("jog_um",     lambda: ctrl.jog_um("z", 10.0)),
    ):
        try:
            call()
            r.check(False, f"{label} after an undetected-until-now limit "
                    f"hit should refuse")
        except StageControllerError:
            r.check(True, f"{label} refuses — the exact failure mode "
                    f"reported: a move that looked ordinary in the UI, "
                    f"computed through a now-wrong command map")
    r.check(ctrl._dev.moves == [],
            "…and in neither case did a command actually reach the device")

    # establish_frame()'s update shape (fresh slope/offset) clears the flag.
    z.apply_updates({"slope": 17.9, "offset": 2.0})
    ctrl.move_to_um("z", 100.0)
    r.check(len(ctrl._dev.moves) == 1,
            "re-establishing the frame restores absolute motion")


def check_limit_bit_ignored_on_non_drifting_backend(r: Report) -> None:
    """The MCM301's readout never drifts on a limit hit: no false alarm."""
    from acqApp.devices.stage.driver import AxisStatus, STATUS_FWD_HWLIMIT

    z = _calibrated_axis()
    s = S.StageSettings(z=z)
    ctrl = StageController(s)
    ctrl._dev = _FakeDevNoReframe()
    ctrl.backend_kind = "mcm301"
    r.check(not ctrl.supports_reframe,
            "fixture: this fake reports non-drifting, like the MCM301")

    ctrl._dev.statuses[6] = AxisStatus(6, 1500, 1500, STATUS_FWD_HWLIMIT)
    ctrl.read_z_um()
    r.check(not z.frame_stale,
            "a limit bit on a non-drifting backend is not mistaken for "
            "stale calibration")
    r.check(z.has_frame, "…and absolute go-to stays available")


def _part_z() -> int:
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
        check_axis_frame_stale(r)
        check_clamp_counts_refuses_when_uncalibrated(r)
        check_mock_refuses_uncalibrated_and_stale(r)
        check_real_controller_detects_hard_limit(r)
        check_limit_bit_ignored_on_non_drifting_backend(r)
    finally:
        S.config_path = real_config_path
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


PARTS = {
    "state": _part_state,
    "z": _part_z,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
