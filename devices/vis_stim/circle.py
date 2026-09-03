"""Shared aperture geometry for the circle-in-a-region trial types (tuning,
contrast, and eventually size). No Qt.

A circle sits at one of regions.py's 9 regions (selected 1-indexed, to match
the operator's "region 1-9" language), diameter equal to that region's
WIDTH (not height — the operator's spec). Which parameter the circle's
*content* sweeps (orientation, contrast, ...) is each trial type's own
concern (tuning.py, contrast.py); this only answers "where is the circle".
"""
from __future__ import annotations

from . import regions as regions_mod


def circle_geometry(region_1based: int, ignored_column: int,
                    screen_w: int, screen_h: int
                    ) -> tuple[float, float, float]:
    """(center_x, center_y, diameter) for the aperture."""
    regs = regions_mod.region_rects(ignored_column, screen_w, screen_h)
    idx = max(0, min(int(region_1based) - 1, len(regs) - 1))
    x, y, w, h = regs[idx]
    return (x + w / 2.0, y + h / 2.0, w)
