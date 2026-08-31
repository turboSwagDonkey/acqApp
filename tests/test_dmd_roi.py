"""
Stimulation ROIs: the model, the round trip through the calibration, and the
editor widget.

The check that matters is the round trip — an ROI drawn in camera pixels has to
come back as mirrors that, projected, land on it. Everything else is bookkeeping
around that. The transform here is one we chose, so unlike the rig there is a
right answer to compare against.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd_roi.py
"""
from __future__ import annotations

import sys
import tempfile
import tracemalloc
from pathlib import Path

import numpy as np

from _harness import Report, isolate_user_state, qt_app

from acqApp.devices.dmd.calibration import DmdCalibration, apply_transform
from acqApp.devices.dmd.roi import CircleRoi, RectRoi, RoiSet, roi_from_dict

DW, DH = 256, 192
CW, CH = 320, 240


def calib() -> DmdCalibration:
    """A DMD sitting rotated and offset inside a slightly larger camera FOV."""
    th = np.radians(7.0)
    s = 1.05
    dmd_to_cam = np.array([[s * np.cos(th), -s * np.sin(th), 34.0],
                           [s * np.sin(th),  s * np.cos(th), 22.0],
                           [0.0, 0.0, 1.0]])
    return DmdCalibration(cam_to_dmd=np.linalg.inv(dmd_to_cam),
                          dmd_size=(DW, DH), cam_size=(CW, CH),
                          model="homography", rms_px=0.31, n_points=4096,
                          created="2026-08-18T00:00:00")


def _reach_unbounded(rset, calib, max_side: int = 512) -> float:
    """`reach_fraction` with no bounding — every ROI over the whole grid.

    The algorithm as it stood before 2026-08-25, kept here as the reference the
    bounded one must reproduce bit for bit.
    """
    w, h = calib.cam_size
    step = max(1, int(np.ceil(max(int(w), int(h)) / max(1, max_side))))
    xs = np.arange(0, int(w), step, dtype=np.float64)
    ys = np.arange(0, int(h), step, dtype=np.float64)
    want = np.zeros((ys.size, xs.size), bool)
    for r in rset.rois:
        if r.enabled:
            want |= r.mask_at(xs, ys)
    iy, ix = np.nonzero(want)
    if not iy.size:
        return 1.0
    return float(calib.accessible(np.column_stack((xs[ix], ys[iy]))).mean())


def _set(*rois) -> RoiSet:
    """A RoiSet of the given ROIs — `add()` names them as it goes."""
    st = RoiSet()
    for roi in rois:
        st.add(roi)
    return st


