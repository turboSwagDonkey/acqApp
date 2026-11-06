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
    p.goto_fov(_fov(0.0, 0.0))
    p._stop_all()


def _fov(x_um: float, y_um: float):
    from types import SimpleNamespace
    return SimpleNamespace(name="test_fov", x_um=x_um, y_um=y_um, z_um=None,
                           path=None)


def check_healthy(r: Report) -> None:
    WARNINGS.clear()
    c = FakeCtrl()
    p = _panel(c)
    r.check(p._axis_widgets["x"]["step"] is p._axis_widgets["y"]["step"],
            "X and Y share one step")
    p._axis_widgets["x"]["step"].setValue(12.5)
    # The FOV is where the stage is, so the confirm dialog is not involved.
    p.set_readout(0.0, 0.0)
    _drive(p)

    names = [n for n, _ in c.calls]
    for want in ("jog_xy_um", "move_to_um", "stop_all"):
        r.check(want in names, f"{want} reaches the controller")

    jogs = [a for n, a in c.calls if n == "jog_xy_um"]
    r.check(jogs == [(-12.5, 0.0), (0.0, 12.5)],
            f"jog carries axis, sign and the step (got {jogs})")
    r.check(not WARNINGS, f"a healthy controller warns about nothing "
                          f"({len(WARNINGS)} warnings)")


def check_confirm_threshold(r: Report) -> None:
    """A long Go to FOV asks first — the guard against the wrong FOV."""
    global _ANSWER
    ASKED.clear()
    c = FakeCtrl()
    p = _panel(c)

    p.set_readout(0.0, 0.0)
    p.goto_fov(_fov(0.0, 0.0))
    r.check(not ASKED, "a zero-length move asks nothing")
    r.check([n for n, _ in c.calls] == ["move_to_um"] * 2, "and still moves")

    p.set_readout(10 * p._s.confirm_move_um, 0.0)
    _ANSWER = QMessageBox.StandardButton.No
    try:
        p.goto_fov(_fov(0.0, 0.0))
    finally:
        _ANSWER = QMessageBox.StandardButton.Yes
    r.check(len(ASKED) == 1, "a move past confirm_move_um asks first")
    r.check([n for n, _ in c.calls] == ["move_to_um"] * 2,
            "answering No does not move")


def check_dead_link(r: Report) -> None:
    """Every handler must survive a controller whose every call raises."""
    WARNINGS.clear()
    c = FakeCtrl(boom=True)
    p = _panel(c)
    p.set_readout(0.0, 0.0)
    try:
        _drive(p)
        r.check(True, "no handler lets an exception escape into the Qt slot")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a handler: {e}")

    r.check(len(c.calls) >= 4,
            f"every command was still attempted ({len(c.calls)})")
    r.check(len(WARNINGS) >= 4,
            f"each failure is reported, not swallowed ({len(WARNINGS)})")
    r.check(any("STOP ALL" in w for w in WARNINGS),
            "the panic path reports by name")

    # Control: the pre-2026-08-13 body on the same controller. If it stops
    # raising, the guards above are no longer what is being tested.
    for label, body in (("_stop_all", lambda: c.stop_all()),):
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
    p.set_readout(0.0, 0.0)
    try:
        _drive(p)
        r.check(True, "an unbound panel ignores every command")
    except Exception as e:                       # noqa: BLE001
        r.check(False, f"unbound panel raised {type(e).__name__}: {e}")
    r.check(not WARNINGS, "and says nothing about it")


