"""Where the camera's view lands on the DMD.

An ROI is drawn in *camera* pixels; a mask is in *DMD mirrors*. This measures
the affine between the two, so `mask_from_roi` can turn one into the other and
`visible_mirrors` can say which part of the panel the camera actually sees.

**Coarse patterns only, and that is a measurement, not a shortcut.** On this rig
(2026-08-24) a solid bar modulates the camera cleanly while a 280 px
checkerboard modulates 13 % of the frame and a 70 px stripe pattern 9 %. The
relay and the sample scatter enough to erase fine structure at any pitch, so
Gray coding cannot work here — it was tried down to 16 mirrors per code and
still decoded 0.0 %. Nothing here projects a pattern finer than 5 % of the panel.

The method: project a narrow stripe at nine known offsets along each axis, find
where each lands, fit a straight line. Two lines give the whole affine —
position, scale, rotation and shear — and because the offsets are signed, the
direction comes out with its sign attached.

Patterns are device-sized and go to `AlpDevice.project()` **directly**. Never
through `build_frame`: its scale/rotation/offset, and `fit` which overrides all
three, would transform the geometry being measured.
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

# Stripe offsets from the panel centre, as fractions of the half-extent.
STRIPE_OFFSETS = (-0.80, -0.60, -0.40, -0.20, 0.0, 0.20, 0.40, 0.60, 0.80)
STRIPE_WIDTH = 0.05         # stripe thickness, fraction of the panel
STRIPE_CROSS = 0.30         # its length across the other axis


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


def field_mask(on: np.ndarray, off: np.ndarray, *,
               min_modulation: float = MIN_MODULATION) -> np.ndarray:
    """Camera pixels this pattern lit, against a dark reference."""
    return np.abs(modulation(on, off)) >= min_modulation


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

    A narrow stripe rather than a growing bar. A *centred* bar's image should
    hold still as it grows; on the rig it drifted 527 px, because the frame
    clips one side while vignetting eats the other, and a lopsided region's
    centroid measures the lopsidedness. A stripe's centroid is local, and one
    that runs off the frame is dropped instead of biasing the fit.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    half = (w / 2.0, h / 2.0)

    project(_blank(w, h))
    dark = np.asarray(grab(), dtype=np.float32)

    out: dict = {0: [], 1: []}
    for axis in (0, 1):
        for frac in offsets:
            d = frac * half[axis]
            project(offset_stripe(w, h, axis, d))
            m = field_mask(np.asarray(grab(), dtype=np.float32), dark,
                           min_modulation=min_modulation)
            n = int(m.sum())
            box = bounding_box(m)
            edge = bool(box and (box[0] == 0 or box[1] == 0
                                 or box[2] == m.shape[1] or box[3] == m.shape[0]))
            ys, xs = np.nonzero(m) if n else (np.empty(0), np.empty(0))
            cx = float(xs.mean()) if n else float("nan")
            cy = float(ys.mean()) if n else float("nan")
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


def fit_axis_line(points: list[tuple]) -> tuple | None:
    """[(offset, cam_x, cam_y)] → (origin, px-per-mirror vector, rms, n).

    The vector carries both the scale (its length) and which way the axis runs
    (its sign) — the reason signed offsets beat symmetric patterns.
    """
    if len(points) < 3:
        return None
    d = np.array([p[0] for p in points], float)
    P = np.array([[p[1], p[2]] for p in points], float)
    A = np.column_stack([d, np.ones(len(d))])
    sol, *_ = np.linalg.lstsq(A, P, rcond=None)
    rms = float(np.sqrt(np.mean(((P - A @ sol) ** 2).sum(axis=1))))
    return sol[1], sol[0], rms, len(points)


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

    fits = {}
    for axis in (0, 1):
        f = fit_axis_line(seen[axis])
        if f is None:
            raise CalibrationError(
                f"only {len(seen[axis])} usable stripe(s) on the "
                f"{'xy'[axis]} axis — need 3. The rest were off the frame or "
                f"too dim, so the DMD field and the camera's view barely "
                f"overlap on that axis.")
        fits[axis] = f
        _o, vec, rms, n = f
        log(f"[dmd-calib] {'xy'[axis]}: {np.hypot(*vec):.3f} px/mirror along "
            f"({vec[0]:+.3f}, {vec[1]:+.3f}), residual {rms:.2f} px over "
            f"{n} stripes")

    ox, vx, rms_x, n_x = fits[0]
    oy, vy, rms_y, n_y = fits[1]
    # Both lines pass through the panel centre at offset 0, so their origins
    # are two measurements of one point. Their gap is a real error bar.
    gap = float(np.hypot(*(ox - oy)))
    centre = (ox + oy) / 2.0

    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    A = np.eye(3)
    A[:2, 0], A[:2, 1] = vx, vy
    A[:2, 2] = centre - cx * vx - cy * vy
    if abs(float(np.linalg.det(A[:2, :2]))) < 1e-9:
        raise CalibrationError(
            "the two measured axes came out parallel, so the registration "
            "cannot be inverted — one of them was not really measured")

    rms = float(np.sqrt((rms_x ** 2 + rms_y ** 2) / 2.0))
    shape = np.asarray(grab()).shape
    c = DmdCalibration(
        cam_to_dmd=np.linalg.inv(A), dmd_size=(w, h),
        cam_size=(int(shape[1]), int(shape[0])), rms_px=rms,
        n_points=n_x + n_y,
        created=datetime.now().isoformat(timespec="seconds"),
        notes=f"stripes only; panel centre ({centre[0]:.0f}, {centre[1]:.0f}), "
              f"the two axes' estimates of it {gap:.1f} px apart")
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

    The residual and point count travel with the matrix: a transform with no
    provenance cannot be judged later, and "0.4 px over 18 stripes" is the
    difference between trusting it and re-running it.
    """
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "affine"
    rms_px:     float = 0.0
    n_points:   int = 0
    created:    str = ""
    notes:      str = ""

    def __post_init__(self) -> None:
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
                "n_points": int(self.n_points), "created": self.created,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> "DmdCalibration":
        return cls(cam_to_dmd=np.array(d["cam_to_dmd"], float),
                   dmd_size=tuple(d["dmd_size"]), cam_size=tuple(d["cam_size"]),
                   model=d.get("model", "affine"),
                   rms_px=float(d.get("rms_px", 0.0)),
                   n_points=int(d.get("n_points", 0)),
                   created=d.get("created", ""), notes=d.get("notes", ""))

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
                + (f" ({self.created})" if self.created else ""))


def mask_from_roi(roi_cam: np.ndarray, dmd_to_cam: np.ndarray,
                  width: int, height: int) -> np.ndarray:
    """Camera-space ROI mask → the DMD frame that illuminates it.

    Iterates over *mirrors* and asks where each lands. A forward map leaves
    holes wherever the DMD is coarser than the camera, and a mask with holes is
    a stimulus with holes.
    """
    roi = np.asarray(roi_cam)
    yy, xx = np.mgrid[:height, :width]
    cam = apply_transform(dmd_to_cam,
                          np.column_stack((xx.ravel(), yy.ravel())))
    cx = np.rint(cam[:, 0]).astype(np.int64)
    cy = np.rint(cam[:, 1]).astype(np.int64)
    inside = ((cx >= 0) & (cx < roi.shape[1])
              & (cy >= 0) & (cy < roi.shape[0]))
    out = np.zeros(width * height, dtype=bool)
    out[inside] = roi[cy[inside], cx[inside]].astype(bool)
    return np.where(out.reshape(height, width), ON, OFF).astype(np.uint8)
