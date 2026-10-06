"""Where the camera's view lands on the DMD.

ROIs are camera px, masks are DMD mirrors; this measures the transform between
them. A narrow stripe at signed mirror offsets per axis, fit to where each
lands. Signed offsets carry direction, so a mirror flip can't pass.

Coarse patterns only (rig, 2026-08-24): scattering erases fine structure — a
70 px stripe pattern modulated 9 % of the frame, Gray codes decoded 0 %.

Patterns go to `AlpDevice.project()` directly, never `build_frame`, whose
geometry would transform what's being measured.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

ON, OFF = np.uint8(255), np.uint8(0)

# Below this, the projector doesn't reach the pixel.
MIN_MODULATION = 0.15

# Offsets from the panel centre, in mirrors. ±500 runs past the y half-extent
# (384), so a stripe off the panel edge shows as "invisible".
STRIPE_OFFSETS = tuple(range(-500, 501, 50))
STRIPE_WIDTH = 0.01      # fraction of the panel
STRIPE_CROSS = 0.05      # length across the other axis


class CalibrationError(RuntimeError):
    """The sweep couldn't be registered, with a reason worth reading."""


# ── patterns ──────────────────────────────────────────────────────────────────

def _blank(width: int, height: int) -> np.ndarray:
    return np.full((height, width), OFF, np.uint8)


def offset_stripe(width: int, height: int, axis: int, offset: float, *,
                  thick_frac: float = STRIPE_WIDTH,
                  cross_frac: float = STRIPE_CROSS) -> np.ndarray:
    """A stripe `offset` mirrors from the panel centre along `axis`."""
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    y, x = np.ogrid[:height, :width]
    half = thick_frac * (width if axis == 0 else height) / 2.0
    cross = cross_frac * (height if axis == 0 else width) / 2.0
    if axis == 0:
        m = (np.abs(x - cx - offset) <= half) & (np.abs(y - cy) <= cross)
    else:
        m = (np.abs(y - cy - offset) <= half) & (np.abs(x - cx) <= cross)
    return np.where(m, ON, OFF)


# ── measuring ─────────────────────────────────────────────────────────────────

def modulation(on: np.ndarray, off: np.ndarray) -> np.ndarray:
    """(on - off) / (on + off): the sample cancels, the projector doesn't."""
    a = np.asarray(on, dtype=np.float64)
    b = np.asarray(off, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"pair shapes differ: {a.shape} vs {b.shape}")
    return (a - b) / np.maximum(a + b, 1e-9)