def check_frame_gating(r: Report) -> None:
    """Absolute moves are meaningless without a frame; jog is not."""
    S = stage_settings
    c = FakeCtrl()
    axes = [S.StageAxis(i, n, 2.0, slope=1.0, offset=0.0)
            for i, n in ((0, "X"), (1, "Y"))]
    for ax in axes:
        ax.apply_updates(ax.center_updates(5000))
    p = SettingsPanel(S.StageSettings(x=axes[0], y=axes[1]))
    p.bind_controller(c)
    r.check(p._btn_goto_fov.isEnabled(), "control: framed, Go to FOV is on")
    for ax in (p._s.x, p._s.y):
        ax.slope = None
    p._update_frame_status()
    r.check(not p._btn_goto_fov.isEnabled() and not p._map._draggable,
            "no frame -> Go to FOV and the map drag are off")
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

    # Disconnect clears the dot; reconnecting at the same spot must redraw it.
    p.bind_controller(FakeCtrl())
    p.bind_controller(None)
    calls.clear()
    p.set_readout(100.0 + p._MAP_EPS_UM * 4, 200.0)
    r.check(len(calls) == 1,
            f"after a disconnect the same position repaints the map ({calls})")


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

    p.set_readout(0.0, 0.0)
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
    dead.set_readout(0.0, 0.0)
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
    p.set_readout(0.0, 0.0)
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
    r.check(len(got) == 2 and x0 <= got[1][0] < -350.0
            and 350.0 < got[1][1] <= y1,
            f"a drag off the map goes past the window, within the soft "
            f"limits ({got[-1:]})")
    r.check(not m.dragging and m._held_view is None,
            "release ends the mouse drag, and the map may rescale again")
    m.set_position(-1000.0, 1000.0)
    r.check(m._view()[0] == p._s.x.travel_limits_um(),
            "…zooming out once the stage is beyond the window")
    p._s.x.soft_min, p._s.x.soft_max = sorted((p._s.x.um_to_counts(-300.0),
                                               p._s.x.um_to_counts(300.0)))
    m.set_position(0.0, 0.0)
    got.clear()
    QTest.mousePress(m, left, pos=dot)
    QTest.mouseMove(m, QPoint(-500, -500))
    QTest.mouseRelease(m, left, pos=QPoint(-500, -500))
    r.check(got and abs(got[-1][0] + 300.0) < 0.01,      # via whole counts
            f"control: soft limits narrower than the drag still clamp it "
            f"({got[-1:]})")


def check_fovs_on_map(r: Report) -> None:
    """This session's FOVs are drawn and labelled; the gone-to one is active."""
    from acqApp.devices.stage import fov_store
    a = fov_store.load(fov_store.save("spotA_fov", 100.0, 200.0))
    fov_store.save("spotB_fov", -300.0, 50.0)
    p = _panel(FakeCtrl())
    names = [n for n, _, _ in p._map._fovs]
    r.check({"spotA_fov", "spotB_fov"} <= set(names),
            f"saved FOVs appear on the map by name ({names})")
    p.set_readout(100.0, 200.0)
    p.goto_fov(a)
    r.check(p._map._active_fov == "spotA_fov", "the gone-to FOV is active")
    p.set_readout(900.0, 900.0)
    r.check(p._map._active_fov == "", "drifting off it clears the highlight")
    p._map.resize(400, 400)
    p._map.grab()                                    # paints without raising

    # A hand-edited or foreign file must not break the panel or the picker.
    from acqApp.devices.stage.fov_picker import FovPicker
    bad = fov_store.SESSION_DIR / f"bad{fov_store.SUFFIX}"
    bad.write_text('{"name": "bad", "x_um": null}', encoding="utf-8")
    (fov_store.SESSION_DIR / f"list{fov_store.SUFFIX}").write_text("[1]", encoding="utf-8")
    try:
        p.refresh_fovs()
        names = [n for n, _, _ in p._map._fovs]
        r.check("bad" not in names and "spotB_fov" in names,
                f"an unreadable FOV is skipped, the rest still listed ({names})")
        WARNINGS.clear()
        dlg = FovPicker()
        dlg.chose(bad)                               # as Browse would
        r.check(dlg.fov is None and len(WARNINGS) == 1,
                f"choosing an unreadable file warns, not raises ({WARNINGS})")
    finally:
        bad.unlink()
        (fov_store.SESSION_DIR / f"list{fov_store.SUFFIX}").unlink()


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
    p.set_readout(0.0, 0.0)
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


def check_save_fov_disk_error(r: Report) -> None:
    """Save FOV is a slot: a full or read-only disk warns, never escapes."""
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QInputDialog
    from acqApp.adapters.stage import StageModule
    from acqApp.devices.stage import fov_store

    def refuse(*_a, **_k):
        raise PermissionError("read-only")

    panel = SimpleNamespace(connected=True, position_known=True,
                            current_position=(1.0, 2.0, None),
                            refresh_fovs=lambda: None)
    win = SimpleNamespace(pattern_target=lambda: None,
                          camera_preset=lambda _k: None,
                          status=lambda _m: None, latest_frame=lambda _k: None)
    fake = SimpleNamespace(panel=panel, win=win, _snapshot_png=lambda: None)
    WARNINGS.clear()
    saved = fov_store.save, QInputDialog.getText
    fov_store.save = refuse
    QInputDialog.getText = staticmethod(lambda *a, **k: ("spot", True))
    try:
        StageModule.save_fov(fake)
        r.check(len(WARNINGS) == 1 and "read-only" in WARNINGS[0],
                f"a failed FOV write is reported ({WARNINGS})")
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"save_fov raised {type(e).__name__}: {e}")
    finally:
        fov_store.save, QInputDialog.getText = saved


