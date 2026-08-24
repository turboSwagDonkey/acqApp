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

# "first" mode only: robust sigmas of a ray's own gradient noise an edge must
# clear on top of `min_strength`. Sensitive — 4.0 killed 19 of 64 good rays and
# broke the synthetic eyelid case; 1.5 satisfies that suite and the rig clip.
_NOISE_K = 1.5

# Minimum span, in px, over which an edge must not reverse to count as a step
# rather than a glint. `smooth_sigma` can be far smaller than any real edge.
_SUSTAIN_PX = 3.0


def _bilinear(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Bilinear sample `img` at float coords; NaN outside the image."""
    h, w = img.shape
    x0 = np.floor(xs).astype(np.intp)
    y0 = np.floor(ys).astype(np.intp)
    inside = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)

    x0c = np.clip(x0, 0, w - 2)
    y0c = np.clip(y0, 0, h - 2)
    x1c, y1c = x0c + 1, y0c + 1
    fx = (xs - x0c).astype(np.float32)
    fy = (ys - y0c).astype(np.float32)

    # Gather, then cast. Casting the frame would copy megabytes per call to read
    # a few thousand points, and this runs once per refinement pass per frame.
    v00 = img[y0c, x0c].astype(np.float32)
    v01 = img[y0c, x1c].astype(np.float32)
    v10 = img[y1c, x0c].astype(np.float32)
    v11 = img[y1c, x1c].astype(np.float32)
    gx, gy = 1.0 - fx, 1.0 - fy
    out = (v00 * gx + v01 * fx) * gy + (v10 * gx + v11 * fx) * fy
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


def _sample_annulus(frame, cx, cy, r_in, r_out, dirs, samples_per_px):
    """Unwrap the annulus → (values[n_rays, n_samples], r0, step).

    `dirs` is (cos, sin) per ray, precomputed by the caller — the annulus is
    re-sampled once per refinement pass and the angles never change.
    """
    ca, sa = dirs
    n_samples = max(5, int(round((r_out - r_in) * samples_per_px)) + 1)
    radii = np.linspace(r_in, r_out, n_samples)
    step = float(radii[1] - radii[0])
    xs = cx + radii[None, :] * ca[:, None]
    ys = cy + radii[None, :] * sa[:, None]
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


def _nanmedian_rows(a: np.ndarray) -> np.ndarray:
    """Row-wise nanmedian, keepdims. Same answer as `np.nanmedian(a, axis=1)`.

    Not that function: for rows shorter than ~600 numpy falls back to
    `_nanmedian_small`, which builds a **masked array** per call. Profiled on
    the rig clip that was ~30 % of the whole tracker. `np.sort` puts NaN last,
    so the valid count indexes straight to the middle.
    """
    s = np.sort(a, axis=1)
    n = np.count_nonzero(~np.isnan(a), axis=1)
    rows = np.arange(a.shape[0])
    # Even counts average the two middle values, as a median must. n == 0 hits
    # a trailing NaN and yields NaN, which is what nanmedian does too.
    med = 0.5 * (s[rows, (n - 1) // 2] + s[rows, n // 2])
    return med[:, None]


def _sustained(score: np.ndarray, width: int) -> np.ndarray:
    """Lowest score within `width` samples *after* each column.

    Distinguishes a step from a spike: after a real boundary the gradient decays
    towards zero, whereas a corneal glint is a bright bar whose rising edge is
    followed by an equally strong falling one. Padded with +inf on the right so
    the last columns are never rejected for running out of profile.
    """
    n = score.shape[1]
    pad = np.pad(score, ((0, 0), (0, width)), constant_values=np.inf)
    out = np.full_like(score, np.inf)
    for d in range(1, width + 1):
        out = np.minimum(out, pad[:, d:d + n])
    return out


def _edges_along_rays(values, r0, step, polarity, min_strength, smooth_sigma,
                      edge_select: str = "first"):
    """
    The pupil edge along each ray → (r_edge, strength, hit).

    `polarity` mirrors the IMAQ edge-polarity control:
      "rising"  dark→bright scanning outward (a dark pupil on a bright iris)
      "falling" bright→dark  (bright-pupil / retro-illumination setups)
      "any"     strongest transition either way

    `edge_select` decides *which* qualifying edge on the ray is the pupil:
      "first"      the innermost sustained one — right by construction, since
                   scanning outward from inside the pupil its rim is what you
                   meet first. The default.
      "strongest"  the largest gradient on the ray. Fine on a synthetic disc,
                   wrong as soon as anything outside the pupil out-contrasts its
                   edge: on real IR footage the orbit→fur margin is ~200 grey
                   levels against the pupil's ~30, so it wins every ray and the
                   fit lands on the eyelid.
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
    if edge_select == "strongest":
        k = np.argmax(score, axis=1)
        hit_row = np.ones(score.shape[0], dtype=bool)
    elif edge_select == "first":
        # Innermost means noise gets the first vote, so the floor has to rise
        # with each ray's own noise: a fixed `min_strength` lets a speckle
        # inside the pupil pass and the fit collapses inward. MAD, not sd — the
        # true edge would inflate an sd.
        sfin = np.where(np.isfinite(score), score, np.nan)
        # A ray wholly off-image is all-NaN; zero it so nanmedian has something
        # to chew on (`hit` rejects the row anyway).
        sfin = np.where(np.isfinite(sfin).any(axis=1, keepdims=True), sfin, 0.0)
        med = _nanmedian_rows(sfin)
        mad = 1.4826 * _nanmedian_rows(np.abs(sfin - med))
        floor = np.maximum(min_strength, _NOISE_K * np.nan_to_num(mad))

        # Local maxima at or above that floor…
        cand = np.zeros(score.shape, dtype=bool)
        cand[:, 1:-1] = ((score[:, 1:-1] >= score[:, :-2])
                         & (score[:, 1:-1] > score[:, 2:])
                         & (score[:, 1:-1] >= floor))
        # …that are not the leading edge of a spike. Half the peak is a wide
        # margin: a step's gradient decays to ~0, a glint's reverses to ~-peak.
        width = max(1, int(round(max(smooth_sigma, _SUSTAIN_PX)
                                 / max(step, 1e-6))))
        cand &= _sustained(score, width) > -0.5 * np.where(cand, score, 0.0)
        # First True per row; argmax on an all-False row gives 0, which `hit`
        # rejects anyway because column 0 is -inf.
        k = np.argmax(cand, axis=1)
        hit_row = cand.any(axis=1)
    else:
        raise ValueError(
            f"edge_select must be first/strongest, got {edge_select!r}")
    peak = score[rows, k]
    hit = hit_row & np.isfinite(peak) & (peak >= min_strength)

    # Parabola through (k-1, k, k+1) for the sub-pixel peak. Rows that never hit
    # carry -inf neighbours, so zero them rather than do inf arithmetic; their
    # delta is discarded anyway.
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
