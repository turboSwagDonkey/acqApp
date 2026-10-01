"""The Stage panel's Z controls and the calibration dialog's Z gate — the
Qt-level half of test_stage.py's stage-z part. Z is right under the objective:

  1. Z's Go confirms against its OWN, tighter `confirm_move_z_um`, not X/Y's
     `confirm_move_um`.
  2. `_reestablish_frame_z` takes two separate Yes answers before a
     `_FrameWorker` exists; a No at either aborts with nothing started.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _harness import Report, pump, qt_app

from acqApp.devices.stage import settings as stage_settings

# Before `panel` binds `config_path` by import.
_TMP = Path(tempfile.mkdtemp(prefix="acqapp_stagefocus_"))
stage_settings.config_path = lambda: _TMP / "config.json"

from PyQt6.QtWidgets import QMessageBox                      # noqa: E402

from acqApp.devices.stage.panel import CalibrationDialog, SettingsPanel  # noqa: E402
from acqApp.devices.stage.settings import StageAxis, StageSettings  # noqa: E402


class FakeCtrl:
    """Records every call; `boom` makes each one raise, like a dead link."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[tuple] = []
        self.boom = boom

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.boom:
                raise RuntimeError("serial link gone")
        return record

    def names(self) -> list[str]:
        return [n for n, _a, _k in self.calls]


class NoReframeCtrl(FakeCtrl):
    """A backend with no establish_frame (the real MCM301 driver). A real
    property: FakeCtrl's __getattr__ would answer with a truthy function."""

    @property
    def supports_reframe(self) -> bool:
        return False


def _settings(has_frame: bool = False) -> StageSettings:
    z = StageAxis(6, "Z", 4.7254, step_um=20.0)
    if has_frame:
        z.slope, z.offset, z.ref_counts, z.origin_set = 1.0, 0.0, 0.0, True
    return StageSettings(z=z, confirm_move_z_um=500.0)


# Each .warning()/.question() pops the next answer (then No).
_ANSWERS: list[QMessageBox.StandardButton] = []


def _fake_dialogs() -> None:
    def next_answer(*_a, **_k):
        return _ANSWERS.pop(0) if _ANSWERS else QMessageBox.StandardButton.No
    QMessageBox.warning = staticmethod(next_answer)
    QMessageBox.question = staticmethod(next_answer)


def check_z_rides_the_motion_grid(r: Report) -> None:
    """One Motion grid, one control shape for every axis (2026-09-13)."""
    p = SettingsPanel(_settings())
    r.check("z" in p._axis_widgets, "z is registered in _axis_widgets")
    w = p._axis_widgets["z"]
    r.check(set(w) == {"step", "goto", "buttons"},
            f"z's entry is the same shape as x/y's — no slider left over "
            f"({sorted(w)})")
    r.check(len(w["buttons"]) == 4,
            "z has the same four buttons (jog-, jog+, Go, Stop) as x/y")
    lo, hi = p._s.z.soft_limits_um()
    r.check(w["goto"].minimum() == lo and w["goto"].maximum() == hi,
            "z's Go-to spin box ranges over its own soft limits")

    p2 = SettingsPanel(StageSettings())      # no z
    r.check("z" not in p2._axis_widgets,
            "a no-z rig gets no 'z' row at all")


def check_z_gauge_exists_and_updates(r: Report) -> None:
    """`set_readout` feeds the Z gauge through the map's repaint guard."""
    p = SettingsPanel(_settings(has_frame=True))
    r.check(p._z_gauge is not None, "a has_z rig gets a Z gauge")
    p.bind_controller(FakeCtrl())

    calls: list[float] = []
    p._z_gauge.set_position = lambda z: calls.append(z)
    p.set_readout(0.0, 0.0, z_um=333.0)
    r.check(calls == [333.0], f"the first Z readout repaints the gauge ({calls})")

    calls.clear()
    p.set_readout(0.0, 0.0, z_um=333.0 + p._MAP_EPS_UM / 4)   # sub-epsilon
    r.check(calls == [],
            f"a sub-epsilon Z move does not repaint the gauge ({calls})")

    calls.clear()
    p.set_readout(0.0, 0.0, z_um=333.0 + p._MAP_EPS_UM * 4)   # a real move
    r.check(len(calls) == 1, f"control: a real Z move still repaints ({calls})")

    p2 = SettingsPanel(StageSettings())      # no z
    r.check(p2._z_gauge is None, "a no-z rig gets no Z gauge at all")