def check_save_fov_needs_a_reading(r: Report) -> None:
    """Before the first readout (or after the poll died) the panel's position
    is a placeholder (0,0) or stale: saving it would bookmark a wrong spot."""
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QInputDialog
    from acqApp.adapters.stage import StageModule
    from acqApp.devices.stage import fov_store

    p = _panel(FakeCtrl())
    r.check(not p.position_known, "bound, no readout yet: position unknown")
    win = SimpleNamespace(pattern_target=lambda: None,
                          camera_preset=lambda _k: None,
                          status=lambda _m: None, latest_frame=lambda _k: None)
    fake = SimpleNamespace(panel=p, win=win, _snapshot_png=lambda: None)
    wrote: list = []
    saved = fov_store.save, QInputDialog.getText
    fov_store.save = lambda *a, **k: wrote.append(a) or Path("x.fov.json")
    QInputDialog.getText = staticmethod(lambda *a, **k: ("spot", True))
    INFOS.clear()
    try:
        StageModule.save_fov(fake)
        r.check(not wrote and INFOS and "No position" in INFOS[-1],
                f"Save FOV with no reading refuses ({wrote}, {INFOS[-1:]})")
        p.set_readout(12.0, 34.0)
        StageModule.save_fov(fake)
        r.check(wrote and wrote[-1][1:3] == (12.0, 34.0),
                f"control: after a readout it saves that position ({wrote})")
    finally:
        fov_store.save, QInputDialog.getText = saved


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
    p.set_readout(0.0, 0.0)
    orig = QInputDialog.getDouble
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (25.0, True))
    try:
        p._measure_axes()
    finally:
        QInputDialog.getDouble = orig
    r.check(p._axes_run is not None and p._axes_run.running
            and not p._motion.isEnabled() and not p._map._draggable,
            "measuring: jog/go-to and map drag are off")
    run = p._axes_run
    p._stop_all()
    r.check(p._axes_run is None and p._motion.isEnabled(),
            "Esc cancels the measurement and gives the controls back")
    r.check(len(WARNINGS) == 1 and "Cancelled" in WARNINGS[0],
            f"…and says so, with where the stage is ({WARNINGS})")
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    r.check(sip.isdeleted(run), "the finished measurement is freed, not "
            "kept as a child of the panel")


def check_window(r: Report) -> None:
    """Home (0,0) set: the map shows ±350 µm of it with the window dashed,
    zooming out when the stage is beyond; motion is never confined to it.
    Set home here sets it, after asking, without moving the stage."""
    global _ANSWER
    from types import SimpleNamespace

    S = stage_settings

    def homed(i: int, name: str) -> S.StageAxis:
        ax = S.StageAxis(i, name, 2.0, slope=1.0, offset=0.0)
        ax.apply_updates(ax.center_updates(5000))
        return ax

    ctrl = FakeCtrl()
    p = SettingsPanel(S.StageSettings(x=homed(0, "X"), y=homed(1, "Y")))
    p.bind_controller(ctrl)
    p._map.resize(400, 400)
    p._map.set_fovs([("near_fov", 100.0, -50.0)])
    p.set_readout(10.0, 10.0)
    r.check(p._map._view() == ((-350.0, 350.0), (-350.0, 350.0)),
            "home set, stage inside: the map shows ±350 µm of home")
    p._map.grab()                                   # window + labels paint
    from PyQt6.QtCore import QPointF
    xf = p._map._frame()
    sq = xf.to_px(100.0, -50.0)
    r.check(p._map.fov_at(sq + QPointF(3, 3)) == 0
            and p._map.fov_at(xf.to_px(-300.0, 300.0)) is None,
            "a right-click finds the FOV square under it, and only there")
    p.set_readout(1000.0, 0.0)
    r.check(p._map._view()[0] == p._s.x.travel_limits_um(),
            "the stage beyond the window: the map zooms out to the travel")

    WARNINGS.clear()
    ctrl.calls.clear()
    p.set_readout(0.0, 0.0)
    p.goto_fov(SimpleNamespace(name="far_fov", x_um=1000.0, y_um=0.0,
                               z_um=None, path=None))
    r.check(any(n == "move_to_um" for n, *_ in ctrl.calls) and not WARNINGS,
            f"an FOV beyond the window is reached, not refused "
            f"({ctrl.calls}, {WARNINGS})")

    ctrl.calls.clear()
    ASKED.clear()
    _ANSWER = QMessageBox.StandardButton.No
    p._set_home_here()
    r.check(ASKED and not ctrl.calls,
            "Set home here asks first; No changes nothing")
    _ANSWER = QMessageBox.StandardButton.Yes
    p._set_home_here()
    r.check(("set_center_here", ()) in [(n, a) for n, a, *_ in ctrl.calls]
            and not any(n.startswith(("move", "jog")) for n, *_ in ctrl.calls),
            f"…Yes sets home here, without moving the stage ({ctrl.calls})")


