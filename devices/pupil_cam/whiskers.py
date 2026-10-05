"""Whiskers: long, straight, bright ridges across the eye. Found and painted
over before the fit, like a reflection. No Qt, no EyeLoop.

Ported from the pupil prototype (Downloads/pupil_proto ridge.py). A Hessian
ridge test keeps lines and drops blobs (glints); length and elongation drop
the curved iris band and short fur.
"""
from __future__ import annotations

import numpy as np


def ridge_strength(img: np.ndarray, k: int = 9) -> np.ndarray:
    """Bright thin-line strength: minus the most negative Hessian eigenvalue,
    where the other is small (a line, not a blob)."""
    import cv2
    s = cv2.blur(cv2.blur(img.astype(np.float32), (k, k)), (k, k))
    gy, gx = np.gradient(s)
    gyy, gyx = np.gradient(gy)
    _gxy, gxx = np.gradient(gx)
    m = (gxx + gyy) / 2
    d = np.sqrt(((gxx - gyy) / 2) ** 2 + gyx ** 2)
    l1, l2 = m - d, m + d
    return np.where((l1 < 0) & (np.abs(l2) < 0.5 * np.abs(l1)), -l1, 0.0)


def whisker_mask(img: np.ndarray, thr: float = 0.06, k: int = 9,
                 min_len: float = 30.0, min_elong: float = 6.0,
                 dilate: int = 4) -> np.ndarray:
    """Pixels on a whisker (bool, `img`'s shape)."""
    import cv2
    rs = ridge_strength(img, k)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(
        (rs > thr).astype(np.uint8), connectivity=8)
    out = np.zeros(img.shape, bool)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_len:
            continue
        x, y, w, h = stats[i, :4]
        ys, xs = np.nonzero(lab[y:y + h, x:x + w] == i)
        ev = np.linalg.eigvalsh(np.cov(np.vstack([xs, ys]).astype(float)))
        if 4 * np.sqrt(ev[1]) < min_len or ev[1] < min_elong ** 2 * max(ev[0], 1e-6):
            continue
        out[y:y + h, x:x + w] |= lab[y:y + h, x:x + w] == i
    if out.any() and dilate:
        out = cv2.dilate(out.astype(np.uint8), np.ones((3, 3), np.uint8),
                         iterations=dilate).astype(bool)
    return out


def paint_whiskers(img: np.ndarray, mask: np.ndarray, k: int = 15) -> np.ndarray:
    """`img` with `mask` filled from the unmasked pixels nearby (a local mean,
    so a whisker over the pupil becomes pupil and over the iris iris)."""
    import cv2
    if not mask.any():
        return img
    free = (~mask).astype(np.float32)
    val = img.astype(np.float32) * free
    out = img.copy()
    todo = mask.copy()
    # A band wider than the window has no free pixel mid-way: widen there.
    while todo.any() and k <= 4 * max(img.shape):
        num, den = cv2.blur(val, (k, k)), cv2.blur(free, (k, k))
        hit = todo & (den > 1e-6)
        out[hit] = (num[hit] / den[hit]).astype(img.dtype)
        todo &= ~hit
        k = 2 * k + 1
    return out
