"""Registering the DMD to the ORCA — patterns, decode, and the fit.

For aiming: an ROI is drawn in *camera* pixels, a mask is in *DMD mirrors*, and
turning one into the other needs a measured transform. Nothing here projects or
grabs; it is the pure half, so the whole pipeline can be tested against a known
transform before any light is emitted (PLAN.md §2).

Everything rests on **complementary pairs**: project a pattern and its inverse,
and `m = (on - off) / (on + off)` cancels the sample. Structure, background and
vignetting are identical in both frames and divide out; what survives is how
strongly the projector modulates each *camera* pixel. So `m` is both the
per-pixel bit decision and, thresholded, the DMD's field boundary.

Two ways to use it:

  `checkerboard_pair()`  one pair — the field extent and an eyeball check, with
                         corner marks that are mutually distinguishable so a
                         mirror flip cannot pass as a valid registration.
  `gray_planes()`        ~2*(10+10) frames Gray-coding each mirror's x and y, so
                         every camera pixel is labelled with the mirror that lit
                         it. Thousands of correspondences rather than four —
                         and four points fit a homography *exactly*, leaving no
                         residual to tell a good fit from a bad one.

Patterns are built at the device's own size and must be handed to
`AlpDevice.project()` **directly**. Do not route them through `build_frame`: its
scale/rotation/offset — and `fit`, which overrides all three — would transform
the very geometry this is measuring.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

ON, OFF = np.uint8(255), np.uint8(0)

# Modulation below this counts as "the projector does not reach this pixel".
# Well under a real lit/unlit contrast, well over sensor noise on a dark frame.
MIN_MODULATION = 0.15


# ══════════════════════════════════════════════════════════════════════════════
#  Patterns
# ══════════════════════════════════════════════════════════════════════════════

def _blank(width: int, height: int) -> np.ndarray:
    return np.full((height, width), OFF, np.uint8)


def corner_marks(width: int, height: int, *, inset: int = 40,
                 dot: int = 12, gap: int = 26) -> np.ndarray:
    """Four corner marks carrying 1/2/3/4 dots, clockwise from top-left.

    Distinguishable, not merely present: the relay can introduce a mirror flip,
    and four identical marks cannot reveal one — a flipped field still shows a
    mark in every corner. Counting dots pins orientation and handedness.
    """
    f = _blank(width, height)
    corners = ((inset, inset, +1, +1), (width - inset, inset, -1, +1),
               (width - inset, height - inset, -1, -1),
               (inset, height - inset, +1, -1))
    for n, (cx, cy, sx, sy) in enumerate(corners, start=1):
        for i in range(n):
            x = int(cx + sx * i * gap)
            y = int(cy)
            y0, y1 = max(0, y - dot // 2), min(height, y + dot // 2 + 1)
            x0, x1 = max(0, x - dot // 2), min(width, x + dot // 2 + 1)
            f[y0:y1, x0:x1] = ON
    return f


def checkerboard(width: int, height: int, square: int = 64, *,
                 invert: bool = False, marks: bool = True) -> np.ndarray:
    """Checkerboard at the device size, optionally with the corner marks.

    The marks are ORed in on both phases, so they survive the differencing as a
    common feature rather than flipping with the board.
    """
    if square < 1:
        raise ValueError("square must be >= 1 px")
    y, x = np.ogrid[:height, :width]
    board = ((x // square + y // square) % 2).astype(bool)
    if invert:
        board = ~board
    f = np.where(board, ON, OFF).astype(np.uint8)
    if marks:
        f = np.maximum(f, corner_marks(width, height))
    return f


def checkerboard_pair(width: int, height: int, square: int = 64, *,
                      marks: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """(pattern, its inverse) — the one pair that gives the field extent."""
    return (checkerboard(width, height, square, invert=False, marks=marks),
            checkerboard(width, height, square, invert=True, marks=marks))


def _gray(v: np.ndarray) -> np.ndarray:
    """Binary → reflected Gray code."""
    return v ^ (v >> 1)


def gray_planes(width: int, height: int) -> tuple[list[np.ndarray], int, int]:
    """Gray-coded bit planes for x then y → (planes, n_bits_x, n_bits_y).

    Gray rather than plain binary because adjacent mirrors differ in exactly one
    bit, so a pixel straddling a pattern boundary misdecodes by ±1 mirror rather
    than by half the panel.

    Project each plane with its inverse (`255 - plane`); `decode` wants both.
    """
    nbx = max(1, int(np.ceil(np.log2(max(width, 2)))))
    nby = max(1, int(np.ceil(np.log2(max(height, 2)))))
    gx = _gray(np.arange(width, dtype=np.int64))
    gy = _gray(np.arange(height, dtype=np.int64))

    planes: list[np.ndarray] = []
    for k in range(nbx - 1, -1, -1):                    # MSB first
        row = np.where((gx >> k) & 1, ON, OFF).astype(np.uint8)
        planes.append(np.broadcast_to(row, (height, width)).copy())
    for k in range(nby - 1, -1, -1):
        col = np.where((gy >> k) & 1, ON, OFF).astype(np.uint8)
        planes.append(np.broadcast_to(col[:, None], (height, width)).copy())
    return planes, nbx, nby


# ══════════════════════════════════════════════════════════════════════════════
#  Decode
# ══════════════════════════════════════════════════════════════════════════════

def modulation(on: np.ndarray, off: np.ndarray) -> np.ndarray:
    """(on - off) / (on + off), the sample-cancelled contrast per camera pixel."""
    a = np.asarray(on, dtype=np.float64)
    b = np.asarray(off, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"pair shapes differ: {a.shape} vs {b.shape}")
    return (a - b) / np.maximum(a + b, 1e-9)


def field_mask(on: np.ndarray, off: np.ndarray, *,
               min_modulation: float = MIN_MODULATION) -> np.ndarray:
    """Camera pixels the projector actually reaches, from one complementary pair.

    |m| rather than m: a pixel under a *dark* square of the first phase is lit in
    the second, and is every bit as much inside the field.
    """
    return np.abs(modulation(on, off)) >= min_modulation


def bounding_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the True region, end-exclusive; None if empty."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def decode(on_stack, off_stack, n_bits_x: int, n_bits_y: int, *,
           min_modulation: float = MIN_MODULATION):
    """Gray-coded stacks → (dmd_x, dmd_y, valid), all camera-shaped.

    `on_stack[i]`/`off_stack[i]` are the camera's view of plane i and its
    inverse, in `gray_planes` order. A pixel is valid only if *every* plane
    modulated it: one ambiguous bit is a wrong mirror coordinate, and a
    confidently wrong correspondence is worse than a missing one.
    """
    on = np.asarray(on_stack, dtype=np.float64)
    off = np.asarray(off_stack, dtype=np.float64)
    if on.shape != off.shape:
        raise ValueError(f"stack shapes differ: {on.shape} vs {off.shape}")
    if on.shape[0] != n_bits_x + n_bits_y:
        raise ValueError(f"expected {n_bits_x + n_bits_y} planes, got {on.shape[0]}")

    m = (on - off) / np.maximum(on + off, 1e-9)
    bits = m > 0.0
    valid = np.abs(m).min(axis=0) >= min_modulation

    def to_int(stack: np.ndarray) -> np.ndarray:
        # Gray → binary, MSB first: b[msb] = g[msb], then b[i] = b[i+1] ^ g[i].
        run = stack[0].astype(np.int64)
        out = run.copy()
        for i in range(1, len(stack)):
            run = run ^ stack[i].astype(np.int64)
            out = (out << 1) | run
        return out

    x = to_int(bits[:n_bits_x])
    y = to_int(bits[n_bits_x:n_bits_x + n_bits_y])
    return x, y, valid


def correspondences(dmd_x, dmd_y, valid, *, step: int = 8):
    """Valid pixels as (camera_xy, dmd_xy) point arrays, subsampled by `step`.

    Subsampled because a full frame is millions of points and the fit converges
    on thousands; `step` trades fit time against coverage, not accuracy.
    """
    sub = np.zeros_like(valid)
    sub[::step, ::step] = True
    sel = valid & sub
    ys, xs = np.nonzero(sel)
    cam = np.column_stack((xs, ys)).astype(np.float64)
    dmd = np.column_stack((dmd_x[sel], dmd_y[sel])).astype(np.float64)
    return cam, dmd


# ══════════════════════════════════════════════════════════════════════════════
#  Fit
# ══════════════════════════════════════════════════════════════════════════════

def _affine(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    n = len(src)
    A = np.zeros((2 * n, 6))
    b = np.empty(2 * n)
    A[0::2, 0:2], A[0::2, 2] = src, 1.0
    A[1::2, 3:5], A[1::2, 5] = src, 1.0
    b[0::2], b[1::2] = dst[:, 0], dst[:, 1]
    p = np.linalg.lstsq(A, b, rcond=None)[0]
    return np.array([[p[0], p[1], p[2]], [p[3], p[4], p[5]], [0.0, 0.0, 1.0]])


def _normalise(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hartley normalisation — centroid at 0, mean radius √2. Without it the
    DLT's design matrix is badly conditioned at pixel magnitudes."""
    c = p.mean(axis=0)
    d = float(np.sqrt(((p - c) ** 2).sum(axis=1)).mean())
    s = np.sqrt(2.0) / max(d, 1e-12)
    T = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1.0]])
    return (p - c) * s, T


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    sn, Ts = _normalise(src)
    dn, Td = _normalise(dst)
    n = len(src)
    A = np.zeros((2 * n, 9))
    x, y = sn[:, 0], sn[:, 1]
    u, v = dn[:, 0], dn[:, 1]
    A[0::2] = np.column_stack([-x, -y, -np.ones(n), np.zeros((n, 3)),
                               u * x, u * y, u])
    A[1::2] = np.column_stack([np.zeros((n, 3)), -x, -y, -np.ones(n),
                               v * x, v * y, v])
    # full_matrices=False: only the 9x9 Vh is wanted, and the default would
    # build a (2n, 2n) U — 300 MB and seconds at a few thousand points.
    H = np.linalg.svd(A, full_matrices=False)[2][-1].reshape(3, 3)
    H = np.linalg.inv(Td) @ H @ Ts
    return H / H[2, 2]