def bounding_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1), end-exclusive; None if empty."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def stripe_sweep(project: Callable[[np.ndarray], None],
                 grab: Callable[[], np.ndarray],
                 dmd_size: tuple[int, int], *,
                 offsets=STRIPE_OFFSETS,
                 min_modulation: float = MIN_MODULATION,
                 thick_frac: float = STRIPE_WIDTH,
                 cross_frac: float = STRIPE_CROSS,
                 log: Callable[[str], None] = print) -> dict:
    """-> {axis: [(offset, cam_x, cam_y), …]}. Stripes clipped by the frame
    edge are dropped.

    A stripe, not a growing bar: a centred bar drifted 527 px on the rig as
    clipping and vignetting ate its sides. `cross_frac` is shrinkable for a
    steeply tilted camera, whose near end magnifies the stripe off the frame.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    half = (w / 2.0, h / 2.0)

    project(_blank(w, h))
    dark = np.asarray(grab(), dtype=np.float32)

    out: dict = {0: [], 1: []}
    for axis in (0, 1):
        for d in offsets:
            frac = d / half[axis]
            project(offset_stripe(w, h, axis, d,
                                  thick_frac=thick_frac, cross_frac=cross_frac))
            mod = np.abs(modulation(np.asarray(grab(), dtype=np.float32), dark))
            m = mod >= min_modulation
            n = int(m.sum())
            box = bounding_box(m)
            edge = bool(box and (box[0] == 0 or box[1] == 0
                                 or box[2] == m.shape[1] or box[3] == m.shape[0]))
            # Modulation-weighted centroid: a bare-mask one moves with the
            # threshold as vignetting tips edge pixels in or out.
            if n:
                wgt = np.where(m, mod, 0.0)
                tot = float(wgt.sum())
                cx = float(wgt.sum(axis=0) @ np.arange(wgt.shape[1])) / tot
                cy = float(wgt.sum(axis=1) @ np.arange(wgt.shape[0])) / tot
            else:
                cx = cy = float("nan")
            keep = n >= 50 and not edge
            log(f"[dmd-calib] {'xy'[axis]} {frac:+.2f} ({d:+7.1f} mirrors) -> "
                + (f"{n:>8d} px at ({cx:7.1f}, {cy:7.1f})" if n
                   else f"{'invisible':>28}")
                + ("" if keep else
                   "   [dropped: " + ("off the frame edge" if edge
                                      else "too little light") + "]"))
            if keep:
                out[axis].append((d, cx, cy))
    return out


def _robust_fit(solve, pts, min_keep: int):
    """Fit, then ONE rejection pass at 3 sigma, sigma from the MEDIAN error
    (a gross outlier inflates the rms enough to shelter itself). Not
    iterated: on clean data a second pass starts rejecting good stripes.
    -> (solve's result, keep) or (None, keep)."""
    keep = np.ones(len(pts), bool)
    out = solve(pts, keep)
    if out is None:
        return None, keep
    sigma = 1.4826 * float(np.median(out[-1]))    # 1.4826: median -> sigma
    if sigma > 0:
        wild = out[-1] > 3.0 * sigma
        if wild.any() and (~wild).sum() >= max(min_keep, len(pts) // 2):
            refit = solve(pts, ~wild)
            if refit is not None:
                out, keep = refit, ~wild
    return out, keep


def fit_axes(seen: dict) -> tuple | None:
    """-> (centre, vx, vy, rms, n, keep). ONE shared centre: separate
    per-axis intercepts disagreed by 67 px on the rig, both extrapolated."""
    pts = [(axis, d, cx, cy) for axis in (0, 1) for d, cx, cy in seen[axis]]
    if len(seen[0]) < 2 or len(seen[1]) < 2 or len(pts) < 5:
        return None
    out, keep = _robust_fit(_solve, pts, 5)
    if out is None:
        return None
    centre, vx, vy, rms, _err = out
    return centre, vx, vy, rms, int(keep.sum()), keep


def _solve(pts, keep):
    """-> (centre, vx, vy, rms, err over ALL points). Separable: two
    3-parameter fits, not one 6."""
    if int(np.sum(keep)) < 5:
        return None
    A = np.array([[1.0, d if axis == 0 else 0.0, d if axis == 1 else 0.0]
                  for axis, d, _cx, _cy in pts])
    bx = np.array([cx for _a, _d, cx, _cy in pts])
    by = np.array([cy for _a, _d, _cx, cy in pts])
    px, *_ = np.linalg.lstsq(A[keep], bx[keep], rcond=None)
    py, *_ = np.linalg.lstsq(A[keep], by[keep], rcond=None)
    err = np.hypot(bx - A @ px, by - A @ py)
    rms = float(np.sqrt(np.mean(err[keep] ** 2)))
    return (np.array([px[0], py[0]]), np.array([px[1], py[1]]),
            np.array([px[2], py[2]]), rms, err)


def _hold_out(seen: dict, min_per_axis: int):
    """Remove the middle stripe of each axis -> (trial, held)."""
    trial = {a: list(seen[a]) for a in (0, 1)}
    held = []
    for a in (0, 1):
        if len(trial[a]) < min_per_axis:
            return None, None
        held.append((a, *trial[a].pop(len(trial[a]) // 2)))
    return trial, held


def holdout_error(seen: dict) -> float | None:
    """Refit without one stripe per axis and predict it — the residual is
    optimistic by construction. The WORST axis, not the mean."""
    trial, held = _hold_out(seen, 4)
    if trial is None:
        return None
    out = fit_axes(trial)
    if out is None:
        return None
    centre, vx, vy, _rms, _n, _keep = out
    errs = []
    for axis, d, cx, cy in held:
        pred = centre + d * (vx if axis == 0 else vy)
        errs.append(float(np.hypot(cx - pred[0], cy - pred[1])))
    return float(max(errs))


def deshear(vx: np.ndarray, vy: np.ndarray,
            weights: tuple = (1.0, 1.0)) -> tuple:
    """Force the axes perpendicular, keeping scales and handedness. A relay
    has no real shear (tilt is keystone, which an affine can't hold), so
    shear only soaks up error. Rotation estimates are averaged by lever arm:
    one axis often keeps far fewer stripes."""
    kx, ky = float(np.hypot(*vx)), float(np.hypot(*vy))
    turn = 1.0 if float(vx[0] * vy[1] - vx[1] * vy[0]) >= 0 else -1.0
    ax = float(np.arctan2(vx[1], vx[0]))
    ay = float(np.arctan2(vy[1], vy[0])) - turn * np.pi / 2.0
    wx, wy = float(weights[0]), float(weights[1])
    if wx <= 0 and wy <= 0:
        wx = wy = 1.0
    # Circular mean: the estimates can straddle ±pi.
    th = float(np.arctan2(wx * np.sin(ax) + wy * np.sin(ay),
                          wx * np.cos(ax) + wy * np.cos(ay)))
    return (kx * np.array([np.cos(th), np.sin(th)]),
            ky * np.array([np.cos(th + turn * np.pi / 2.0),
                           np.sin(th + turn * np.pi / 2.0)]))


# ── full perspective, for a steeply tilted camera ─────────────────────────────
# The affine above is a relay: rotation, per-axis scale, offset. A tilted
# camera sees equal DMD steps at unequal image spacing (dim, tiny stripes on
# one side, frame-clipping ones on the other); only a homography (8 DOF, DLT)
# describes that.

def _homography_points(seen: dict, w: int, h: int) -> list[tuple]:
    """(DMD_x, DMD_y, cam_x, cam_y) per stripe; stripes sit on the centrelines."""
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    return ([(cx + d, cy, u, v) for d, u, v in seen[0]]
            + [(cx, cy + d, u, v) for d, u, v in seen[1]])


def _dlt(pts: list[tuple]) -> np.ndarray:
    """Direct linear transform: H is the singular vector of the smallest value."""
    rows = []
    for X, Y, u, v in pts:
        rows.append([-X, -Y, -1.0, 0.0, 0.0, 0.0, u * X, u * Y, u])
        rows.append([0.0, 0.0, 0.0, -X, -Y, -1.0, v * X, v * Y, v])
    _, _, Vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    H = Vt[-1].reshape(3, 3)
    return H / H[2, 2] if abs(H[2, 2]) > 1e-9 else H


def _solve_homography(pts: list[tuple], keep: np.ndarray):
    """-> (H, rms, err over ALL points)."""
    kept = [p for p, k in zip(pts, keep) if k]
    if len(kept) < 8:
        return None
    H = _dlt(kept)
    XY = np.array([[X, Y] for X, Y, _u, _v in pts], dtype=np.float64)
    UV = np.array([[u, v] for _X, _Y, u, v in pts], dtype=np.float64)
    pred = apply_transform(H, XY)
    err = np.hypot(*(pred - UV).T)
    rms = float(np.sqrt(np.mean(err[keep] ** 2))) if keep.any() else float("nan")
    return H, rms, err


def fit_homography(seen: dict, w: int, h: int):
    """-> (H, rms, n, keep, pts). Needs ≥3 stripes on EACH axis: each axis's
    points constrain a different part of H, so missing one leaves it
    undetermined, not just noisy."""
    if len(seen[0]) < 3 or len(seen[1]) < 3:
        return None
    pts = _homography_points(seen, w, h)
    if len(pts) < 8:
        return None
    out, keep = _robust_fit(_solve_homography, pts, 8)
    if out is None:
        return None
    H, rms, _err = out
    return H, rms, int(keep.sum()), keep, pts


def holdout_error_homography(seen: dict, w: int, h: int) -> float | None:
    trial, held = _hold_out(seen, 5)
    if trial is None:
        return None
    out = fit_homography(trial, w, h)
    if out is None:
        return None
    H, _rms, _n, _keep, _pts = out
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    errs = []
    for axis, d, u, v in held:
        X, Y = (cx + d, cy) if axis == 0 else (cx, cy + d)
        pred = apply_transform(H, np.array([[X, Y]], dtype=np.float64))[0]
        errs.append(float(np.hypot(u - pred[0], v - pred[1])))
    return float(max(errs))


def _calibrate_homography(seen: dict, w: int, h: int,
                          grab: Callable[[], np.ndarray],
                          log: Callable[[str], None]) -> "DmdCalibration":
    fit = fit_homography(seen, w, h)
    if fit is None:
        raise CalibrationError(
            f"too few usable stripes to fit a homography — {len(seen[0])} on "
            f"x and {len(seen[1])} on y, and each axis needs at least 3 (8 "
            f"total; a homography has more freedom than the affine fit, so it "
            f"needs more points to pin down). The rest were off the frame or "
            f"too dim — a smaller cross_frac may keep more of them inside "
            f"the frame on a steeply tilted rig.")
    H, rms, n, keep, pts = fit
    total = len(pts)

    # Scale and rotation vary across the field; report the Jacobian at centre.
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    eps = 1.0
    base = apply_transform(H, np.array([[cx, cy]], dtype=np.float64))[0]
    dx = apply_transform(H, np.array([[cx + eps, cy]], dtype=np.float64))[0] - base
    dy = apply_transform(H, np.array([[cx, cy + eps]], dtype=np.float64))[0] - base
    kx, ky = float(np.hypot(*dx)), float(np.hypot(*dy))
    ax = float(np.degrees(np.arctan2(dx[1], dx[0])))
    log(f"[dmd-calib] homography fit: local x {kx:.3f} px/mirror, y {ky:.3f} "
        f"px/mirror at {ax:+.2f}deg — AT THE PANEL CENTRE ONLY, this model's "
        f"scale and rotation vary across the field by design")
    if n < total:
        log(f"[dmd-calib] {total - n} stripe(s) rejected as outliers "
            f"(>3x the residual); {n} kept")
    log(f"[dmd-calib] residual {rms:.2f} px over {n} stripes")
    hold = holdout_error_homography(seen, w, h)
    if hold is not None:
        log(f"[dmd-calib] hold-out error {hold:.2f} px — refitted without a "
            f"stripe, then asked to predict it. This is the number to trust, "
            f"not the residual.")

    shape = np.asarray(grab()).shape
    c = DmdCalibration(
        cam_to_dmd=np.linalg.inv(H), dmd_size=(w, h),
        cam_size=(int(shape[1]), int(shape[0])), rms_px=rms, n_points=n,
        holdout_px=float(hold or 0.0), model="homography",
        stripes=_raw_stripes(seen),
        created=datetime.now().isoformat(timespec="seconds"),
        notes=f"full projective fit (8 DOF, for a steeply tilted camera) — "
              f"local scale near centre {kx:.3f} x {ky:.3f} px/mirror at "
              f"{ax:+.2f}deg")
    log(f"[dmd-calib] {c.describe()}")
    log(f"[dmd-calib] the camera sees mirrors {c.visible_mirrors()}")
    if rms > 10.0:
        log("[dmd-calib] WARNING: that residual is large for a homography "
            "fit too — check the log above for a stripe that was kept but "
            "looks out of place.")
    return c


def _raw_stripes(seen: dict) -> list:
    """The raw measurements, saved with the fit so it can be redone offline
    rather than by re-projecting onto an animal."""
    return [[axis, d, px, py] for axis in (0, 1) for d, px, py in seen[axis]]


# ── the transform ─────────────────────────────────────────────────────────────

def apply_transform(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Map (N, 2) points through a 3×3 homogeneous matrix."""
    p = np.asarray(pts, dtype=np.float64)
    h = np.column_stack([p, np.ones(len(p))]) @ np.asarray(M).T
    w = np.where(np.abs(h[:, 2]) < 1e-12, 1e-12, h[:, 2])
    return h[:, :2] / w[:, None]


def calibrate(project: Callable[[np.ndarray], None],
              grab: Callable[[], np.ndarray],
              dmd_size: tuple[int, int], *,
              offsets=STRIPE_OFFSETS,
              allow_shear: bool = False,
              min_modulation: float = MIN_MODULATION,
              thick_frac: float = STRIPE_WIDTH,
              cross_frac: float = STRIPE_CROSS,
              model: str = "affine",
              log: Callable[[str], None] = print) -> "DmdCalibration":
    """Project the stripes, fit, return the registration. Callables, so it's
    testable against a known transform before any light is emitted.

    "affine": camera close to straight-on. "homography": a tilted camera.
    The caller owns the actuation — this projects on every offset.
    """
    if model not in ("affine", "homography"):
        raise CalibrationError(f"unknown calibration model {model!r}")
    w, h = int(dmd_size[0]), int(dmd_size[1])
    seen = stripe_sweep(project, grab, (w, h), offsets=offsets,
                        min_modulation=min_modulation,
                        thick_frac=thick_frac, cross_frac=cross_frac, log=log)

    if model == "homography":
        return _calibrate_homography(seen, w, h, grab, log=log)

    fit = fit_axes(seen)
    if fit is None:
        raise CalibrationError(
            f"too few usable stripes to fit — {len(seen[0])} on x and "
            f"{len(seen[1])} on y, and each axis needs at least 2. The rest "
            f"were off the frame or too dim, so the DMD field and the camera's "
            f"view barely overlap.")
    centre, vx, vy, rms, n, keep = fit

    kx, ky = float(np.hypot(*vx)), float(np.hypot(*vy))
    ax = float(np.degrees(np.arctan2(vx[1], vx[0])))
    ay = float(np.degrees(np.arctan2(vy[1], vy[0])))
    gap = abs(ay - ax)
    gap = min(gap, 360.0 - gap)
    shear = gap - 90.0
    log(f"[dmd-calib] x {kx:.3f} px/mirror at {ax:+.2f}deg, "
        f"y {ky:.3f} at {ay:+.2f}deg")
    log(f"[dmd-calib] axes {gap:.2f}deg apart -> shear {shear:+.2f}deg"
        + ("  (kept)" if allow_shear else "  (DISCARDED — see allow_shear)"))
    if not allow_shear:
        lever = tuple(float(np.sqrt(sum(d * d for d, _x, _y in seen[a])))
                      for a in (0, 1))
        log(f"[dmd-calib] rotation weighted {lever[0]:.0f} : {lever[1]:.0f} "
            f"(x : y lever arm)")
        vx, vy = deshear(vx, vy, lever)
    total = len(seen[0]) + len(seen[1])
    if n < total:
        log(f"[dmd-calib] {total - n} stripe(s) rejected as outliers "
            f"(>3x the residual); {n} kept")
    log(f"[dmd-calib] residual {rms:.2f} px over {n} stripes; panel centre "
        f"({centre[0]:.0f}, {centre[1]:.0f})")
    hold = holdout_error(seen)
    if hold is not None:
        log(f"[dmd-calib] hold-out error {hold:.2f} px ({hold / max(kx, ky):.1f} "
            f"mirrors) — refitted without a stripe, then asked to predict it. "
            f"This is the number to trust, not the residual.")

    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    A = np.eye(3)
    A[:2, 0], A[:2, 1] = vx, vy
    A[:2, 2] = centre - cx * vx - cy * vy
    if abs(float(np.linalg.det(A[:2, :2]))) < 1e-9:
        raise CalibrationError(
            "the two measured axes came out parallel, so the registration "
            "cannot be inverted — one of them was not really measured")

    shape = np.asarray(grab()).shape
    c = DmdCalibration(
        cam_to_dmd=np.linalg.inv(A), dmd_size=(w, h),
        cam_size=(int(shape[1]), int(shape[0])), rms_px=rms, n_points=n,
        holdout_px=float(hold or 0.0),
        model="affine" if allow_shear else "affine-noshear",
        stripes=_raw_stripes(seen),
        created=datetime.now().isoformat(timespec="seconds"),
        notes=f"{kx:.3f} x {ky:.3f} px/mirror, DMD-x {ax:+.2f}deg, measured "
              f"shear {shear:+.2f}deg "
              f"({'kept' if allow_shear else 'discarded'}), panel centre "
              f"({centre[0]:.0f}, {centre[1]:.0f})")
    log(f"[dmd-calib] {c.describe()}")
    log(f"[dmd-calib] the camera sees mirrors {c.visible_mirrors()}")
    if rms > 10.0:
        log("[dmd-calib] WARNING: that residual is large — the stripe centroids "
            "are not on a straight line, so an affine does not describe this "
            "relay. Check the log above for a stripe that was kept but looks "
            "out of place.")
    return c


@dataclass
class DmdCalibration:
    """A measured DMD↔camera registration and its provenance."""
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "affine"
    rms_px:     float = 0.0
    n_points:   int = 0
    holdout_px: float = 0.0           # error on a stripe left out of the fit
    created:    str = ""
    notes:      str = ""
    stripes:    list = None           # [axis, offset, cam_x, cam_y] raw
    # (cx, cy, r) camera px: outside it the optics dim the field. Advisory
    # only (`well_lit`); never clips the projection.
    vignette:   tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.stripes is None:
            self.stripes = []
        # Safe to cache: every edit (flip, corners) is a new object.
        self._mask_cache: dict[tuple[int, int], np.ndarray] = {}

    @property
    def dmd_to_cam(self) -> np.ndarray:
        return np.linalg.inv(self.cam_to_dmd)

    # ── what the camera can see, and what the DMD can reach ──
    def visible_mirrors(self) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) of the mirrors inside the camera's view, clipped
        to the panel."""
        cw, ch = self.cam_size
        d = apply_transform(self.cam_to_dmd,
                            np.array([[0, 0], [cw - 1, 0],
                                      [cw - 1, ch - 1], [0, ch - 1]], float))
        w, h = self.dmd_size
        return (int(max(0, np.floor(d[:, 0].min()))),
                int(max(0, np.floor(d[:, 1].min()))),
                int(min(w, np.ceil(d[:, 0].max()))),
                int(min(h, np.ceil(d[:, 1].max()))))

    def accessible(self, pts: np.ndarray) -> np.ndarray:
        """Which camera points the DMD can illuminate."""
        d = apply_transform(self.cam_to_dmd, np.atleast_2d(pts))
        w, h = self.dmd_size
        return ((d[:, 0] >= 0) & (d[:, 0] <= w - 1)
                & (d[:, 1] >= 0) & (d[:, 1] <= h - 1))

    def well_lit(self, pts: np.ndarray) -> np.ndarray:
        """Inside the vignette circle; all True if none was marked."""
        p = np.atleast_2d(pts)
        if self.vignette is None:
            return np.ones(p.shape[0], dtype=bool)
        cx, cy, r = self.vignette
        return (p[:, 0] - cx) ** 2 + (p[:, 1] - cy) ** 2 <= r * r

    _MASK_ROWS = 256            # rows per band; caps the temporaries

    def accessible_mask(self, shape: tuple[int, int]) -> np.ndarray:
        """(H, W) reachable pixels. Cached and banded: the ROI editor asks on
        every drag, and the whole-grid version took 798 ms and ~1 GB."""
        h, w = int(shape[0]), int(shape[1])
        hit = self._mask_cache.get((h, w))
        if hit is not None:
            return hit

        M = np.asarray(self.cam_to_dmd, dtype=np.float64)
        dw, dh = self.dmd_size
        x = np.arange(w, dtype=np.float64)
        out = np.empty((h, w), dtype=bool)
        for y0 in range(0, h, self._MASK_ROWS):
            y = np.arange(y0, min(y0 + self._MASK_ROWS, h),
                          dtype=np.float64)[:, None]
            den = M[2, 0] * x + M[2, 1] * y + M[2, 2]
            den = np.where(np.abs(den) < 1e-12, 1e-12, den)
            dx = (M[0, 0] * x + M[0, 1] * y + M[0, 2]) / den
            dy = (M[1, 0] * x + M[1, 1] * y + M[1, 2]) / den
            out[y0:y0 + y.shape[0]] = ((dx >= 0) & (dx <= dw - 1)
                                       & (dy >= 0) & (dy <= dh - 1))
        out.flags.writeable = False         # shared
        self._mask_cache[(h, w)] = out
        return out

    def accessible_corners(self) -> np.ndarray:
        """The panel's four corners in camera px."""
        w, h = self.dmd_size
        return apply_transform(
            self.dmd_to_cam,
            np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float))

    # ── persistence ──
    def to_dict(self) -> dict:
        return {"cam_to_dmd": np.asarray(self.cam_to_dmd).tolist(),
                "dmd_size": list(self.dmd_size), "cam_size": list(self.cam_size),
                "model": self.model, "rms_px": float(self.rms_px),
                "n_points": int(self.n_points),
                "holdout_px": float(self.holdout_px), "created": self.created,
                "notes": self.notes, "stripes": list(self.stripes or []),
                "vignette": list(self.vignette) if self.vignette else None}

    @classmethod
    def from_dict(cls, d: dict) -> "DmdCalibration":
        vignette = d.get("vignette")
        return cls(cam_to_dmd=np.array(d["cam_to_dmd"], float),
                   dmd_size=tuple(d["dmd_size"]), cam_size=tuple(d["cam_size"]),
                   model=d.get("model", "affine"),
                   rms_px=float(d.get("rms_px", 0.0)),
                   n_points=int(d.get("n_points", 0)),
                   holdout_px=float(d.get("holdout_px", 0.0)),
                   created=d.get("created", ""), notes=d.get("notes", ""),
                   stripes=d.get("stripes") or [],
                   vignette=tuple(vignette) if vignette else None)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "DmdCalibration":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def describe(self) -> str:
        return (f"DMD {self.dmd_size[0]}x{self.dmd_size[1]} → camera "
                f"{self.cam_size[0]}x{self.cam_size[1]}, {self.model}, "
                f"rms {self.rms_px:.2f} px over {self.n_points} points"
                + (f", hold-out {self.holdout_px:.2f} px"
                   if self.holdout_px else "")
                + (f" ({self.created})" if self.created else ""))


def _flipped(calib: DmdCalibration, flip: np.ndarray) -> DmdCalibration:
    return replace(calib, cam_to_dmd=flip @ np.asarray(calib.cam_to_dmd,
                                                       dtype=np.float64))


def flip_y(calib: DmdCalibration) -> DmdCalibration:
    """Mirror the projected rows (r -> h-1-r), an operator knob
    (`DmdSettings.roi_flip_y`). The panel's corners map onto each other, so
    the field outline and reach don't move; only which mirrors an ROI lights
    does. Composed onto the matrix so `dmd_to_cam` stays right for free."""
    h = float(calib.dmd_size[1])
    return _flipped(calib, np.array([[1.0, 0.0, 0.0], [0.0, -1.0, h - 1.0],
                                     [0.0, 0.0, 1.0]]))


def flip_x(calib: DmdCalibration) -> DmdCalibration:
    """`flip_y` for columns."""
    w = float(calib.dmd_size[0])
    return _flipped(calib, np.array([[-1.0, 0.0, w - 1.0], [0.0, 1.0, 0.0],
                                     [0.0, 0.0, 1.0]]))


def with_corners(calib: DmdCalibration, corners_cam) -> DmdCalibration:
    """Replace the fit so the panel corners land exactly on `corners_cam`
    (order of `accessible_corners()`): an operator's manual correction. Four
    points determine a homography exactly, so the residuals are zeroed."""
    w, h = calib.dmd_size
    dmd_pts = ((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1))
    cam_pts = np.asarray(corners_cam, dtype=np.float64)
    if cam_pts.shape != (4, 2):
        raise ValueError(f"need 4 (x, y) camera points, got {cam_pts.shape}")
    pts = [(float(X), float(Y), float(u), float(v))
          for (X, Y), (u, v) in zip(dmd_pts, cam_pts)]
    H = _dlt(pts)
    # inv() only raises on EXACT singularity; near-collinear corners come back
    # "invertible" at cond ~1e15 (a sane rectangle is ~1e3).
    if np.linalg.cond(H) > 1e8:
        raise CalibrationError(
            "the four corners don't determine a valid registration — they're "
            "too close to collinear. Drag them further apart.")
    cam_to_dmd = np.linalg.inv(H)
    model = calib.model if calib.model.endswith("+corners") else f"{calib.model}+corners"
    notes = (calib.notes + " — corners manually adjusted" if calib.notes
             else "corners manually adjusted")
    return replace(calib, cam_to_dmd=cam_to_dmd, model=model, rms_px=0.0,
                   holdout_px=0.0, notes=notes,
                   created=datetime.now().isoformat(timespec="seconds"))


def manual_seed(dmd_size: tuple[int, int], cam_size: tuple[int, int],
                frac: float = 0.6) -> DmdCalibration:
    """A starting point for dragging corners when no sweep exists: the panel
    drawn as a centred `frac`-wide rectangle in the camera frame, panel aspect."""
    dw, dh = dmd_size
    cw, ch = cam_size
    hw = frac * cw / 2.0
    hh = min(hw * dh / dw, frac * ch / 2.0)
    hw = hh * dw / dh
    cx, cy = (cw - 1) / 2.0, (ch - 1) / 2.0
    blank = DmdCalibration(cam_to_dmd=np.eye(3), dmd_size=dmd_size,
                           cam_size=cam_size, model="manual")
    return with_corners(blank, [(cx - hw, cy - hh), (cx + hw, cy - hh),
                                (cx + hw, cy + hh), (cx - hw, cy + hh)])


def with_vignette(calib: DmdCalibration, cx: float, cy: float,
                  r: float) -> DmdCalibration:
    """Record the operator-drawn vignette circle (advisory; see `well_lit`)."""
    if r <= 0:
        raise ValueError(f"vignette radius must be positive, got {r!r}")
    return replace(calib, vignette=(float(cx), float(cy), float(r)))


def without_vignette(calib: DmdCalibration) -> DmdCalibration:
    return replace(calib, vignette=None)
