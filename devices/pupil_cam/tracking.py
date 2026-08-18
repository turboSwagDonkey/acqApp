"""Pupil detection — a port of LabVIEW's IMAQ **Find Circular Edge**.

An annular search region around an estimated centre, a fan of rays cast outward
through it, the intensity edge along each (dark pupil → brighter iris), and a
circle least-squares-fitted to those edge points:

    1. sample the annulus along `n_rays` radial lines   (_sample_annulus)
    2. per ray, the strongest 1-D gradient in the band, sub-pixel by a parabola
       on the peak                                      (_edges_along_rays)
    3. robust circle fit with iterative outlier rejection, so eyelashes, glints
       and eyelid crossings drop out                    (fit_circle_robust)
    4. optionally an ellipse re-fit to the inliers, for an off-axis eye

1–3 repeat `refine_iters` times, each pass re-centring the annulus on the last
fit, so a coarse seed converges.

Pure numpy — cv2 has no wheels for the 3.14 venv. scipy is used only for the
coarse seed's labelling, with a fallback if absent.

Entry points: `detect()` stateless per-frame; `PupilTracker` stateful, seeding
each frame from the previous fit (cheaper and steadier — prefer it live);
`find_circular_edge()` the primitive, to drive the annulus yourself.

Angles: 0 rad = +x, increasing toward +y (image *down*), so 90° is the image
bottom and 270° the top — what `exclude_deg` takes, e.g. eyelids at
``[(60, 120), (240, 300)]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

# The separately testable halves live in their own modules, re-exported here so
# `tracking.fit_circle_robust` keeps working.
from acqApp.devices.pupil_cam.fits import (_ellipse_radial_residual, fit_circle_robust,
                                   fit_circle_taubin, fit_ellipse,
                                   fit_ellipse_robust)
from acqApp.devices.pupil_cam.rays import (_edges_along_rays, _ray_angles,
                                   _sample_annulus)

# Longest side the coarse seed's distance transform is allowed to run on; a
# larger component is decimated to fit (see coarse_seed).
_SEED_MAX_PX = 256

# Annulus half-width per refinement pass, and the floor it stops at. The floor
# has to stay wide enough for a dilating pupil to move between frames.
_BAND_CONTRACT = 0.7
_MIN_HALF_BAND = 6.0

# Most the annulus may widen while the eye stays shut, before the tracker is
# looking over so much of the orbit that anything could satisfy it.
_MAX_BAND_GROWTH = 2.5

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

    # This frame's unfiltered fit, when PupilTracker smoothed the fields above.
    # Kept so the per-frame measurement is never destroyed by the filter — the
    # smoothed value is the better estimate, the raw one is the observation.
    raw_center: tuple[float, float] | None = None
    raw_radius: float | None = None

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
                       samples_per_px: float = 2.0,
                       edge_select: str = "first") -> PupilResult:
    """Locate the pupil boundary in the annulus around `center`.

    Parameters mirror the IMAQ VI's controls:
      center, r_inner, r_outer   the annular ROI
      n_rays                     number of search lines
      polarity                   edge polarity (see _edges_along_rays)
      min_strength               minimum |gradient| per px to accept an edge
      exclude_deg                angular sectors to skip, e.g. eyelids
      fit                        "circle" or "ellipse". The ellipse fits every
                                 ray hit with its own rejection; the circle pass
                                 only sanity-checks it, since no circle
                                 consensus holds both axis extremes.
      refine_iters               re-centre the annulus on the fit and repeat

    `radius is None` means no confident detection.
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
            values, r0, step, polarity, min_strength, smooth_sigma, edge_select)

        if hit.sum() < min_rays:
            return result

        sel = hit.copy()
        if fit != "ellipse":
            # Pre-reject in radius space before fitting: the annulus is roughly
            # concentric, so true edge radii cluster while an eyelid crossing
            # scatters. Dropping those first stops them dragging the initial fit
            # somewhere residual-based rejection can't recover from. Skipped for
            # ellipse mode, where radius genuinely varies with angle.
            rr = r_edge[sel]
            med_r = np.median(rr)
            # Scale from the OUTER side only. With edge_select="first" every
            # error is inward — an eyelid crossing the pupil, or speckle inside
            # it, reports a radius too small and never one too large — so a
            # two-sided MAD is inflated by exactly the points being rejected and
            # breaks down near 50 % occlusion (the 40 % eyelid case). The outer
            # half is uncontaminated, so it measures the real spread.
            hi = rr[rr >= med_r]
            mad_r = 1.4826 * np.median(np.abs(hi - med_r)) if hi.size else 0.0
            tol_r = max(3.0 * mad_r, 0.08 * med_r)
            near = np.abs(r_edge - med_r) <= tol_r
            if (sel & near).sum() >= min_rays:
                sel &= near

        ex = cx + r_edge[sel] * np.cos(angles[sel])
        ey = cy + r_edge[sel] * np.sin(angles[sel])

        # In ellipse mode the circle pass only kills gross outliers, so its
        # tolerance must stay wide enough to keep the axis extremes.
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
            # To *all* ray hits, not the circle pass's inliers: no circle
            # consensus holds both axis extremes, so reusing `keep` silently
            # truncates the major axis. fit_ellipse_robust rejects on ellipse
            # residuals, the only kind meaning anything once it isn't round.
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

        # Re-centre the annulus on this fit and go round again, narrowing the
        # band as it converges. A fixed half-width lets a pass that landed on a
        # stronger outer edge drag the band outward with it, and the next pass
        # then only sees the wrong edge — the eyelid lock is self-reinforcing.
        # Contracting means each pass searches nearer the radius it just found.
        moved = np.hypot(fcx - cx, fcy - cy)
        cx, cy = float(fcx), float(fcy)
        half = max(_MIN_HALF_BAND, half * _BAND_CONTRACT)
        r_inner, r_outer = max(1.0, radius - half), radius + half
        if moved < 0.25 and it > 0:
            break

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Coarse seed — "estimate a rough pupil centre" before the annulus exists
# ══════════════════════════════════════════════════════════════════════════════

