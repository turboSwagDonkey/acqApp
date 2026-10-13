"""The stage panel's motion handlers, and the panic path (Esc → STOP ALL).

2026-08-13: `_stop`/`_stop_all` were unguarded. A dead serial link is exactly
when STOP ALL is pressed, and an exception escaping a Qt slot aborts the
process. Drives the real `SettingsPanel` against a fake controller, healthy
then with every call raising; the control is the old unguarded body, which
must raise.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from _harness import Report, isolate_user_state, qt_app

from acqApp.devices.stage import settings as stage_settings

# Before `panel` binds `config_path` by import. The operator's calibration is
# shared with the standalone stage_control app.
_TMP = Path(tempfile.mkdtemp(prefix="acqapp_stagepanel_"))
_REAL_CONFIG = stage_settings.config_path()
stage_settings.config_path = lambda: _TMP / "config.json"

from PyQt6.QtCore import Qt                       # noqa: E402
from PyQt6.QtWidgets import QMessageBox           # noqa: E402

from acqApp.devices.stage.panel import SettingsPanel      # noqa: E402

WARNINGS: list[str] = []
ASKED: list[str] = []
INFOS: list[str] = []
_ANSWER = QMessageBox.StandardButton.Yes


def _fake_dialogs() -> None:
    """A modal would hang the run."""
    QMessageBox.warning = staticmethod(
        lambda *a, **k: WARNINGS.append(a[2] if len(a) > 2 else ""))
    QMessageBox.question = staticmethod(
        lambda *a, **k: (ASKED.append(a[2] if len(a) > 2 else ""), _ANSWER)[1])
    QMessageBox.information = staticmethod(
        lambda *a, **k: INFOS.append(a[2] if len(a) > 2 else ""))


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
    p._go_zero()
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
    for want in ("jog_xy_um", "move_to_um", "go_to_center", "stop",
                 "stop_all"):
        r.check(want in names, f"{want} reaches the controller")

    jogs = [a for n, a in c.calls if n == "jog_xy_um"]
    r.check(jogs == [(-12.5, 0.0), (0.0, 4.0)],
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

    r.check(len(c.calls) >= 6,
            f"every command was still attempted ({len(c.calls)})")
    r.check(len(WARNINGS) >= 5,
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


def check_current_position_not_consumed(r: Report) -> None:
    """Save FOV reads these, not the poll worker's one-shot `get_latest()`,
    which the 30 Hz display tick drains first — a connected stage read as
    disconnected almost every time "Save current as FOV..." was clicked."""
    p = _panel(None)
    r.check(p.connected is False, "an unbound panel reports not connected")

    c = FakeCtrl()
    p.bind_controller(c)
    r.check(p.connected is True, "…and a bound one reports connected")

    p.set_readout(123.0, -45.0)
    pos = p.current_position
    r.check(pos == (123.0, -45.0, None),
            f"current_position reflects the last readout, Z absent on a "
            f"no-Z rig (got {pos})")
    r.check(p.current_position == pos,
            "reading current_position twice does not consume/clear it")


def check_real_config_untouched(r: Report) -> None:
    r.check(stage_settings.config_path() != _REAL_CONFIG,
            "the calibration path was redirected away from the operator's")
    r.check(not (_TMP / "config.json").exists(),
            "the panel wrote no calibration at all — it only reads")


def check_map_repaint_guard(r: Report) -> None:
    """A stationary stage must not repaint the travel map every poll tick
    (2026-08-27): `StageMap.set_position` always calls `update()`."""
    p = _panel(None)
    calls: list[tuple] = []
    p._map.set_position = lambda x, y: calls.append((x, y))

    p.set_readout(100.0, 200.0)
    r.check(calls == [(100.0, 200.0)], f"the first readout always repaints ({calls})")

    calls.clear()
    p.set_readout(100.0 + p._MAP_EPS_UM / 4, 200.0)   # well under the epsilon
    r.check(calls == [], f"a sub-epsilon move does not repaint the map ({calls})")
    r.check(p._lbl_x.text().strip() != "",
            "…but the numeric readout label still updates regardless")

    calls.clear()
    p.set_readout(100.0 + p._MAP_EPS_UM * 4, 200.0)   # well past the epsilon
    r.check(len(calls) == 1,
            f"control: a real move still repaints ({calls})")


def _framed(p: SettingsPanel) -> SettingsPanel:
    for ax in (p._s.x, p._s.y):
        ax.slope, ax.offset, ax.origin_set = 1.0, 0.0, True
    p._update_frame_status()
    return p


def _tick(p: SettingsPanel, dt: float) -> None:
    """One drag tick as if `dt` s had passed (no event loop needed)."""
    p._drag_t = time.monotonic() - dt
    p._drag_tick()


def check_drag_speed_cap(r: Report) -> None:
    """Dragging the map dot moves the stage no faster than the drag speed."""
    WARNINGS.clear()
    c = FakeCtrl()
    p = _framed(_panel(c))
    speed = p._spn_drag.value()
    r.check(p._map._draggable, "connected + framed -> the dot is draggable")
    r.check(p.settings.drag_speed_um_s == speed,
            "the drag speed rides along in the saved settings")

    p._last_xy = (0.0, 0.0)
    p._on_drag_moved(5000.0, 0.0)
    r.check(p._drag_timer.isActive(), "a drag starts the chase timer")
    _tick(p, 0.1)
    moves = [a for n, a in c.calls if n == "move_to_um"]
    r.check(len(moves) == 1 and moves[0][0] == "x"
            and 0.0 < moves[0][1] <= speed * 0.1 * 1.5,
            f"one tick moves X by about speed*dt, Y untouched ({moves})")

    c.calls.clear()
    _tick(p, 10.0)                                   # a stalled GUI
    x = [a for n, a in c.calls if n == "move_to_um"][0][1]
    r.check(x - moves[0][1] <= speed * p._DRAG_MAX_DT_S * 1.01,
            f"a stall is capped at {p._DRAG_MAX_DT_S} s of travel "
            f"(stepped {x - moves[0][1]:.1f} µm)")

    p._on_drag_moved(*p._drag_cmd)                   # mouse up on the dot
    _tick(p, 0.1)
    r.check(not p._drag_timer.isActive() and p._map._target is None,
            "at the target with the mouse up, the chase ends")

    p._on_drag_moved(3000.0, 3000.0)
    p._stop_all()
    r.check(not p._drag_timer.isActive() and p._drag_cmd is None,
            "STOP ALL (Esc) ends a drag")
    p._on_drag_moved(3000.0, 3000.0)
    p._jog("x", +1)
    r.check(p._drag_cmd is None, "any other motion command ends a drag")
    r.check(not WARNINGS, f"no warnings on a healthy drag ({WARNINGS})")

    dead = _framed(_panel(FakeCtrl(boom=True)))
    dead._last_xy = (0.0, 0.0)
    dead._on_drag_moved(1000.0, 0.0)
    try:
        _tick(dead, 0.1)
        _tick(dead, 0.1)
        r.check(True, "a failing drag move does not escape the timer slot")
    except Exception as e:                           # noqa: BLE001
        r.check(False, f"{type(e).__name__} escaped the drag tick: {e}")
    r.check(len(WARNINGS) == 1 and dead._drag_cmd is None,
            f"a failed step warns once and ends the drag ({len(WARNINGS)})")


def check_drag_gating(r: Report) -> None:
    """Dragging is an absolute move: no frame or no stage, no drag."""
    p = _panel(None)
    p._on_drag_moved(100.0, 100.0)
    r.check(not p._map._draggable and p._drag_cmd is None,
            "unbound: not draggable, and a stray drag is ignored")
    c = FakeCtrl()
    p = _framed(_panel(c))
    p._on_drag_moved(100.0, 100.0)
    p._s.x.frame_stale = True                        # a hard-limit hit
    p._update_frame_status()
    r.check(not p._map._draggable and p._drag_cmd is None,
            "losing the frame mid-drag ends it and disables dragging")
    p = _framed(_panel(FakeCtrl()))
    p.bind_controller(None)
    r.check(not p._map._draggable, "disconnecting disables dragging")


def check_map_mouse(r: Report) -> None:
    """Only a press on the dot starts a drag; moves emit soft-limited µm."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    p = _framed(_panel(FakeCtrl()))
    m = p._map
    m.resize(400, 400)
    m.set_position(0.0, 0.0)
    got: list[tuple] = []
    m.drag_moved.connect(lambda x, y: got.append((x, y)))
    dot = m._frame().to_px(0.0, 0.0).toPoint()
    left = Qt.MouseButton.LeftButton

    QTest.mousePress(m, left, pos=dot + QPoint(60, 0))
    QTest.mouseMove(m, dot + QPoint(80, 0))
    QTest.mouseRelease(m, left, pos=dot + QPoint(80, 0))
    r.check(got == [], f"a press off the dot moves nothing ({got})")

    QTest.mousePress(m, left, pos=dot)
    QTest.mouseMove(m, dot + QPoint(40, -40))
    QTest.mouseMove(m, QPoint(-500, -500))           # far past the travel
    QTest.mouseRelease(m, left, pos=QPoint(-500, -500))
    r.check(len(got) == 2 and got[0][0] > 0 and got[0][1] > 0,
            f"dragging right/up from the dot emits +X/+Y µm ({got[:1]})")
    (x0, _), (_, y1) = p._s.x.soft_limits_um(), p._s.y.soft_limits_um()
    r.check(len(got) == 2 and got[1] == (x0, y1),
            f"a drag off the map is clamped to the soft limits ({got[-1:]})")
    r.check(not m.dragging, "release ends the mouse drag")


