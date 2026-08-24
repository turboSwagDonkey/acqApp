"""Registering the DMD to the ORCA, against a transform we chose.

The point of testing this offline is that the real thing has no ground truth:
once light is on a sample, a wrong registration and a right one both produce a
picture. Here the transform is known, so the pipeline can be held to it —
project, image, fit, and the fit must come back as the transform we started
from.

The synthetic camera is the honest part. It renders what the ORCA *would* see,
carrying the two things that actually broke this at the rig: a DMD field larger
than the camera frame, so patterns run off the edge, and heavy vignetting, so a
region's brightness is not uniform.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd_calibration.py
"""
from __future__ import annotations

import sys

import numpy as np

from _harness import Report

from acqApp.devices.dmd.calibration import (ON, STRIPE_OFFSETS,
                                            CalibrationError, DmdCalibration,
                                            apply_transform, calibrate,
                                            deshear, fit_axes, mask_from_roi,
                                            offset_stripe, stripe_sweep)

DW, DH = 256, 192          # a small DMD
CW, CH = 320, 240          # the "ORCA"


def true_transform() -> np.ndarray:
    """DMD → camera: scale, a 7° rotation and an offset."""
    th = np.radians(7.0)
    s = 1.05
    return np.array([[s * np.cos(th), -s * np.sin(th), 34.0],
                     [s * np.sin(th),  s * np.cos(th), 22.0],
                     [0.0, 0.0, 1.0]])


def footprint(pattern: np.ndarray, M: np.ndarray) -> np.ndarray:
    """Which camera pixels `pattern` lights, as pure geometry — no sensor."""
    yy, xx = np.mgrid[:CH, :CW]
    d = apply_transform(np.linalg.inv(M),
                        np.column_stack((xx.ravel(), yy.ravel())))
    dx = np.rint(d[:, 0]).astype(np.int64)
    dy = np.rint(d[:, 1]).astype(np.int64)
    ok = (dx >= 0) & (dx < DW) & (dy >= 0) & (dy < DH)
    lit = np.zeros(CW * CH, bool)
    lit[ok] = pattern[dy[ok], dx[ok]] > 127
    return lit.reshape(CH, CW)


def make_camera(M, rng, *, vignette=True):
    """A camera that images `pattern` through `M`, vignetted and noisy."""
    yy, xx = np.mgrid[:CH, :CW].astype(np.float64)
    v = (0.25 + 0.75 * np.exp(-(((xx - CW * 0.35) ** 2 + (yy - CH * 0.45) ** 2)
                                / (2 * (0.45 * CW) ** 2)))
         if vignette else np.ones((CH, CW)))
    base = 300.0 * v

    def image(pattern):
        img = base * np.where(footprint(pattern, M), 3.0, 1.0)
        return img + rng.normal(0, 4.0, img.shape)
    return image


def run(image, dmd_size=(DW, DH), **kw):
    """Drive `calibrate` against a synthetic camera."""
    held = {"f": np.zeros((DH, DW), np.uint8)}
    return calibrate(lambda f: held.__setitem__("f", f),
                     lambda: image(held["f"]), dmd_size,
                     log=lambda _s: None, **kw)


