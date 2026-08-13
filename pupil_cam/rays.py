"""
Annulus sampling — the "search lines through the ROI" half of the IMAQ VI.

Casts a fan of rays outward through a ring-shaped region and finds the
intensity edge along each one. What `tracking.find_circular_edge` feeds its
fits. Pure numpy, no state.

Angle convention (shared with `tracking`): 0 rad = +x (image right), increasing
toward +y (image *down*, row-major), so 90 deg is the bottom of the image.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def _bilinear(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Bilinear sample `img` at float coords; NaN outside the image."""
    h, w = img.shape
    x0 = np.floor(xs).astype(np.intp)
    y0 = np.floor(ys).astype(np.intp)
    inside = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)

    x0c = np.clip(x0, 0, w - 2)
    y0c = np.clip(y0, 0, h - 2)
    fx = (xs - x0c).astype(np.float32)
    fy = (ys - y0c).astype(np.float32)

    im = img.astype(np.float32, copy=False)
    v00 = im[y0c,     x0c]
    v01 = im[y0c,     x0c + 1]
    v10 = im[y0c + 1, x0c]
    v11 = im[y0c + 1, x0c + 1]
    out = (v00 * (1 - fx) * (1 - fy) + v01 * fx * (1 - fy)
           + v10 * (1 - fx) * fy + v11 * fx * fy)
    return np.where(inside, out, np.nan)


def _ray_angles(n_rays: int, exclude_deg: Sequence[tuple[float, float]]) -> np.ndarray:
    """`n_rays` evenly spaced angles, minus any excluded sectors (degrees)."""
    ang = np.arange(n_rays, dtype=np.float64) * (2.0 * np.pi / n_rays)
    if not exclude_deg:
        return ang
    deg = np.degrees(ang)
    keep = np.ones(n_rays, dtype=bool)
    for lo, hi in exclude_deg:
        lo %= 360.0
        hi %= 360.0
        # a sector may wrap past 360°
        keep &= ~((deg >= lo) & (deg <= hi) if lo <= hi
                  else (deg >= lo) | (deg <= hi))
    return ang[keep]


def _sample_annulus(frame, cx, cy, r_in, r_out, angles, samples_per_px):
    """Unwrap the annulus → (values[n_rays, n_samples], r0, step)."""
    n_samples = max(5, int(round((r_out - r_in) * samples_per_px)) + 1)
    radii = np.linspace(r_in, r_out, n_samples)
    step = float(radii[1] - radii[0])
    xs = cx + radii[None, :] * np.cos(angles)[:, None]
    ys = cy + radii[None, :] * np.sin(angles)[:, None]
    return _bilinear(frame, xs, ys), float(radii[0]), step


def _smooth_rows(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur along axis 1, NaN-aware (normalised convolution)."""
    if sigma <= 0:
        return a
    rad = max(1, int(round(3.0 * sigma)))
    t = np.arange(-rad, rad + 1, dtype=np.float32)
    k = np.exp(-0.5 * (t / sigma) ** 2)
    k /= k.sum()

    good = np.isfinite(a)
    n = a.shape[1]
    pv = np.pad(np.where(good, a, 0.0).astype(np.float32), ((0, 0), (rad, rad)), mode="edge")
    pw = np.pad(good.astype(np.float32),                   ((0, 0), (rad, rad)), mode="edge")

    num = np.zeros((a.shape[0], n), dtype=np.float32)
    den = np.zeros((a.shape[0], n), dtype=np.float32)
    for i, ki in enumerate(k):
        num += ki * pv[:, i:i + n]
        den += ki * pw[:, i:i + n]
    return np.divide(num, den, out=np.full_like(num, np.nan), where=den > 0.35)


def _edges_along_rays(values, r0, step, polarity, min_strength, smooth_sigma):
    """
    Strongest gradient along each ray → (r_edge, strength, hit).

    `polarity` mirrors the IMAQ edge-polarity control:
      "rising"  dark→bright scanning outward (a dark pupil on a bright iris)
      "falling" bright→dark  (bright-pupil / retro-illumination setups)
      "any"     strongest transition either way
    """
    prof = _smooth_rows(values, smooth_sigma)
    grad = np.gradient(prof, step, axis=1)

    if polarity == "rising":
        score = grad
    elif polarity == "falling":
        score = -grad
    elif polarity == "any":
        score = np.abs(grad)
    else:
        raise ValueError(f"polarity must be rising/falling/any, got {polarity!r}")

    # kill non-finite samples and the two boundary columns (no parabola there,
    # and np.gradient uses a one-sided stencil at the ends)
    score = np.where(np.isfinite(score), score, -np.inf)
    score[:, 0] = -np.inf
    score[:, -1] = -np.inf

    rows = np.arange(score.shape[0])
    k = np.argmax(score, axis=1)
    peak = score[rows, k]
    hit = np.isfinite(peak) & (peak >= min_strength)

    # parabola through (k-1, k, k+1) for the sub-pixel peak. Rows that never
    # hit carry -inf neighbours, so zero them first rather than doing inf
    # arithmetic — delta is discarded for those rows anyway.
    kc = np.clip(k, 1, score.shape[1] - 2)
    fin = hit & np.isfinite(score[rows, kc - 1]) & np.isfinite(score[rows, kc + 1])
    sm = np.where(fin, score[rows, kc - 1], 0.0)
    sp = np.where(fin, score[rows, kc + 1], 0.0)
    pk = np.where(fin, peak, 0.0)
    den = sm - 2.0 * pk + sp
    safe = np.abs(den) > 1e-9
    delta = np.divide(0.5 * (sm - sp), den, out=np.zeros_like(den), where=safe)
    delta = np.clip(delta, -1.0, 1.0)

    return r0 + (kc + delta) * step, np.where(hit, peak, 0.0), hit
