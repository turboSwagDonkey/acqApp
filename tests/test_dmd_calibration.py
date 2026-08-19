"""
Registering the DMD to the ORCA, against a transform we chose.

The point of testing this offline is that the real thing has no ground truth:
once light is on a sample, a wrong registration and a right one both produce a
picture. Here the transform is known, so the pipeline can be held to it —
project, image, decode, fit, and the fit must come back as the transform we
started from.

The synthetic camera is the honest part. It renders what the ORCA *would* see:
the DMD pattern warped by a known homography, times a structured "sample" that
is brighter in one corner, plus a vignette, an offset and shot noise. Every one
of those is there because the complementary-pair method claims to cancel it.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd_calibration.py
"""
from __future__ import annotations

import sys
import tracemalloc

import numpy as np

from _harness import Report

from acqApp.devices.dmd.calibration import (ON, apply_transform, bounding_box,
                                            checkerboard, checkerboard_pair,
                                            corner_marks, correspondences,
                                            decode, field_mask, fit_transform,
                                            gray_planes, mask_from_roi,
                                            modulation)

DW, DH = 256, 192          # a small DMD, so 40 planes stay quick
CW, CH = 320, 240          # the "ORCA"


def true_transform() -> np.ndarray:
    """DMD → camera: scale, a 7° rotation, an offset, and a little keystone."""
    th = np.radians(7.0)
    s = 1.05
    A = np.array([[s * np.cos(th), -s * np.sin(th), 34.0],
                  [s * np.sin(th),  s * np.cos(th), 22.0],
                  [0.0, 0.0, 1.0]])
    A[2, 0] = 1.5e-4          # keystone: an affine fit must show a residual
    return A


def sample_field(rng) -> np.ndarray:
    """What the specimen looks like with no projector: structure + vignette."""
    y, x = np.mgrid[:CH, :CW].astype(np.float64)
    struct = 400 + 600 * np.exp(-(((x - 90) ** 2 + (y - 70) ** 2) / (2 * 60.0 ** 2)))
    vign = 0.55 + 0.45 * np.exp(-(((x - CW / 2) ** 2 + (y - CH / 2) ** 2)
                                  / (2 * 190.0 ** 2)))
    return struct * vign + rng.normal(0, 3.0, (CH, CW))


def footprint(pattern: np.ndarray, M: np.ndarray) -> np.ndarray:
    """Which camera pixels `pattern` lights, as pure geometry — no sensor.

    Kept separate from `image_through` because "did the mask land on the ROI"
    is a geometric question. Asking it through a simulated exposure measures the
    noise model instead: at sigma 4 on an unlit level of 1, one camera pixel in
    six crosses any threshold near the lit level, which swamps a small ROI.
    """
    yy, xx = np.mgrid[:CH, :CW]
    d = apply_transform(np.linalg.inv(M),
                        np.column_stack((xx.ravel(), yy.ravel())))
    dx = np.rint(d[:, 0]).astype(np.int64)
    dy = np.rint(d[:, 1]).astype(np.int64)
    ok = (dx >= 0) & (dx < DW) & (dy >= 0) & (dy < DH)
    lit = np.zeros(CW * CH, bool)
    lit[ok] = pattern[dy[ok], dx[ok]] > 127
    return lit.reshape(CH, CW)


def image_through(pattern: np.ndarray, M: np.ndarray, sample: np.ndarray,
                  rng, *, lit_gain: float = 3.0) -> np.ndarray:
    """Camera view of `pattern` projected through `M`, on `sample`."""
    img = sample * np.where(footprint(pattern, M), lit_gain, 1.0)
    return np.clip(img + rng.normal(0, 4.0, img.shape), 0, 65535)


