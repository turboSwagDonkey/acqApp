"""
Where the tracker is allowed to look: the region, and the directions.

Two halves of one idea, and between them they are what makes tracking work on
this rig's framing at all.

The animal is head-fixed, so the eye occupies one fixed part of the frame while
the rest of it — fur, the orbit, the headplate, shadow — is dark in IR and much
larger than the pupil. That is why auto-seeding never worked at this framing:
`coarse_seed` thresholds the whole sensor, finds a dark mask covering more than
half of it, and bails at its own guard. Click-to-seed was the only way in.

This checks the three things the limit has to do, each against a control that
fails without it:

  * `coarse_seed` finds the pupil in a frame where, unrestricted, it finds
    nothing at all (the rig's actual failure) or finds the wrong dark region;
  * the crop it does for speed does not shift the answer — an offset bug here
    would put the seed a few hundred px off with nothing to show for it;
  * a fit centred outside the limit is refused by `PupilTracker`, and changing
    the limit drops the annulus lock;

plus the panel/adapter half: the circle is drawn whenever it is in force, a
placement writes back as ONE settings change, and a click outside it does not
seed.

**The directions** are the second half. Where an eyelid crosses the pupil the
rays find the LID's edge, and those points go into the fit like any other —
measured on the rig clip, that is two thirds of the ray failures and it caps
confidence at 0.26. `find_circular_edge` has always taken `exclude_deg` and its
docstring gives the eyelid example; nothing exposed it. `lid_sectors()` measures
the sectors from a run of fits rather than asking the operator to know that the
lower lid is at 70-155 degrees.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_limit.py
"""
from __future__ import annotations

import sys
import time

import numpy as np
from PyQt6.QtCore import QPointF

from _harness import Report, isolate_user_state, pump, qt_app

BG = 180            # iris/fur background, grey levels
DARK = 20           # pupil and the decoy dark regions
THR = 60            # the shipped seed threshold