def check_z_gauge_geometry(r: Report) -> None:
    """Bigger Z reads higher on screen; out of range clamps into the bar."""
    from acqApp.devices.stage.map_widget import ZGauge

    g = ZGauge()
    g.resize(80, 200)
    z = StageAxis(6, "Z", 1.0, step_um=20.0)
    g.set_axis(z)

    lim = z.travel_limits_um()
    bar = g._bar()
    y_lo, y_hi = g._y_for(lim[0], lim, bar), g._y_for(lim[1], lim, bar)
    r.check(y_hi < y_lo, f"a bigger Z value is higher on screen ({y_hi} < {y_lo})")

    y_mid = g._y_for((lim[0] + lim[1]) / 2.0, lim, bar)
    r.check(abs(y_mid - (y_lo + y_hi) / 2.0) < 1.0,
            "the midpoint value lands at the midpoint pixel")

    y_over = g._y_for(lim[1] + 10_000.0, lim, bar)
    r.check(abs(y_over - bar.top()) < 1e-6,
            "a value past the top of travel clamps to the top of the bar")


def check_jog_arrows(r: Report) -> None:
    _fake_dialogs()
    _ANSWERS.clear()
    p = SettingsPanel(_settings())
    c = FakeCtrl()
    p.bind_controller(c)
    p._axis_widgets["z"]["step"].setValue(12.5)
    p._jog("z", +1)
    p._jog("z", -1)
    r.check(("jog_um", ("z", 12.5), {}) in c.calls,
            "up arrow jogs +step through jog_um")
    r.check(("jog_um", ("z", -12.5), {}) in c.calls,
            "down arrow jogs -step through jog_um")


def check_frame_gating_disables_z_goto(r: Report) -> None:
    """As X/Y: absolute go-to needs a frame, jog does not."""
    p = SettingsPanel(_settings(has_frame=False))
    p.bind_controller(FakeCtrl())
    w = p._axis_widgets["z"]
    r.check(not w["goto"].isEnabled(),
            "no valid Z frame -> the Go-to spin box is disabled")
    r.check(not w["buttons"][2].isEnabled(),
            "…and the Go button too")
    r.check(w["buttons"][0].isEnabled(),
            "…but a jog button stays enabled (jog needs no frame)")

    p2 = SettingsPanel(_settings(has_frame=True))
    p2.bind_controller(FakeCtrl())
    w2 = p2._axis_widgets["z"]
    r.check(w2["goto"].isEnabled() and w2["buttons"][2].isEnabled(),
            "a valid Z frame enables Go-to and the Go button")


def check_set_readout_updates_z(r: Report) -> None:
    p = SettingsPanel(_settings(has_frame=True))
    p.bind_controller(FakeCtrl())
    p.set_readout(0.0, 0.0, z_um=333.0)
    r.check(p._last_z == 333.0,
            "set_readout updates _last_z, used by _goto's confirm-move check")
    r.check(p._lbl_z.text().strip() == "333.0",
            f"…and the Z readout label ({p._lbl_z.text()!r})")


def check_goto_uses_z_threshold(r: Report) -> None:
    _fake_dialogs()
    s = _settings(has_frame=True)
    s.confirm_move_um, s.confirm_move_z_um = 50000.0, 500.0
    p = SettingsPanel(s)
    c = FakeCtrl()
    p.bind_controller(c)
    p._last_z = 0.0
    p._axis_widgets["z"]["goto"].setValue(1000.0)   # > z threshold, < xy one

    _ANSWERS[:] = [QMessageBox.StandardButton.No]
    p._goto("z")
    r.check("move_to_um" not in c.names(),
            "a 1000 µm Z move is 'large' by Z's own 500 µm threshold, and a "
            "No here blocks it even though it's far under the XY threshold")


def check_active_fov(r: Report) -> None:
    """goto_fov() marks a FOV active until the position drifts off it; the
    Save panel's "append active FOV name" reads this."""
    from acqApp.devices.stage.fov_store import SavedFov

    s = _settings(has_frame=True)
    p = SettingsPanel(s)
    p.bind_controller(FakeCtrl())
    fov = SavedFov(path=Path("f.fov.json"), name="window1", saved_at="",
                   x_um=100.0, y_um=200.0, z_um=None,
                   camera_preset=None, image_path=None)

    r.check(p.active_fov_name == "", "no FOV is active before any goto")
    p.goto_fov(fov)
    r.check(p.active_fov_name == "window1", "goto_fov marks the FOV active")

    p.set_readout(100.0, 200.0)
    r.check(p.active_fov_name == "window1",
            "…and stays active at (near) the same position")

    p.set_readout(100.0 + p._MAP_EPS_UM * 4, 200.0)
    r.check(p.active_fov_name == "",
            "…but clears once the live position drifts off it")

    # CONTROL: noise under the epsilon must not read as a move away.
    p.goto_fov(fov)
    p.set_readout(100.0 + p._MAP_EPS_UM / 4, 200.0)
    r.check(p.active_fov_name == "window1",
            "control: sub-epsilon noise does not clear it")


