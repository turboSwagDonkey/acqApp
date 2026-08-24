"""Pupil camera settings model — camera, eye region, LED. No Qt (see `panel.py`).

The tracking knobs are gone with the tracker (2026-08-24, PLAN §7 (ai)); they
are in `archive/pupil_tracking/` with the algorithm they configured.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class PupilSettings:
    exposure_us: float = 8000.0
    fps:         float = 20.0
    # ── eye region ──
    # The animal is head-fixed, so the eye occupies one fixed part of the frame.
    # Kept through the tracker's removal: it is drawn by hand on the preview and
    # is the one piece of eye geometry the operator has already set.
    # r <= 0 = no region.
    limit_x:      float = 0.0
    limit_y:      float = 0.0
    limit_r:      float = 0.0
    # Replay a clip instead of the camera; "" = camera/mock. Recorded in the
    # session metadata — replayed frames must never read as rig data.
    video_path:   str = ""

    def search_limit(self) -> tuple[float, float, float] | None:
        """The region as (cx, cy, r), or None. One representation for "none"."""
        if self.limit_r <= 0.0:
            return None
        return (float(self.limit_x), float(self.limit_y), float(self.limit_r))