def apply_transform(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Map (N, 2) points through a 3×3 homogeneous matrix."""
    p = np.asarray(pts, dtype=np.float64)
    h = np.column_stack([p, np.ones(len(p))]) @ np.asarray(M).T
    w = np.where(np.abs(h[:, 2]) < 1e-12, 1e-12, h[:, 2])
    return h[:, :2] / w[:, None]


def fit_transform(src: np.ndarray, dst: np.ndarray, *, model: str = "affine",
                  reject_sigma: float = 3.0, iters: int = 3):
    """Least-squares src → dst → (M, rms_px, inliers).

    **The residual is the point.** Four corner marks fit a homography exactly
    and report rms 0 right or wrong; a dense Gray-code fit reports a residual
    that says whether the model is adequate — an affine rms of several px on a
    well-decoded frame means the optics are not affine, not that it is noisy.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if len(src) != len(dst):
        raise ValueError(f"{len(src)} src points vs {len(dst)} dst")
    if model not in ("affine", "homography"):
        raise ValueError(f"model must be affine/homography, got {model!r}")
    need = 3 if model == "affine" else 4
    if len(src) < need:
        raise ValueError(f"{model} needs >= {need} points, got {len(src)}")
    fit = _affine if model == "affine" else _homography

    keep = np.ones(len(src), bool)
    M = fit(src, dst)
    for _ in range(max(0, iters)):
        err = np.hypot(*(apply_transform(M, src) - dst).T)
        rms = float(np.sqrt(np.mean(err[keep] ** 2)))
        if rms <= 0:
            break
        nk = keep & (err <= reject_sigma * rms)
        if nk.sum() < need or nk.sum() == keep.sum():
            break
        keep = nk
        M = fit(src[keep], dst[keep])
    err = np.hypot(*(apply_transform(M, src) - dst).T)
    return M, float(np.sqrt(np.mean(err[keep] ** 2))), keep


# ══════════════════════════════════════════════════════════════════════════════
#  Using it
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DmdCalibration:
    """A measured DMD↔camera registration, and what it took to get it.

    The residual and point count are stored alongside the matrix: a transform
    with no provenance cannot be judged later, and "rms 0.4 px over 4100 points"
    is the difference between trusting it and re-running it.
    """
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "homography"
    rms_px:     float = 0.0
    n_points:   int = 0
    created:    str = ""
    notes:      str = ""

    @property
    def dmd_to_cam(self) -> np.ndarray:
        return np.linalg.inv(self.cam_to_dmd)

    # ── the accessible area ──────────────────────────────────────────────────
    def accessible(self, pts: np.ndarray) -> np.ndarray:
        """Which camera points the DMD can actually illuminate.

        Answered by mapping into mirror space and bounds-checking, not by a
        polygon test — the transform already knows the shape of the field, and
        under keystone that shape is not a rectangle.
        """
        d = apply_transform(self.cam_to_dmd, np.atleast_2d(pts))
        w, h = self.dmd_size
        return ((d[:, 0] >= 0) & (d[:, 0] <= w - 1)
                & (d[:, 1] >= 0) & (d[:, 1] <= h - 1))

    def accessible_mask(self, shape: tuple[int, int]) -> np.ndarray:
        """(H, W) bool: the camera pixels an ROI may legally cover."""
        h, w = shape
        yy, xx = np.mgrid[:h, :w]
        flat = self.accessible(np.column_stack((xx.ravel(), yy.ravel())))
        return flat.reshape(h, w)

    def accessible_corners(self) -> np.ndarray:
        """The DMD field's four corners in camera px, for drawing its outline."""
        w, h = self.dmd_size
        return apply_transform(
            self.dmd_to_cam,
            np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float))

    # ── persistence ──────────────────────────────────────────────────────────
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
                   model=d.get("model", "homography"),
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


