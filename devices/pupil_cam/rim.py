"""Is a fit a pupil? Its rim must be darker inside than out, and its disc
dark. No Qt, no EyeLoop.

After the ring-contrast score of the pupil prototype (Downloads/pupil_proto
ring.py), on an ellipse. EyeLoop fits whatever dark blob the walk lands in;
on a closed eye that is a lid crease, whose disc is mostly not dark.
"""
from __future__ import annotations

import numpy as np

_N_ANG = 64
_ANG = np.arange(_N_ANG) * 2.0 * np.pi / _N_ANG
_OFF_IN = (-5.0, -4.0, -3.0)
_OFF_OUT = (3.0, 4.0, 5.0)
_DISC = (0.15, 0.35, 0.55, 0.75)
# Glints are blanked before this; the cap keeps bright fur from deciding it.
# Not lower: a pupil filling half the region would be flattened.
_CAP_PCT = 90
_KEEP = 0.65        # best fraction of the rim that votes (lids, whiskers)


def _points(fit, d_axis: float, scale: float = 1.0):
    a = fit.semi_major * scale + d_axis
    b = fit.semi_minor * scale + d_axis
    th = np.deg2rad(fit.angle_deg)
    u, v = a * np.cos(_ANG), b * np.sin(_ANG)
    return (fit.center_x + u * np.cos(th) - v * np.sin(th),
            fit.center_y + u * np.sin(th) + v * np.cos(th))


def _sample(img: np.ndarray, xs, ys) -> np.ndarray:
    import cv2
    return cv2.remap(img, np.float32(xs), np.float32(ys), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=np.nan)


def rim_measures(gray: np.ndarray, fit) -> tuple[float, float]:
    """(contrast, dark) for `fit` in `gray`'s pixels. contrast: grey levels
    outside minus inside the rim, mean of its best 65%; dark: fraction of the
    disc darker than halfway between the two."""
    import cv2
    s = gray.astype(np.float32)
    s = np.minimum(s, np.percentile(s, _CAP_PCT))
    s = cv2.blur(cv2.blur(s, (5, 5)), (5, 5))

    def ring(offs):
        pts = [_points(fit, o) for o in offs]
        v = _sample(s, np.stack([p[0] for p in pts]),
                    np.stack([p[1] for p in pts]))
        return v.mean(axis=0)              # NaN where it leaves the image

    vin, vout = ring(_OFF_IN), ring(_OFF_OUT)
    ok = np.isfinite(vin) & np.isfinite(vout)
    if ok.sum() < _KEEP * _N_ANG:          # mostly off the image: no vote
        return 0.0, 0.0
    vin, vout = vin[ok], vout[ok]
    con = np.sort(vout - vin)[::-1]
    contrast = float(con[:int(_KEEP * _N_ANG)].mean())
    pts = [_points(fit, 0.0, f) for f in _DISC]
    disc = _sample(s, np.stack([p[0] for p in pts]),
                   np.stack([p[1] for p in pts]))
    disc = disc[np.isfinite(disc)]
    mid = 0.5 * (np.median(vin) + np.median(vout))
    dark = float(np.mean(disc < mid)) if disc.size else 0.0
    return contrast, dark


def looks_like_pupil(gray: np.ndarray, fit, min_dark: float) -> bool:
    contrast, dark = rim_measures(gray, fit)
    return contrast > 0.0 and dark >= min_dark


# Pupil-iris contrast (grey levels) below which one threshold step moves the
# radius ~2 px or more: VF203.2R at 1.5 moves it ~8; simulated 3x exposure
# gives 5.2 and ~1 px (SESSIONLOG co).
LOW_CONTRAST = 4.0


class ContrastMeter:
    """Pupil-iris contrast of the accepted fits, sampled every `every`th
    frame. `value`: median of the last `window` samples; `samples`: every
    one so far (append-only, so a recording can take its own stretch)."""

    def __init__(self, every: int = 10, window: int = 15) -> None:
        self.every, self.window = every, window
        self.samples: list[float] = []
        self._n = 0

    def offer(self, gray: np.ndarray, fit) -> None:
        self._n += 1
        if self._n % self.every == 1 or self.every == 1:
            self.samples.append(rim_measures(gray, fit)[0])

    @property
    def value(self) -> float | None:
        recent = self.samples[-self.window:]
        return float(np.median(recent)) if recent else None


def contrast_summary(samples: list[float]) -> dict:
    """For a recording's metadata: median contrast and the verdict."""
    if not samples:
        return {}
    c = float(np.median(samples))
    return {"pupil_contrast": round(c, 2),
            "pupil_underexposed": bool(c < LOW_CONTRAST)}
