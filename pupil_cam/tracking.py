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
                                              signature, kept stable.
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

# The two halves that are separately testable live in their own modules; they
# are re-exported below so `tracking.fit_circle_robust` keeps working.
from acqApp.pupil_cam.fits import (_ellipse_radial_residual, fit_circle_robust,
                                   fit_circle_taubin, fit_ellipse,
                                   fit_ellipse_robust)
from acqApp.pupil_cam.rays import (_edges_along_rays, _ray_angles,
                                   _sample_annulus)

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
    owns a tracker and queues the panel's edits into it between frames;
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
