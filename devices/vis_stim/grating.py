"""Grating texture + aperture geometry, ported from visStimCode's
genGratingTex and runStimManager.m. No Qt. `window.py` paints the row
rotated by Orientation and clips it to the aperture (in place of MATLAB's
alpha-masked overlay texture).
"""
from __future__ import annotations

import numpy as np

from .settings import StimParams

GAMMA = 2.2


def build_grating(p: StimParams, white: float = 255.0) -> np.ndarray:
    """One row of a gamma-corrected sinusoidal grating, uint8,
    `ceil(StimDiameter / WaveSpPeriod) + 2` cycles wide. Rotated at paint
    time, as PTB's DrawTexture(..., Orientation) did."""
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
    """(center_x, center_y, radius) in screen px (runStimManager.m's
    circleIdx/maskRadius)."""
    cx = screen_w / 2.0 + p.StimXPosition
    cy = screen_h / 2.0 + p.StimYPosition
    r = max(p.StimDiameter / 2.0, 0.0)
    return cx, cy, r
