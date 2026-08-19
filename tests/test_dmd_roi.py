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

from acqApp.devices.dmd.calibration import (DmdCalibration, apply_transform,
                                            mask_from_roi)
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
    bad = c.dmd_to_cam.copy()
    bad[0, 2] += 40.0
    frame_bad = mask_from_roi(want, bad, DW, DH)
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

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