class CalibrationError(RuntimeError):
    """The sweep could not be registered — with a reason worth reading."""


def run_calibration(project: Callable[[np.ndarray], None],
                    grab: Callable[[], np.ndarray],
                    dmd_size: tuple[int, int], *,
                    model: str = "homography",
                    square: int = 64,
                    step: int = 8,
                    min_modulation: float = MIN_MODULATION,
                    log: Callable[[str], None] = print) -> DmdCalibration:
    """Project a sweep, image it, and return the registration.

    `project(frame)` displays one device-sized frame; `grab()` returns the
    camera's view of whatever is currently displayed. Taking both as callables
    keeps this testable against a simulated camera — the whole pipeline can be
    held to a transform we chose before any light is emitted — and keeps the
    hardware out of the algorithm (§5b A3's lesson, applied up front).

    **The caller owns the actuation.** By the time this is called the decision
    to emit light has been made; it projects on every plane.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])

    def shot(frame: np.ndarray) -> np.ndarray:
        project(frame)
        return np.asarray(grab(), dtype=np.float64)

    # 1. one complementary pair: does the projector reach the camera at all?
    log("[dmd-calib] field extent (1 pair)")
    a, b = checkerboard_pair(w, h, square)
    ia, ib = shot(a), shot(b)
    cam_shape = ia.shape
    lit = field_mask(ia, ib, min_modulation=min_modulation)
    frac = float(lit.mean())
    box = bounding_box(lit)
    log(f"[dmd-calib] {100 * frac:.1f}% of the frame is modulated, bbox {box}")
    if frac < 0.01:
        raise CalibrationError(
            "the projector does not modulate the camera at all — check that "
            "the DMD is displaying, the illumination is on, the camera is "
            "exposing, and that the two fields overlap")

    # 2. Gray sweep
    planes, nbx, nby = gray_planes(w, h)
    log(f"[dmd-calib] {2 * len(planes)} exposures ({nbx} x-bits, {nby} y-bits)")
    on, off = [], []
    for i, p in enumerate(planes):
        on.append(shot(p))
        off.append(shot((255 - p).astype(np.uint8)))
    dx, dy, valid = decode(on, off, nbx, nby, min_modulation=min_modulation)
    log(f"[dmd-calib] {100 * valid.mean():.1f}% of pixels decoded")

    cam, dmd = correspondences(dx, dy, valid, step=step)
    if len(cam) < 20:
        raise CalibrationError(
            f"only {len(cam)} usable correspondences — too few to register. "
            f"Check focus and exposure: every plane must modulate a pixel for "
            f"it to count.")

    M, rms, keep = fit_transform(cam, dmd, model=model)
    log(f"[dmd-calib] {model}: rms {rms:.3f} px over {int(keep.sum())} inliers")
    return DmdCalibration(
        cam_to_dmd=M, dmd_size=(w, h),
        cam_size=(int(cam_shape[1]), int(cam_shape[0])),
        model=model, rms_px=rms, n_points=int(keep.sum()),
        created=datetime.now().isoformat(timespec="seconds"),
        notes=f"field {100 * frac:.1f}% of frame, bbox {box}")


def mask_from_roi(roi_cam: np.ndarray, dmd_to_cam: np.ndarray,
                  width: int, height: int) -> np.ndarray:
    """Camera-space ROI mask → the DMD frame that illuminates it.

    Iterates over *mirrors* and asks where each lands, rather than pushing ROI
    pixels forward: a forward map leaves holes wherever the DMD is coarser than
    the camera, and a mask with holes is a stimulus with holes.
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
