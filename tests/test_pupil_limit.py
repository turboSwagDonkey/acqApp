"""
The eye region: placed on the preview, drawn, persisted, recorded.

A head-fixed animal puts the eye in one fixed part of the frame, so the operator
marks it once. It no longer bounds a search — the tracker was archived on
2026-08-24 (PLAN §7 (ai)) — but it is operator-set geometry that persists, draws
on the preview and lands in the session metadata, so it is still checked here:

  * the rectangle is drawn whenever it is in force, and follows the spinboxes;
  * a placement writes back as ONE settings change, not four;
  * a drag places it, the release commits AND disarms, and with the tool off
    the same drag does nothing.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_limit.py
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import QPointF, Qt

from _harness import Report, isolate_user_state, pump, qt_app


def npoints(item) -> int:
    """How many points a PlotCurveItem is drawing (None before any setData)."""
    xs = item.getData()[0]
    return 0 if xs is None else len(xs)


class _DragEv:
    """Stands in for pyqtgraph's MouseDragEvent, delivered to
    `DragRectViewBox.mouseDragEvent` (`adapters/base.py`) exactly as a real
    drag would be — this drives the real armed/unarmed gate, not just the
    adapter's own `_on_limit_drag`."""

    def __init__(self, start, pos, finish: bool):
        self._start = start
        self._pos = pos
        self._finish = finish

    def button(self):
        return Qt.MouseButton.LeftButton

    def buttonDownScenePos(self):
        return self._start

    def scenePos(self):
        return self._pos

    def isFinish(self) -> bool:
        return self._finish

    def accept(self) -> None:
        pass


