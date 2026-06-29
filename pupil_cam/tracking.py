"""
Pupil detection — runs on each camera frame and returns a PupilResult.

TODO: replace the threshold stub with a proper algorithm.
      Options in rough order of complexity:
        1. cv2.HoughCircles  (fast, works under good contrast)
        2. Starburst / edge-based ellipse fit  (robust to reflections)
        3. DeepLabCut / Lightning Pose  (marker-free DL, best for noisy data)
"""

from __future__ import annotations
from dataclasses import dataclass

import numpy as np


@dataclass
class PupilResult:
    center_x:  float | None   # px
    center_y:  float | None   # px
    radius:    float | None   # px (mean of semi-axes for an ellipse)
    confidence: float = 0.0   # 0–1


def detect(frame: np.ndarray,
           threshold: int = 60,
           min_r: int = 10,
           max_r: int = 80) -> PupilResult:
    """
    Minimal stub: threshold the frame, find the largest dark blob,
    fit a bounding circle.  Replace with a real algorithm.
    """
    try:
        import cv2  # optional dependency
        _, mask = cv2.threshold(frame, threshold, 255, cv2.THRESH_BINARY_INV)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return PupilResult(None, None, None, 0.0)

        best = max(contours, key=cv2.contourArea)
        (cx, cy), r = cv2.minEnclosingCircle(best)
        if not (min_r < r < max_r):
            return PupilResult(None, None, None, 0.0)
        return PupilResult(float(cx), float(cy), float(r), confidence=0.8)

    except ImportError:
        # cv2 not installed — return center-of-mass of dark pixels as fallback
        dark = frame < threshold
        if not dark.any():
            return PupilResult(None, None, None, 0.0)
        ys, xs = np.where(dark)
        return PupilResult(float(xs.mean()), float(ys.mean()), None, 0.3)
