"""Orientation-tuning trial: which orientations to sweep, and how many
pretrial flashes come first. The aperture geometry itself is shared
(circle.py) — this module owns only what is specific to Tuning.
"""
from __future__ import annotations

N_ORIENTATIONS = 8
N_PRETRIALS = 2
ORIENTATION_STEP_DEG = 45.0


def orientations() -> tuple[float, ...]:
    """0, 45, 90, ..., 315 degrees."""
    return tuple(i * ORIENTATION_STEP_DEG for i in range(N_ORIENTATIONS))
