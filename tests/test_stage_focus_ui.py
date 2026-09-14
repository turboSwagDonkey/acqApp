"""
The Stage panel's Focus (Z) widgets — slider, jog arrows, and the
calibration dialog's two-stage Z warning gate.

`tests/test_stage_z.py` covers the settings/controller/worker layers with no
Qt at all; nothing there ever builds a `SettingsPanel` or opens
`CalibrationDialog`, so none of the actual widget code added alongside those
layers was exercised. This file is the Qt-level half.

Two things matter enough to pin down:

  1. The slider must never command a move while being dragged — only on
     release, through the SAME large-move confirm as the typed "Go" (using
     Z's own, tighter `confirm_move_z_um`, not X/Y's). A "No" on that confirm
     must snap the handle back, not leave it sitting on an unsent target.
  2. `_reestablish_frame_z` is a "multiple warnings" gate BY DESIGN — it must
     take two separate Yes answers before a `_FrameWorker` is ever
     constructed, and a No at either stage must abort with no worker started
     (no motion attempted) at all.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_stage_focus_ui.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _harness import Report, pump, qt_app

from acqApp.devices.stage import settings as stage_settings

# Bind the temp path before `panel` does `from ... import config_path`, or it
# keeps a reference to the real one — same reason as test_stage_panel.py.
_TMP = Path(tempfile.mkdtemp(prefix="acqapp_stagefocus_"))
stage_settings.config_path = lambda: _TMP / "config.json"

from PyQt6.QtWidgets import QMessageBox                      # noqa: E402

from acqApp.devices.stage.control import StageControllerError  # noqa: E402
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
    """Stands in for a real StageController connected to a backend with no
    establish_frame (e.g. this app's actual MCM301 driver) — a real property
    here, not FakeCtrl's catch-all __getattr__, so CalibrationDialog's
    getattr(self._ctrl, "supports_reframe", True) sees False."""

    @property
    def supports_reframe(self) -> bool:
        return False


def _settings(has_frame: bool = False) -> StageSettings:
    z = StageAxis(6, "Z", 4.7254, step_um=20.0)
    if has_frame:
        z.slope, z.offset, z.ref_counts, z.origin_set = 1.0, 0.0, 0.0, True
    return StageSettings(z=z, confirm_move_z_um=500.0)


# ── a queue-driven dialog fake: each .warning()/.question() call pops the
#    next answer, so a two-stage gate's Yes/Yes vs Yes/No can be told apart ──
_ANSWERS: list[QMessageBox.StandardButton] = []


def _fake_dialogs() -> None:
    def next_answer(*_a, **_k):
        return _ANSWERS.pop(0) if _ANSWERS else QMessageBox.StandardButton.No
    QMessageBox.warning = staticmethod(next_answer)
    QMessageBox.question = staticmethod(next_answer)


def check_focus_group_exists(r: Report) -> None:
    p = SettingsPanel(_settings())
    r.check(p._focus is not None, "a has_z rig gets a Focus (Z) group")
    r.check("z" in p._axis_widgets, "z is registered in _axis_widgets")
    w = p._axis_widgets["z"]
    r.check("slider" in w, "…with a slider entry")
    lo, hi = p._s.z.soft_limits_um()
    r.check(p._sld_z.minimum() == int(round(lo))
            and p._sld_z.maximum() == int(round(hi)),
            "the slider's range matches the axis's soft limits")

    p2 = SettingsPanel(StageSettings())      # no z
    r.check(p2._focus is None, "a no-z rig gets no Focus group at all")
    r.check("z" not in p2._axis_widgets, "…and no 'z' entry in _axis_widgets")


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


def check_slider_commits_on_release_only(r: Report) -> None:
    _fake_dialogs()
    _ANSWERS.clear()
    p = SettingsPanel(_settings(has_frame=True))
    c = FakeCtrl()
    p.bind_controller(c)
    p._last_z = 0.0

    p._sld_z.setValue(200)          # a plain valueChanged — no release yet
    r.check("move_to_um" not in c.names(),
            "dragging the slider alone never commands a move")
    r.check(p._lbl_z_target.text() == "200 µm",
            "…but the drag-preview label does update live")

    # A small move (< confirm_move_z_um): release commits with no dialog.
    p._on_z_slider_released()
    r.check(("move_to_um", ("z", 200.0), {}) in c.calls,
            "release commits the move via move_to_um")


def check_slider_confirms_large_move(r: Report) -> None:
    _fake_dialogs()
    p = SettingsPanel(_settings(has_frame=True))
    c = FakeCtrl()
    p.bind_controller(c)
    p._last_z = 0.0

    # A large jump answered No: no move, and the handle snaps back.
    _ANSWERS[:] = [QMessageBox.StandardButton.No]
    p._sld_z.setValue(5000)         # >> confirm_move_z_um (500)
    p._on_z_slider_released()
    r.check("move_to_um" not in c.names(),
            "a large move answered No never reaches the controller")
    r.check(p._sld_z.value() == 0,
            "…and the slider snaps back to the last known position")
    r.check(p._lbl_z_target.text() == "0 µm",
            "…and the preview label snaps back to match")

    # Same large jump answered Yes: it goes through.
    _ANSWERS[:] = [QMessageBox.StandardButton.Yes]
    p._sld_z.setValue(5000)
    p._on_z_slider_released()
    r.check(("move_to_um", ("z", 5000.0), {}) in c.calls,
            "the same move answered Yes reaches the controller")


def check_frame_gating_disables_slider(r: Report) -> None:
    p = SettingsPanel(_settings(has_frame=False))
    p.bind_controller(FakeCtrl())
    r.check(p._sld_z.isEnabled() is False,
            "no valid Z frame -> the slider (absolute go-to) is disabled")
    r.check(p._axis_widgets["z"]["buttons"][0].isEnabled(),
            "…but a jog arrow stays enabled (jog needs no frame)")

    p2 = SettingsPanel(_settings(has_frame=True))
    p2.bind_controller(FakeCtrl())
    r.check(p2._sld_z.isEnabled() is True,
            "a valid Z frame enables the slider")


def check_set_readout_syncs_without_fighting_drag(r: Report) -> None:
    p = SettingsPanel(_settings(has_frame=True))
    p.bind_controller(FakeCtrl())

    p.set_readout(0.0, 0.0, z_um=333.0)
    r.check(p._sld_z.value() == 333, "set_readout syncs the idle slider")
    r.check(p._last_z == 333.0, "…and _last_z, used by _goto/_on_z_slider_released")

    # Simulate "the user is holding the slider down" and confirm a readout
    # tick does not yank the handle out from under them.
    p._sld_z.setSliderDown(True)
    p.set_readout(0.0, 0.0, z_um=9000.0)
    r.check(p._sld_z.value() == 333,
            "a readout tick never moves the handle while it's held down")
    p._sld_z.setSliderDown(False)


def check_goto_uses_z_threshold(r: Report) -> None:
    """_goto('z', ...) must confirm against confirm_move_z_um, not the much
    larger X/Y confirm_move_um — the whole reason the field exists."""
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
    """The core "multiple warnings" property: only Yes-then-Yes may ever
    start a worker; either No must abort with NOTHING started."""
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
    # Let the (instant, fake-backed) worker thread finish and clean up.
    worker = dlg3._worker
    pump(app, 0.5)
    if worker is not None:
        worker.wait(2000)
    calls = [(n, k) for n, _a, k in c3.calls if n == "establish_frame"]
    r.check(len(calls) == 1 and calls[0][1].get("axes") == ("z",),
            "…and it calls establish_frame(axes=('z',)) — X/Y untouched")
    dlg3.reject()


def check_reframe_hidden_when_unsupported(r: Report) -> None:
    """A rig whose backend can't ever re-establish a Z frame (this app's real
    MCM301 driver) shouldn't offer a button that always refuses — 'Set Z = 0'
    is that rig's complete Z calibration on its own."""
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
        check_focus_group_exists(r)
        check_jog_arrows(r)
        check_slider_commits_on_release_only(r)
        check_slider_confirms_large_move(r)
        check_frame_gating_disables_slider(r)
        check_set_readout_syncs_without_fighting_drag(r)
        check_goto_uses_z_threshold(r)
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
