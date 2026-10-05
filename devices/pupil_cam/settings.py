"""Pupil camera settings — camera, eye region, EyeLoop tracking. No Qt."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class PupilSettings:
    exposure_us: float = 8000.0
    rate_hz:     float = 20.0
    # ── eye region (x0<x1, y0<y1; anything else = none) ──
    limit_x0:     float = 0.0
    limit_y0:     float = 0.0
    limit_x1:     float = 0.0
    limit_y1:     float = 0.0
    # A drag, Set eye region and Auto leave a locked region where it is.
    limit_locked: bool = False
    # Replay a clip instead of the camera; "" = camera/mock.
    video_path:   str = ""

    # ── tracking (needs an EyeLoop clone beside the repo) ──
    track:            bool = False
    # Sets the reported radius (~60% swing over 25-60) at an unchanged fit
    # rate; illumination-dependent (docs/EYELOOP.md).
    track_threshold:  int = 45
    track_blur:       int = 3
    track_model:      str = "ellipsoid"     # or "circular" (~2.5x cheaper)
    # Drop a fit whose disc isn't mostly darker than its rim (closed eye):
    # none, not a guess. rim.py.
    track_rim_check:  bool = False
    track_rim_dark:   float = 0.8
    # Paint long straight bright ridges over before the fit. whiskers.py.
    track_whiskers:   bool = True
    # Rolling mean; applies to the drawn AND recorded fit.
    smooth:           bool = False
    smooth_window:    int = 5

    # ── blink detection (on the RAW fit, so smoothing can't hide one) ──
    blink_detect:          bool = False
    blink_drop_frac:       float = 0.35     # radius <= baseline * (1 - this)
    blink_baseline_window: int = 15         # median of this many recent frames

    # ── corneal reflection (ours; EyeLoop's is disabled upstream) ──
    cr_remove:        bool = True
    cr_threshold:     int = 120
    cr_pad:           int = 4
    cr_ring:          int = 6
    # Fraction of the ellipse searched; past ~0.85 it masks the lash line and
    # inflates the radius.
    cr_reach:         float = 0.70
    # (x, y, r) in full-frame px. Rig geometry: clear when the optics move.
    cr_pins:          list[tuple[float, float, float]] = field(default_factory=list)
    # Unused: what removal blanks is always shown while it is on. Kept so
    # older saved settings still load.
    cr_show_mask:     bool = False

    # ── preview ──
    show_lut:     bool = True
    auto_levels:  bool = True

    # Live: say when the pupil barely stands out from the iris (rim.py).
    warn_dark:       bool = True

    led_follow_live: bool = True
    led_intensity:   float = 1.0            # 0..1 of the LEDD1B's MOD range

    def __post_init__(self) -> None:
        # JSON reloads pins as lists, which compare unequal to the panel's
        # tuples: a save that never settles.
        self.cr_pins = [tuple(float(v) for v in pin) for pin in self.cr_pins]

    def search_limit(self) -> tuple[float, float, float, float] | None:
        if self.limit_x1 <= self.limit_x0 or self.limit_y1 <= self.limit_y0:
            return None
        return (float(self.limit_x0), float(self.limit_y0),
                float(self.limit_x1), float(self.limit_y1))

    def crop_box(self, shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """The region clamped to `shape`. Required, not an optimisation:
        EyeLoop fits nothing on a full rig frame."""
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
