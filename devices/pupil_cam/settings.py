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
    # Which qualifying edge on a ray is the pupil — see rays._edges_along_rays.
    # "first" (innermost) is right by construction; "strongest" locks onto the
    # eyelid margin whenever it out-contrasts the pupil edge, which on real IR
    # footage is always.
    edge_select:    str   = "first"
    # Smoothing along each ray, px. Raise it if the pupil edge is being lost in
    # fur or lash texture; 1.5 tracks the rig's full-frame footage as it stands.
    smooth_sigma:   float = 1.5
    # Confidence floor below which PupilTracker calls the frame lost.
    # conf = (rays kept / rays cast) * exp(-rms/2). Only ~half the rays ever
    # find an edge on a real eye (lashes, lids, the glint), which caps a *good*
    # real fit around 0.28 — so the old 0.25 threw away frames whose rms was
    # 1.3 px. Measured on rig footage: 0.10 keeps them and still rejects the
    # blinks. Raise it towards 0.3 for a clean synthetic disc.
    min_confidence: float = 0.10
    # ── temporal filtering (PupilTracker) ──
    # Smoothing runs over CONSECUTIVE tracked frames only, so a blink still
    # reads as lost frames rather than being interpolated over. The median
    # removes the one-or-two-frame excursions a half-closed lid causes; the EMA
    # takes out the residual jitter. 1 / 1.0 disables each.
    smooth_median:  int   = 3        # frames in the median window
    smooth_ema:     float = 0.5      # EMA weight on the newest frame
    # Consecutive losses before the tracker re-thresholds. It still keeps the
    # pre-blink estimate if that finds nothing, so a reopening eye resumes
    # where it left off instead of being hunted for from scratch.
    reseed_after:   int   = 30
    # Replay a recorded clip instead of the camera — "" = use the camera (or the
    # mock when emulating). For tuning the tracker on real footage; a session
    # recorded from it is not rig data, and the metadata says so.
    video_path:   str   = ""
    # Display-only: draw the annulus and per-ray edge points over the preview,
    # and allow click-to-seed. Persisted (it is a working preference, not
    # runtime state like the LED) but deliberately absent from the session
    # metadata — how the operator was looking at the fit is not a property of
    # the recording.
    show_search:  bool  = False
