"""Two cameras' frames as one RGB preview, each in its own colour. No Qt.

Display only: each channel is stretched by its own 1-99 percentile, so a
dim camera still shows beside a bright one. Complementary colours (green +
magenta, the defaults) add to white where both are bright.

No registration yet: the other frame is only resized to this one's shape
(nearest pixel). Different crops, binning, a mirror or a rotation between
the paths show as misalignment, not as an error.
"""
from __future__ import annotations

import numpy as np

# name -> (r, g, b), 0..1
OVERLAY_COLORS: dict[str, tuple[float, float, float]] = {
    "green":   (0.0, 1.0, 0.0),
    "magenta": (1.0, 0.0, 1.0),
    "red":     (1.0, 0.0, 0.0),
    "cyan":    (0.0, 1.0, 1.0),
    "blue":    (0.2, 0.4, 1.0),     # pure blue is too dark to see on black
    "yellow":  (1.0, 1.0, 0.0),
    "gray":    (1.0, 1.0, 1.0),
}
DEFAULT_COLOR = "green"


def levels(frame: np.ndarray) -> tuple[float, float]:
    """(lo, hi) to stretch `frame` by: its 1st and 99th percentiles."""
    lo, hi = np.percentile(frame, (1, 99))
    return float(lo), (float(hi) if hi > lo else float(lo) + 1.0)


def fit_to(frame: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """`frame` resized to `shape` by nearest pixel (itself if it fits)."""
    if frame.shape[:2] == tuple(shape):
        return frame
    rows = np.linspace(0, frame.shape[0] - 1, shape[0]).round().astype(int)
    cols = np.linspace(0, frame.shape[1] - 1, shape[1]).round().astype(int)
    return frame[np.ix_(rows, cols)]


def _stretch(frame: np.ndarray, lv: tuple[float, float]) -> np.ndarray:
    lo, hi = lv
    return np.clip((frame.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)


def composite(own: np.ndarray, other: np.ndarray, own_color: str,
              other_color: str, own_levels: tuple[float, float],
              other_levels: tuple[float, float]) -> np.ndarray:
    """(rows, cols, 3) float32 in 0..1: `own` in its colour plus `other`
    (resized to `own`'s shape) in its. An unknown colour name is gray."""
    a = _stretch(own, own_levels)
    b = _stretch(fit_to(other, own.shape[:2]), other_levels)
    ca = np.asarray(OVERLAY_COLORS.get(own_color, OVERLAY_COLORS["gray"]), np.float32)
    cb = np.asarray(OVERLAY_COLORS.get(other_color, OVERLAY_COLORS["gray"]), np.float32)
    return np.clip(a[..., None] * ca + b[..., None] * cb, 0.0, 1.0)
