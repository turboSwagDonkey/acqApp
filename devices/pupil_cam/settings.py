"""Pupil camera settings model — camera, tracking, LED. No Qt (see `panel.py`)."""
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
    # "first" = innermost edge, right by construction; "strongest" locks the
    # eyelid margin, which out-contrasts the pupil on real IR footage.
    edge_select:    str   = "first"
    smooth_sigma:   float = 1.5     # px along each ray; raise if fur/lash noise wins
    # conf = (rays kept / cast) * exp(-rms/2). Half the rays miss on a real eye,
    # capping a *good* fit near 0.28. Raise towards 0.3 for a synthetic disc.
    min_confidence: float = 0.10
    # ── temporal filtering (PupilTracker) ──
    # Runs over CONSECUTIVE tracked frames only, so a blink stays lost frames
    # rather than being interpolated across. 1 / 1.0 disables each.
    smooth_median:  int   = 3        # frames in the median window
    smooth_ema:     float = 0.5      # EMA weight on the newest frame
    # Losses before re-thresholding; the pre-blink estimate is kept if that
    # finds nothing, so a reopening eye resumes instead of being hunted for.
    reseed_after:   int   = 30
    # Replay a clip instead of the camera; "" = camera/mock. Recorded in the
    # session metadata — replayed frames must never read as rig data.
    video_path:   str   = ""
    # Draw the annulus and per-ray edges, and allow click-to-seed. Persisted but
    # kept out of the metadata: how the operator looked at the fit is not a
    # property of the recording.
    show_search:  bool  = False