def check_window_edges(r: Report) -> None:
    """Motion that starts at or past the soft limits' edge (narrowed here to
    ±350 µm, as a hand-edited config could): a drag from outside would jump
    to it at full speed; a FOV saved on the edge reads back a count's
    rounding past it; an axes nudge there would be cut short."""
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QInputDialog
    import numpy as np
    S = stage_settings

    def homed(i: int, name: str) -> S.StageAxis:
        ax = S.StageAxis(i, name, 2.0, slope=1.0, offset=0.0)
        ax.apply_updates(ax.center_updates(5000))
        ax.soft_min, ax.soft_max = sorted((ax.um_to_counts(-350.0),
                                           ax.um_to_counts(350.0)))
        return ax

    ctrl = FakeCtrl()
    p = SettingsPanel(S.StageSettings(x=homed(0, "X"), y=homed(1, "Y")))
    p.bind_controller(ctrl)
    WARNINGS.clear()
    p.set_readout(1000.0, 0.0)                    # before the limits narrowed
    p._on_drag_moved(300.0, 0.0)
    _tick(p, 0.1)
    r.check(not [c for c in ctrl.calls if c[0] == "move_to_um"]
            and p._drag_cmd is None and WARNINGS
            and "outside its allowed range" in WARNINGS[-1],
            f"a drag from outside the soft limits refuses instead of jumping "
            f"650 µm to their edge ({ctrl.calls}, {WARNINGS[-1:]})")
    p.set_readout(100.0, 0.0)
    WARNINGS.clear()
    p._on_drag_moved(300.0, 0.0)
    _tick(p, 0.1)
    r.check(not WARNINGS and [c for c in ctrl.calls if c[0] == "move_to_um"],
            "control: the same drag from inside the limits goes")
    p._end_drag()

    ctrl.calls.clear()
    p.set_readout(0.0, 0.0)
    p.goto_fov(SimpleNamespace(name="edge_fov", x_um=350.01, y_um=0.0,
                               z_um=None, path=None))
    r.check(("move_to_um", ("x", 350.01)) in ctrl.calls,
            f"a FOV saved on the limits' edge can be gone to ({ctrl.calls})")
    p._active_fov = None

    p.set_camera_source(lambda: np.zeros((8, 8), np.float32), lambda: 1)
    ctrl.calls.clear()
    WARNINGS.clear()
    p.set_readout(340.0, 0.0)
    orig = QInputDialog.getDouble
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (50.0, True))
    try:
        p._measure_axes()
    finally:
        QInputDialog.getDouble = orig
    r.check(p._axes_run is None and not ctrl.calls and WARNINGS
            and "No room" in WARNINGS[-1],
            f"a +50 µm axes nudge 10 µm from the edge refuses before moving "
            f"({ctrl.calls}, {WARNINGS[-1:]})")

    p.set_readout(0.0, 0.0)
    p.clear_position()
    r.check(p._map._pos is None and p._last_map_xy is None,
            "the poll gone: the dot leaves the map, so no drag can grab it")

    # Stale _last_xy (0,0) would make a FOV at 0,0 look like no move at all.
    ASKED.clear()
    WARNINGS.clear()
    ctrl.calls.clear()
    p.goto_fov(_fov(0.0, 0.0))
    p._active_fov = None
    r.check(ASKED and "unknown" in ASKED[-1],
            f"position unknown: Go to FOV always asks ({ASKED[-1:]})")
    p._on_drag_moved(100.0, 0.0)
    r.check(p._drag_cmd is None and WARNINGS and "unknown" in WARNINGS[-1],
            f"…a map drag refuses ({WARNINGS[-1:]})")
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (5.0, True))
    try:
        p._measure_axes()
    finally:
        QInputDialog.getDouble = orig
    r.check(p._axes_run is None and "unknown" in WARNINGS[-1],
            f"…and so does Measure camera axes ({WARNINGS[-1:]})")
    r.check([c for c in ctrl.calls if c[0] != "move_to_um"] == [],
            f"…with no motion but the confirmed Go to ({ctrl.calls})")
    p.set_readout(0.0, 0.0)
    r.check(p._map._pos == (0.0, 0.0),
            "…and the next readout puts it back, even at the same spot")


