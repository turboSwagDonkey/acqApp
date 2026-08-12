"""
The pupil fits, as pure functions — no Qt, no camera, no threads.

`fit_circle_taubin`, `fit_circle_robust` and `fit_ellipse` are the bottom of the
pupil pipeline: everything above them (annulus sampling, ray edges, the tracker)
is judged by what these return. They are also the part with no observable
behaviour of its own — a fit that is 3 px out still draws a plausible circle on
the preview, and the error only shows up as noise in an analysis months later.

Each is checked on geometry it must reproduce exactly, then on the property it
was chosen FOR, with a control that fails that property:

  * Taubin over the naive Kåsa fit — because eyelids leave short arcs, and Kåsa
    biases the radius badly on those. The control is a Kåsa fit on the same
    points.
  * robust over plain — because eyelashes and glints put a handful of edge
    points far off the rim. The control is `fit_circle_taubin` on the same
    contaminated set.
  * the ellipse's angle must belong to the semi-major axis, which is the easy
    thing to get 90 degrees out of phase.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_fits.py
"""
from __future__ import annotations

import sys

import numpy as np

from _harness import Report      # noqa: F401  (also puts acqApp on sys.path)

from acqApp.pupil_cam.tracking import (          # noqa: E402
    fit_circle_robust, fit_circle_taubin, fit_ellipse,
)

CX, CY, R = 163.5, 121.25, 37.75        # deliberately non-integer


def circle_pts(n: int, span_deg: float = 360.0, start_deg: float = 0.0,
               cx: float = CX, cy: float = CY, r: float = R):
    a = np.radians(start_deg + np.linspace(0.0, span_deg, n, endpoint=span_deg < 360))
    return cx + r * np.cos(a), cy + r * np.sin(a)


def ellipse_pts(n: int, cx: float, cy: float, a: float, b: float, deg: float):
    t = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    u, v = a * np.cos(t), b * np.sin(t)
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return cx + u * c - v * s, cy + u * s + v * c


def kasa(x, y):
    """The naive algebraic circle fit, as a control for Taubin's short-arc bias."""
    A = np.column_stack((x, y, np.ones_like(x)))
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    cx, cy = sol[0] / 2.0, sol[1] / 2.0
    return cx, cy, float(np.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0)))


