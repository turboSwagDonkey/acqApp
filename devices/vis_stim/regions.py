"""The 3x3 region grid shared by the map/tuning/contrast/size trial types.

4 vertical columns; the last is always black (fixed, not a setting). The
other 3 split into 3 rows each: 9 regions, column-major:

    +--+--+--+--+
    |1 |4 |7 |  |
    +--+--+--+bk|
    |2 |5 |8 |  |
    +--+--+--+  |
    |3 |6 |9 |  |
    +--+--+--+--+

No Qt: (x, y, w, h) tuples in screen pixels.
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
    """The 9 regions' (x, y, w, h), column-major, skipping IGNORED_COLUMN."""
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
