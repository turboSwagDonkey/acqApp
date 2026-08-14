"""Pupil camera — the settings model. No Qt (the widgets are in `panel.py`).

Bundles the camera (exposure, frame rate), the tracking parameters fed to
`tracking.detect`, and the eye-tracking LED toggle.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class PupilSettings:
    exposure_us: float = 8000.0
    fps:         float = 20.0
    threshold:   int   = 60      # seed-blob threshold for tracking.detect
    min_r:       int   = 10      # px
    max_r:       int   = 80      # px
    # ── annulus edge search (tracking.find_circular_edge) ──
    n_rays:       int   = 64        # search lines through the annulus
    polarity:     str   = "rising"  # dark pupil → bright iris, scanning outward
    min_strength: float = 4.0       # min |gradient| (grey levels/px) per ray
    fit:          str   = "circle"  # "circle" or "ellipse"
    # Display-only: draw the annulus and per-ray edge points over the preview,
    # and allow click-to-seed. Persisted (it is a working preference, not
    # runtime state like the LED) but deliberately absent from the session
    # metadata — how the operator was looking at the fit is not a property of
    # the recording.
    show_search:  bool  = False