def check_window_toggle_persists(r: Report) -> None:
    """A config saved while "Limit to window" existed still loads, and the
    card shows home and rotation rather than repeating the readout."""
    from types import SimpleNamespace

    from acqApp import config
    from acqApp.adapters.stage import StageModule

    config.save_settings("stage", {"window_limit": True,
                                   "frame_rotation_deg": 12.5})
    win = SimpleNamespace(status=lambda _m: None)
    m = StageModule(win)
    try:
        m.build_panel()
        r.check(True, "an old window_limit key loads without complaint")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} loading an old window_limit: {e}")
        return
    home, detail = m._mini_text()
    s = m.panel.settings
    want = "home set" if s.x.origin_set and s.y.origin_set else "home not set"
    r.check(home == want and "rotation 12.5°" in detail
            and "X " not in home,
            f"the card says home and rotation, not X/Y/Z ({home!r}, "
            f"{detail!r})")


def check_poll_failure_clears_position(r: Report) -> None:
    """The poll worker stops for good on a failed read: the adapter must take
    the now-stale position off the map."""
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QApplication
    from acqApp.adapters.stage import StageModule
    S = stage_settings

    win = SimpleNamespace(status=lambda _m: None,
                          on_worker_error=lambda _m: None)
    m = StageModule(win)
    s = S.StageSettings()
    for ax in (s.x, s.y):
        ax.apply_updates(ax.center_updates(0))
    m.panel = SettingsPanel(s)
    m.build_controller(emulate=True)
    try:
        deadline = time.monotonic() + 3.0
        while m.panel._map._pos is None and time.monotonic() < deadline:
            QApplication.processEvents()
            m.update_display()
            time.sleep(0.02)
        r.check(m.panel._map._pos is not None, "fixture: the poll draws the dot")

        def dead():
            raise OSError("serial link gone")
        m.controller.read_xy_um = dead
        deadline = time.monotonic() + 3.0
        while m.panel._map._pos is not None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        r.check(m.panel._map._pos is None,
                "a failed poll read takes the stale dot off the map")
    finally:
        m.close_controller()


def check_no_arrow_keys(r: Report) -> None:
    """Keyboard jogging is gone: an arrow key on the page moves nothing."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent
    c = FakeCtrl()
    p = _panel(c)
    for key in (Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
        p.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key.value,
                                       Qt.KeyboardModifier.NoModifier))
    r.check(not c.calls, f"arrow keys and PgUp move nothing ({c.calls})")


def check_esc_when_hidden(r: Report) -> None:
    """Esc = STOP ALL must work with the stage page out of sight (a hidden
    widget's shortcut never fires), and leave with the page."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
    c = FakeCtrl()
    p = _panel(c)
    host = QWidget()
    lay = QVBoxLayout(host)
    edit = QLineEdit()
    lay.addWidget(edit)
    lay.addWidget(p.view)
    host.show()
    host.activateWindow()
    QApplication.processEvents()
    p.view.hide()
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key.Key_Escape)
    stops = lambda: [n for n, _ in c.calls].count("stop_all")    # noqa: E731
    r.check(stops() == 1, f"Esc stops all with the page hidden ({stops()})")

    p.view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    QApplication.processEvents()
    QTest.keyClick(edit, Qt.Key.Key_Escape)
    r.check(stops() == 1, "…and the shortcut goes with the page")
    host.close()


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
    check_save_fov_disk_error(r)
    check_save_fov_needs_a_reading(r)
    check_window(r)
    check_window_edges(r)
    check_window_toggle_persists(r)
    check_poll_failure_clears_position(r)
    check_no_arrow_keys(r)
    check_esc_when_hidden(r)
    check_real_config_untouched(r)

    after = _REAL_CONFIG.stat().st_mtime if _REAL_CONFIG.exists() else None
    r.check(before == after,
            "the operator's real stage config was not modified")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