def main() -> int:
    r = Report("dmd-calib")
    rng = np.random.default_rng(7)
    M = true_transform()

    # ── 1. the stripe ────────────────────────────────────────────────────────
    s = offset_stripe(DW, DH, 0, 40.0)
    r.check(s.shape == (DH, DW) and s.dtype == np.uint8
            and set(np.unique(s)) <= {0, 255},
            f"a stripe is a device-sized binary frame {s.shape}")
    box = np.nonzero(s.any(axis=0))[0]
    r.check(abs((box.mean()) - ((DW - 1) / 2 + 40.0)) < 1.0,
            f"…centred on its offset (at x={box.mean():.1f}, "
            f"want {(DW - 1) / 2 + 40.0:.1f})")
    r.check(len(box) <= 0.08 * DW,
            f"…and narrow: {len(box)} of {DW} columns. Fine patterns do not "
            f"survive this relay, so nothing here may project one")

    # ── 2. the fit recovers the transform ────────────────────────────────────
    c = run(make_camera(M, rng))
    r.check(c.rms_px < 2.0,
            f"the stripe centroids fall on a straight line "
            f"(rms {c.rms_px:.2f} px over {c.n_points} stripes)")
    pts = np.array([[DW / 2, DH / 2], [DW / 4, DH / 4],
                    [3 * DW / 4, 2 * DH / 3]], float)
    err = float(np.abs(apply_transform(np.linalg.inv(c.cam_to_dmd), pts)
                       - apply_transform(M, pts)).max())
    r.check(err < 4.0,
            f"…and it agrees with the transform we projected through "
            f"(max {err:.2f} px)")
    r.check(c.dmd_size == (DW, DH) and c.cam_size == (CW, CH),
            f"…recording both sizes it was measured at {c.dmd_size} -> "
            f"{c.cam_size}")

    # A mirrored relay must come back mirrored. The offsets are signed for
    # exactly this: a symmetric pattern could not tell the two apart, and a
    # mirrored registration aims every ROI wrongly while looking well fitted.
    flip = np.array([[-1.05, 0.10, 300.0], [0.08, 1.02, 22.0], [0.0, 0.0, 1.0]])
    cf = run(make_camera(flip, rng))
    errf = float(np.abs(apply_transform(np.linalg.inv(cf.cam_to_dmd), pts)
                        - apply_transform(flip, pts)).max())
    r.check(errf < 4.0, f"a mirrored relay is recovered mirrored ({errf:.2f} px)")
    d = (apply_transform(flip, np.array([[DW - 1, DH / 2]], float))
         - apply_transform(flip, np.array([[0, DH / 2]], float)))[0]
    r.check(d[0] < 0,
            f"control: that transform really does run DMD +x towards camera -x "
            f"({d[0]:+.0f} px), so a sign error would have been caught")

    # CONTROL: vignetting must not move the answer. It ate the previous method.
    c_flat = run(make_camera(M, rng, vignette=False))
    moved = float(np.abs(c.cam_to_dmd - c_flat.cam_to_dmd).max())
    r.check(moved < 0.05,
            f"control: vignetting barely moves the fit ({moved:.4f} in the "
            f"matrix) — a stripe's centroid is local")

    # ── 3. where the camera's view lands on the panel ────────────────────────
    x0, y0, x1, y1 = c.visible_mirrors()
    want = apply_transform(np.linalg.inv(M),
                           np.array([[0, 0], [CW - 1, 0], [CW - 1, CH - 1],
                                     [0, CH - 1]], float))
    r.check(abs(x0 - max(0, want[:, 0].min())) < 6
            and abs(x1 - min(DW, want[:, 0].max())) < 6,
            f"the camera sees mirrors x {x0}..{x1}, against a true "
            f"{max(0, want[:, 0].min()):.0f}..{min(DW, want[:, 0].max()):.0f}")
    r.check(0 <= x0 < x1 <= DW and 0 <= y0 < y1 <= DH,
            f"…clipped to the panel ({x0}, {y0}, {x1}, {y1}), so it is the "
            f"usable region and not an extrapolation")

    # ── 4. ROI → mask, the thing the transform is for ────────────────────────
    roi = np.zeros((CH, CW), bool)
    roi[110:150, 150:210] = True
    frame = mask_from_roi(roi, np.linalg.inv(c.cam_to_dmd), DW, DH)
    r.check(frame.shape == (DH, DW) and set(np.unique(frame)) <= {0, 255},
            "the ROI becomes a device-sized binary frame")
    r.check(frame.max() == ON, "…emitted at full-on, not scaled")
    lit = footprint(frame, M)
    hit = (lit & roi).sum() / max(1, roi.sum())
    spill = (lit & ~roi).sum() / max(1, lit.sum())
    r.check(hit > 0.85 and spill < 0.15,
            f"projected, the mask lands on the ROI ({100 * hit:.0f}% covered, "
            f"{100 * spill:.0f}% spill)")
    # CONTROL: aim with the wrong transform and it must miss, or the check
    # above would pass on any mask at all.
    bad = c.cam_to_dmd.copy()
    bad[0, 2] += 40.0
    hit_bad = (footprint(mask_from_roi(roi, np.linalg.inv(bad), DW, DH), M)
               & roi).sum() / max(1, roi.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px error in the transform misses "
            f"({100 * hit_bad:.0f}% covered)")

    # ── 5. refusing to guess ─────────────────────────────────────────────────
    try:
        run(lambda _p: np.full((CH, CW), 50.0))
        r.check(False, "a dark rig raises rather than fitting noise")
    except CalibrationError as e:
        r.check("usable stripe" in str(e),
                f"a dark rig raises, naming the axis and the count "
                f"({str(e)[:46]}…)")
    r.check(fit_axes({0: [(0.0, 1.0, 2.0)], 1: [(0.0, 1.0, 2.0)]}) is None,
            "one stripe per axis is refused — a fit with no residual cannot be "
            "judged")

    # Shear is DISCARDED by default, and the discarded amount is recorded.
    r.check(c.model == "affine-noshear" and "shear" in c.notes,
            f"shear is off by default and the measured value is kept in the "
            f"notes ({c.model})")
    A = c.dmd_to_cam[:2, :2]
    gap = abs(np.degrees(np.arctan2(A[1, 1], A[0, 1]))
              - np.degrees(np.arctan2(A[1, 0], A[0, 0])))
    gap = min(gap, 360 - gap)
    r.check(abs(gap - 90.0) < 0.01,
            f"…so the two axes come out exactly perpendicular ({gap:.3f}deg)")
    withshear = run(make_camera(M, rng), allow_shear=True)
    r.check(withshear.model == "affine",
            "…and allow_shear=True keeps it, for a relay where it is real")
    # deshear must preserve both scales and the handedness, not just square up.
    vx = np.array([3.0, 1.0])
    vy = np.array([-0.6, 2.0])
    ox, oy = deshear(vx, vy)
    r.check(abs(np.hypot(*ox) - np.hypot(*vx)) < 1e-9
            and abs(np.hypot(*oy) - np.hypot(*vy)) < 1e-9,
            "deshear keeps each axis's measured scale")
    r.check(abs(float(ox @ oy)) < 1e-9,
            "…makes them perpendicular")
    r.check(np.sign(vx[0] * vy[1] - vx[1] * vy[0])
            == np.sign(ox[0] * oy[1] - ox[1] * oy[0]),
            "…and preserves handedness, so it cannot mirror the registration")

    # The raw stripes travel with the result, so a fit can be redone offline.
    r.check(len(c.stripes) == c.n_points and len(c.stripes[0]) == 4,
            f"the {len(c.stripes)} raw stripe measurements are stored "
            f"[axis, offset, cam_x, cam_y]")

    # Stripes that run off the frame are dropped, not fitted. This is what the
    # rig does: its DMD field is ~1.9x the camera's area.
    seen = stripe_sweep(lambda _f: None, lambda: np.full((CH, CW), 50.0),
                        (DW, DH), log=lambda _s: None)
    r.check(seen[0] == [] and seen[1] == [],
            "an invisible stripe is dropped rather than contributing a NaN")

    big = np.array([[6.0, 0.0, -700.0], [0.0, 6.0, -500.0], [0.0, 0.0, 1.0]])
    img = make_camera(big, rng)
    held = {"f": np.zeros((DH, DW), np.uint8)}
    seen = stripe_sweep(lambda f: held.__setitem__("f", f),
                        lambda: img(held["f"]), (DW, DH), log=lambda _s: None)
    kept = len(seen[0]) + len(seen[1])
    r.check(kept < 2 * len(STRIPE_OFFSETS),
            f"…and with the field {big[0, 0]:.0f}x the panel, only {kept} of "
            f"{2 * len(STRIPE_OFFSETS)} stripes stay on the frame")

    # ── 6. it round-trips through JSON ───────────────────────────────────────
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "calib.json"
    c.save(p)
    back = DmdCalibration.load(p)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.dmd_size == c.dmd_size and back.rms_px == c.rms_px
            and back.stripes == c.stripes,
            "a calibration survives save/load with its provenance and its "
            "raw stripes")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
