"""The stage panel's motion handlers, and the panic path.

Nothing else in this suite drives them: the GUI tests build the panel but never
press its buttons, so every handler here was unexercised. That mattered on
2026-08-13 — `_stop`, `_stop_all` and the calibration dialog's `_stop_all` were
the only controller calls with no guard, and that is the panic path (Esc → STOP
ALL, app-wide). A dead serial link is exactly when it gets pressed, and an
exception escaping a Qt slot aborts the process, so the abort button could kill
the app at the one moment it is needed.

Drives the real `SettingsPanel` against a fake controller, twice: healthy, then
with every call raising. The control is the old unguarded body run on the same
controller — it must raise, or this test proves nothing.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_stage_panel.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _harness import Report, isolate_user_state, qt_app

from acqApp.devices.stage import settings as stage_settings

# Bind the temp path before `panel` does `from ... import config_path`, or it
# keeps a reference to the real one. The operator's calibration is shared with
# the standalone stage_control app; this test must not read or write it.
_TMP = Path(tempfile.mkdtemp(prefix="acqapp_stagepanel_"))
_REAL_CONFIG = stage_settings.config_path()
stage_settings.config_path = lambda: _TMP / "config.json"

from PyQt6.QtWidgets import QMessageBox           # noqa: E402

from acqApp.devices.stage.panel import SettingsPanel      # noqa: E402

WARNINGS: list[str] = []
ASKED: list[str] = []
_ANSWER = QMessageBox.StandardButton.Yes


def _fake_dialogs() -> None:
    """No modal may open in a test — it would hang the run."""
    QMessageBox.warning = staticmethod(
        lambda *a, **k: WARNINGS.append(a[2] if len(a) > 2 else ""))
    QMessageBox.question = staticmethod(
        lambda *a, **k: (ASKED.append(a[2] if len(a) > 2 else ""), _ANSWER)[1])


class FakeCtrl:
    """Records every call. `boom` makes each one fail like a dead serial link."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[tuple] = []
        self.boom = boom

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
            if self.boom:
                raise RuntimeError("serial link gone")
        return record


def _panel(ctrl: FakeCtrl | None) -> SettingsPanel:
    p = SettingsPanel(stage_settings.load_settings())
    if ctrl is not None:
        p.bind_controller(ctrl)
    return p


def _drive(p: SettingsPanel) -> None:
    """Every handler a button or shortcut can reach."""
    p._jog("x", -1)
    p._jog("y", +1)
    p._goto("x")
    p._go_home()
    p._go_zero()
    p._set_home_here()
    p._clear_home()
    p._stop("x")
    p._stop_all()


def check_healthy(r: Report) -> None:
    WARNINGS.clear()
    c = FakeCtrl()
    p = _panel(c)
    p._axis_widgets["x"]["step"].setValue(12.5)
    p._axis_widgets["y"]["step"].setValue(4.0)
    # Same target as current, so the confirm dialog is not involved here.
    p._last_xy = (p._axis_widgets["x"]["goto"].value(), 0.0)
    _drive(p)

    names = [n for n, _ in c.calls]
    for want in ("jog_um", "move_to_um", "go_home", "go_to_center",
                 "set_home_here", "clear_home", "stop", "stop_all"):
        r.check(want in names, f"{want} reaches the controller")

    jogs = [a for n, a in c.calls if n == "jog_um"]
    r.check(jogs == [("x", -12.5), ("y", 4.0)],
            f"jog carries axis, sign and that axis's step (got {jogs})")
    r.check(not WARNINGS, f"a healthy controller warns about nothing "
                          f"({len(WARNINGS)} warnings)")