def main() -> int:
    r = Report("dmd-calib")
    rng = np.random.default_rng(7)
    M = true_transform()
    sample = sample_field(rng)

    # ── 1. patterns ──────────────────────────────────────────────────────────
    a, b = checkerboard_pair(DW, DH, square=32)
    r.check(a.shape == (DH, DW) and a.dtype == np.uint8,
            f"checkerboard is a device-sized uint8 frame {a.shape}")
    r.check(set(np.unique(a)) <= {0, 255},
            "…and strictly binary — the mirrors have no grey")
    board_only_a = checkerboard(DW, DH, 32, marks=False)
    board_only_b = checkerboard(DW, DH, 32, invert=True, marks=False)
    r.check(np.array_equal(board_only_a, 255 - board_only_b),
            "control: without marks the pair really is complementary")
    r.check(not np.array_equal(a, 255 - b),
            "…and with marks it deliberately is not — the marks survive "
            "differencing by appearing in both phases")

    # The marks must be mutually distinguishable, or a mirror flip reads as a
    # valid registration. Count lit pixels per quadrant: 1, 2, 3, 4 dots.
    cm = corner_marks(DW, DH)
    h, w = DH // 2, DW // 2
    quad = [int((cm[:h, :w] > 0).sum()), int((cm[:h, w:] > 0).sum()),
            int((cm[h:, w:] > 0).sum()), int((cm[h:, :w] > 0).sum())]
    r.check(len(set(quad)) == 4 and quad == sorted(quad),
            f"the four corner marks differ, clockwise from top-left {quad}")
    flipped = cm[:, ::-1]
    r.check(not np.array_equal(cm, flipped),
            "control: a mirror flip changes the marks, so it is detectable")

    # ── 2. field extent from one complementary pair ──────────────────────────
    ia = image_through(a, M, sample, rng)
    ib = image_through(b, M, sample, rng)
    mask = field_mask(ia, ib)
    box = bounding_box(mask)
    corners = apply_transform(M, np.array([[0, 0], [DW, 0], [DW, DH], [0, DH]],
                                          float))
    exp = (corners[:, 0].min(), corners[:, 1].min(),
           corners[:, 0].max(), corners[:, 1].max())
    r.check(box is not None and all(abs(g - e) <= 6 for g, e in zip(box, exp)),
            f"the field extent falls out of one pair: {box} vs "
            f"{tuple(round(v) for v in exp)}")
    # CONTROL: the sample alone must NOT look like a field — this is the claim
    # that differencing cancels structure rather than finding it.
    plain = field_mask(sample, sample + rng.normal(0, 4.0, sample.shape))
    r.check(plain.mean() < 0.02,
            f"control: with no projector the field is empty "
            f"({100 * plain.mean():.1f}% lit)")

    # ── 3. Gray decode ───────────────────────────────────────────────────────
    planes, nbx, nby = gray_planes(DW, DH)
    r.check(len(planes) == nbx + nby == 8 + 8,
            f"{len(planes)} planes for {DW}x{DH} ({nbx} x-bits, {nby} y-bits)")
    on = [image_through(p, M, sample, rng) for p in planes]
    off = [image_through(255 - p, M, sample, rng) for p in planes]
    dx, dy, valid = decode(on, off, nbx, nby)
    r.check(valid.sum() > 0.4 * valid.size,
            f"most of the frame decodes ({100 * valid.mean():.0f}%)")

    # Every valid pixel must name the mirror that actually lit it.
    yy, xx = np.mgrid[:CH, :CW]
    truth = apply_transform(np.linalg.inv(M),
                            np.column_stack((xx.ravel(), yy.ravel())))
    tx = truth[:, 0].reshape(CH, CW)
    ty = truth[:, 1].reshape(CH, CW)
    err = np.hypot(dx - tx, dy - ty)[valid]
    r.check(np.median(err) <= 1.5,
            f"decoded mirror coords are right (median {np.median(err):.2f} px, "
            f"p95 {np.percentile(err, 95):.2f})")

    # decode must stream the planes, not stack them. At ORCA full frame the
    # float64 stack version peaked at 7.8 GB — 40 frames x 10.5 Mpx plus the
    # temporaries of abs(m).min(axis=0) — which the rig box cannot spare while
    # it is pinning camera buffers. Measure the peak against what holding both
    # stacks as float64 would cost on its own.
    stack_f64 = 2 * len(planes) * CH * CW * 8
    tracemalloc.start()
    decode(on, off, nbx, nby)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    r.check(peak < 0.5 * stack_f64,
            f"decode streams the planes: peak {peak / 2**20:.1f} MB against "
            f"{stack_f64 / 2**20:.1f} MB for the float64 stacks alone")
    # CONTROL: that budget is not vacuous — the old approach really does exceed
    # it, so this check can fail.
    tracemalloc.start()
    _m = (np.asarray(on, dtype=np.float64) - np.asarray(off, dtype=np.float64))
    _cur, peak_old = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del _m
    r.check(peak_old >= 0.5 * stack_f64,
            f"control: materialising the stacks alone already costs "
            f"{peak_old / 2**20:.1f} MB, over the budget above")

    # A list of frames is what run_calibration collects, so it must not need a
    # 3-D array, and a ragged one must be named rather than broadcast.
    try:
        decode(on[:-1] + [np.zeros((CH + 2, CW), np.uint16)], off, nbx, nby)
        r.check(False, "a mis-shaped plane raises")
    except ValueError as e:
        # "plane 15", not numpy's "inhomogeneous shape after 1 dimensions" —
        # naming which exposure is wrong is the difference between a two-minute
        # fix and re-running the whole sweep blind.
        r.check(f"plane {len(planes) - 1}" in str(e),
                f"a mis-shaped plane is refused BY INDEX ({e})")

    # ── 4. the fit, and what its residual is for ─────────────────────────────
    cam, dmd = correspondences(dx, dy, valid, step=4)
    r.check(len(cam) > 500, f"{len(cam)} correspondences from one sweep")

    H, rms_h, keep_h = fit_transform(cam, dmd, model="homography")
    r.check(rms_h < 1.0, f"homography recovers the mapping (rms {rms_h:.3f} px)")
    A, rms_a, _ = fit_transform(cam, dmd, model="affine")
    # CONTROL: the true transform has keystone in it, so an affine model MUST
    # do measurably worse. If it did not, the residual would be telling us
    # nothing and a wrong model would pass unnoticed.
    r.check(rms_a > 2 * rms_h,
            f"control: the affine model shows the keystone in its residual "
            f"({rms_a:.3f} vs {rms_h:.3f} px)")

    # Round-trip: DMD → camera → DMD is the identity we actually rely on.
    probe = np.array([[10, 10], [DW - 10, 10], [DW // 2, DH // 2],
                      [DW - 10, DH - 10]], float)
    back = apply_transform(np.linalg.inv(H), probe)
    again = apply_transform(H, back)
    r.check(np.abs(again - probe).max() < 0.01,
            "the fitted transform inverts cleanly")
    cam_pred = apply_transform(M, probe)
    r.check(np.abs(back - cam_pred).max() < 2.0,
            f"…and it agrees with the transform we projected through "
            f"(max {np.abs(back - cam_pred).max():.2f} px)")

    # ── 5. ROI → mask ────────────────────────────────────────────────────────
    roi = np.zeros((CH, CW), bool)
    roi[110:150, 150:210] = True
    dmd_to_cam = np.linalg.inv(H)
    frame = mask_from_roi(roi, dmd_to_cam, DW, DH)
    r.check(frame.shape == (DH, DW) and set(np.unique(frame)) <= {0, 255},
            "the ROI becomes a device-sized binary frame")
    r.check(0 < (frame > 0).mean() < 0.5,
            f"…covering a plausible share of the panel "
            f"({100 * (frame > 0).mean():.1f}%)")
    # It has to land back on the ROI when projected — the whole purpose.
    lit = footprint(frame, M)
    hit = (lit & roi).sum() / max(1, roi.sum())
    spill = (lit & ~roi).sum() / max(1, lit.sum())
    r.check(hit > 0.9 and spill < 0.1,
            f"projected, the mask lands on the ROI ({100*hit:.0f}% covered, "
            f"{100*spill:.0f}% spill)")
    # CONTROL: aim with the wrong transform and it must miss, or the check above
    # would pass on any mask at all.
    bad = H.copy()
    bad[0, 2] += 40.0
    frame_bad = mask_from_roi(roi, np.linalg.inv(bad), DW, DH)
    hit_bad = (footprint(frame_bad, M) & roi).sum() / max(1, roi.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px error in the transform misses "
            f"({100*hit_bad:.0f}% covered)")
    r.check(frame.max() == ON, "frames are emitted at full-on, not scaled")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
