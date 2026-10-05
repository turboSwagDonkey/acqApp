"""Whiskers: long, straight, bright ridges across the eye. Found and painted
over before the fit, like a reflection. No Qt, no EyeLoop.

Ported from the pupil prototype (Downloads/pupil_proto ridge.py). A Hessian
ridge test keeps lines and drops blobs (glints); length and elongation drop
the curved iris band and short fur; a dark-surroundings test drops long
straight fur (VF203/VF215 bench: fur 22-36 vs whiskers 19-22, fits unchanged).
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
                 dilate: int = 4, ring_q: float | None = 25) -> np.ndarray:
    """Pixels on a whisker (bool, `img`'s shape). `ring_q` (percentile of
    its surroundings) drops lines with no dark eye beside them; None keeps all."""
    import cv2
    rs = ridge_strength(img, k)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(
        (rs > thr).astype(np.uint8), connectivity=8)
    out = np.zeros(img.shape, bool)
    p10, p50 = np.percentile(img, [10, 50])
    dark = (p10 + p50) / 2
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_len:
            continue
        x, y, w, h = stats[i, :4]
        ys, xs = np.nonzero(lab[y:y + h, x:x + w] == i)
        ev = np.linalg.eigvalsh(np.cov(np.vstack([xs, ys]).astype(float)))
        if 4 * np.sqrt(ev[1]) < min_len or ev[1] < min_elong ** 2 * max(ev[0], 1e-6):
            continue
        if ring_q is not None and _background(img, lab, i, (x, y, w, h), ring_q) >= dark:
            continue        # bright fur, not a line across the eye
        out[y:y + h, x:x + w] |= lab[y:y + h, x:x + w] == i
    if out.any() and dilate:
        out = cv2.dilate(out.astype(np.uint8), np.ones((3, 3), np.uint8),
                         iterations=dilate).astype(bool)
    return out


def _background(img: np.ndarray, lab: np.ndarray, i: int, bbox,
                q: float, near: int = 3, far: int = 8) -> float:
    """Percentile `q` of a ring `near`..`far` px around component `i`, off any ridge."""
    import cv2
    x, y, w, h = bbox
    ys, xs = slice(max(y - far, 0), y + h + far), slice(max(x - far, 0), x + w + far)
    sub = lab[ys, xs]
    comp = (sub == i).astype(np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    ring = (cv2.dilate(comp, k3, iterations=far).astype(bool)
            & ~cv2.dilate(comp, k3, iterations=near).astype(bool) & (sub == 0))
    vals = img[ys, xs][ring]
    return float(np.percentile(vals, q)) if vals.size else float("inf")


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
