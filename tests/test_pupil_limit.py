"""
The eye region: placed on the preview, drawn, persisted, recorded.

A head-fixed animal puts the eye in one fixed part of the frame, so the operator
marks it once. It no longer bounds a search — the tracker was archived on
2026-08-24 (PLAN §7 (ai)) — but it is operator-set geometry that persists, draws
on the preview and lands in the session metadata, so it is still checked here:

  * the circle is drawn whenever it is in force, and follows the spinboxes;
  * a placement writes back as ONE settings change, not three;
  * two clicks place it, the second commits AND disarms, and with the tool off
    the same clicks do nothing.

  acqApp\.venv\Scripts\python.exe acqApp\tests\test_pupil_limit.py
"""
from __future__ import annotations

import sys

import numpy as np
from PyQt6.QtCore import QPointF

from _harness import Report, isolate_user_state, pump, qt_app


def npoints(item) -> int:
    """How many points a PlotCurveItem is drawing (None before any setData)."""
    xs = item.getData()[0]
    return 0 if xs is None else len(xs)


def main() -> int:
    r = Report("pupil-limit")

    from acqApp.devices.pupil_cam.settings import PupilSettings

    # ── 1. the settings model ────────────────────────────────────────────────
    r.check(PupilSettings().search_limit() is None,
            "shipped default is no region")
    s = PupilSettings(limit_x=640.0, limit_y=300.0, limit_r=110.0)
    r.check(s.search_limit() == (640.0, 300.0, 110.0),
            "a set region reads back as one tuple")
    r.check(PupilSettings(limit_x=640.0, limit_y=300.0, limit_r=0.0)
            .search_limit() is None,
            "control: radius 0 is 'no region' whatever x and y say")

    # ══ the app half ═════════════════════════════════════════════════════════
    app = qt_app()
    isolate_user_state()
    import acqApp.main as M
    sys.argv = ["main.py", "--mock"]

    win = M.MainWindow(cam_info=None, mock=True, enabled={"pupil_cam"},
                       cam_handle=None)
    mod = win._modules[0]
    panel = mod.panel

    # ── 8. the circle is drawn whenever it is in force ───────────────────────
    r.check(npoints(mod._limit_curve) == 0, "no limit set: nothing is drawn")
    panel.set_limit(640.0, 300.0, 110.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(npoints(mod._limit_curve) > 8,
            f"setting a limit outlines it on the preview "
            f"({npoints(mod._limit_curve)} points)")
    ys = mod._limit_curve.getData()[1]
    span = (0.5 * (xs.min() + xs.max()), 0.5 * (ys.min() + ys.max()),
            0.5 * (xs.max() - xs.min()))
    r.check(all(abs(a - b) < 1.0 for a, b in zip(span, (640.0, 300.0, 110.0))),
            f"…at the centre and radius it was given "
            f"({span[0]:.1f}, {span[1]:.1f}, r={span[2]:.1f})")
    r.check(panel.settings.limit_r == 110.0, "the panel carries the radius")
    md = mod.metadata()
    r.check(md.get("pupil_limit_r") == 110.0 and md.get("pupil_limit_x") == 640.0,
            f"the session metadata records the limit ({md.get('pupil_limit_x')}, "
            f"{md.get('pupil_limit_y')}, {md.get('pupil_limit_r')})")

    # ── 9. one settings change per placement, not three ──────────────────────
    seen: list = []
    panel.settings_changed.connect(lambda s: seen.append(s))
    panel.set_limit(500.0, 250.0, 80.0)
    r.check(len(seen) == 1, f"a placement writes back as one settings change "
                            f"({len(seen)})")
    seen.clear()
    # CONTROL: the same three values typed into the spinboxes really do emit
    # three times, so the check above is not vacuous.
    panel._spn_lx.setValue(501.0)
    panel._spn_ly.setValue(251.0)
    panel._spn_lr.setValue(81.0)
    r.check(len(seen) == 3, f"control: three typed edits are three changes "
                            f"({len(seen)})")

    # ── 10. clearing, from either place ──────────────────────────────────────
    r.check(panel._btn_limit_clear.isEnabled(), "Clear is live while a region is set")
    r.check(mod._btn_limit_off.isEnabled(), "…on the preview bar too")
    mod._btn_limit_off.click()
    pump(app, 0.05)
    r.check(panel.settings.search_limit() is None, "…and clearing removes it")
    r.check(npoints(mod._limit_curve) == 0, "…and un-draws the circle")
    r.check(not panel._btn_limit_clear.isEnabled() and
            not mod._btn_limit_off.isEnabled(),
            "control: with no region there is nothing to clear")

    # ── 11. typing a number must move the drawn circle ───────────────────────
    # The bug the operator hit: the drawn circle did not follow the spinboxes.
    # A second circle (a draggable pg.CircleROI) was painted over the top in the
    # same colour and did NOT follow, so the one you looked at never moved. It
    # is gone; this holds the property it was breaking.
    panel.set_limit(300.0, 200.0, 60.0)
    pump(app, 0.05)
    panel._spn_lx.setValue(150.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(xs is not None and abs(0.5 * (xs.min() + xs.max()) - 150.0) < 1.0,
            f"typing a new centre X moves the drawn circle "
            f"({0.5 * (xs.min() + xs.max()):.0f})")
    panel._spn_lr.setValue(120.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(abs(0.5 * (xs.max() - xs.min()) - 120.0) < 1.0,
            f"…and a new radius resizes it "
            f"({0.5 * (xs.max() - xs.min()):.0f})")
    r.check("120" in mod._lbl_limit.text() and "150" in mod._lbl_limit.text(),
            f"…and the preview bar reads it back ({mod._lbl_limit.text()!r})")
    # Only ONE circle item can be drawn now, so there is nothing left to go
    # stale behind it.
    r.check(not hasattr(mod, "_limit_roi"),
            "control: the second, non-following circle is gone entirely")
    panel.clear_limit()
    pump(app, 0.05)

    # ── 12. placing it on the preview: two clicks, no toggle to remember ─────
    # The controls are over the image, the circle follows the cursor between the
    # clicks, and the second click commits AND disarms.
    win._btn_run.setChecked(True)
    pump(app, 1.0)                       # frames flow

    # The window is never shown, so the view has no size and never auto-ranges:
    # every scene point would map to the default 0-1 view range and the two
    # clicks would land 0 px apart. Give it both explicitly.
    mod._gv.resize(400, 300)
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.05)

    rect = mod._vb.sceneBoundingRect()
    centre = rect.center()
    edge = QPointF(centre.x(), centre.y() + 0.25 * rect.height())
    c_view = mod._vb.mapSceneToView(centre)
    e_view = mod._vb.mapSceneToView(edge)
    want_r = float(np.hypot(e_view.x() - c_view.x(), e_view.y() - c_view.y()))
    if not r.check(want_r > 5.0,
                   f"fixture: the two clicks are {want_r:.1f} px apart in the "
                   f"frame — without this the placement checks are vacuous"):
        return r.finish()

    class _Ev:                          # the scene hands the handler one of these
        def __init__(self, pt): self._p = pt
        def scenePos(self): return self._p

    r.check(not mod._btn_limit.isChecked(), "the region tool starts off")
    mod._btn_limit.setChecked(True)
    pump(app, 0.05)
    r.check("centre of the eye" in mod._lbl_limit.text(),
            f"arming says what to do next ({mod._lbl_limit.text()!r})")

    mod._on_click(_Ev(centre))
    r.check(mod._limit_centre is not None,
            f"the first click takes the centre ({mod._limit_centre})")
    r.check(panel.settings.search_limit() is None,
            "…and commits nothing yet — one click is not a region")
    r.check("outer edge" in mod._lbl_limit.text(),
            f"…and the prompt moves on ({mod._lbl_limit.text()!r})")

    mod._on_move(edge)                  # the rubber band follows the cursor
    r.check(npoints(mod._limit_ghost) > 8,
            f"the circle follows the cursor before it is committed "
            f"({npoints(mod._limit_ghost)} points)")

    mod._on_click(_Ev(edge))
    pump(app, 0.05)
    s1 = panel.settings
    r.check(abs(s1.limit_x - c_view.x()) < 1 and abs(s1.limit_y - c_view.y()) < 1
            and abs(s1.limit_r - want_r) < 1,
            f"the second click sets centre and radius ({s1.limit_x:.0f}, "
            f"{s1.limit_y:.0f}, r={s1.limit_r:.0f}; wanted {c_view.x():.0f}, "
            f"{c_view.y():.0f}, r={want_r:.0f})")
    r.check(not mod._btn_limit.isChecked(),
            "…and disarms itself — no mode left switched on")
    r.check(npoints(mod._limit_ghost) == 0, "…and the rubber band is cleared")

    # CONTROL: with the tool off, the same two clicks must NOT place a region.
    before = panel.settings.search_limit()
    mod._on_click(_Ev(centre))
    mod._on_click(_Ev(edge))
    r.check(panel.settings.search_limit() == before,
            "control: with the tool off, clicks do not place a region")

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
