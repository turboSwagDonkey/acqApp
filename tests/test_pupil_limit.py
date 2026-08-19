"""
The pupil search limit: the disc of the frame the eye is allowed to be in.

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
drag writes back as ONE settings change, and a click outside it does not seed.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_limit.py
"""
from __future__ import annotations

import sys
import time

import numpy as np

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

    # ── 4. the crop is also why it is affordable ─────────────────────────────
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

    # ── 9. one settings change per drag, not three ───────────────────────────
    # `limit` is in PupilTracker._RESEED_ON, so three separate emissions would
    # throw the annulus lock away three times for one movement of the mouse.
    seen: list = []
    panel.settings_changed.connect(lambda s: seen.append(s))
    panel.set_limit(500.0, 250.0, 80.0)
    r.check(len(seen) == 1, f"a drag writes back as one settings change "
                            f"({len(seen)})")
    seen.clear()
    # CONTROL: the same three values typed into the spinboxes really do emit
    # three times, so the check above is not vacuous.
    panel._spn_lx.setValue(501.0)
    panel._spn_ly.setValue(251.0)
    panel._spn_lr.setValue(81.0)
    r.check(len(seen) == 3, f"control: three typed edits are three changes "
                            f"({len(seen)})")

    # ── 10. clearing ─────────────────────────────────────────────────────────
    r.check(panel._btn_limit_clear.isEnabled(), "Clear is live while a limit is set")
    panel._btn_limit_clear.click()
    pump(app, 0.05)
    r.check(panel.settings.search_limit() is None, "…and clearing removes it")
    r.check(npoints(mod._limit_curve) == 0, "…and un-draws the circle")
    r.check(not panel._btn_limit_clear.isEnabled(),
            "control: with no limit there is nothing to clear")

    # ── 11. drag-to-set on the preview ───────────────────────────────────────
    r.check(mod._limit_roi is None, "no draggable circle until it is asked for")
    panel._chk_limit.setChecked(True)
    pump(app, 0.05)
    if not r.check(mod._limit_roi is not None,
                   "ticking 'Set on preview' adds the draggable circle"):
        return r.finish()
    # With nothing set it starts in the middle of the frame, so it is on screen
    # rather than at (0, 0) waiting to be found.
    s0 = panel.settings
    r.check(s0.limit_r > 0 and s0.limit_x > 0 and s0.limit_y > 0,
            f"…seeded at the middle of the frame ({s0.limit_x:.0f}, "
            f"{s0.limit_y:.0f}, r={s0.limit_r:.0f})")

    mod._limit_roi.setSize((200.0, 200.0), finish=False)
    mod._limit_roi.setPos((300.0, 100.0), finish=True)   # emits the real signal
    pump(app, 0.05)
    s1 = panel.settings
    r.check((s1.limit_x, s1.limit_y, s1.limit_r) == (400.0, 200.0, 100.0),
            f"moving it writes centre and radius back to the panel "
            f"({s1.limit_x}, {s1.limit_y}, {s1.limit_r})")

    panel._chk_limit.setChecked(False)
    pump(app, 0.05)
    r.check(mod._limit_roi is None, "unticking takes the handle away again")
    r.check(panel.settings.limit_r == 100.0, "…and leaves the limit it set")

    # ── 12. a click outside the limit must not seed ──────────────────────────
    win._btn_run.setChecked(True)
    pump(app, 0.3)
    panel._chk_search.setChecked(True)
    pump(app, 0.05)
    seeded: list = []
    mod._track.seed = lambda cx, cy, rr: seeded.append((cx, cy, rr))

    class _Ev:
        def __init__(self, pt): self._p = pt
        def scenePos(self): return self._p

    rect = mod._vb.sceneBoundingRect()
    # Put the limit on the view point we are about to click, then well away
    # from it, and check the same click is accepted then refused.
    p = mod._vb.mapSceneToView(rect.center())
    panel.set_limit(p.x(), p.y(), 50.0)
    pump(app, 0.05)
    mod._on_click(_Ev(rect.center()))
    r.check(len(seeded) == 1,
            f"control: a click inside the limit still seeds {seeded}")

    panel.set_limit(p.x() + 4000.0, p.y() + 4000.0, 50.0)
    pump(app, 0.05)
    mod._on_click(_Ev(rect.center()))
    r.check(len(seeded) == 1,
            "a click outside the limit is refused — every fit from it would be")

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
