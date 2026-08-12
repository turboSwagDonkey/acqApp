"""
Pupil detection — a port of LabVIEW's IMAQ **Find Circular Edge**.

The rig's LabVIEW pipeline defines a ring-shaped (annular) search region around
an estimated pupil centre, casts a fan of search lines (rays) outward through
that annulus, finds the intensity edge along each ray (dark pupil → brighter
iris), and least-squares-fits a circle to the resulting edge points.  This module
does the same thing:

    1. sample the annulus along `n_rays` radial lines   (_sample_annulus)
    2. per ray, take the strongest 1-D intensity gradient within the band,
       refined to sub-pixel by a parabola on the gradient peak   (_edges_along_rays)
    3. robust least-squares circle fit with iterative outlier rejection, so
       eyelashes / corneal glints / eyelid crossings drop out   (fit_circle_robust)
    4. optionally re-fit an ellipse to the surviving inliers, for an off-axis eye

Step 1–3 are re-run `refine_iters` times, each pass re-centring the annulus on
the previous fit, so a coarse seed converges.

Pure numpy — no cv2 (it has no wheels for the 3.14 venv).  scipy is used only
for the coarse seed's connected-component labelling, with a fallback if absent.

Entry points
------------
detect(frame, threshold, min_r, max_r, ...)   stateless, per-frame.  Unchanged
                                              signature — main.py/_toy.py-safe.
PupilTracker                                  stateful; seeds each frame's
                                              annulus from the previous fit.
                                              Cheaper and steadier on a video
                                              stream — prefer it for live use.
find_circular_edge(...)                       the IMAQ primitive itself, if you
                                              want to drive the annulus yourself.

Angle convention: 0 rad = +x (image right), increasing toward +y (image *down*,
row-major).  So 90° is the bottom of the image and 270° the top — that is what
`exclude_deg` takes, e.g. eyelids at ``[(60, 120), (240, 300)]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

# Longest side the coarse seed's distance transform is allowed to run on; a
# larger component is decimated to fit (see coarse_seed).
_SEED_MAX_PX = 256

__all__ = [
    "PupilResult", "detect", "find_circular_edge", "PupilTracker",
    "coarse_seed", "fit_circle_taubin", "fit_circle_robust", "fit_ellipse",
]


@dataclass
class PupilResult:
    center_x:   float | None        # px
    center_y:   float | None        # px
    radius:     float | None        # px (mean of semi-axes for an ellipse)
    confidence: float = 0.0         # 0–1

    # ── extras (all defaulted, so PupilResult(cx, cy, r, conf) still works) ──
    axes:    tuple[float, float] | None = None   # (semi-major, semi-minor) px
    angle:   float | None = None                 # ellipse rotation, degrees
    edge_x:  np.ndarray | None = None            # edge point found on each ray
    edge_y:  np.ndarray | None = None
    inliers: np.ndarray | None = None            # bool mask into edge_x/edge_y
    rms:     float | None = None                 # fit residual, px
    n_rays:  int = 0                             # rays that yielded an edge

    @property
    def found(self) -> bool:
        return self.radius is not None


# ══════════════════════════════════════════════════════════════════════════════
#  Annulus sampling — the "search lines through the ROI" half of the IMAQ VI
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
#  Fitting
# ══════════════════════════════════════════════════════════════════════════════

def fit_circle_taubin(x: np.ndarray, y: np.ndarray):
    """
    Taubin algebraic circle fit → (cx, cy, r), or None.

    Taubin rather than the naive Kåsa fit because eyelids routinely leave us
    with a partial arc, and Kåsa biases the radius badly on short arcs.
    """
    if x.size < 3:
        return None
    mx, my = float(x.mean()), float(y.mean())
    u, v = x - mx, y - my
    z = u * u + v * v

    Mz = z.mean()
    Mxx, Myy, Mxy = (u * u).mean(), (v * v).mean(), (u * v).mean()
    Mxz, Myz, Mzz = (u * z).mean(), (v * z).mean(), (z * z).mean()

    cov_xy = Mxx * Myy - Mxy * Mxy
    var_z = Mzz - Mz * Mz
    a3 = 4.0 * Mz
    a2 = -3.0 * Mz * Mz - Mzz
    a1 = var_z * Mz + 4.0 * cov_xy * Mz - Mxz * Mxz - Myz * Myz
    a0 = Mxz * (Mxz * Myy - Myz * Mxy) + Myz * (Myz * Mxx - Mxz * Mxy) - var_z * cov_xy
    a22, a33 = a2 + a2, a3 + a3 + a3

    # Newton on the characteristic polynomial, started at 0 (Chernov)
    xn, yn = 0.0, 1e20
    for _ in range(40):
        yo = yn
        yn = a0 + xn * (a1 + xn * (a2 + xn * a3))
        if abs(yn) > abs(yo):
            break
        dy = a1 + xn * (a22 + xn * a33)
        if dy == 0.0:
            break
        xo, xn = xn, xn - yn / dy
        if abs((xn - xo) / xn if xn else xn - xo) < 1e-12:
            break
        if xn < 0:
            xn = 0.0
            break

    det = xn * xn - xn * Mz + cov_xy
    if abs(det) < 1e-12:
        return None
    ux = (Mxz * (Myy - xn) - Myz * Mxy) / det / 2.0
    uy = (Myz * (Mxx - xn) - Mxz * Mxy) / det / 2.0
    r = float(np.sqrt(ux * ux + uy * uy + Mz))
    if not np.isfinite(r):
        return None
    return float(ux + mx), float(uy + my), r


def _circle_through_3(x, y):
    """Circumcircle of three points → (cx, cy, r), or None if collinear."""
    ax, ay, bx, by, cx, cy = x[0], y[0], x[1], y[1], x[2], y[2]
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, float(np.hypot(ax - ux, ay - uy))


def _ransac_circle(x, y, tol: float, trials: int = 48):
    """Largest inlier set over random 3-point circumcircles, or None.

    Deterministically seeded — the same frame must always give the same fit,
    or a re-analysis of a recorded session won't reproduce.
    """
    n = x.size
    if n < 4:
        return None
    rng = np.random.default_rng(20250730)
    tri = rng.integers(0, n, size=(trials, 3))
    best_keep, best_n = None, 0
    for t in tri:
        if t[0] == t[1] or t[1] == t[2] or t[0] == t[2]:
            continue
        f = _circle_through_3(x[t], y[t])
        if f is None:
            continue
        cx, cy, r = f
        inl = np.abs(np.hypot(x - cx, y - cy) - r) <= tol
        c = int(inl.sum())
        if c > best_n:
            best_n, best_keep = c, inl
    return best_keep


def fit_circle_robust(x, y, sigma_k: float = 2.5, iters: int = 4,
                      min_points: int = 5, tol_floor: float = 0.75,
                      ransac_tol: float = 2.0):
    """
    Circle fit with iterative MAD-based outlier rejection → (cx, cy, r, keep).

    Cheaper and more stable here than RANSAC: the edge points are already
    ordered by ray and mostly correct, so a few reweighting passes converge —
    eyelashes and eyelid crossings fall outside 2.5·MAD within one or two.

    `tol_floor` keeps a near-perfect fit from rejecting good points on its own
    numerical noise; raise it when the shape is only approximately a circle
    (see the ellipse pre-pass in find_circular_edge).
    """
    n = x.size
    keep = np.ones(n, dtype=bool)
    best = None

    # Pass A — RANSAC. A single Taubin fit is badly dragged by gross outliers
    # (an eyelash arc, an eyelid crossing), and MAD rejection started from that
    # biased fit happily settles on the wrong circle. Consensus over random
    # 3-point circumcircles is indifferent to how far the outliers are, so
    # pass B gets a starting estimate worth refining.
    cons = _ransac_circle(x, y, tol=max(2.0 * tol_floor, ransac_tol))
    if cons is not None and cons.sum() >= min_points:
        keep = cons

    # Pass B — MAD rejection, scored over *all* points so anything pass A
    # left out is re-admitted once the fit is trustworthy.
    for _ in range(iters):
        if keep.sum() < min_points:
            break
        fit = fit_circle_taubin(x[keep], y[keep])
        if fit is None:
            break
        best = fit
        cx, cy, r = fit
        d = np.hypot(x - cx, y - cy) - r
        med = np.median(d[keep])
        mad = 1.4826 * np.median(np.abs(d[keep] - med))
        tol = max(sigma_k * mad, tol_floor)
        new = np.abs(d - med) <= tol
        if new.sum() < min_points or np.array_equal(new, keep):
            break
        keep = new
    if best is None:
        return None
    return best[0], best[1], best[2], keep


def _ellipse_radial_residual(x, y, ell) -> np.ndarray:
    """Signed distance (px) from each point to the ellipse, along its own ray."""
    cx, cy, a, b, ang = ell
    t = np.radians(ang)
    u = (x - cx) * np.cos(t) + (y - cy) * np.sin(t)
    v = -(x - cx) * np.sin(t) + (y - cy) * np.cos(t)
    r_pt = np.hypot(u, v)
    phi = np.arctan2(v, u)
    r_el = (a * b) / np.sqrt((b * np.cos(phi)) ** 2 + (a * np.sin(phi)) ** 2)
    return r_pt - r_el


def fit_ellipse_robust(x, y, sigma_k: float = 2.5, iters: int = 4,
                       min_points: int = 8, tol_floor: float = 0.75):
    """
    Ellipse fit with MAD rejection on the *ellipse's own* radial residual
    → (ell, keep), or None.

    Rejecting on circle residuals instead would throw away exactly the points
    that define an elongated pupil — the major- and minor-axis extremes are the
    furthest from any circle through the rest.
    """
    n = x.size
    keep = np.ones(n, dtype=bool)
    best = None

    # Warm-up: peel the worst residuals before trusting a MAD. Rays that latch
    # onto a corneal glint instead of the pupil rim are few but far out, and
    # measured against the fit they themselves contaminated they inflate the
    # MAD enough to mask their own rejection — the fit then quietly shrinks
    # with every point still marked an inlier.
    frac = 1.0
    for _ in range(2):
        ell = fit_ellipse(x[keep], y[keep])
        if ell is None:
            break
        best = ell
        frac *= 0.85
        m = max(min_points, int(round(n * frac)))
        if m >= n:
            break
        d = np.abs(_ellipse_radial_residual(x, y, ell))
        keep = np.zeros(n, dtype=bool)
        keep[np.argsort(d)[:m]] = True

    # MAD rejection, scored over all points so the warm-up's over-trimming is
    # re-admitted once the fit is trustworthy.
    for _ in range(iters):
        if keep.sum() < min_points:
            break
        ell = fit_ellipse(x[keep], y[keep])
        if ell is None:
            break
        best = ell
        d = _ellipse_radial_residual(x, y, ell)
        med = np.median(d[keep])
        mad = 1.4826 * np.median(np.abs(d[keep] - med))
        tol = max(sigma_k * mad, tol_floor)
        new = np.abs(d - med) <= tol
        if new.sum() < min_points or np.array_equal(new, keep):
            break
        keep = new
    if best is None:
        return None
    return best, keep


def fit_ellipse(x: np.ndarray, y: np.ndarray):
    """
    Fitzgibbon direct least-squares ellipse (Halir–Flusser form)
    → (cx, cy, semi_major, semi_minor, angle_deg), or None.
    """
    if x.size < 6:
        return None
    mx, my = float(x.mean()), float(y.mean())
    s = float(max(np.abs(x - mx).max(), np.abs(y - my).max()))
    if s <= 0:
        return None
    u, v = (x - mx) / s, (y - my) / s

    D1 = np.column_stack((u * u, u * v, v * v))
    D2 = np.column_stack((u, v, np.ones_like(u)))
    S1, S2, S3 = D1.T @ D1, D1.T @ D2, D2.T @ D2
    try:
        T = -np.linalg.solve(S3, S2.T)
    except np.linalg.LinAlgError:
        return None
    M = S1 + S2 @ T
    M = np.array([M[2] / 2.0, -M[1], M[0] / 2.0])
    try:
        _, evecs = np.linalg.eig(M)     # only the eigenvectors matter here
    except np.linalg.LinAlgError:
        return None

    cond = 4.0 * evecs[0] * evecs[2] - evecs[1] ** 2
    idx = np.argmax(np.where(cond > 0, 1.0, -np.inf))
    if not np.isfinite(cond[idx]) or cond[idx] <= 0:
        return None
    a1 = np.real(evecs[:, idx])
    a, b, c = a1
    d, e, f = np.real(T @ a1)
    b2, d2, e2 = b / 2.0, d / 2.0, e / 2.0

    den = b2 * b2 - a * c
    if abs(den) < 1e-12:
        return None
    x0 = (c * d2 - b2 * e2) / den
    y0 = (a * e2 - b2 * d2) / den

    # Axes and orientation from the eigendecomposition of the centred quadratic
    # form. Branching on which root of the discriminant is the major axis is
    # easy to get backwards — this pairs each semi-axis with its own
    # eigenvector by construction, so the angle can't drift 90° out of phase.
    #   centred conic:  a·u² + 2b2·u·v + c·v² + Fp = 0
    #   in eigen-coords: semi-axis along eigenvector i is sqrt(-Fp / lambda_i)
    Fp = f + d2 * x0 + e2 * y0
    evals, vecs = np.linalg.eigh(np.array([[a, b2], [b2, c]], dtype=float))
    if np.any(np.abs(evals) < 1e-12):
        return None
    sq = -Fp / evals
    if np.any(sq <= 0):
        return None                     # not a real ellipse (hyperbola/empty)
    semi = np.sqrt(sq)

    i_maj = int(np.argmax(semi))
    semi_major = float(semi[i_maj])
    semi_minor = float(semi[1 - i_maj])
    vx, vy = vecs[0, i_maj], vecs[1, i_maj]
    theta = float(np.arctan2(vy, vx))

    return (float(x0 * s + mx), float(y0 * s + my),
            float(semi_major * s), float(semi_minor * s),
            float(np.degrees(theta) % 180.0))


# ══════════════════════════════════════════════════════════════════════════════
#  The IMAQ primitive
# ══════════════════════════════════════════════════════════════════════════════

def find_circular_edge(frame: np.ndarray,
                       center: tuple[float, float],
                       r_inner: float,
                       r_outer: float,
                       *,
                       n_rays: int = 64,
                       polarity: str = "rising",
                       min_strength: float = 4.0,
                       smooth_sigma: float = 1.5,
                       exclude_deg: Sequence[tuple[float, float]] = (),
                       fit: str = "circle",
                       sigma_k: float = 2.5,
                       min_rays: int = 8,
                       refine_iters: int = 2,
                       samples_per_px: float = 2.0) -> PupilResult:
    """
    Locate the pupil boundary in the annulus around `center`.

    Parameters mirror the IMAQ VI's controls:
      center, r_inner, r_outer   the annular ROI
      n_rays                     number of search lines
      polarity                   edge polarity (see _edges_along_rays)
      min_strength               minimum |gradient| (grey levels per px) to
                                 accept a ray's edge — the IMAQ "edge strength"
      exclude_deg                angular sectors to skip, e.g. eyelids
      fit                        "circle" or "ellipse". The ellipse is fitted to
                                 every ray hit with its own robust rejection —
                                 the circle pass only sanity-checks it, since no
                                 circle consensus can hold both axis extremes.
      refine_iters               re-centre the annulus on the fit and repeat

    Returns a PupilResult; `radius is None` means no confident detection.
    """
    if frame.ndim != 2:
        raise ValueError(f"expected a 2-D grayscale frame, got shape {frame.shape}")
    if r_outer <= r_inner:
        return PupilResult(None, None, None, 0.0)

    angles = _ray_angles(n_rays, exclude_deg)
    if angles.size < min_rays:
        return PupilResult(None, None, None, 0.0)

    cx, cy = float(center[0]), float(center[1])
    half = (r_outer - r_inner) / 2.0
    result = PupilResult(None, None, None, 0.0)

    for it in range(max(1, refine_iters)):
        values, r0, step = _sample_annulus(frame, cx, cy, r_inner, r_outer,
                                           angles, samples_per_px)
        r_edge, strength, hit = _edges_along_rays(
            values, r0, step, polarity, min_strength, smooth_sigma)

        if hit.sum() < min_rays:
            return result

        sel = hit.copy()
        if fit != "ellipse":
            # Pre-reject in radius space before any fitting. The annulus is
            # already roughly concentric with the pupil, so true edge radii
            # cluster tightly while an eyelid crossing scatters — and dropping
            # those first stops them from dragging the initial fit somewhere
            # the residual-based rejection can't recover from. Skipped for
            # ellipse mode, where radius genuinely varies with angle.
            rr = r_edge[sel]
            med_r = np.median(rr)
            mad_r = 1.4826 * np.median(np.abs(rr - med_r))
            tol_r = max(3.0 * mad_r, 0.15 * med_r)
            near = np.abs(r_edge - med_r) <= tol_r
            if (sel & near).sum() >= min_rays:
                sel &= near

        ex = cx + r_edge[sel] * np.cos(angles[sel])
        ey = cy + r_edge[sel] * np.sin(angles[sel])

        # For ellipse mode the circle pass only exists to kill gross outliers,
        # so give it a tolerance wide enough to keep the axis extremes — a
        # tight circle consensus would discard exactly those.
        scale = float(np.median(r_edge[sel]))
        wide = fit == "ellipse"
        rob = fit_circle_robust(
            ex, ey, sigma_k=sigma_k, min_points=min_rays,
            tol_floor=(0.75 if not wide else 0.45 * scale),
            ransac_tol=(max(2.0, 0.06 * scale) if not wide else 0.6 * scale))
        if rob is None:
            return result
        fcx, fcy, fr, keep = rob
        if keep.sum() < min_rays:
            return result

        resid = np.hypot(ex[keep] - fcx, ey[keep] - fcy) - fr
        rms = float(np.sqrt(np.mean(resid ** 2)))

        axes = angle = None
        radius = fr
        if fit == "ellipse":
            # Fit the ellipse to *all* the ray hits, not to the circle pass's
            # inliers: no circle consensus can hold both axis extremes of an
            # elongated pupil, so reusing `keep` here silently truncates the
            # major axis. fit_ellipse_robust does its own rejection, scored on
            # ellipse residuals, which is the only kind that means anything
            # once the shape isn't round.
            rob_e = fit_ellipse_robust(ex, ey, sigma_k=sigma_k,
                                       min_points=max(6, min_rays))
            if rob_e is not None:
                ell, ekeep = rob_e
                ecx, ecy, smaj, smin, ang = ell
                # sanity-check the ellipse against the circle it refines
                if (np.hypot(ecx - fcx, ecy - fcy) < 1.0 * fr
                        and 0.25 < smin / max(smaj, 1e-6) <= 1.0
                        and 0.4 * fr < 0.5 * (smaj + smin) < 2.5 * fr
                        and ekeep.sum() >= max(6, min_rays)):
                    fcx, fcy = ecx, ecy
                    axes, angle = (smaj, smin), ang
                    radius = 0.5 * (smaj + smin)
                    keep = ekeep
                    rms = float(np.sqrt(np.mean(
                        _ellipse_radial_residual(ex[keep], ey[keep], ell) ** 2)))

        # confidence: how much of the ring agreed, discounted by fit residual
        conf = float(np.clip(keep.sum() / angles.size, 0.0, 1.0) * np.exp(-rms / 2.0))

        result = PupilResult(
            float(fcx), float(fcy), float(radius), conf,
            axes=axes, angle=angle,
            edge_x=ex, edge_y=ey, inliers=keep, rms=rms, n_rays=int(keep.sum()),
        )

        # re-centre the annulus on this fit and go round again
        moved = np.hypot(fcx - cx, fcy - cy)
        cx, cy = float(fcx), float(fcy)
        r_inner, r_outer = max(1.0, radius - half), radius + half
        if moved < 0.25 and it > 0:
            break

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Coarse seed — "estimate a rough pupil centre" before the annulus exists
# ══════════════════════════════════════════════════════════════════════════════

def coarse_seed(frame: np.ndarray, threshold: int = 60,
                min_r: int = 10, max_r: int = 80, *, bright: bool = False):
    """
    Rough (cx, cy, r) for the first annulus, or None.

    Takes the largest thresholded blob, then its **deepest interior point** via
    a distance transform rather than a centroid.  That matters: dark eyelashes
    and eyelid margins routinely touch the pupil and merge into one component,
    and a centroid of that merged blob lands off the pupil entirely, whereas the
    inscribed-circle centre stays put.  The EDT peak value is also a far better
    radius estimate than √(area/π) for a merged blob.

    Only ever used to *place* the annulus — the ray/fit stage does the real
    work, so this just has to land inside the pupil.
    """
    mask = (frame > threshold) if bright else (frame < threshold)
    n = int(mask.sum())
    if n == 0:
        return None
    # A pupil cannot be half the sensor. When it is — lens cap on, illumination
    # off, or simply a badly set threshold — labelling and running a distance
    # transform over a frame-sized blob costs ~100 ms and can only ever be
    # rejected at the end anyway. Bail out on the cheap test instead, so a dark
    # stretch doesn't stall the display loop on every re-seed.
    if n > 0.5 * frame.size:
        return None

    try:
        from scipy import ndimage
    except ImportError:                     # no scipy: plain centroid fallback
        ys, xs = np.nonzero(mask)
        r = float(np.sqrt(n / np.pi))
        if not (0.4 * min_r < r < 2.5 * max_r):
            return None
        return float(xs.mean()), float(ys.mean()), float(np.clip(r, min_r, max_r))

    lab, nlab = ndimage.label(mask)
    if nlab == 0:
        return None
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0

    # Score the largest few components rather than blindly taking the biggest:
    # a dark eyelid margin or a shadowed orbit is often larger than the pupil,
    # but it is nowhere near as round. Circularity = area / (pi*r_inscribed^2),
    # which is ~1 for a disc and grows without bound for an elongated band.
    order = np.argsort(sizes)[::-1][:4]
    slices = ndimage.find_objects(lab)
    best = None
    for li in order:
        li = int(li)
        if li == 0 or sizes[li] == 0:
            break
        sl = slices[li - 1]
        if sl is None:
            continue
        # fill holes first: the IR corneal glint sits inside the pupil and
        # punches a hole that would otherwise shrink the inscribed circle
        comp = ndimage.binary_fill_holes(lab[sl] == li)

        # Bound the distance transform's cost. It is the expensive step here and
        # scales with the component's bounding box — which for a degenerate mask
        # (lens cap on, so every pixel is below threshold) is the whole sensor,
        # turning a re-seed into ~200 ms. Decimating is safe: the seed only has
        # to land inside the pupil, and find_circular_edge re-centres from there.
        step = max(1, int(np.ceil(max(comp.shape) / _SEED_MAX_PX)))
        if step > 1:
            comp = comp[::step, ::step]

        edt = ndimage.distance_transform_edt(np.pad(comp, 1))[1:-1, 1:-1]
        iy, ix = np.unravel_index(int(np.argmax(edt)), edt.shape)
        r_dec = float(edt[iy, ix])
        r = r_dec * step
        if r < 0.4 * min_r or r > 2.5 * max_r:
            continue
        # circularity is scale-invariant, so measure it on the decimated mask
        circ = float(comp.sum()) / max(np.pi * r_dec * r_dec, 1e-6)
        score = abs(np.log(max(circ, 1e-6)))          # 0 == a perfect disc
        cand = (score,
                float(ix * step + sl[1].start),
                float(iy * step + sl[0].start), r)
        if best is None or cand[0] < best[0]:
            best = cand

    if best is None:
        return None
    _, cx, cy, r = best
    return cx, cy, float(np.clip(r, min_r, max_r))


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

# Annulus half-width as a fraction of the seed radius: the band spans
# r*(1-BAND) … r*(1+BAND), so the true edge sits comfortably inside it.
# Ellipse mode needs a much wider band: the seed radius comes from an inscribed
# circle, which for an elongated pupil is the semi-*minor* axis, so a symmetric
# band would never reach the major-axis edge.
_BAND = 0.55
_BAND_ELLIPSE = (0.35, 2.9)          # (inner, outer) as multiples of the seed r


def _band(r: float, fit: str, max_r: float) -> tuple[float, float]:
    if fit == "ellipse":
        lo, hi = _BAND_ELLIPSE
        return max(1.0, r * lo), min(r * hi, max_r * 2.0)
    return max(1.0, r * (1.0 - _BAND)), r * (1.0 + _BAND)


def detect(frame: np.ndarray,
           threshold: int = 60,
           min_r: int = 10,
           max_r: int = 80,
           *,
           n_rays: int = 64,
           polarity: str = "rising",
           min_strength: float = 4.0,
           exclude_deg: Sequence[tuple[float, float]] = (),
           fit: str = "circle",
           seed: tuple[float, float, float] | None = None) -> PupilResult:
    """
    Stateless per-frame pupil detection: coarse seed → annular edge search →
    robust circle fit.  Signature-compatible with the old stub.

    `seed` is an optional (cx, cy, r) placing the annulus explicitly — the
    LabVIEW workflow, where the operator draws the annulus over the pupil once.
    Pass it whenever you have it; auto-seeding is only a bootstrap convenience
    and can be fooled by a dark eyelid margin larger than the pupil itself.

    For live video prefer `PupilTracker`, which seeds each annulus from the
    previous frame instead of re-thresholding every time.
    """
    if seed is None:
        # a "falling" edge means a bright pupil on a darker iris, so the seed
        # blob is the bright one — threshold the other way round
        seed = coarse_seed(frame, threshold, min_r, max_r,
                           bright=(polarity == "falling"))
        if seed is None:
            return PupilResult(None, None, None, 0.0)
    cx, cy, r = seed
    r_in, r_out = _band(r, fit, max_r)

    res = find_circular_edge(
        frame, (cx, cy), r_in, r_out,
        n_rays=n_rays, polarity=polarity, min_strength=min_strength,
        exclude_deg=exclude_deg, fit=fit, refine_iters=3,
    )
    if res.radius is not None and not (min_r < res.radius < max_r):
        # edge found but out of the allowed size band — report the position only
        return PupilResult(res.center_x, res.center_y, None, 0.0,
                           edge_x=res.edge_x, edge_y=res.edge_y,
                           inliers=res.inliers, rms=res.rms)
    return res


class PupilTracker:
    """
    Stateful pupil tracker for a live stream.

    Each frame's annulus is seeded from the previous good fit (the LabVIEW
    workflow: the operator seeds the annulus once, then it follows the pupil).
    After `max_lost` consecutive failures it falls back to `coarse_seed`.

    Thread note: not thread-safe — call `process()`/`configure()` from one
    thread. In the app that is the tracking thread (`track_worker.py`), which
    owns a tracker and queues the panel's edits into it between frames; _toy.py,
    being one device on one timer, drives it from the GUI thread directly.
    """

    def __init__(self, threshold: int = 60, min_r: int = 10, max_r: int = 80,
                 *, n_rays: int = 64, polarity: str = "rising",
                 min_strength: float = 4.0, smooth_sigma: float = 1.5,
                 exclude_deg: Sequence[tuple[float, float]] = (),
                 fit: str = "circle", min_confidence: float = 0.25,
                 max_lost: int = 5, max_jump: float = 0.5):
        self.threshold = threshold
        self.min_r = min_r
        self.max_r = max_r
        self.n_rays = n_rays
        self.polarity = polarity
        self.min_strength = min_strength
        self.smooth_sigma = smooth_sigma
        self.exclude_deg = exclude_deg
        self.fit = fit
        self.min_confidence = min_confidence
        self.max_lost = max_lost
        self.max_jump = max_jump          # allowed centre jump, × previous radius
        self.reset()

    def reset(self) -> None:
        self._last: tuple[float, float, float] | None = None   # cx, cy, r
        self._lost = 0

    def seed(self, cx: float, cy: float, r: float) -> None:
        """Place the annulus by hand (the LabVIEW operator workflow).

        Use this when the auto-seed picks the wrong dark region — tracking
        continues from here exactly as if the previous frame had fitted it.
        """
        self._last = (float(cx), float(cy), float(r))
        self._lost = 0

    @property
    def locked(self) -> bool:
        return self._last is not None

    # Changing any of these invalidates the current lock: the annulus was
    # placed under the old assumptions, so re-seed rather than let it drift.
    _RESEED_ON = frozenset({"threshold", "min_r", "max_r", "polarity", "fit"})

    def configure(self, **kw) -> None:
        """Cheap per-tick update from the settings panel."""
        reseed = False
        for k, v in kw.items():
            if not hasattr(self, k):
                raise AttributeError(f"PupilTracker has no option {k!r}")
            if getattr(self, k) != v:
                setattr(self, k, v)
                reseed |= k in self._RESEED_ON
        if reseed:
            self._last = None

    def process(self, frame: np.ndarray) -> PupilResult:
        seeded_from_last = self._last is not None
        if seeded_from_last:
            cx, cy, r = self._last
        else:
            seed = coarse_seed(frame, self.threshold, self.min_r, self.max_r,
                               bright=(self.polarity == "falling"))
            if seed is None:
                self._lost += 1
                return PupilResult(None, None, None, 0.0)
            cx, cy, r = seed

        r_in, r_out = _band(r, self.fit, self.max_r)
        res = find_circular_edge(
            frame, (cx, cy), r_in, r_out,
            n_rays=self.n_rays, polarity=self.polarity,
            min_strength=self.min_strength, smooth_sigma=self.smooth_sigma,
            exclude_deg=self.exclude_deg, fit=self.fit,
            refine_iters=2 if seeded_from_last else 3,
        )

        ok = (res.radius is not None
              and res.confidence >= self.min_confidence
              and self.min_r < res.radius < self.max_r)
        if ok and seeded_from_last:
            # reject a fit that teleported — usually an eyelid edge grabbed mid-blink
            ok = np.hypot(res.center_x - cx, res.center_y - cy) <= self.max_jump * r

        if ok:
            self._last = (res.center_x, res.center_y, res.radius)
            self._lost = 0
            return res

        self._lost += 1
        if self._lost >= self.max_lost:
            self._last = None          # give up on the seed; re-threshold next frame
        return PupilResult(None, None, None, 0.0,
                           edge_x=res.edge_x, edge_y=res.edge_y,
                           inliers=res.inliers, rms=res.rms)