def check_confirm_threshold(r: Report) -> None:
    """A long move asks first — the guard against a typo'd absolute target."""
    global _ANSWER
    ASKED.clear()
    c = FakeCtrl()
    p = _panel(c)
    target = p._axis_widgets["x"]["goto"].value()

    p._last_xy = (target, 0.0)
    p._goto("x")
    r.check(not ASKED, "a zero-length move asks nothing")
    r.check([n for n, _ in c.calls] == ["move_to_um"], "and still moves")

    p._last_xy = (target + 10 * p._s.confirm_move_um, 0.0)
    _ANSWER = QMessageBox.StandardButton.No
    try:
        p._goto("x")
    finally:
        _ANSWER = QMessageBox.StandardButton.Yes
    r.check(len(ASKED) == 1, "a move past confirm_move_um asks first")
    r.check([n for n, _ in c.calls] == ["move_to_um"],
            "answering No does not move")


def check_dead_link(r: Report) -> None:
    """Every handler must survive a controller whose every call raises."""
    WARNINGS.clear()
    c = FakeCtrl(boom=True)
    p = _panel(c)
    p._last_xy = (p._axis_widgets["x"]["goto"].value(), 0.0)
    try:
        _drive(p)
        r.check(True, "no handler lets an exception escape into the Qt slot")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a handler: {e}")

    r.check(len(c.calls) >= 8,
            f"every command was still attempted ({len(c.calls)})")
    r.check(len(WARNINGS) >= 7,
            f"each failure is reported, not swallowed ({len(WARNINGS)})")
    r.check(any("STOP ALL" in w for w in WARNINGS),
            "the panic path reports by name")

    # Control: the pre-2026-08-13 bodies on the same controller. If these stop
    # raising, the guards above are no longer what is being tested.
    for label, body in (("_stop", lambda: c.stop("x")),
                        ("_stop_all", lambda: c.stop_all())):
        try:
            body()
        except RuntimeError:
            r.check(True, f"control: the old unguarded {label} DOES raise")
        else:
            r.check(False, f"control: old {label} no longer raises — "
                           f"this test has stopped proving anything")


def check_unbound(r: Report) -> None:
    """No controller at all is a silent no-op, not an AttributeError."""
    WARNINGS.clear()
    p = _panel(None)
    p._last_xy = (p._axis_widgets["x"]["goto"].value(), 0.0)
    try:
        _drive(p)
        r.check(True, "an unbound panel ignores every command")
    except Exception as e:                       # noqa: BLE001
        r.check(False, f"unbound panel raised {type(e).__name__}: {e}")
    r.check(not WARNINGS, "and says nothing about it")


def check_frame_gating(r: Report) -> None:
    """Absolute go-to is meaningless without a frame; jog is not."""
    c = FakeCtrl()
    p = _panel(c)
    for ax in (p._s.x, p._s.y):
        ax.ref_counts = None
    p._update_frame_status()
    r.check(not p._btn_go_zero.isEnabled(), "no frame -> Go to 0,0 disabled")
    r.check(not p._axis_widgets["x"]["goto"].isEnabled(),
            "no frame -> the absolute target is disabled")
    r.check(p._axis_widgets["x"]["buttons"][0].isEnabled(),
            "no frame -> jog stays available (the control)")


def check_real_config_untouched(r: Report) -> None:
    r.check(stage_settings.config_path() != _REAL_CONFIG,
            "the calibration path was redirected away from the operator's")
    r.check(not (_TMP / "config.json").exists(),
            "the panel wrote no calibration at all — it only reads")


def main() -> int:
    r = Report("stage-panel")
    isolate_user_state()
    _fake_dialogs()
    app = qt_app()          # must stay referenced — a collected QApplication
    assert app is not None  # aborts widget construction natively, no traceback
    before = _REAL_CONFIG.stat().st_mtime if _REAL_CONFIG.exists() else None

    check_healthy(r)
    check_confirm_threshold(r)
    check_dead_link(r)
    check_unbound(r)
    check_frame_gating(r)
    check_real_config_untouched(r)

    after = _REAL_CONFIG.stat().st_mtime if _REAL_CONFIG.exists() else None
    r.check(before == after,
            "the operator's real stage config was not modified")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