def main() -> int:
    r = Report("pupil-limit")

    from acqApp.devices.pupil_cam.settings import PupilSettings

    # ── 1. the settings model ────────────────────────────────────────────────
    r.check(PupilSettings().search_limit() is None,
            "shipped default is no region")
    s = PupilSettings(limit_x0=530.0, limit_y0=190.0, limit_x1=750.0, limit_y1=410.0)
    r.check(s.search_limit() == (530.0, 190.0, 750.0, 410.0),
            "a set region reads back as one tuple")
    r.check(PupilSettings(limit_x0=530.0, limit_y0=190.0, limit_x1=530.0,
                          limit_y1=410.0).search_limit() is None,
            "control: a collapsed box (X1<=X0) is 'no region' whatever Y says")

    # ══ the app half ═════════════════════════════════════════════════════════
    app = qt_app()
    isolate_user_state()
    import acqApp.main as M
    sys.argv = ["main.py", "--mock"]

    win = M.MainWindow(cam_info=None, mock=True, enabled={"pupil_cam"},
                       cam_handle=None)
    mod = win._modules[0]
    panel = mod.panel

    # ── 8. the rectangle is drawn whenever it is in force ────────────────────
    r.check(npoints(mod._limit_curve) == 0, "no limit set: nothing is drawn")
    panel.set_limit(530.0, 190.0, 750.0, 410.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(npoints(mod._limit_curve) == 5,
            f"setting a limit outlines it on the preview as a closed rectangle "
            f"({npoints(mod._limit_curve)} points)")
    ys = mod._limit_curve.getData()[1]
    r.check(abs(xs.min() - 530.0) < 1.0 and abs(xs.max() - 750.0) < 1.0
            and abs(ys.min() - 190.0) < 1.0 and abs(ys.max() - 410.0) < 1.0,
            f"…at the box it was given ({xs.min():.0f},{ys.min():.0f})-"
            f"({xs.max():.0f},{ys.max():.0f})")
    r.check(panel.settings.limit_x1 == 750.0, "the panel carries X1")
    md = mod.metadata()
    r.check(md.get("pupil_limit_x0") == 530.0 and md.get("pupil_limit_x1") == 750.0,
            f"the session metadata records the limit ({md.get('pupil_limit_x0')}, "
            f"{md.get('pupil_limit_y0')}, {md.get('pupil_limit_x1')}, "
            f"{md.get('pupil_limit_y1')})")

    # ── 9. one settings change per placement, not four ───────────────────────
    seen: list = []
    panel.settings_changed.connect(lambda s: seen.append(s))
    panel.set_limit(400.0, 150.0, 600.0, 350.0)
    r.check(len(seen) == 1, f"a placement writes back as one settings change "
                            f"({len(seen)})")
    seen.clear()
    # CONTROL: the same four values typed into the spinboxes really do emit
    # four times, so the check above is not vacuous.
    panel._spn_lx0.setValue(401.0)
    panel._spn_ly0.setValue(151.0)
    panel._spn_lx1.setValue(601.0)
    panel._spn_ly1.setValue(351.0)
    r.check(len(seen) == 4, f"control: four typed edits are four changes "
                            f"({len(seen)})")

    # ── 10. clearing, from either place ──────────────────────────────────────
    r.check(panel._btn_limit_clear.isEnabled(), "Clear is live while a region is set")
    r.check(mod._btn_limit_off.isEnabled(), "…on the preview bar too")
    mod._btn_limit_off.click()
    pump(app, 0.05)
    r.check(panel.settings.search_limit() is None, "…and clearing removes it")
    r.check(npoints(mod._limit_curve) == 0, "…and un-draws the rectangle")
    r.check(not panel._btn_limit_clear.isEnabled() and
            not mod._btn_limit_off.isEnabled(),
            "control: with no region there is nothing to clear")

    # ── 11. typing a number must move the drawn rectangle ────────────────────
    panel.set_limit(300.0, 200.0, 420.0, 320.0)
    pump(app, 0.05)
    panel._spn_lx0.setValue(150.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(xs is not None and abs(xs.min() - 150.0) < 1.0,
            f"typing a new X0 moves the drawn rectangle ({xs.min():.0f})")
    panel._spn_lx1.setValue(500.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(abs(xs.max() - 500.0) < 1.0,
            f"…and a new X1 resizes it ({xs.max():.0f})")
    r.check("150" in mod._lbl_limit.text() and "500" in mod._lbl_limit.text(),
            f"…and the preview bar reads it back ({mod._lbl_limit.text()!r})")
    panel.clear_limit()
    pump(app, 0.05)

    # ── 12. placing it on the preview: press-drag, release commits + disarms ─
    win._btn_run.setChecked(True)
    pump(app, 1.0)                       # frames flow

    # The window is never shown, so the view has no size and never auto-ranges:
    # every scene point would map to the default 0-1 view range and the drag
    # would span 0 px. Give it both explicitly.
    mod._gv.resize(400, 300)
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.05)

    rect = mod._vb.sceneBoundingRect()
    start = QPointF(rect.center().x() - 0.2 * rect.width(),
                    rect.center().y() - 0.2 * rect.height())
    end = QPointF(rect.center().x() + 0.2 * rect.width(),
                  rect.center().y() + 0.2 * rect.height())
    s_view = mod._vb.mapSceneToView(start)
    e_view = mod._vb.mapSceneToView(end)
    want_x0, want_x1 = sorted((s_view.x(), e_view.x()))
    want_y0, want_y1 = sorted((s_view.y(), e_view.y()))
    if not r.check(want_x1 - want_x0 > 5.0 and want_y1 - want_y0 > 5.0,
                   f"fixture: the drag spans ({want_x1-want_x0:.1f}, "
                   f"{want_y1-want_y0:.1f}) px in the frame — without this the "
                   f"placement checks are vacuous"):
        return r.finish()

    r.check(not mod._btn_limit.isChecked(), "the region tool starts off")
    mod._btn_limit.setChecked(True)
    pump(app, 0.05)
    r.check("drag from one corner" in mod._lbl_limit.text(),
            f"arming says what to do next ({mod._lbl_limit.text()!r})")

    # Delivered to the ViewBox itself, not the adapter's handler directly —
    # this exercises DragRectViewBox's own armed/unarmed gate too.
    mod._vb.mouseDragEvent(_DragEv(start, start, finish=False))
    r.check(npoints(mod._limit_ghost) == 5,
            f"the rectangle follows the cursor before it is committed "
            f"({npoints(mod._limit_ghost)} points)")
    r.check(panel.settings.search_limit() is None,
            "…and commits nothing yet — mid-drag is not a region")

    mod._vb.mouseDragEvent(_DragEv(start, end, finish=True))
    pump(app, 0.05)
    s1 = panel.settings
    r.check(abs(s1.limit_x0 - want_x0) < 1 and abs(s1.limit_y0 - want_y0) < 1
            and abs(s1.limit_x1 - want_x1) < 1 and abs(s1.limit_y1 - want_y1) < 1,
            f"release sets the box ({s1.limit_x0:.0f}, {s1.limit_y0:.0f})-"
            f"({s1.limit_x1:.0f}, {s1.limit_y1:.0f}); wanted "
            f"({want_x0:.0f}, {want_y0:.0f})-({want_x1:.0f}, {want_y1:.0f})")
    r.check(not mod._btn_limit.isChecked(),
            "…and disarms itself — no mode left switched on")
    r.check(npoints(mod._limit_ghost) == 0, "…and the rubber band is cleared")

    # CONTROL: disarmed (as it now is, having just placed one), the ViewBox's
    # own gate must be off — a real pg drag event then falls all the way
    # through to `pg.ViewBox`'s own pan, which a minimal stub can't stand in
    # for, so this checks the flag `mouseDragEvent` actually gates on rather
    # than replaying a full pan through it.
    r.check(mod._vb._draw is False,
            "control: disarmed, the ViewBox's own draw-mode flag is off — the "
            "next real drag falls through to pyqtgraph's own pan")

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
