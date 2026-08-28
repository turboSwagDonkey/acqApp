"""Settings + a frame in, a `PupilFit` out. No Qt, no EyeLoop imports.

The join between `PupilSettings` (what the operator set) and
`eyeloop_tracker.py` (what EyeLoop needs). It exists so the panel does not have
to know that the tracker is stateful, that it wants a crop rather than a frame,
or that re-arming is what a resized eye region requires.

EyeLoop is reached only through `eyeloop_tracker`, and only on first use — a
rig with no clone beside it runs exactly as before, with `available` False.
"""
from __future__ import annotations

import numpy as np

from acqApp.devices.pupil_cam.settings import PupilSettings


class PupilTracking:
    """One tracker, kept armed and re-armed as the eye region — or the model —
    changes.

    Holds the crop box it was armed for; when the operator drags or resizes the
    region the box changes and the tracker is rebuilt, because `Shape` computes
    its walk corners once from the frame size it was given. The fit model
    (ellipse/circle) is the same story: EyeLoop bakes it into the `Shape` it
    builds in `arm()` (`config.arguments.model`, read once at construction), so
    it is not one of `apply_settings`'s live knobs either — switching it has to
    re-arm, or the operator keeps fitting the old shape until something else
    (moving the region, restarting the session) happens to force a re-arm.
    """

    def __init__(self) -> None:
        self._tracker = None
        self._box: tuple[int, int, int, int] | None = None
        self._model: str | None = None
        self._error: str | None = None
        self.last_fit = None
        self.last_mask: np.ndarray | None = None
        self.last_box: tuple[int, int, int, int] | None = None

    @property
    def available(self) -> bool:
        """False when there is no EyeLoop clone. The error says where to get one."""
        return self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def reset(self) -> None:
        """Drop the tracker; the next frame re-arms it and re-seeds from centre."""
        self._tracker = None
        self._box = None
        self._model = None
        self.last_fit = None
        self.last_mask = None

    def track(self, frame: np.ndarray, st: PupilSettings):
        """Track one full frame. Returns a `PupilFit` in FULL-FRAME pixels, or None.

        None is genuine: the wrapper nulls EyeLoop's `params` before each frame,
        so a failed frame cannot return the previous one's answer.
        """
        self.last_fit = None
        self.last_mask = None
        if not st.track or frame is None or frame.ndim != 2:
            return None

        box = st.crop_box(frame.shape)
        if box is None:                 # no eye region: nothing to crop to
            return None
        x0, y0, x1, y1 = box
        self.last_box = box
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return None

        try:
            from acqApp.devices.pupil_cam.eyeloop_tracker import (
                EyeLoopTracker, EyeLoopUnavailable, GlintRemoval, Pin, PupilFit)
        except ImportError as e:        # pragma: no cover - import guard
            self._error = str(e)
            return None

        glint = GlintRemoval(
            enabled=st.cr_remove,
            threshold=st.cr_threshold,
            pad=st.cr_pad,
            ring=st.cr_ring,
            search_scale=st.cr_reach,
            # Pins are stored in full-frame pixels so that moving the eye
            # region does not walk them off the reflections they mark.
            pins=tuple(Pin(px - x0, py - y0, pr) for px, py, pr in st.cr_pins),
        )

        if (self._tracker is None or self._box != box
                or self._model != st.track_model):
            try:
                self._tracker = EyeLoopTracker(
                    threshold=st.track_threshold, blur=st.track_blur,
                    model=st.track_model, glint=glint)
                self._tracker.arm(x1 - x0, y1 - y0,
                                  ((x1 - x0) / 2.0, (y1 - y0) / 2.0))
                self._box = box
                self._model = st.track_model
                self._error = None
            except EyeLoopUnavailable as e:
                self._tracker = None
                self._error = str(e)
                return None
        else:
            self._tracker.glint = glint
            self._tracker.apply_settings(threshold=st.track_threshold,
                                         blur=st.track_blur)

        fit = self._tracker.track(crop)
        self.last_mask = self._tracker.last_glint_mask
        if fit is None:
            return None

        self.last_fit = PupilFit(fit.center_x + x0, fit.center_y + y0,
                                 fit.semi_major, fit.semi_minor, fit.angle_deg)
        return self.last_fit