def main() -> int:
    r = Report("pupil-fits")

    # ══ fit_circle_taubin ═════════════════════════════════════════════════════
    x, y = circle_pts(32)
    fit = fit_circle_taubin(x, y)
    if not r.check(fit is not None, "taubin: fits a full circle"):
        return r.finish()
    r.check(max(abs(fit[0] - CX), abs(fit[1] - CY), abs(fit[2] - R)) < 1e-6,
            f"taubin: exact points give exact answer (got {fit})")

    x, y = circle_pts(12, span_deg=70.0, start_deg=200.0)
    fit = fit_circle_taubin(x, y)
    r.check(fit is not None and
            max(abs(fit[0] - CX), abs(fit[1] - CY), abs(fit[2] - R)) < 1e-6,
            f"taubin: a 70-degree arc is still exact (got {fit})")

    # The reason Taubin is here at all: on a noisy short arc — an eyelid over
    # most of the pupil — Kåsa's radius runs away and Taubin's doesn't. Over
    # many draws, since on any single one either fit can get lucky.
    rng = np.random.default_rng(4)
    e_taubin, e_kasa = [], []
    for _ in range(60):
        ax, ay = circle_pts(16, span_deg=45.0, start_deg=200.0)
        ax = ax + rng.normal(0.0, 0.5, ax.size)
        ay = ay + rng.normal(0.0, 0.5, ay.size)
        ft, fk = fit_circle_taubin(ax, ay), kasa(ax, ay)
        if ft is not None:
            e_taubin.append(abs(ft[2] - R))
        e_kasa.append(abs(fk[2] - R))
    mt, mk = float(np.median(e_taubin)), float(np.median(e_kasa))
    r.check(mt < 0.6 * mk,
            f"taubin: beats the Kasa control on a noisy 45-degree arc "
            f"(median radius error {mt:.2f} px vs {mk:.2f} px)")
    r.check(mk > 2.0,
            f"control: Kasa really is biased here ({mk:.2f} px), so the "
            f"comparison above is not vacuous")

    r.check(fit_circle_taubin(np.array([1.0, 2.0]), np.array([1.0, 2.0])) is None,
            "taubin: fewer than 3 points -> None")
    xs = np.linspace(0.0, 50.0, 9)
    col = fit_circle_taubin(xs, 2.0 * xs + 3.0)
    r.check(col is None or col[2] > 1e6,
            f"taubin: collinear points give no finite circle (got {col})")

    # ══ fit_circle_robust ═════════════════════════════════════════════════════
    x, y = circle_pts(48)
    rob = fit_circle_robust(x, y)
    if not r.check(rob is not None, "robust: fits a clean circle"):
        return r.finish()
    cx, cy, rr, keep = rob
    r.check(max(abs(cx - CX), abs(cy - CY), abs(rr - R)) < 1e-6,
            f"robust: clean circle is exact (got {cx:.4f}, {cy:.4f}, {rr:.4f})")
    r.check(bool(keep.all()), "robust: keeps every point when none is an outlier")

    # An eyelash: a contiguous run of rays that latched onto something 14 px
    # outside the rim. Contiguous, because that is the case a median-based
    # rejection is least able to shrug off.
    x, y = circle_pts(48)
    bad = np.zeros(48, dtype=bool)
    bad[10:19] = True                       # 9 of 48 rays, ~19 %
    a = np.radians(np.linspace(0.0, 360.0, 48, endpoint=False))
    x = np.where(bad, CX + (R + 14.0) * np.cos(a), x)
    y = np.where(bad, CY + (R + 14.0) * np.sin(a), y)

    rob = fit_circle_robust(x, y)
    if not r.check(rob is not None, "robust: fits a contaminated circle"):
        return r.finish()
    cx, cy, rr, keep = rob
    err = max(abs(cx - CX), abs(cy - CY), abs(rr - R))
    r.check(err < 0.5, f"robust: outliers do not move the fit ({err:.3f} px)")
    r.check(not keep[bad].any(), f"robust: every outlier rejected "
                                 f"({int(keep[bad].sum())} kept)")
    r.check(keep[~bad].sum() >= 0.9 * (~bad).sum(),
            f"robust: good points survive ({int(keep[~bad].sum())} of "
            f"{int((~bad).sum())})")

    plain = fit_circle_taubin(x, y)
    r.check(plain is not None and abs(plain[2] - R) > 2.0,
            f"control: the plain fit on the same points is off by "
            f"{abs(plain[2] - R):.1f} px")

    again = fit_circle_robust(x, y)
    r.check(again[:3] == rob[:3] and np.array_equal(again[3], keep),
            "robust: deterministic — the RANSAC seed is fixed, so a "
            "re-analysis reproduces the fit")
    r.check(fit_circle_robust(*circle_pts(3)) is None,
            "robust: too few points -> None")

    # ══ fit_ellipse ═══════════════════════════════════════════════════════════
    EA, EB, EDEG = 44.0, 27.5, 33.0
    ex, ey = ellipse_pts(40, CX, CY, EA, EB, EDEG)
    ell = fit_ellipse(ex, ey)
    if not r.check(ell is not None, "ellipse: fits an exact ellipse"):
        return r.finish()
    ecx, ecy, smaj, smin, ang = ell
    r.check(max(abs(ecx - CX), abs(ecy - CY)) < 1e-6,
            f"ellipse: centre exact (got {ecx:.6f}, {ecy:.6f})")
    r.check(abs(smaj - EA) < 1e-6 and abs(smin - EB) < 1e-6,
            f"ellipse: semi-axes exact (got {smaj:.6f}, {smin:.6f})")
    r.check(smaj >= smin, "ellipse: semi-major is returned first")
    r.check(abs(((ang - EDEG + 90.0) % 180.0) - 90.0) < 1e-6,
            f"ellipse: angle belongs to the MAJOR axis (got {ang:.4f}, "
            f"expected {EDEG})")

    # The 90-degree phase error the axis/angle pairing exists to prevent would
    # pass every check above except this one: walk out along the reported angle
    # and land on the ellipse at the reported semi-major distance.
    t = np.radians(ang)
    px, py = ecx + smaj * np.cos(t), ecy + smaj * np.sin(t)
    d = np.min(np.hypot(ex - px, ey - py))
    r.check(d < 0.2, f"ellipse: the semi-major endpoint is on the curve "
                     f"({d:.4f} px from the nearest sample)")

    ell = fit_ellipse(*circle_pts(32))
    r.check(ell is not None and abs(ell[2] - ell[3]) < 1e-6
            and abs(ell[2] - R) < 1e-6,
            f"ellipse: a circle comes back with equal axes (got {ell})")

    r.check(fit_ellipse(*circle_pts(5)) is None,
            "ellipse: fewer than 6 points -> None")
    xs = np.linspace(0.0, 50.0, 12)
    r.check(fit_ellipse(xs, 2.0 * xs + 3.0) is None,
            "ellipse: collinear points are not an ellipse -> None")
    zero = np.zeros(8)
    r.check(fit_ellipse(zero, zero) is None,
            "ellipse: a single repeated point -> None")

    ex, ey = ellipse_pts(40, CX, CY, EA, EB, EDEG)
    rng = np.random.default_rng(11)
    ell = fit_ellipse(ex + rng.normal(0, 0.4, 40), ey + rng.normal(0, 0.4, 40))
    ok = (ell is not None and abs(ell[2] - EA) < 1.5 and abs(ell[3] - EB) < 1.5
          and abs(((ell[4] - EDEG + 90.0) % 180.0) - 90.0) < 3.0)
    r.check(ok, f"ellipse: survives 0.4 px of edge noise (got {ell})")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
