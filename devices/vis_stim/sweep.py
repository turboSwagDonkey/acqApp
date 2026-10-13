"""The tuning trial types (orientation, contrast, spatial frequency, size):
drifting gratings in a region's circle. A condition is one of 8 orientations
x one level of the swept variable (`settings.DEFAULT_LEVELS`; orientation
tuning has none). No Qt.

Timeline: pretrial (each orientation once, before the trial-start mark),
grey, then every condition once per repeat in a fresh order, each as
stimulus-on then grey gap, then grey.
"""
from __future__ import annotations

import math

N_ORIENTATIONS = 8
ORIENTATION_STEP_DEG = 45.0


def orientations() -> tuple[float, ...]:
    """0, 45, 90, ..., 315 degrees."""
    return tuple(i * ORIENTATION_STEP_DEG for i in range(N_ORIENTATIONS))


def conditions(levels: tuple[float, ...]
               ) -> list[tuple[float, float | None]]:
    """(orientation, level) for every pair; level None when nothing else is
    swept."""
    lv: tuple[float | None, ...] = tuple(levels) or (None,)
    return [(o, x) for x in lv for o in orientations()]


def deg_to_px(deg: float, distance_cm: float, px_per_cm: float) -> float:
    """Screen span of `deg` of visual angle centred on the line of sight."""
    return 2.0 * distance_cm * math.tan(math.radians(deg) / 2.0) * px_per_cm


def period_px(cycles_per_deg: float, distance_cm: float,
              px_per_cm: float) -> float:
    """One grating cycle, in screen px."""
    return deg_to_px(1.0 / max(cycles_per_deg, 1e-6), distance_cm, px_per_cm)
