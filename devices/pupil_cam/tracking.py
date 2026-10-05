"""Settings + a frame in, a `PupilFit` out. No Qt.

Hides that the tracker is stateful, wants a crop, and must be re-armed when
the eye region changes. EyeLoop is imported on first use only; with no clone
`available` is False and nothing else changes.
"""
from __future__ import annotations

import numpy as np

from acqApp.devices.pupil_cam.rim import ContrastMeter, looks_like_pupil
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.whiskers import paint_whiskers, whisker_mask


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
        # Underexposure: how far the pupil stands out from the iris.
        self.contrast = ContrastMeter()
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
        if crop.size == 0 or not self._ready(box, st):
            return None

        if st.track_whiskers:
            crop = paint_whiskers(crop, whisker_mask(crop))
        fit = self._tracker.track(crop)
        self.last_mask = self._tracker.last_glint_mask   # shown red
        if fit is None:
            return None
        # Output only: the walk carries on from EyeLoop's own answer.
        if st.track_rim_check and not looks_like_pupil(
                self._tracker.last_input, fit, st.track_rim_dark):
            return None
        self.contrast.offer(self._tracker.last_input, fit)

        PupilFit = self._eyeloop_cls[4]
        return PupilFit(fit.center_x + x0, fit.center_y + y0,
                        fit.semi_major, fit.semi_minor, fit.angle_deg)

    def seed(self, shape: tuple[int, ...], st: PupilSettings, fit) -> None:
        """Start the next frame from `fit` (full-frame px), as if it had just
        been tracked: where the walk begins and where reflections are looked
        for. A jump in a clip lands where a run through it would have been."""
        box = st.crop_box(shape)
        if fit is None or box is None or not self._ready(box, st):
            return
        x0, y0 = box[0], box[1]
        PupilFit = self._eyeloop_cls[4]
        self._tracker.seed(PupilFit(fit.center_x - x0, fit.center_y - y0,
                                    fit.semi_major, fit.semi_minor,
                                    fit.angle_deg))

    def _ready(self, box: tuple[int, int, int, int], st: PupilSettings) -> bool:
        """The tracker armed for `box` and the model, with `st`'s knobs."""
        x0, y0, x1, y1 = box
        if self._eyeloop_cls is None:
            try:
                from acqApp.devices.pupil_cam.eyeloop_tracker import (
                    EyeLoopTracker, EyeLoopUnavailable, GlintRemoval, Pin,
                    PupilFit)
            except ImportError as e:    # pragma: no cover - import guard
                self._error = str(e)
                return False
            self._eyeloop_cls = (EyeLoopTracker, EyeLoopUnavailable,
                                 GlintRemoval, Pin, PupilFit)
        EyeLoopTracker, EyeLoopUnavailable, GlintRemoval, Pin, _ = \
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
                return False
        else:
            self._tracker.glint = glint
            self._tracker.apply_settings(threshold=st.track_threshold,
                                         blur=st.track_blur)
        return True
