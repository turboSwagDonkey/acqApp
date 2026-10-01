"""Settings + a frame in, a `PupilFit` out. No Qt.

Hides that the tracker is stateful, wants a crop, and must be re-armed when
the eye region changes. EyeLoop is imported on first use only; with no clone
`available` is False and nothing else changes.
"""
from __future__ import annotations

import numpy as np

from acqApp.devices.pupil_cam.settings import PupilSettings


class PupilTracking:
    """One tracker, re-armed when the crop box or the fit model changes.

    `Shape` computes its walk corners once from the frame size, and EyeLoop
    bakes the model (`config.arguments.model`) into the Shape built at
    `arm()` — so neither is a live `apply_settings` knob. Without its own
    re-arm, a model switch keeps fitting the old shape.
    """

    def __init__(self) -> None:
        self._tracker = None
        self._box: tuple[int, int, int, int] | None = None
        self._model: str | None = None
        self._error: str | None = None
        self.last_mask: np.ndarray | None = None
        self.last_box: tuple[int, int, int, int] | None = None
        # Cached on success only, so a failed import retries every call.
        self._eyeloop_cls: tuple | None = None

    @property
    def available(self) -> bool:
        """False when there's no EyeLoop clone. The error says where to get one."""
        return self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def track(self, frame: np.ndarray, st: PupilSettings):
        """One full frame -> a `PupilFit` in FULL-FRAME pixels, or a genuine
        None (never the previous frame's answer)."""
        self.last_mask = None
        if not st.track or frame is None or frame.ndim != 2:
            return None

        box = st.crop_box(frame.shape)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        self.last_box = box
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return None

        if self._eyeloop_cls is None:
            try:
                from acqApp.devices.pupil_cam.eyeloop_tracker import (
                    EyeLoopTracker, EyeLoopUnavailable, GlintRemoval, Pin,
                    PupilFit)
            except ImportError as e:    # pragma: no cover - import guard
                self._error = str(e)
                return None
            self._eyeloop_cls = (EyeLoopTracker, EyeLoopUnavailable,
                                 GlintRemoval, Pin, PupilFit)
        EyeLoopTracker, EyeLoopUnavailable, GlintRemoval, Pin, PupilFit = \
            self._eyeloop_cls

        glint = GlintRemoval(
            enabled=st.cr_remove,
            threshold=st.cr_threshold,
            pad=st.cr_pad,
            ring=st.cr_ring,
            search_scale=st.cr_reach,
            # Full-frame px, so moving the region can't walk them off.
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

        return PupilFit(fit.center_x + x0, fit.center_y + y0,
                        fit.semi_major, fit.semi_minor, fit.angle_deg)
