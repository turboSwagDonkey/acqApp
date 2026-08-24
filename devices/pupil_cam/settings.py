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
    # ── search limit ──
    # The animal is head-fixed, so the eye occupies one fixed part of the frame.
    # This circle (camera px) is where the pupil is allowed to be: it bounds the
    # seed search and rejects a fit centred outside it. r <= 0 = whole frame.
    # It constrains the CENTRE, not the edge points — a rim may cross it.
    limit_x:      float = 0.0
    limit_y:      float = 0.0
    limit_r:      float = 0.0
    # ── ignored directions ──
    # Angular sectors the edge search skips, as (from, to) degrees. 0 = +x and
    # the angle increases toward +y, which is image DOWN — so 90 is the image
    # bottom. This is for the eyelids: where a lid crosses the pupil the rays
    # find the LID's edge, and those points go into the fit like any other.
    # Measured on the rig clip, the lids occupy about 60-160 and 235-290.
    exclude_deg:  tuple = ()
    # Replay a clip instead of the camera; "" = camera/mock. Recorded in the
    # session metadata — replayed frames must never read as rig data.
    video_path:   str   = ""
    # Draw the annulus and per-ray edges, and allow click-to-seed. Persisted but
    # kept out of the metadata: how the operator looked at the fit is not a
    # property of the recording.
    show_search:  bool  = False

    def search_limit(self) -> tuple[float, float, float] | None:
        """The limit circle as (cx, cy, r), or None for the whole frame.

        Three fields, one tracker option: "no limit" then has exactly one
        representation, so nothing downstream has to test the radius itself.
        """
        if self.limit_r <= 0.0:
            return None
        return (float(self.limit_x), float(self.limit_y), float(self.limit_r))

    def risky(self) -> list[tuple[str, str]]:
        """(field, cost) for values measured to break real footage. Warns only.

        Needs a measured number to qualify. smooth_sigma is out: its best value
        is 0.5 on one rig clip and 1.5-3.0 on another, so any cutoff is invented.
        """
        out: list[tuple[str, str]] = []
        if self.edge_select == "strongest":
            out.append(("edge_select",
                        "takes the eyelid margin instead of the pupil rim; on "
                        "the rig clips 151/151 frames becomes 44/151"))
        return out

    def excluded(self) -> tuple[tuple[float, float], ...]:
        """`exclude_deg` as pairs, whatever the JSON round trip made of it.

        Saved as a tuple of tuples and read back as a list of lists, and a
        hand-edited config can hold anything at all — a malformed entry is
        dropped rather than raised on, since the cost of ignoring one is a
        noisier fit and the cost of raising is an app that will not start.
        """
        out: list[tuple[float, float]] = []
        for item in self.exclude_deg or ():
            try:
                lo, hi = item
                out.append((float(lo), float(hi)))
            except (TypeError, ValueError):
                continue
        return tuple(out)