def check_calibration_dialog_z_section(r: Report) -> None:
    s = _settings()
    dlg = CalibrationDialog(FakeCtrl(), s)
    r.check(dlg._btn_set_zero_z is not None,
            "a has_z rig gets a 'Set Z = 0' button in the calibration dialog")
    r.check(dlg._btn_reframe_z is not None,
            "…and a 'Re-establish Z frame' button")
    dlg.reject()

    dlg2 = CalibrationDialog(FakeCtrl(), StageSettings())   # no z
    r.check(dlg2._btn_set_zero_z is None and dlg2._btn_reframe_z is None,
            "a no-z rig gets neither Z calibration button")
    dlg2.reject()


def check_set_zero_z_here(r: Report) -> None:
    _fake_dialogs()
    _ANSWERS[:] = [QMessageBox.StandardButton.Yes]
    c = FakeCtrl()
    dlg = CalibrationDialog(c, _settings())
    dlg._set_zero_z_here()
    r.check("set_z_zero_here" in c.names(),
            "confirming 'Set Z = 0' reaches the controller")
    dlg.reject()

    _ANSWERS[:] = [QMessageBox.StandardButton.No]
    c2 = FakeCtrl()
    dlg2 = CalibrationDialog(c2, _settings())
    dlg2._set_zero_z_here()
    r.check("set_z_zero_here" not in c2.names(),
            "declining the confirm never touches the controller")
    dlg2.reject()


def check_reframe_z_needs_both_warnings(r: Report, app) -> None:
    _fake_dialogs()

    # Yes, then No at the second (stage-is-clear) re-check: must abort.
    c1 = FakeCtrl()
    dlg1 = CalibrationDialog(c1, _settings())
    _ANSWERS[:] = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]
    dlg1._reestablish_frame_z()
    r.check(dlg1._worker is None,
            "Yes then No: no _FrameWorker is ever constructed")
    r.check("establish_frame" not in c1.names(),
            "…and the controller's establish_frame is never called")
    dlg1.reject()

    # No at the very first (general risk) warning: must abort just as hard.
    c2 = FakeCtrl()
    dlg2 = CalibrationDialog(c2, _settings())
    _ANSWERS[:] = [QMessageBox.StandardButton.No]
    dlg2._reestablish_frame_z()
    r.check(dlg2._worker is None, "No at the first warning aborts immediately")
    dlg2.reject()

    # Yes, then Yes: only now may it actually start.
    c3 = FakeCtrl()
    dlg3 = CalibrationDialog(c3, _settings())
    _ANSWERS[:] = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes]
    dlg3._reestablish_frame_z()
    r.check(dlg3._worker is not None,
            "Yes then Yes: a _FrameWorker is started")
    worker = dlg3._worker
    pump(app, 0.5)
    if worker is not None:
        worker.wait(2000)
    calls = [(n, k) for n, _a, k in c3.calls if n == "establish_frame"]
    r.check(len(calls) == 1 and calls[0][1].get("axes") == ("z",),
            "…and it calls establish_frame(axes=('z',)) — X/Y untouched")
    dlg3.reject()


def check_reframe_hidden_when_unsupported(r: Report) -> None:
    """No button that always refuses; 'Set Z = 0' is that rig's whole Z
    calibration."""
    s = _settings()
    dlg = CalibrationDialog(NoReframeCtrl(), s)
    r.check(dlg._btn_set_zero_z is not None,
            "'Set Z = 0' is still offered even when hard-limit reframe isn't")
    r.check(dlg._btn_reframe_z is None,
            "no hard-limit button at all when the backend can't do it")
    dlg.reject()


def main() -> int:
    app = qt_app()
    r = Report("stage-focus-ui")
    try:
        check_z_rides_the_motion_grid(r)
        check_z_gauge_exists_and_updates(r)
        check_z_gauge_geometry(r)
        check_jog_arrows(r)
        check_frame_gating_disables_z_goto(r)
        check_set_readout_updates_z(r)
        check_goto_uses_z_threshold(r)
        check_active_fov(r)
        check_calibration_dialog_z_section(r)
        check_set_zero_z_here(r)
        check_reframe_z_needs_both_warnings(r, app)
        check_reframe_hidden_when_unsupported(r)
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