def disc(frame, cx, cy, r, value):
    h, w = frame.shape
    yy, xx = np.ogrid[:h, :w]
    frame[(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r] = value


def npoints(item) -> int:
    """How many points a PlotCurveItem is drawing (None before any setData)."""
    xs = item.getData()[0]
    return 0 if xs is None else len(xs)


def rig_frame(w=800, h=600):
    """A frame shaped like the rig's: a small pupil, and a dark surround that
    covers more than half the sensor."""
    f = np.full((h, w), BG, dtype=np.uint8)
    f[:, : int(0.62 * w)] = DARK        # fur / the rest of the head: 62 % dark
    disc(f, 640, 300, 60, BG)           # the lit orbit the eye sits in
    disc(f, 640, 300, 28, DARK)         # the pupil
    return f


def main() -> int:
    r = Report("pupil-limit")

    from acqApp.devices.pupil_cam.tracking import (PupilTracker, coarse_seed,
                                                   detect)
    from acqApp.devices.pupil_cam.settings import PupilSettings

    EYE = (640.0, 300.0, 110.0)         # the limit circle around the eye

    # ── 1. the rig's failure, and the limit fixing it ────────────────────────
    f = rig_frame()
    # CONTROL: this is the state of things today — more than half the frame is
    # below threshold, so coarse_seed gives up before labelling anything.
    r.check(coarse_seed(f, THR, 10, 80) is None,
            "control: unrestricted, the seed gives up on the rig's framing")

    seed = coarse_seed(f, THR, 10, 80, limit=EYE)
    if not r.check(seed is not None, "with a limit it finds the pupil"):
        return r.finish()
    cx, cy, rad = seed
    r.check(abs(cx - 640) <= 6 and abs(cy - 300) <= 6,
            f"…at the pupil, not somewhere the crop offset moved it "
            f"(got {cx:.1f}, {cy:.1f}, want 640, 300)")
    r.check(f[int(round(cy)), int(round(cx))] == DARK,
            "…and the seed lands on a dark pixel of the frame it was given")
    r.check(20.0 <= rad <= 40.0, f"…with about the pupil's radius ({rad:.1f} px)")

    # ── 2. the wrong dark region ─────────────────────────────────────────────
    # A rounder, bigger dark blob outside the limit. Nothing here is degenerate,
    # so the unrestricted seed runs to completion — and picks the decoy.
    f2 = np.full((600, 800), BG, dtype=np.uint8)
    disc(f2, 200, 300, 70, DARK)        # decoy: bigger and just as round
    disc(f2, 640, 300, 28, DARK)        # the pupil
    got = coarse_seed(f2, THR, 10, 80)
    r.check(got is not None and abs(got[0] - 200) < 20,
            f"control: unrestricted, the seed prefers the larger decoy blob "
            f"({None if got is None else round(got[0])})")
    got = coarse_seed(f2, THR, 10, 80, limit=EYE)
    r.check(got is not None and abs(got[0] - 640) < 10,
            f"the limit sends it to the pupil instead "
            f"({None if got is None else round(got[0])})")

    # ── 3. degenerate limits must not crash ──────────────────────────────────
    r.check(coarse_seed(f, THR, 10, 80, limit=(-500.0, -500.0, 50.0)) is None,
            "a limit circle entirely off the frame reads as no pupil")
    edge = coarse_seed(f, THR, 10, 80, limit=(640.0, 300.0, 400.0))
    r.check(edge is not None,
            "a limit whose box runs off the frame is clipped, not refused")
    r.check(coarse_seed(f, THR, 10, 80, limit=(0.0, 0.0, 0.0)) is None,
            "a zero-radius circle contains nothing")

    # ── 4. the crop, when the whole-frame path actually does the work ────────
    # Only when it does: a whole-frame call that bails at the >50 %-dark guard
    # is cheaper than any limited one, because it gives up before labelling
    # anything. Measured on the rig clip, 1.96 ms bailing vs 5.67 ms limited —
    # so this is not "the limit is faster", it is "the limit does not cost the
    # labelling of a sensor-sized frame".
    big = np.full((1208, 1928), BG, dtype=np.uint8)
    disc(big, 1400, 600, 200, DARK)     # a large dark region: real labelling work
    disc(big, 1400, 600, 30, BG)
    disc(big, 1400, 600, 26, DARK)
    t0 = time.perf_counter()
    coarse_seed(big, THR, 10, 80)
    t_full = time.perf_counter() - t0
    t0 = time.perf_counter()
    coarse_seed(big, THR, 10, 80, limit=(1400.0, 600.0, 110.0))
    t_lim = time.perf_counter() - t0
    r.check(t_lim < 0.5 * t_full,
            f"the crop makes the seed cheaper on a sensor-sized frame "
            f"({t_lim * 1e3:.1f} ms vs {t_full * 1e3:.1f} ms)")

    # ── 5. a fit centred outside the limit is refused ────────────────────────
    # Seeded straight at the decoy, so the search really does find a circle
    # there; the only thing that can reject it is the limit.
    kw = dict(threshold=THR, min_r=10, max_r=80)
    hit = detect(f2, **kw, seed=(200.0, 300.0, 60.0))
    r.check(hit.found,
            f"control: seeded at the decoy and unlimited, a pupil is reported "
            f"(r={hit.radius})")
    miss = detect(f2, **kw, seed=(200.0, 300.0, 60.0), limit=EYE)
    r.check(not miss.found, "the same fit outside the limit is not a pupil")
    r.check(miss.center_x is not None,
            "…and its position is still reported, as for a wrong-sized fit")

    # ── 6. PupilTracker: the lock, and what invalidates it ───────────────────
    trk = PupilTracker(threshold=THR, min_r=10, max_r=80, limit=EYE)
    res = trk.process(f)
    r.check(res.found and abs(res.center_x - 640) < 15,
            f"the tracker auto-seeds inside the limit and locks the pupil "
            f"({'lost' if not res.found else round(res.center_x)})")
    r.check(trk.locked, "…so it holds an annulus for the next frame")

    trk.configure(smooth_ema=0.4)
    r.check(trk.locked,
            "control: an unrelated setting keeps the lock (it would re-seed "
            "every frame otherwise)")
    trk.configure(limit=(200.0, 300.0, 110.0))
    r.check(not trk.locked,
            "moving the limit drops the lock — the annulus was placed under "
            "the old one")

    # A tracker limited to the decoy must track the decoy: the limit decides,
    # not which blob is biggest.
    trk2 = PupilTracker(threshold=THR, min_r=10, max_r=80,
                        limit=(200.0, 300.0, 160.0))
    got2 = trk2.process(f2)
    r.check(got2.found and abs(got2.center_x - 200) < 20,
            "a limit around the decoy tracks the decoy, not the pupil")

    # The circle must be drawn GENEROUSLY, and this is why: `coarse_seed` bails
    # when the dark mask covers more than half its search area, and that area is
    # now the circle rather than the sensor. Drawn tight around a 70 px blob a
    # 90 px circle is 60 % dark and auto-seeding stops working — the same
    # failure the limit exists to fix, moved inside it. Named here so the next
    # session recognises it instead of hunting the tracker.
    r.check(coarse_seed(f2, THR, 10, 80, limit=(200.0, 300.0, 90.0)) is None,
            "a limit drawn tight around the pupil trips the >50 %-dark guard")
    r.check(coarse_seed(f2, THR, 10, 80, limit=(200.0, 300.0, 160.0)) is not None,
            "control: the same blob inside a generous circle seeds fine")

    # ── 7. the settings model ────────────────────────────────────────────────
    r.check(PupilSettings().search_limit() is None,
            "shipped default is no limit — the whole frame")
    s = PupilSettings(limit_x=640.0, limit_y=300.0, limit_r=110.0)
    r.check(s.search_limit() == (640.0, 300.0, 110.0),
            "a set limit reaches the tracker as one tuple")
    r.check(PupilSettings().excluded() == (),
            "shipped default ignores no directions")
    # The JSON round trip turns tuples into lists, and a hand-edited config can
    # hold anything: a bad entry must cost a noisier fit, never a dead app.
    r.check(PupilSettings(exclude_deg=[[60, 160], [240, 300]]).excluded()
            == ((60.0, 160.0), (240.0, 300.0)),
            "lists of lists (what JSON gives back) normalise to pairs")
    r.check(PupilSettings(exclude_deg=[[60, 160], "junk", [1, 2, 3]]).excluded()
            == ((60.0, 160.0),),
            "…and a malformed entry is dropped, not raised on")

    # ── 7b. finding the lids: where the rays stop surviving ──────────────────
    # The real defect behind "tracking is awful": where a lid crosses the pupil
    # the rays find the LID's edge, and those points go into the fit like any
    # other. `find_circular_edge` has always taken `exclude_deg` — nothing
    # exposed it.
    from acqApp.devices.pupil_cam.tracking import PupilResult, lid_sectors

    def ring(occluded=(), n=64, frames=40):
        """Fits on a clean ring, with `occluded` sectors yielding no inlier."""
        out = []
        a = np.linspace(0, 2 * np.pi, n, endpoint=False)
        deg = np.degrees(a)
        keep = np.ones(n, dtype=bool)
        for lo, hi in occluded:
            keep &= ~((deg >= lo) & (deg <= hi))
        for _ in range(frames):
            out.append(PupilResult(100.0, 100.0, 30.0, 0.9,
                                   edge_x=100 + 30 * np.cos(a[keep]),
                                   edge_y=100 + 30 * np.sin(a[keep]),
                                   inliers=np.ones(int(keep.sum()), bool)))
        return out

    got = lid_sectors(ring(occluded=[(70, 150)]))
    r.check(len(got) == 1 and got[0][0] <= 70 and got[0][1] >= 150,
            f"an occluded sector is found, with margin ({got})")
    got2 = lid_sectors(ring(occluded=[(70, 150), (250, 290)]))
    r.check(len(got2) == 2, f"…and both lids when both occlude ({got2})")
    # CONTROL: a clean ring must not invent lids. Without this the detector
    # could "work" by always excluding something.
    r.check(lid_sectors(ring()) == (),
            "control: an evenly-tracked ring yields no sectors at all")
    r.check(lid_sectors(ring(occluded=[(70, 150)], frames=5)) == (),
            "too few frames yields () — leave it alone, not exclude nothing")
    r.check(lid_sectors([]) == (), "…and so does no data at all")
    # A sector spanning 0° must come back as one range, not two.
    wrap = lid_sectors(ring(occluded=[(0, 30)]) )
    r.check(len(wrap) == 1, f"a sector touching 0° stays one range ({wrap})")

    # It really changes the search: the excluded rays are not cast.
    from acqApp.devices.pupil_cam.rays import _ray_angles
    r.check(len(_ray_angles(64, ((70, 150),))) < 64,
            "control: the sectors reach _ray_angles and drop rays")

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
    # Deliberately NOT tied to the search overlay: it changes what the tracker
    # accepts, so hiding the annulus must not hide it.
    panel._chk_search.setChecked(False)
    pump(app, 0.05)
    r.check(mod._limit_curve.isVisible() and npoints(mod._limit_curve) > 8,
            "…and it stays drawn with the search overlay off")

    r.check(panel.settings.limit_r == 110.0, "the panel carries the radius")
    md = mod.metadata()
    r.check(md.get("pupil_limit_r") == 110.0 and md.get("pupil_limit_x") == 640.0,
            f"the session metadata records the limit ({md.get('pupil_limit_x')}, "
            f"{md.get('pupil_limit_y')}, {md.get('pupil_limit_r')})")

    # ── 9. one settings change per placement, not three ──────────────────────
    # `limit` is in PupilTracker._RESEED_ON, so three separate emissions would
    # throw the annulus lock away three times for one placement.
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
    pump(app, 1.0)                       # frames flow and the tracker locks

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

    # ── 13. "From fit" — one click, once anything is tracking ────────────────
    if r.check(mod._last_fit is not None,
               f"the adapter remembers the current fit ({mod._last_fit})"):
        fcx, fcy, fr = mod._last_fit
        r.check(mod._btn_limit_fit.isEnabled(), "…so From fit is live")
        mod._btn_limit_fit.click()
        pump(app, 0.05)
        s2 = panel.settings
        r.check(abs(s2.limit_x - fcx) < 1 and abs(s2.limit_y - fcy) < 1,
                f"From fit centres the region on the pupil ({s2.limit_x:.0f}, "
                f"{s2.limit_y:.0f} vs {fcx:.0f}, {fcy:.0f})")
        # Generous by construction — the >50 %-dark guard applies inside the
        # circle, and the pupil must still be able to dilate to max_r.
        r.check(s2.limit_r >= mod._FIT_MARGIN * fr - 0.5
                and s2.limit_r >= 2.5 * s2.max_r - 0.5,
                f"…with margin: r {s2.limit_r:.0f} for a {fr:.0f} px pupil "
                f"(max_r {s2.max_r})")

    # ── 13b. the lid controls on the preview bar ─────────────────────────────
    r.check(mod._btn_lids.isEnabled(), "Find lids is live once something tracks")
    r.check(not mod._btn_lids_off.isEnabled(),
            "control: nothing to reset while no sector is ignored")
    # The mock pupil is a clean disc, so the detector must decline rather than
    # invent lids — the same control as above, but through the button.
    mod._recent.clear()
    for _ in range(40):
        mod._recent.append(ring()[0])
    mod._btn_lids.click()
    pump(app, 0.05)
    r.check(panel.settings.excluded() == (),
            "clicking Find lids on an evenly-tracked eye ignores nothing")

    mod._recent.clear()
    for res_ in ring(occluded=[(70, 150)]):
        mod._recent.append(res_)
    mod._btn_lids.click()
    pump(app, 0.05)
    ex = panel.settings.excluded()
    r.check(len(ex) == 1, f"…and finds the lid when there is one ({ex})")
    r.check(mod._track.tracker.exclude_deg == ex,
            "…which reaches the tracker on its own thread")
    r.check(npoints(mod._excl_curve) > 8,
            f"…and is drawn on the preview, not left invisible "
            f"({npoints(mod._excl_curve)} points)")
    md2 = mod.metadata()
    r.check(md2.get("pupil_exclude_deg") == [ex[0][0], ex[0][1]],
            f"…and recorded in the session metadata "
            f"({md2.get('pupil_exclude_deg')})")
    r.check(mod._btn_lids_off.isEnabled(), "Reset goes live")
    mod._btn_lids_off.click()
    pump(app, 0.05)
    r.check(panel.settings.excluded() == (), "…and clears the sectors")
    r.check(npoints(mod._excl_curve) == 0, "…and un-draws them")

    # ── 14. a click outside the region must not seed ─────────────────────────
    panel._chk_search.setChecked(True)
    pump(app, 0.05)
    seeded: list = []
    mod._track.seed = lambda cx, cy, rr: seeded.append((cx, cy, rr))

    panel.set_limit(c_view.x(), c_view.y(), 50.0)
    pump(app, 0.05)
    mod._on_click(_Ev(centre))
    r.check(len(seeded) == 1,
            f"control: a click inside the region still seeds {seeded}")

    panel.set_limit(c_view.x() + 4000.0, c_view.y() + 4000.0, 50.0)
    pump(app, 0.05)
    mod._on_click(_Ev(centre))
    r.check(len(seeded) == 1,
            "a click outside the region is refused — every fit from it would be")

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
