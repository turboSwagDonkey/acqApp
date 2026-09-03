"""The 3x3 region grid shared by the map/tuning/contrast/size trial types.

The screen is divided into 4 vertical columns; the last one (column 3,
0-indexed) is always ignored and stays black for the whole trial — this rig
has no use case for blacking out a different column, so it's a fixed
constant rather than an operator-facing setting. The other 3 are each split
into 3 rows, giving 9 regions in column-major order — the first visible
column's 3 rows top to bottom, then the next column's, then the next:

    +--+--+--+--+
    |1 |4 |7 |  |
    +--+--+--+bk|
    |2 |5 |8 |  |
    +--+--+--+  |
    |3 |6 |9 |  |
    +--+--+--+--+

No Qt — plain (x, y, w, h) tuples in screen pixels, so this is testable
without a display and reusable by window.py's QRectF construction.
"""
from __future__ import annotations

N_COLUMNS = 4
N_ROWS = 3                          # rows per visible column
N_REGIONS = (N_COLUMNS - 1) * N_ROWS   # 9
IGNORED_COLUMN = N_COLUMNS - 1         # always the last column


def ignored_rect(screen_w: int, screen_h: int
                 ) -> tuple[float, float, float, float]:
    """The blacked-out column's own (x, y, w, h)."""
    cw = screen_w / N_COLUMNS
    return (IGNORED_COLUMN * cw, 0.0, cw, float(screen_h))


def region_rects(screen_w: int, screen_h: int
                 ) -> list[tuple[float, float, float, float]]:
    """The 9 regions' (x, y, w, h), column-major, skipping IGNORED_COLUMN.

    Column order follows screen left-to-right, so the 9 regions are drawn
    from columns 0, 1, 2 in that order — the spatial layout always matches
    what is actually on screen.
    """
    cw = screen_w / N_COLUMNS
    rh = screen_h / N_ROWS
    regions: list[tuple[float, float, float, float]] = []
    for c in range(N_COLUMNS):
        if c == IGNORED_COLUMN:
            continue
        x = c * cw
        for row in range(N_ROWS):
            regions.append((x, row * rh, cw, rh))
    return regions
