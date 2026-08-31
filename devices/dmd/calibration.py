"""Where the camera's view lands on the DMD.

ROIs are camera px, masks are DMD mirrors; this measures the affine between
them. A narrow stripe at a set of signed mirror offsets per axis, a line fit to
where each lands, and the two lines are the transform. Signed offsets carry
direction, so a mirror flip cannot pass.

**Coarse patterns only, and that is measured.** On this rig (2026-08-24) a solid
bar images cleanly, a 280 px checkerboard modulates 13 % of the frame and a
70 px stripe pattern 9 % — scattering erases fine structure at any pitch, and
Gray coding down to 16 mirrors per code still decoded 0.0 %. Nothing here is
finer than 5 % of the panel.

Patterns go to `AlpDevice.project()` directly, never `build_frame`, whose
scale/rotation/offset (and `fit`, which overrides them) would transform the
geometry being measured.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

ON, OFF = np.uint8(255), np.uint8(0)

# Modulation below this counts as "the projector does not reach this pixel".
# Well under a real lit/unlit contrast, well over sensor noise.
MIN_MODULATION = 0.15

# Stripe offsets from the panel centre, in mirrors — same units the log prints.
# ±500 reaches past the y half-extent (384) and close to the x half-extent
# (512), so a stripe run off the true panel edge shows up as "invisible" here
# rather than being mistaken for a camera-FOV limit.
STRIPE_OFFSETS = tuple(range(-500, 501, 50))
STRIPE_WIDTH = 0.025         # stripe thickness, fraction of the panel
STRIPE_CROSS = 0.25         # its length across the other axis


class CalibrationError(RuntimeError):
    """The sweep could not be registered — with a reason worth reading."""


# ── patterns ──────────────────────────────────────────────────────────────────

def _blank(width: int, height: int) -> np.ndarray:
    return np.full((height, width), OFF, np.uint8)


def offset_stripe(width: int, height: int, axis: int, offset: float, *,
                  thick_frac: float = STRIPE_WIDTH,
                  cross_frac: float = STRIPE_CROSS) -> np.ndarray:
    """A narrow stripe `offset` mirrors from the panel centre along `axis`."""
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    y, x = np.ogrid[:height, :width]
    half = thick_frac * (width if axis == 0 else height) / 2.0
    cross = cross_frac * (height if axis == 0 else width) / 2.0
    if axis == 0:
        m = (np.abs(x - cx - offset) <= half) & (np.abs(y - cy) <= cross)
    else:
        m = (np.abs(y - cy - offset) <= half) & (np.abs(x - cx) <= cross)
    return np.where(m, ON, OFF).astype(np.uint8)


# ── measuring ─────────────────────────────────────────────────────────────────

def modulation(on: np.ndarray, off: np.ndarray) -> np.ndarray:
    """(on - off) / (on + off): the sample cancels, the projector does not."""
    a = np.asarray(on, dtype=np.float64)
    b = np.asarray(off, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"pair shapes differ: {a.shape} vs {b.shape}")
    return (a - b) / np.maximum(a + b, 1e-9)


def bounding_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the True region, end-exclusive; None if empty."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def stripe_sweep(project: Callable[[np.ndarray], None],
                 grab: Callable[[], np.ndarray],
                 dmd_size: tuple[int, int], *,
                 offsets=STRIPE_OFFSETS,
                 min_modulation: float = MIN_MODULATION,
                 log: Callable[[str], None] = print) -> dict:
    """Step a stripe across each axis → {axis: [(offset, cam_x, cam_y), …]}.

    A stripe, not a growing bar: a centred bar should hold still as it grows and
    on the rig it drifted 527 px, the frame clipping one side while vignetting
    ate the other, so its centroid measured the lopsidedness. A stripe's is
    local, and one off the frame is dropped.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    half = (w / 2.0, h / 2.0)

    project(_blank(w, h))
    dark = np.asarray(grab(), dtype=np.float32)

    out: dict = {0: [], 1: []}
    for axis in (0, 1):
        for d in offsets:
            frac = d / half[axis]
            project(offset_stripe(w, h, axis, d))
            mod = np.abs(modulation(np.asarray(grab(), dtype=np.float32), dark))
            m = mod >= min_modulation
            n = int(m.sum())
            box = bounding_box(m)
            edge = bool(box and (box[0] == 0 or box[1] == 0
                                 or box[2] == m.shape[1] or box[3] == m.shape[0]))
            # Centroid WEIGHTED by modulation, not of the bare threshold mask.
            # A binary centroid moves as the threshold moves, and vignetting
            # tips which edge pixels clear it; weighting makes the estimate
            # smooth in both. Separable sums, so it costs one pass, not a grid.
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


