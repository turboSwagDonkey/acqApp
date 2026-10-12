"""Stage <-> camera axes: which way the picture slides when the stage moves.
No Qt.

`CamAxes` is (ax, ay, bx, by): the image shift, in unbinned camera pixels
(x right, y down, as displayed), per µm of stage +X (ax, ay) and of stage +Y
(bx, by). `axes_measure.py` measures it by nudging the stage and correlating
frames; jogs then move the picture straight along the screen
(`camera_jog`), and the camera view draws it as arrows.
"""
from __future__ import annotations

import math

import numpy as np

CamAxes = tuple[float, float, float, float]

# Below this correlation peak (in standard deviations of the correlation
# surface) the "shift" is noise: an untextured or defocused field.
MIN_PEAK = 10.0
# Phase correlation whitens the spectrum; this Gaussian (cycles/px) keeps it
# from being run by pixel noise.
_LOWPASS = 0.12
# Fixed sensor pattern and uneven light peak at zero lag; a real nudge is
# never this small (in block-averaged px).
_ZERO_MASK_PX = 3


def _square(frame: np.ndarray, max_side: int) -> tuple[np.ndarray, int]:
    """The largest centred square, block-averaged to <= max_side -> (it, k)."""
    h, w = frame.shape[:2]
    side = min(h, w)
    k = max(1, -(-side // max_side))
    n = side // k
    side = n * k
    y0, x0 = (h - side) // 2, (w - side) // 2
    sq = frame[y0:y0 + side, x0:x0 + side].astype(np.float32)
    return sq.reshape(n, k, n, k).mean(axis=(1, 3)), k


def _subpixel(r: np.ndarray, i: int, j: int, axis: int) -> float:
    """Parabolic peak offset along `axis` (wrapping), in -0.5..0.5."""
    n = r.shape[axis]
    if axis == 0:
        lo, mid, hi = r[(i - 1) % n, j], r[i, j], r[(i + 1) % n, j]
    else:
        lo, mid, hi = r[i, (j - 1) % n], r[i, j], r[i, (j + 1) % n]
    den = lo - 2.0 * mid + hi
    return 0.0 if den == 0 else float(np.clip(0.5 * (lo - hi) / den, -0.5, 0.5))


def image_shift(a: np.ndarray, b: np.ndarray,
                max_side: int = 512) -> tuple[float, float, float]:
    """(dx, dy, peak): how far `a`'s content moved to land in `b`, in `a`'s
    pixels, by phase correlation on the centred square; `peak` is the match
    strength (compare MIN_PEAK). Zero lag is masked (`_ZERO_MASK_PX`)."""
    sa, k = _square(a, max_side)
    sb, _ = _square(b, max_side)
    n = sa.shape[0]
    win = np.outer(np.hanning(n), np.hanning(n)).astype(np.float32)
    fa = np.fft.fft2((sa - sa.mean()) * win)
    fb = np.fft.fft2((sb - sb.mean()) * win)
    cross = np.conj(fa) * fb
    cross /= np.abs(cross) + 1e-12
    f = np.fft.fftfreq(n)
    cross *= np.exp(-(f[:, None] ** 2 + f[None, :] ** 2) / (2 * _LOWPASS ** 2))
    r = np.fft.ifft2(cross).real
    # Only a true local maximum counts: beside the masked zero lag, the
    # zero-lag peak's own skirt would otherwise pass for a small shift.
    local_max = np.ones_like(r, dtype=bool)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di or dj:
                local_max &= r >= np.roll(r, (di, dj), axis=(0, 1))
    m = _ZERO_MASK_PX
    masked = r.copy()
    for di in range(-m, m + 1):
        for dj in range(-m, m + 1):
            masked[di % n, dj % n] = np.nan
    mean, std = np.nanmean(masked), np.nanstd(masked) + 1e-12
    masked[~local_max] = np.nan
    i, j = np.unravel_index(int(np.nanargmax(masked)), r.shape)
    peak = float((r[i, j] - mean) / std)
    dy = i + _subpixel(r, i, j, 0)
    dx = j + _subpixel(r, i, j, 1)
    if dy > n / 2:
        dy -= n
    if dx > n / 2:
        dx -= n
    return dx * k, dy * k, peak


def fit_axes(shift_x: tuple[float, float], shift_y: tuple[float, float],
             step_um: float, binning: int) -> CamAxes:
    """Image shifts (frame px) for a +step_um nudge of X, then Y -> CamAxes."""
    s = binning / step_um
    return (shift_x[0] * s, shift_x[1] * s, shift_y[0] * s, shift_y[1] * s)


def implausible(axes: CamAxes) -> str:
    """Why `axes` can't be a stage seen through a camera, or ""."""
    ax, ay, bx, by = axes
    na, nb = math.hypot(ax, ay), math.hypot(bx, by)
    if na == 0 or nb == 0:
        return "an axis did not move the image"
    if not 0.8 <= na / nb <= 1.25:
        return (f"X and Y scale differ ({1 / na:.3g} vs {1 / nb:.3g} µm/px); "
                "a stage axis should not stretch the image")
    cos = (ax * bx + ay * by) / (na * nb)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
    if not 70.0 <= angle <= 110.0:
        return f"X and Y are {angle:.0f}° apart on the image, not ~90°"
    return ""


def screen_angle(vx: float, vy: float) -> float:
    """Degrees counter-clockwise from screen-right (image y points down)."""
    return math.degrees(math.atan2(-vy, vx))


def describe(axes: CamAxes) -> str:
    ax, ay, bx, by = axes
    um_px = 2.0 / (math.hypot(ax, ay) + math.hypot(bx, by))
    return (f"+X {screen_angle(ax, ay):.0f}°, +Y {screen_angle(bx, by):.0f}°, "
            f"{um_px:.3g} µm/px")


def camera_jog(which: str, delta_um: float, rotation_deg: float,
               axes: CamAxes | None) -> tuple[float, float]:
    """A jog along the screen's `which` axis -> the stage's (dx, dy) in µm.

    Measured `axes`: +x slides the picture right, +y up, by `delta_um` of
    stage travel. Otherwise the manual `rotation_deg` (0 = stage axes as-is,
    exact)."""
    if axes is not None:
        ax, ay, bx, by = axes
        ux, uy = (1.0, 0.0) if which == "x" else (0.0, -1.0)
        det = ax * by - bx * ay
        sx, sy = (by * ux - bx * uy) / det, (ax * uy - ay * ux) / det
        k = delta_um / math.hypot(sx, sy)
        return sx * k, sy * k
    dx, dy = (delta_um, 0.0) if which == "x" else (0.0, delta_um)
    if rotation_deg == 0.0:
        return dx, dy
    rad = math.radians(rotation_deg)
    c, s = math.cos(rad), math.sin(rad)
    return dx * c - dy * s, dx * s + dy * c