def main() -> int:
    r = Report("dmd-roi")
    c = calib()

    # ── 1. shapes ────────────────────────────────────────────────────────────
    rect = RectRoi(x=100, y=80, w=40, h=20)
    m = rect.mask((CH, CW))
    r.check(m.sum() > 0 and abs(m.sum() - 41 * 21) < 60,
            f"rect mask covers about w*h px ({m.sum()} vs {41*21})")
    ys, xs = np.nonzero(m)
    r.check(abs(xs.mean() - 100) < 0.6 and abs(ys.mean() - 80) < 0.6,
            f"…centred on (x, y) ({xs.mean():.1f}, {ys.mean():.1f})")

    turned = RectRoi(x=100, y=80, w=40, h=20, angle_deg=90).mask((CH, CW))
    r.check(abs(turned.sum() - m.sum()) < 60,
            "a rotated rect keeps its area")
    # CONTROL: rotating by 90° must actually change which pixels are covered,
    # or `angle_deg` is being ignored.
    r.check((turned != m).sum() > 0.5 * m.sum(),
            "control: rotating by 90 deg really moves the covered pixels")

    circ = CircleRoi(x=160, y=120, r=25)
    cm = circ.mask((CH, CW))
    r.check(abs(cm.sum() - np.pi * 25 ** 2) / (np.pi * 25 ** 2) < 0.03,
            f"circle mask is pi*r^2 within 3% ({cm.sum()})")

    # ── 2. the set ───────────────────────────────────────────────────────────
    s = RoiSet()
    s.add(RectRoi(x=100, y=80, w=40, h=20))
    s.add(CircleRoi(x=160, y=120, r=25))
    s.add(RectRoi(x=100, y=80, w=40, h=20))
    r.check([x.name for x in s] == ["rect1", "circle1", "rect2"],
            f"auto-named without collisions ({[x.name for x in s]})")
    r.check(s.mask((CH, CW)).sum() == (m | cm).sum(),
            "the set's mask is the union of its ROIs")

    s[1].enabled = False
    r.check(s.mask((CH, CW)).sum() == m.sum(),
            "disabling one drops it from the union")
    s[1].enabled = True

    # round-trip through dicts
    again = RoiSet.from_list(s.to_list())
    r.check(len(again) == 3
            and np.array_equal(again.mask((CH, CW)), s.mask((CH, CW))),
            "a set survives to_list/from_list unchanged")
    r.check(isinstance(roi_from_dict({"kind": "circle", "x": 1, "y": 2, "r": 3}),
                       CircleRoi),
            "roi_from_dict rebuilds the right class")

    # ── 3. the accessible area ───────────────────────────────────────────────
    reach = c.accessible_mask((CH, CW))
    exp = DW * DH / np.linalg.det(c.dmd_to_cam[:2, :2])
    r.check(0.2 < reach.mean() < 0.95,
            f"the DMD reaches part but not all of the camera "
            f"({100 * reach.mean():.0f}%)")
    # The editor rebuilds all of this on EVERY drag (_refresh_status →
    # clipped_mask), so it has to be cheap at camera scale, not just correct at
    # test scale. Whole-grid versions cost ~800 ms and ~1 GB per drag at ORCA
    # full frame. Budget: a coordinate grid the old code materialised outright.
    BH, BW = 1200, 1600
    grid = 2 * BH * BW * 8                      # what np.mgrid alone would cost

    def mask_peak(rows: int) -> int:
        """Peak allocation for one accessible_mask, on a fresh calibration so
        the cache cannot hide the work."""
        cal = DmdCalibration(cam_to_dmd=c.cam_to_dmd, dmd_size=(DW, DH),
                             cam_size=(BW, rows), model="homography")
        tracemalloc.start()
        cal.accessible_mask((rows, BW))
        _cur, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return pk

    # The property that matters is not an absolute figure but that the working
    # set is BANDED: doubling the height must add only the extra output rows,
    # not double the temporaries. A whole-grid version adds 2 int64 grids.
    grew = mask_peak(2 * BH) - mask_peak(BH)
    r.check(grew < 4 * BH * BW,
            f"accessible_mask is banded: doubling the height added "
            f"{grew / 2**20:.1f} MB, against {BH * BW / 2**20:.1f} MB of extra "
            f"output (a coordinate grid would add {grid / 2**20:.1f})")

    big = DmdCalibration(cam_to_dmd=c.cam_to_dmd, dmd_size=(DW, DH),
                         cam_size=(BW, BH), model="homography")
    m1 = big.accessible_mask((BH, BW))
    r.check(big.accessible_mask((BH, BW)) is m1 and not m1.flags.writeable,
            "…and is cached per shape, handed out read-only")

    tracemalloc.start()
    RectRoi(x=800, y=600, w=200, h=150).mask((BH, BW))
    _cur, peak_roi = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    r.check(peak_roi < 0.5 * grid,
            f"an ROI mask broadcasts rather than gridding: peak "
            f"{peak_roi / 2**20:.1f} MB")
    # CONTROL: the budget is not vacuous — a grid really does exceed it.
    tracemalloc.start()
    _yy, _xx = np.mgrid[:BH, :BW]
    _cur, peak_grid = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del _yy, _xx
    r.check(peak_grid >= 0.5 * grid,
            f"control: np.mgrid alone costs {peak_grid / 2**20:.1f} MB, over "
            f"the budget both checks above pass")

    corners = c.accessible_corners()
    # Nudged 2 px toward the centroid: the exact corner is the boundary, and
    # whether it rounds in or out is not what this is checking.
    inset = corners + 2.0 * (corners.mean(axis=0) - corners) / np.linalg.norm(
        corners.mean(axis=0) - corners, axis=1, keepdims=True)
    r.check(bool(c.accessible(inset).all()),
            f"the field's own corners are inside the accessible mask "
            f"({c.accessible(inset)})")
    # CONTROL: step outside them and they must stop being accessible.
    outset = corners - 6.0 * (corners.mean(axis=0) - corners) / np.linalg.norm(
        corners.mean(axis=0) - corners, axis=1, keepdims=True)
    r.check(not c.accessible(outset).any(),
            f"control: just outside the field is not accessible "
            f"({c.accessible(outset)})")

    far = RoiSet()
    far.add(CircleRoi(x=CW - 5, y=CH - 5, r=20))     # deliberately off the field
    r.check(far.outside(c) == ["circle1"],
            f"an ROI off the DMD field is reported ({far.outside(c)})")
    _, kept = far.clipped_mask(c)
    r.check(kept < 0.9, f"…and its unreachable part is not counted ({kept:.2f})")

    # outside() is geometric, which is not just cheaper than rasterising — it is
    # safer. An ROI hanging off the IMAGE edge has no pixels there to raster, so
    # the old mask-based test judged only the visible part and called it fine.
    off = RoiSet()
    off.add(CircleRoi(x=float(corners[:, 0].mean()), y=-6.0, r=30))
    r.check(off.outside(c) == ["circle1"],
            f"an ROI hanging off the image edge is reported, not judged on the "
            f"part that happens to be visible ({off.outside(c)})")
    # CONTROL: the same circle moved fully into the field must not be flagged,
    # so this is about position and not about circles.
    inn = RoiSet()
    inn.add(CircleRoi(x=float(corners[:, 0].mean()),
                      y=float(corners[:, 1].mean()), r=30))
    r.check(inn.outside(c) == [],
            f"control: the same circle inside the field is not flagged "
            f"({inn.outside(c)})")

    # The status line's estimate has to track the exact figure it stands in for.
    for st in (far, off, inn):
        _, exact = st.clipped_mask(c)
        r.check(abs(st.reach_fraction(c) - exact) < 0.05,
                f"reach_fraction tracks clipped_mask "
                f"({st.reach_fraction(c):.3f} vs {exact:.3f})")
    # reach_fraction bounds its grid twice — each ROI to its own bbox, the scan
    # to their union (2026-08-25, ~8x). A bbox one cell tight under-counts and
    # the answer stays plausible, so compare against the UNBOUNDED algorithm and
    # demand they agree exactly. Checking against `clipped_mask` within a
    # tolerance does not work: at this camera size one cell is well under 5 %,
    # and a deliberately broken bbox passed all three ways.
    for name, st in (
            ("two overlapping circles", _set(CircleRoi(x=40.0, y=60.0, r=34),
                                             CircleRoi(x=58.0, y=60.0, r=34))),
            ("two disjoint, far apart", _set(CircleRoi(x=38.0, y=40.0, r=26),
                                             CircleRoi(x=CW * 0.7, y=CH * 0.6,
                                                       r=26))),
            ("rotated rect", _set(RectRoi(x=44.0, y=90.0, w=90, h=30,
                                          angle_deg=37.0))),
            ("straddling the image edge", _set(RectRoi(x=4.0, y=CH * 0.5,
                                                       w=60, h=40))),
            ("one disabled of two", _set(CircleRoi(x=42.0, y=150.0, r=30),
                                         CircleRoi(x=CW * 0.6, y=CH * 0.5, r=30,
                                                   enabled=False))),
    ):
        ref = _reach_unbounded(st, c)
        got = st.reach_fraction(c)
        # Partial, or the two agree only because both saturated.
        r.check(0.02 < ref < 0.98 and got == ref,
                f"reach_fraction bounds correctly — {name} "
                f"({got!r} vs unbounded {ref!r})")

    # CONTROL: the bounding must not be free to return anything. A set whose
    # ROIs are wholly off the DMD field has to come back near 0, and one in the
    # middle near 1 — if both read the same, the checks above prove nothing.
    lo = _set(CircleRoi(x=CW - 5, y=CH - 5, r=20)).reach_fraction(c)
    hi = _set(CircleRoi(x=float(corners[:, 0].mean()),
                        y=float(corners[:, 1].mean()), r=20)).reach_fraction(c)
    r.check(lo < 0.5 < hi,
            f"control: reach_fraction separates an off-field set from an "
            f"on-field one ({lo:.3f} vs {hi:.3f})")

    # CONTROL: an ROI well inside must NOT be flagged, or the check is vacuous.
    near = RoiSet()
    near.add(CircleRoi(x=float(corners[:, 0].mean()),
                       y=float(corners[:, 1].mean()), r=15))
    r.check(near.outside(c) == [],
            f"control: an ROI in the middle of the field is not flagged "
            f"({near.outside(c)})")

    # ── 4. the round trip that matters ───────────────────────────────────────
    want = near.mask((CH, CW))
    frame = near.dmd_frame(c)
    r.check(frame.shape == (DH, DW) and set(np.unique(frame)) <= {0, 255},
            f"the ROI becomes a device-sized binary frame {frame.shape}")

    # Project it back through the same optics and see where it lands.
    yy, xx = np.mgrid[:CH, :CW]
    d = apply_transform(c.cam_to_dmd, np.column_stack((xx.ravel(), yy.ravel())))
    dxi = np.rint(d[:, 0]).astype(np.int64)
    dyi = np.rint(d[:, 1]).astype(np.int64)
    ok = (dxi >= 0) & (dxi < DW) & (dyi >= 0) & (dyi < DH)
    lit = np.zeros(CW * CH, bool)
    lit[ok] = frame[dyi[ok], dxi[ok]] > 127
    lit = lit.reshape(CH, CW)

    hit = (lit & want).sum() / max(1, want.sum())
    spill = (lit & ~want).sum() / max(1, lit.sum())
    r.check(hit > 0.9 and spill < 0.12,
            f"projected, the mask lands on the ROI ({100*hit:.0f}% covered, "
            f"{100*spill:.0f}% spill)")

    # CONTROL: aim through a wrong transform and it must miss.
    bad_cal = DmdCalibration(cam_to_dmd=c.cam_to_dmd.copy(),
                             dmd_size=c.dmd_size, cam_size=c.cam_size)
    off = c.dmd_to_cam.copy()
    off[0, 2] += 40.0
    bad_cal.cam_to_dmd = np.linalg.inv(off)
    frame_bad = near.dmd_frame(bad_cal)
    lit_bad = np.zeros(CW * CH, bool)
    lit_bad[ok] = frame_bad[dyi[ok], dxi[ok]] > 127
    hit_bad = (lit_bad.reshape(CH, CW) & want).sum() / max(1, want.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px registration error misses ({100*hit_bad:.0f}%)")

    # ── 5. persistence of the calibration itself ─────────────────────────────
    tmp = Path(tempfile.mkdtemp(prefix="dmd_calib_")) / "cal.json"
    c.save(tmp)
    back = DmdCalibration.load(tmp)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.dmd_size == c.dmd_size and back.cam_size == c.cam_size,
            "the calibration survives save/load")
    r.check(back.rms_px == c.rms_px and back.n_points == c.n_points,
            "…including the provenance that says whether to trust it")

    # ── 6. the editor ────────────────────────────────────────────────────────
    isolate_user_state()
    app = qt_app()                      # assign it: an unreferenced one is GC'd
    from acqApp.devices.dmd.roi_panel import RoiEditor

    ed = RoiEditor(c)
    ed.set_image(np.random.default_rng(0).integers(0, 255, (CH, CW), dtype=np.uint8))
    r.check(len(ed.roi_set) == 0, "the editor starts empty")

    # ── contrast: an ORCA frame is 16-bit with hot pixels ───────────────────
    # autoLevels stretches to min/max, so two hot pixels collapse the whole
    # image to black — the editor looked far worse than the live preview it is
    # opened from, which has always used percentiles.
    rng16 = np.random.default_rng(1)
    frame16 = rng16.normal(1400, 60, (CH, CW)).astype(np.uint16)
    frame16[3, 4] = 65000                       # every sCMOS has a few
    ed.set_image(frame16)
    lo, hi = ed._img.getLevels()
    s1, s99 = np.percentile(frame16[::4, ::4], (1, 99))
    r.check(abs(lo - s1) < 1 and abs(hi - s99) < 1,
            f"levels come from the 1st/99th percentile ({lo:.0f}-{hi:.0f}), "
            f"not min/max")
    # Taken from a strided view, because percentile SORTS: 87 ms at ORCA full
    # frame for a contrast estimate. Legitimate only if it agrees with the
    # whole-frame answer, so that is asserted rather than assumed.
    f1, f99 = np.percentile(frame16, (1, 99))
    span = float(f99 - f1)
    r.check(abs(lo - f1) < 0.02 * span and abs(hi - f99) < 0.02 * span,
            f"…and 1/16 of the pixels give the same answer as all of them "
            f"({lo:.0f}-{hi:.0f} vs {f1:.0f}-{f99:.0f}, span {span:.0f})")
    r.check(hi < 0.1 * frame16.max(),
            f"control: a hot pixel at {frame16.max()} would have stretched the "
            f"range to it; the shown top is {hi:.0f}")
    # A flat frame must not produce an inverted or zero-width range.
    ed.set_image(np.full((CH, CW), 700, np.uint16))
    flo, fhi = ed._img.getLevels()
    r.check(fhi >= flo, f"a flat frame still gives a usable range ({flo}-{fhi})")
    ed.set_image(frame16)

    # ── drawing: a drag places an ROI where you put it ──────────────────────
    before = len(ed.roi_set)
    ed._on_drawn((100.0, 80.0), (160.0, 130.0))
    if r.check(len(ed.roi_set) == before + 1, "a drag on the image adds an ROI"):
        roi = list(ed.roi_set)[-1]
        r.check(abs(roi.x - 130) < 1 and abs(roi.y - 105) < 1,
                f"…centred on the drag, not on the middle of the field "
                f"({roi.x:.0f}, {roi.y:.0f})")
        r.check(abs(roi.w - 60) < 1 and abs(roi.h - 50) < 1,
                f"…and sized by it ({roi.w:.0f}x{roi.h:.0f})")
    # The REAL drag path, not just the handler: mouseDragEvent maps the event's
    # local coordinates into image space, and nothing exercised that mapping.
    from PyQt6.QtCore import QPointF, Qt as _Qt
    ed._on_clear()
    ed._btn_draw.setChecked(True)
    vb = ed._vb

    class _Ev:
        """Duck-types exactly what _DrawViewBox.mouseDragEvent calls."""

        def __init__(self, down, now, finish=True):
            self._d, self._p, self._f = QPointF(*down), QPointF(*now), finish
            self.accepted = False

        def button(self): return _Qt.MouseButton.LeftButton
        def buttonDownPos(self, *_a): return self._d
        def pos(self): return self._p
        def isFinish(self): return self._f
        def accept(self): self.accepted = True

    want_a, want_b = (200.0, 150.0), (400.0, 330.0)
    la, lb = vb.mapFromView(QPointF(*want_a)), vb.mapFromView(QPointF(*want_b))
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    if r.check(len(ed.roi_set) == 1, "a real drag event creates one ROI"):
        got = list(ed.roi_set)[0]
        r.check(abs(got.x - 300) < 0.5 and abs(got.y - 240) < 0.5
                and abs(got.w - 200) < 0.5 and abs(got.h - 180) < 0.5,
                f"…exactly where it was dragged: centre ({got.x:.1f}, "
                f"{got.y:.1f}) {got.w:.0f}x{got.h:.0f}, want (300, 240) 200x180")

    # The rubber band tracks the drag and clears on release, so the mode is
    # visible before anything is committed.
    ed._on_clear()
    mid = _Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False)
    vb.mouseDragEvent(mid)
    r.check(vb._rect.isVisible() and len(ed.roi_set) == 0,
            "mid-drag shows the band and commits nothing")
    band = vb._rect.rect()
    r.check(abs(band.width() - 200) < 0.5 and abs(band.height() - 180) < 0.5,
            f"…the size being dragged ({band.width():.0f}x{band.height():.0f})")
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    r.check(not vb._rect.isVisible(), "…and it clears on release")

    # A circle's band must be the circle that gets made, or the preview lies.
    ed._on_clear()
    ed._cmb.setCurrentText("circle")
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False))
    er = vb._ellipse.rect()
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    made = list(ed.roi_set)[0]
    r.check(abs(er.width() / 2 - made.r) < 0.5,
            f"the circle band previews the radius it creates "
            f"({er.width() / 2:.1f} vs {made.r:.1f})")
    ed._cmb.setCurrentText("rectangle")
    ed._btn_draw.setChecked(False)
    r.check(not vb._rect.isVisible() and not vb._ellipse.isVisible(),
            "disarming Draw clears any band left on screen")
    ed._on_clear()

    # CONTROL: a stray click must not litter the set with zero-size ROIs.
    n = len(ed.roi_set)
    ed._on_drawn((200.0, 200.0), (200.5, 200.5))
    r.check(len(ed.roi_set) == n,
            "control: a click (not a drag) adds nothing")
    ed._on_clear()

    seen: list = []
    ed.rois_changed.connect(seen.append)
    ed._cmb.setCurrentText("rectangle")
    ed._on_add()
    ed._cmb.setCurrentText("circle")
    ed._on_add()
    r.check(len(ed.roi_set) == 2 and len(seen) == 2,
            f"adding one of each emits and lands in the set "
            f"({len(ed.roi_set)} rois, {len(seen)} signals)")
    kinds = [x.kind for x in ed.roi_set]
    r.check(kinds == ["rect", "circle"], f"both shapes are creatable ({kinds})")

    # A new ROI must be placed where it can actually be projected.
    r.check(ed.roi_set.outside(c) == [],
            f"a freshly added ROI is inside the DMD field "
            f"({ed.roi_set.outside(c)})")

    # Dragging the pyqtgraph item must write back into the model.
    before = (ed.roi_set[0].x, ed.roi_set[0].y)
    ed._items[0].setPos([ed._items[0].pos()[0] + 12,
                         ed._items[0].pos()[1] + 7])
    ed._on_item_changed()
    after = (ed.roi_set[0].x, ed.roi_set[0].y)
    r.check(abs(after[0] - before[0] - 12) < 0.6
            and abs(after[1] - before[1] - 7) < 0.6,
            f"moving the handle moves the model {before} -> {after}")

    ed._list.setCurrentRow(0)
    ed._on_delete()
    r.check(len(ed.roi_set) == 1, "delete removes the selected ROI")
    ed._on_clear()
    r.check(len(ed.roi_set) == 0, "clear empties the set")

    # Without a calibration the editor must say so rather than pretend.
    ed2 = RoiEditor(None)
    r.check("calibration" in ed2._status.text().lower(),
            f"with no calibration the editor says so ({ed2._status.text()!r})")
    ed2._on_add()
    r.check(len(ed2.roi_set) == 1,
            "…but still allows drawing, so ROIs can be prepared beforehand")

    # ── offset: a cropped preset's frame origin isn't the sensor's ─────────
    # devices/voltage_cam/presets.py crops vertically (hpos always 0, vpos
    # variable), and the calibration is always fit full-frame, so an ROI
    # drawn on a cropped preset's frame must land (hpos, vpos) away in the
    # MODEL (absolute sensor) coordinates the calibration expects — not
    # where it was clicked on screen.
    ox, oy = 37.0, 82.0
    ed3 = RoiEditor(c, offset=(ox, oy))
    ed3.set_image(np.zeros((CH, CW), np.uint8))
    ed3._on_drawn((100.0, 80.0), (160.0, 130.0))
    roi3 = list(ed3.roi_set)[-1]
    r.check(abs(roi3.x - (130 + ox)) < 1 and abs(roi3.y - (105 + oy)) < 1,
            f"a drag on a cropped preset's frame is stored in absolute "
            f"sensor coordinates, shifted by the preset offset "
            f"({roi3.x:.0f}, {roi3.y:.0f})")

    # The on-screen item must stay where it was clicked (display-local),
    # even though the model just stored an absolute position.
    it3 = ed3._items[-1]
    r.check(abs(it3.pos()[0] - 100.0) < 1 and abs(it3.pos()[1] - 80.0) < 1,
            f"…but the on-screen item stays at the display-local drag "
            f"position ({it3.pos()[0]:.0f}, {it3.pos()[1]:.0f})")

    # Moving the on-screen (display-local) item must still write back an
    # absolute-coordinate model position. `roi3` and the set's entry are the
    # SAME object, so its pre-move coordinates are captured as plain floats
    # first — reading them back off `roi3` after the move would just compare
    # the mutated value to itself.
    x3, y3 = roi3.x, roi3.y
    before3 = it3.pos()
    it3.setPos([before3[0] + 15, before3[1] - 9])
    ed3._on_item_changed()
    moved3 = list(ed3.roi_set)[-1]
    r.check(abs(moved3.x - x3 - 15) < 0.6 and abs(moved3.y - y3 + 9) < 0.6,
            "moving the on-screen item still writes an absolute-coordinate "
            "model position")

    # The reachable-field outline is absolute (calibration) space too, so it
    # must shift into display-local coordinates the same way ROI items do.
    corners = c.accessible_corners()
    xdata, ydata = ed3._field.getData()
    r.check(np.allclose(xdata[:-1], corners[:, 0] - ox)
            and np.allclose(ydata[:-1], corners[:, 1] - oy),
            "the reachable-field outline is drawn display-local too")

    # CONTROL: zero offset (the default, and the common full-frame case)
    # reproduces the pre-existing no-offset behaviour exactly.
    ed4 = RoiEditor(c)
    ed4._on_drawn((100.0, 80.0), (160.0, 130.0))
    roi4 = list(ed4.roi_set)[-1]
    r.check(abs(roi4.x - 130) < 1 and abs(roi4.y - 105) < 1,
            "control: zero offset behaves exactly as before")

    # ── 7. saved ROI sets: session/archive storage, the picker, routines ────
    from acqApp.devices.dmd import roi_store
    from acqApp.routines.settings import pattern_label

    saved = _set(RectRoi(x=10, y=10, w=4, h=4), CircleRoi(x=50, y=50, r=6))
    p1 = roi_store.save("column A", saved)
    r.check(p1.name.endswith(".roi.json") and p1.parent == roi_store.SESSION_DIR,
            f"save() writes into the session folder ({p1})")
    back = roi_store.load(p1)
    r.check(len(back) == 2 and np.array_equal(back.mask((CH, CW)),
                                              saved.mask((CH, CW))),
            "a saved set survives roi_store save/load unchanged")

    p2 = roi_store.save("column A", saved)      # same name, twice
    r.check(p2 != p1 and p2.exists(),
            f"saving under a taken name does not clobber the first ({p1.name}, "
            f"{p2.name})")

    listed = roi_store.list_session()
    r.check({s.path for s in listed} == {p1, p2},
            f"list_session sees both ({[s.path.name for s in listed]})")
    r.check(roi_store.is_roi_file(p1) and not roi_store.is_roi_file("frame.png"),
            "is_roi_file distinguishes a saved set from a raw pattern file")

    # A second "run" (module re-touched with _rotated reset) must move the
    # first run's session sets into archive, not delete or duplicate them.
    roi_store._rotated = False
    moved = roi_store.list_archive()
    r.check({s.path.name for s in moved} == {p1.name, p2.name},
            f"rotation moves the previous run's sets into archive "
            f"({[s.path.name for s in moved]})")
    r.check(roi_store.list_session() == [],
            "…and leaves the session folder empty for the new run")

    # Display: a routine step's pattern reads as "ROI: <name>", not the
    # raw <name>.roi.json filename a plain image would show.
    r.check(pattern_label(str(p1)) == "ROI: column A",
            f"pattern_label names a saved ROI set ({pattern_label(str(p1))!r})")
    r.check(pattern_label("frame.png") == "frame.png",
            "control: a raw pattern file's label is untouched")

    # ── the panel adopts a routine-chosen ROI set the way it adopts a file ──
    from acqApp.devices.dmd.panel import SettingsPanel

    panel = SettingsPanel()
    emitted: list = []
    panel.settings_changed.connect(emitted.append)
    panel.set_roi_pattern("column A", saved.to_list())
    r.check(panel.mode == "roi" and len(panel.settings.rois) == 2,
            f"set_roi_pattern switches to ROI mode with the loaded set "
            f"({panel.mode}, {len(panel.settings.rois)} ROI(s))")
    r.check(len(emitted) >= 1, "…and emits settings_changed once switched")
    r.check('"column A"' in panel._lbl_rois.text(),
            f"the loaded set's name is shown ({panel._lbl_rois.text()!r})")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