def coarse_seed(frame: np.ndarray, threshold: int = 60,
                min_r: int = 10, max_r: int = 80, *, bright: bool = False):
    """Rough (cx, cy, r) for the first annulus, or None.

    The largest thresholded blob, then its **deepest interior point** by
    distance transform rather than a centroid: eyelashes and eyelid margins
    routinely merge into the pupil's component, and a centroid of that lands
    off the pupil while the inscribed-circle centre stays put. The EDT peak is
    also a better radius than √(area/π) for a merged blob.

    Only places the annulus — it just has to land inside the pupil.
    """
    mask = (frame > threshold) if bright else (frame < threshold)
    n = int(mask.sum())
    if n == 0:
        return None
    # A pupil cannot be half the sensor. When it is (lens cap, illumination off,
    # bad threshold) labelling a frame-sized blob costs ~100 ms and is rejected
    # at the end anyway — bail cheaply so a dark stretch can't stall the display.
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

    # Score the largest few, not blindly the biggest: an eyelid margin or
    # shadowed orbit is often larger than the pupil and nowhere near as round.
    # Circularity = area / (pi*r_inscribed^2) — ~1 for a disc, unbounded for a band.
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

        # Bound the EDT's cost: it scales with the bounding box, which for a
        # degenerate mask is the whole sensor (~200 ms per re-seed). Decimating
        # is safe — find_circular_edge re-centres from wherever this lands.
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

