"""Contrast-tuning trial: which contrast levels to sweep, and how many
pretrial flashes come first. The aperture geometry itself is shared
(circle.py) — this module owns only what is specific to Contrast.
"""
from __future__ import annotations

CONTRAST_LEVELS = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)
N_LEVELS = len(CONTRAST_LEVELS)
N_PRETRIALS = 2
