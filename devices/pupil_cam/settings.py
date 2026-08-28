"""Pupil camera settings model — camera, eye region, tracking. No Qt (see `panel.py`).

Tracking knobs returned 2026-08-26 with EyeLoop, configuring
`eyeloop_tracker.py` rather than the tracker retired on 2026-08-24.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class PupilSettings:
    exposure_us: float = 8000.0
    fps:         float = 20.0
    # ── eye region ──
    # The animal is head-fixed, so the eye occupies one fixed part of the frame.
    # Kept through the tracker's removal: it is drawn by hand on the preview and
    # is the one piece of eye geometry the operator has already set.
    # A rectangle, x0<x1 and y0<y1; anything else (including the shipped
    # 0,0,0,0) = no region.
    limit_x0:     float = 0.0
    limit_y0:     float = 0.0
    limit_x1:     float = 0.0
    limit_y1:     float = 0.0
    # Replay a clip instead of the camera; "" = camera/mock. Recorded in the
    # session metadata — replayed frames must never read as rig data.
    video_path:   str = ""

    # ── tracking ──
    # Off by default: it needs a clone of EyeLoop beside the repo, and a rig
    # that has never run it should not start failing at launch because of it.
    track:            bool = False
    # THE consequential number. Threshold sets the reported radius — a 60 %
    # swing across 25-60 on the test clips, at a clean 151/151 fit rate the
    # whole way. It is illumination-dependent; expect to set it per session,
    # and see docs/EYELOOP.md before trusting a radius.
    track_threshold:  int = 45
    track_blur:       int = 3
    # "ellipsoid" or "circular". Circular is ~2.5x cheaper and equally steady
    # on the test clips; ellipsoid gives the ellipse, which is why EyeLoop was
    # adopted at all.
    track_model:      str = "ellipsoid"
    # Rolling mean of the last `smooth_window` fits — trades frame-to-frame
    # jitter in the outline for lag. Off by default so a session captures the
    # raw fit unless the operator opts into smoother numbers; applies to what
    # is drawn AND what is recorded, so the trace matches what was looked at.
    smooth:           bool = False
    smooth_window:    int = 5

    # ── blink detection ──
    # A blink reads as a sudden, large drop in the fitted radius — the pupil
    # itself does not shrink 30%+ frame to frame, a closing eyelid does. Runs
    # on the RAW fit regardless of `smooth`: averaging is built to blur
    # exactly this kind of sudden change, and must not be able to hide a
    # blink from the detector meant to flag it.
    blink_detect:          bool = False
    # radius <= (recent baseline) * (1 - this) => flagged.
    blink_drop_frac:       float = 0.35
    # How many recent non-blink frames the baseline is the median of.
    blink_baseline_window: int = 15

    # ── corneal reflection ──
    # EyeLoop's own removal has never run (disabled in three places upstream),
    # so this configures ours. Defaults chosen not to inflate the radius rather
    # than to maximise anything.
    cr_remove:        bool = True
    cr_threshold:     int = 120
    cr_pad:           int = 4
    cr_ring:          int = 6
    # Fraction of the fitted ELLIPSE to search. Past ~0.85 it starts masking
    # the eyelash line, which erases the pupil boundary and inflates the radius.
    cr_reach:         float = 0.70
    # Reflections the operator marked, as (x, y, r) in FULL-FRAME pixels.
    # Rig geometry, not a preference: they record where the fixed reflections
    # land, so they belong with the eye region and must be cleared when the
    # optics move.
    cr_pins:          list[tuple[float, float, float]] = field(default_factory=list)
    # Persisted, though it is a view preference: it is how cr_threshold gets
    # tuned, and re-ticking it every launch is friction that stops it being used.
    cr_show_mask:     bool = False

    def __post_init__(self) -> None:
        """Normalise `cr_pins`: JSON has no tuples, so a reloaded pin is a list.

        A list still unpacks, so it still *works* — and compares unequal to
        what the panel emits, which shows up only as a save that never settles.
        """
        self.cr_pins = [tuple(float(v) for v in pin) for pin in self.cr_pins]

    def search_limit(self) -> tuple[float, float, float, float] | None:
        """The region as (x0, y0, x1, y1), or None. One representation for "none"."""
        if self.limit_x1 <= self.limit_x0 or self.limit_y1 <= self.limit_y0:
            return None
        return (float(self.limit_x0), float(self.limit_y0),
                float(self.limit_x1), float(self.limit_y1))

    def crop_box(self, shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """The region, clamped to `shape`, as (x0, y0, x1, y1).

        Not an optimisation: EyeLoop fits *nothing* on a full rig frame (0/151)
        and the same answer on any crop from 200 to 900 px.
        """
        lim = self.search_limit()
        if lim is None:
            return None
        h, w = shape
        x0, y0, x1, y1 = lim
        x0 = min(max(x0, 0.0), w - 1)
        y0 = min(max(y0, 0.0), h - 1)
        x1 = min(max(x1, x0 + 1.0), w)
        y1 = min(max(y1, y0 + 1.0), h)
        return int(x0), int(y0), int(x1), int(y1)