# Annulus half-width as a fraction of the seed radius: r*(1-BAND) … r*(1+BAND).
# Ellipse mode needs a wider band — the seed radius is an inscribed circle, i.e.
# the semi-*minor* axis, so a symmetric band never reaches the major-axis edge.
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
           smooth_sigma: float = 1.5,
           edge_select: str = "first",
           seed: tuple[float, float, float] | None = None) -> PupilResult:
    """Stateless per-frame detection: coarse seed → annular edge search → robust
    circle fit.

    `seed` places the annulus explicitly (the LabVIEW workflow). Pass it
    whenever you have it — auto-seeding is a bootstrap convenience and can be
    fooled by a dark eyelid margin larger than the pupil.

    For live video prefer `PupilTracker`.
    """
    if seed is None:
        # "falling" = a bright pupil on a darker iris, so seed on the bright blob
        seed = coarse_seed(frame, threshold, min_r, max_r,
                           bright=(polarity == "falling"))
        if seed is None:
            return PupilResult(None, None, None, 0.0)
    cx, cy, r = seed
    r_in, r_out = _band(r, fit, max_r)

    res = find_circular_edge(
        frame, (cx, cy), r_in, r_out,
        n_rays=n_rays, polarity=polarity, min_strength=min_strength,
        smooth_sigma=smooth_sigma, exclude_deg=exclude_deg, fit=fit,
        edge_select=edge_select, refine_iters=3,
    )
    if res.radius is not None and not (min_r < res.radius < max_r):
        # edge found but outside the size band — report the position only
        return PupilResult(res.center_x, res.center_y, None, 0.0,
                           edge_x=res.edge_x, edge_y=res.edge_y,
                           inliers=res.inliers, rms=res.rms)
    return res


