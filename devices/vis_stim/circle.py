"""Aperture geometry for the circle-in-a-region trials (tuning, contrast,
size). No Qt.

Regions are 1-indexed (the operator's "region 1-9"); the diameter is the
region's WIDTH, not height (the operator's spec).
"""
from __future__ import annotations

from . import regions as regions_mod


def circle_geometry(region_1based: int, screen_w: int, screen_h: int
                    ) -> tuple[float, float, float]:
    """(center_x, center_y, diameter) for the aperture."""
    regs = regions_mod.region_rects(screen_w, screen_h)
    idx = max(0, min(int(region_1based) - 1, len(regs) - 1))
    x, y, w, h = regs[idx]
    return (x + w / 2.0, y + h / 2.0, w)