def fit_axes(seen: dict) -> tuple | None:
    """All stripes at once → (centre, vx, vy, rms, n).

    **ONE shared centre**, not a line per axis: both axes pass through the panel
    centre at offset 0, and separate intercepts disagreed by 67 px on the rig,
    where the DMD overfills the camera so the survivors sit to one side and each
    fit is an extrapolation. Shear then absorbs that error.
    """
    pts = [(axis, d, cx, cy) for axis in (0, 1) for d, cx, cy in seen[axis]]
    if len(seen[0]) < 2 or len(seen[1]) < 2 or len(pts) < 5:
        return None
    keep = np.ones(len(pts), bool)
    out = _solve(pts, keep)
    if out is None:
        return None
    centre, vx, vy, rms, err = out

    # ONE rejection pass, against a scale taken from that first fit.
    #
    # The median, not the rms: a single gross outlier inflates the rms enough to
    # shelter itself — a 300 px stripe among clean ones pushed the rms to 85, so
    # a 3x-rms cut sat at 254 and caught nothing. 1.4826 makes the median match
    # sigma for a normal.
    #
    # And once, not iterated: on clean data the scale collapses after the first
    # trim, so a second pass starts rejecting perfectly good stripes. With ten
    # stripes there is no budget to hunt outliers one at a time anyway.
    sigma = 1.4826 * float(np.median(err))
    if sigma > 0:
        wild = err > 3.0 * sigma
        if wild.any() and (~wild).sum() >= max(5, len(pts) // 2):
            keep = ~wild
            out = _solve(pts, keep)
            if out is not None:
                centre, vx, vy, rms, err = out
    return centre, vx, vy, rms, int(keep.sum()), keep


def _solve(pts, keep):
    """Least squares over the kept points → (centre, vx, vy, rms, err).

    Separable by coordinate — camera x depends on (cx, vx.x, vy.x) and y on
    (cy, vx.y, vy.y) — so two 3-parameter fits, not one 6.
    """
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


def holdout_error(seen: dict) -> float | None:
    """Refit without one stripe per axis, then predict it.

    The residual cannot tell you this: least squares sits closest to the points
    it was handed, so its rms is optimistic by construction. Free — the stripes
    are already measured.
    """
    trial = {a: list(seen[a]) for a in (0, 1)}
    held = []
    for a in (0, 1):
        if len(trial[a]) < 4:
            return None
        i = len(trial[a]) // 2                  # nearest the middle offset
        held.append((a, *trial[a].pop(i)))
    out = fit_axes(trial)
    if out is None:
        return None
    centre, vx, vy, _rms, _n, _keep = out
    errs = []
    for axis, d, cx, cy in held:
        pred = centre + d * (vx if axis == 0 else vy)
        errs.append(float(np.hypot(cx - pred[0], cy - pred[1])))
    # The WORST axis, not the mean of the two: one axis predicting badly is
    # a bad calibration, and averaging it against a good one hides that.
    return float(max(errs))


def deshear(vx: np.ndarray, vy: np.ndarray,
            weights: tuple = (1.0, 1.0)) -> tuple:
    """Force the axes perpendicular, keeping both scales and the handedness.

    A relay is a rotation plus a per-axis magnification; shear comes only from
    tilt, which is keystone — a term an affine cannot hold anyway. What shear
    can do is soak up measurement error, so it is off unless asked.

    The two rotation estimates are averaged **by evidence** (lever arm
    `sqrt(sum(d^2))`): one axis routinely keeps far fewer stripes, and an even
    split drags the good one towards it.
    """
    kx, ky = float(np.hypot(*vx)), float(np.hypot(*vy))
    turn = 1.0 if float(vx[0] * vy[1] - vx[1] * vy[0]) >= 0 else -1.0
    ax = float(np.arctan2(vx[1], vx[0]))
    ay = float(np.arctan2(vy[1], vy[0])) - turn * np.pi / 2.0
    wx, wy = float(weights[0]), float(weights[1])
    if wx <= 0 and wy <= 0:
        wx = wy = 1.0
    # Weighted circular mean: the two estimates straddle the wrap at ±pi, so
    # they cannot simply be averaged as numbers.
    th = float(np.arctan2(wx * np.sin(ax) + wy * np.sin(ay),
                          wx * np.cos(ax) + wy * np.cos(ay)))
    return (kx * np.array([np.cos(th), np.sin(th)]),
            ky * np.array([np.cos(th + turn * np.pi / 2.0),
                           np.sin(th + turn * np.pi / 2.0)]))


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
              log: Callable[[str], None] = print) -> "DmdCalibration":
    """Project the stripes, fit the affine, return the registration.

    `project(frame)` displays one device-sized frame; `grab()` returns the
    camera's view of it. Both are callables so the whole thing is testable
    against a transform we chose, before any light is emitted (PLAN §2).

    **The caller owns the actuation** — this projects on every offset.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    seen = stripe_sweep(project, grab, (w, h), offsets=offsets,
                        min_modulation=min_modulation, log=log)

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
        # Lever arm per axis: how well each one determines a direction.
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
        # The raw measurements travel with the result, so a fit can be redone
        # offline — re-projecting onto a live animal to re-test a fit is not an
        # acceptable debugging loop.
        stripes=[[axis, d, px, py] for axis in (0, 1)
                 for d, px, py in seen[axis]],
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
    """A measured DMD↔camera registration, and what it took to get it.

    Residual and point count travel with the matrix: a transform with no
    provenance cannot be judged later, and "0.4 px over 18 stripes" is the
    difference between trusting it and re-running it.
    """
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "affine"
    rms_px:     float = 0.0
    n_points:   int = 0
    # Prediction error on a stripe left OUT of the fit. The residual is
    # optimistic by construction; this is not.
    holdout_px: float = 0.0
    created:    str = ""
    notes:      str = ""
    # The raw stripe measurements: [axis, offset_mirrors, cam_x, cam_y]. Kept
    # so a fit can be reconsidered without going back to the rig.
    stripes:    list = None

    def __post_init__(self) -> None:
        if self.stripes is None:
            self.stripes = []
        # Keyed by camera shape. Safe because a calibration is a measurement:
        # a new registration is a new object, never an edit to this one.
        self._mask_cache: dict[tuple[int, int], np.ndarray] = {}

    @property
    def dmd_to_cam(self) -> np.ndarray:
        return np.linalg.inv(self.cam_to_dmd)

    # ── what the camera can see, and what the DMD can reach ──
    def visible_mirrors(self) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) of the mirrors inside the camera's view.

        The question this module exists to answer. Clipped to the panel, so it
        is the usable region rather than an extrapolation.
        """
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
        """Which camera points the DMD can illuminate.

        By mapping into mirror space and bounds-checking, not by a polygon
        test — the transform already knows the field's shape.
        """
        d = apply_transform(self.cam_to_dmd, np.atleast_2d(pts))
        w, h = self.dmd_size
        return ((d[:, 0] >= 0) & (d[:, 0] <= w - 1)
                & (d[:, 1] >= 0) & (d[:, 1] <= h - 1))

    _MASK_ROWS = 256            # rows per band; caps the transform's temporaries

    def accessible_mask(self, shape: tuple[int, int]) -> np.ndarray:
        """(H, W) bool: the camera pixels an ROI may legally cover.

        Read-only, cached per shape, built in row bands. The ROI editor asks
        for this on **every drag**, and the whole-grid version cost 798 ms and
        ~1 GB per call at ORCA full frame.
        """
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
        out.flags.writeable = False         # shared; nobody may edit in place
        self._mask_cache[(h, w)] = out
        return out

    def accessible_corners(self) -> np.ndarray:
        """The DMD field's four corners in camera px, for drawing its outline."""
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
                "notes": self.notes, "stripes": list(self.stripes or [])}

    @classmethod
    def from_dict(cls, d: dict) -> "DmdCalibration":
        return cls(cam_to_dmd=np.array(d["cam_to_dmd"], float),
                   dmd_size=tuple(d["dmd_size"]), cam_size=tuple(d["cam_size"]),
                   model=d.get("model", "affine"),
                   rms_px=float(d.get("rms_px", 0.0)),
                   n_points=int(d.get("n_points", 0)),
                   holdout_px=float(d.get("holdout_px", 0.0)),
                   created=d.get("created", ""), notes=d.get("notes", ""),
                   stripes=d.get("stripes") or [])

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