class PupilTracker:
    """Stateful pupil tracker for a live stream.

    Each annulus is seeded from the previous good fit; after `max_lost`
    consecutive failures it falls back to `coarse_seed`.

    Not thread-safe — call `process()`/`configure()` from one thread. In the app
    that is `track_worker.py`, which queues the panel's edits between frames.
    """

    def __init__(self, threshold: int = 60, min_r: int = 10, max_r: int = 80,
                 *, n_rays: int = 64, polarity: str = "rising",
                 min_strength: float = 4.0, smooth_sigma: float = 1.5,
                 exclude_deg: Sequence[tuple[float, float]] = (),
                 fit: str = "circle", edge_select: str = "first",
                 min_confidence: float = 0.10,
                 max_lost: int = 5, max_jump: float = 0.5,
                 smooth_median: int = 3, smooth_ema: float = 0.5,
                 reseed_after: int = 30):
        self.threshold = threshold
        self.min_r = min_r
        self.max_r = max_r
        self.n_rays = n_rays
        self.polarity = polarity
        self.min_strength = min_strength
        self.smooth_sigma = smooth_sigma
        self.exclude_deg = exclude_deg
        self.fit = fit
        self.edge_select = edge_select
        self.min_confidence = min_confidence
        self.max_lost = max_lost
        self.max_jump = max_jump          # allowed centre jump, × previous radius
        self.smooth_median = smooth_median
        self.smooth_ema = smooth_ema
        self.reseed_after = reseed_after
        self.reset()

    def reset(self) -> None:
        self._last: tuple[float, float, float] | None = None   # cx, cy, r
        self._lost = 0
        self._clear_filter()

    def _clear_filter(self) -> None:
        """Drop the temporal filter's state, keeping the annulus seed.

        Called whenever a frame is lost, so smoothing never runs *across* a gap:
        a blink has to show up in the trace as lost frames, not be interpolated
        away, and the estimate that resumes afterwards must describe the eye that
        reopened rather than a blend with the one that closed.
        """
        self._hist: list[tuple[float, float, float]] = []
        self._ema: tuple[float, float, float] | None = None

    def _filter(self, cx: float, cy: float, r: float
                ) -> tuple[float, float, float]:
        """Median over the last `smooth_median` consecutive fits, then an EMA.

        Two stages doing two jobs: the median removes the single-frame
        excursions that a half-closed lid produces (measured on rig footage:
        the fit leaves and returns within two frames), and the EMA takes out the
        residual sub-pixel jitter the median leaves behind.
        """
        n = max(1, int(self.smooth_median))
        self._hist.append((float(cx), float(cy), float(r)))
        del self._hist[:-n]
        med = tuple(float(np.median([h[i] for h in self._hist]))
                    for i in range(3))
        a = float(np.clip(self.smooth_ema, 0.0, 1.0))
        if self._ema is None or a >= 1.0:
            self._ema = med
        else:
            self._ema = tuple(a * m + (1.0 - a) * p
                              for m, p in zip(med, self._ema))
        return self._ema  # type: ignore[return-value]

    def seed(self, cx: float, cy: float, r: float) -> None:
        """Place the annulus by hand, for when the auto-seed picks the wrong
        dark region. Tracking continues as if the last frame had fitted it."""
        self._last = (float(cx), float(cy), float(r))
        self._lost = 0

    @property
    def locked(self) -> bool:
        return self._last is not None

    # Changing any of these invalidates the lock — the annulus was placed under
    # the old assumptions, so re-seed rather than let it drift.
    _RESEED_ON = frozenset({"threshold", "min_r", "max_r", "polarity", "fit",
                            "edge_select"})

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
        # After a long run of failures, try re-thresholding — but only as an
        # *offer*: if coarse_seed finds nothing (a closed eye is not a blob),
        # the pre-blink estimate is still the best place to look, so it is kept
        # rather than discarded. That is the difference between resuming when the
        # eye reopens and hunting for the pupil from scratch.
        if self._last is not None and self._lost >= self.reseed_after:
            offer = coarse_seed(frame, self.threshold, self.min_r, self.max_r,
                                bright=(self.polarity == "falling"))
            if offer is not None:
                self._last = offer
                self._lost = 0

        seeded_from_last = self._last is not None
        if seeded_from_last:
            cx, cy, r = self._last
        else:
            seed = coarse_seed(frame, self.threshold, self.min_r, self.max_r,
                               bright=(self.polarity == "falling"))
            if seed is None:
                self._lost += 1
                self._clear_filter()
                return PupilResult(None, None, None, 0.0)
            cx, cy, r = seed

        r_in, r_out = _band(r, self.fit, self.max_r)
        # Once the eye has been shut for a while, widen the band around the
        # retained estimate instead of abandoning it: the pupil may have moved
        # or changed size behind the lid, and a band sized for the old radius
        # would never see the new edge.
        if self._lost >= self.max_lost:
            grow = min(_MAX_BAND_GROWTH,
                       1.0 + 0.2 * (self._lost - self.max_lost + 1))
            mid = 0.5 * (r_in + r_out)
            half = 0.5 * (r_out - r_in) * grow
            r_in, r_out = max(1.0, mid - half), min(mid + half, self.max_r * 2.0)
        res = find_circular_edge(
            frame, (cx, cy), r_in, r_out,
            n_rays=self.n_rays, polarity=self.polarity,
            min_strength=self.min_strength, smooth_sigma=self.smooth_sigma,
            exclude_deg=self.exclude_deg, fit=self.fit,
            edge_select=self.edge_select,
            refine_iters=2 if seeded_from_last else 3,
        )

        ok = (res.radius is not None
              and res.confidence >= self.min_confidence
              and self.min_r < res.radius < self.max_r)
        if ok and seeded_from_last:
            # reject a teleport — usually an eyelid edge grabbed mid-blink
            ok = np.hypot(res.center_x - cx, res.center_y - cy) <= self.max_jump * r

        if ok:
            # Report the filtered estimate; keep this frame's own fit in
            # `raw_*` so the observation survives the smoothing. The annulus is
            # seeded from the filtered value too — that is what stops one bad
            # frame from dragging the search region somewhere it then has to
            # crawl back from.
            raw = (res.center_x, res.center_y, res.radius)
            cxs, cys, rs = self._filter(*raw)
            if res.axes is not None and res.radius:
                k = rs / res.radius        # keep the drawn ellipse consistent
                res.axes = (res.axes[0] * k, res.axes[1] * k)
            res.raw_center, res.raw_radius = (raw[0], raw[1]), raw[2]
            res.center_x, res.center_y, res.radius = cxs, cys, rs
            self._last = (cxs, cys, rs)
            self._lost = 0
            return res

        # A blink must show up as lost frames, not be smoothed over, so the
        # filter is dropped here. `_last` is deliberately NOT — it is the eye's
        # last known position and the right place to resume from.
        self._lost += 1
        self._clear_filter()
        return PupilResult(None, None, None, 0.0,
                           edge_x=res.edge_x, edge_y=res.edge_y,
                           inliers=res.inliers, rms=res.rms)