def check_fovs_on_map(r: Report) -> None:
    """This session's FOVs are drawn and labelled; the gone-to one is active."""
    from acqApp.devices.stage import fov_store
    a = fov_store.load(fov_store.save("spotA_fov", 100.0, 200.0))
    fov_store.save("spotB_fov", -300.0, 50.0)
    p = _panel(FakeCtrl())
    names = [n for n, _, _ in p._map._fovs]
    r.check({"spotA_fov", "spotB_fov"} <= set(names),
            f"saved FOVs appear on the map by name ({names})")
    p._last_xy = (100.0, 200.0)
    p.goto_fov(a)
    r.check(p._map._active_fov == "spotA_fov", "the gone-to FOV is active")
    p.set_readout(900.0, 900.0)
    r.check(p._map._active_fov == "", "drifting off it clears the highlight")
    p._map.resize(400, 400)
    p._map.grab()                                    # paints without raising


def check_cam_axes(r: Report) -> None:
    """Measured axes turn the jogs and the map to the camera, and persist."""
    import math
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    WARNINGS.clear()
    c = FakeCtrl()
    p = _framed(_panel(c))
    a = math.radians(40.0)              # stage +X at 40° on screen, mirrored Y
    axes = (math.cos(a) * 2, -math.sin(a) * 2,
            math.sin(a) * 2, math.cos(a) * 2)
    p._set_cam_axes(axes)
    r.check(p.settings.cam_axes == axes, "the axes ride along in the settings")
    r.check(not p._spn_rotation.isEnabled(),
            "Frame rotation is greyed out while measured axes rule")

    p._axis_widgets["x"]["step"].setValue(10.0)
    p._jog("x", +1)
    dx, dy = [a for n, a in c.calls if n == "jog_xy_um"][-1]
    ix, iy = axes[0] * dx + axes[2] * dy, axes[1] * dx + axes[3] * dy
    r.check(ix > 0 and abs(iy) < 1e-9 and abs(math.hypot(dx, dy) - 10) < 1e-9,
            f"Jog X+ slides the picture right ({ix:.2f}, {iy:.2f})")

    m = p._map
    m.resize(400, 400)
    m.set_position(0.0, 0.0)
    got: list[tuple] = []
    m.drag_moved.connect(lambda x, y: got.append((x, y)))
    dot = m._frame().to_px(0.0, 0.0).toPoint()
    QTest.mousePress(m, Qt.MouseButton.LeftButton, pos=dot)
    QTest.mouseMove(m, dot + QPoint(40, 0))
    QTest.mouseRelease(m, Qt.MouseButton.LeftButton, pos=dot + QPoint(40, 0))
    x, y = got[-1]
    ix, iy = axes[0] * x + axes[2] * y, axes[1] * x + axes[3] * y
    r.check(ix > 0 and abs(iy) < 0.05 * ix,
            f"the map is turned to the camera: dragging right moves the stage "
            f"so the picture slides right ({ix:.1f}, {iy:.1f})")

    p._set_cam_axes(None)
    r.check(p.settings.cam_axes is None and p._spn_rotation.isEnabled(),
            "Clear hands the jogs back to Frame rotation")
    r.check(not WARNINGS, f"no warnings ({WARNINGS})")


