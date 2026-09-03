"""Grating texture + aperture geometry. No Qt, pure numpy.

Ports genGratingTex and the circular-aperture math in visStimCode's
runStimManager.m. `window.py` turns these into what it actually paints: the
grating row into a QImage tiled/rotated by Orientation, and the aperture
geometry into a QPainterPath clip — a clip achieves the same visible result as
MATLAB's alpha-masked overlay texture without needing a second full-screen
image per frame.
"""
from __future__ import annotations

import numpy as np

from .settings import StimParams

GAMMA = 2.2


def build_grating(p: StimParams, white: float = 255.0) -> np.ndarray:
    """One period-tiled row of a gamma-corrected sinusoidal grating, uint8,
    `ceil(StimDiameter / WaveSpPeriod) + 2` cycles wide.

    A single row, not a 2-D field: the texture is a horizontal band rotated at
    paint time by `Orientation`, exactly as PTB's DrawTexture(..., Orientation)
    rotates the same 1-D texture in runStimManager.m.
    """
    period = max(float(p.WaveSpPeriod), 1e-6)
    n_cycles = int(np.ceil(p.StimDiameter / period)) + 2
    size = max(int(round(n_cycles * period)), 1)
    x = np.arange(size, dtype=np.float64)
    linear = 0.5 + (0.5 * p.Contrast) * np.cos(2 * np.pi * (x / period))
    linear = np.clip(linear, 0.0, 1.0)
    corrected = white * (linear ** (1.0 / GAMMA))
    return np.clip(corrected, 0.0, 255.0).astype(np.uint8)


def aperture_geometry(p: StimParams, screen_w: int, screen_h: int
                      ) -> tuple[float, float, float]:
    """(center_x, center_y, radius) of the circular aperture, in screen px —
    the same geometry as the circleIdx/maskRadius block in runStimManager.m."""
    cx = screen_w / 2.0 + p.StimXPosition
    cy = screen_h / 2.0 + p.StimYPosition
    r = max(p.StimDiameter / 2.0, 0.0)
    return cx, cy, r
