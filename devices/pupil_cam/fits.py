"""
Circle and ellipse fits — the bottom of the pupil pipeline.

Pure functions over edge points: no image, no state, no Qt. Kept apart from
`tracking.py` because they are separately testable and separately wrong — a fit
3 px out still draws a plausible circle on the preview and shows up as noise in
an analysis months later. `tests/test_pupil_fits.py` pins each one against the
property it was chosen for.

Taubin is the algebraic fit; `fit_circle_robust` wraps it in RANSAC plus
iterative outlier rejection, which drops lashes, glints and lid crossings.
"""
from __future__ import annotations

import numpy as np


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


def _ransac_circle(x, y, tol: float, trials: int = 48):
    """Largest inlier set over random 3-point circumcircles, or None.

    Deterministically seeded — the same frame must always give the same fit, or
    a re-analysis of a recorded session won't reproduce. All `trials`
    circumcircles and their (trials, n) residuals are computed at once: at 48
    trials over 64 rays, per-trial numpy calls cost more than the arithmetic.
    Ties go to the earliest trial, as the loop this replaced did.
    """
    n = x.size
    if n < 4:
        return None
    rng = np.random.default_rng(20250730)
    tri = rng.integers(0, n, size=(trials, 3))
    tri = tri[(tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2])
              & (tri[:, 0] != tri[:, 2])]
    if not len(tri):
        return None

    ax, ay = x[tri[:, 0]], y[tri[:, 0]]
    bx, by = x[tri[:, 1]], y[tri[:, 1]]
    px, py = x[tri[:, 2]], y[tri[:, 2]]
    den = 2.0 * (ax * (by - py) + bx * (py - ay) + px * (ay - by))
    ok = np.abs(den) >= 1e-9                      # collinear triples
    if not ok.any():
        return None
    ax, ay, bx, by, px, py, den = (v[ok] for v in
                                   (ax, ay, bx, by, px, py, den))

    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, px * px + py * py
    ux = (a2 * (by - py) + b2 * (py - ay) + c2 * (ay - by)) / den
    uy = (a2 * (px - bx) + b2 * (ax - px) + c2 * (bx - ax)) / den
    r = np.hypot(ax - ux, ay - uy)

    inl = np.abs(np.hypot(x - ux[:, None], y - uy[:, None])
                 - r[:, None]) <= tol
    counts = inl.sum(axis=1)
    best = int(np.argmax(counts))
    return inl[best] if counts[best] else None


def fit_circle_robust(x, y, sigma_k: float = 2.5, iters: int = 4,
                      min_points: int = 5, tol_floor: float = 0.75,
                      ransac_tol: float = 2.0):
    """
    Circle fit with iterative MAD-based outlier rejection → (cx, cy, r, keep).

    The edge points are ordered by ray and mostly correct, so a few reweighting
    passes converge — lashes and lid crossings fall outside 2.5·MAD in one or
    two. `tol_floor` stops a near-perfect fit rejecting good points on its own
    numerical noise; raise it when the shape is only approximately a circle.
    """
    n = x.size
    keep = np.ones(n, dtype=bool)
    best = None

    # Pass A — RANSAC. A single Taubin fit is dragged by gross outliers (a lash
    # arc, a lid crossing) and MAD rejection started from that biased fit
    # settles on the wrong circle. Consensus over random circumcircles is
    # indifferent to how far the outliers are.
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

    # Warm-up: peel the worst residuals before trusting a MAD. Rays latched
    # onto a glint are few but far out, and measured against the fit they
    # contaminated they inflate the MAD enough to mask their own rejection —
    # the fit then quietly shrinks with every point still an inlier.
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

    # Axes and orientation from the eigendecomposition of the centred conic
    # (a·u² + 2b2·u·v + c·v² + Fp = 0; the semi-axis along eigenvector i is
    # sqrt(-Fp/lambda_i)). Pairing each semi-axis with its own eigenvector by
    # construction is what stops the angle drifting 90° out of phase — picking
    # the major axis off the discriminant's roots is easy to get backwards.
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