def check_measure_gating(r: Report) -> None:
    """A measurement owns the stage: other motion off, Esc cancels it."""
    from PyQt6.QtWidgets import QInputDialog
    import numpy as np
    WARNINGS.clear()
    p = _panel(None)
    r.check(not p._btn_measure_axes.isEnabled(), "unbound: Measure is off")
    c = FakeCtrl()
    c.read_xy_um = lambda: (0.0, 0.0)
    c.is_moving = lambda: False
    p = _framed(_panel(c))
    p._measure_axes()                                   # no camera source
    r.check(p._axes_run is None and INFOS and "Live view" in INFOS[-1],
            "no camera frame: nothing moves, and it says to start Live view")

    frame = np.zeros((64, 64), np.float32)
    p.set_camera_source(lambda: frame, lambda: 1)
    orig = QInputDialog.getDouble
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (25.0, True))
    try:
        p._measure_axes()
    finally:
        QInputDialog.getDouble = orig
    r.check(p._axes_run is not None and p._axes_run.running
            and not p._motion.isEnabled() and not p._map._draggable,
            "measuring: jog/go-to and map drag are off")
    p._stop_all()
    r.check(p._axes_run is None and p._motion.isEnabled(),
            "Esc cancels the measurement and gives the controls back")
    r.check(len(WARNINGS) == 1 and "Cancelled" in WARNINGS[0],
            f"…and says so, with where the stage is ({WARNINGS})")


def main() -> int:
    r = Report("stage-panel")
    isolate_user_state()
    _fake_dialogs()
    app = qt_app()  # held: a collected QApplication aborts widget construction
    assert app is not None
    before = _REAL_CONFIG.stat().st_mtime if _REAL_CONFIG.exists() else None

    check_healthy(r)
    check_confirm_threshold(r)
    check_dead_link(r)
    check_unbound(r)
    check_frame_gating(r)
    check_map_repaint_guard(r)
    check_current_position_not_consumed(r)
    check_drag_speed_cap(r)
    check_drag_gating(r)
    check_map_mouse(r)
    check_fovs_on_map(r)
    check_cam_axes(r)
    check_measure_gating(r)
    check_real_config_untouched(r)

    after = _REAL_CONFIG.stat().st_mtime if _REAL_CONFIG.exists() else None
    r.check(before == after,
            "the operator's real stage config was not modified")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
