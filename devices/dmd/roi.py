"""Stimulation ROIs — the model. No Qt (the editor is in `roi_panel.py`).

ROIs are held in **camera pixels**, because that is the space the operator draws
in: they are placed on a snapshot of the sample. Turning a set into mirrors is
`RoiSet.dmd_frame()`, which needs a `DmdCalibration` — without one there is no
answer, and guessing would aim light at the wrong place.

Two shapes, because those are what an operator asks for: a rectangle (with
rotation, since a cortical column rarely lines up with the sensor) and a circle.
Both are just `mask()`; adding a third shape means one more class, not an edit
to the set or the editor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

from acqApp.devices.dmd.calibration import ON, OFF, DmdCalibration, mask_from_roi


@dataclass
class _Roi:
    name: str = ""
    enabled: bool = True

    kind: str = "roi"

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class RectRoi(_Roi):
    """Axis-aligned unless `angle_deg` says otherwise; (x, y) is the centre.

    Centre rather than a corner so rotation does not move it, which is what an
    operator dragging a handle expects.
    """
    x: float = 0.0
    y: float = 0.0
    w: float = 10.0
    h: float = 10.0
    angle_deg: float = 0.0

    kind: str = "rect"

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        # Broadcast rather than np.mgrid: the editor rebuilds every ROI's mask
        # on every drag, and at ORCA full frame mgrid alone is two 84 MB int64
        # grids per ROI. Unrotated, the two conditions stay separable and only
        # the (H, W) bool is ever materialised.
        h, w = shape
        dx = np.arange(w, dtype=np.float64)[None, :] - self.x
        dy = np.arange(h, dtype=np.float64)[:, None] - self.y
        if self.angle_deg:
            t = np.radians(self.angle_deg)
            c, s = np.cos(t), np.sin(t)
            dx, dy = c * dx + s * dy, -s * dx + c * dy
        return (np.abs(dx) <= self.w / 2.0) & (np.abs(dy) <= self.h / 2.0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "rect", "name": self.name, "enabled": self.enabled,
                "x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "angle_deg": self.angle_deg}


@dataclass
class CircleRoi(_Roi):
    """(x, y) centre, `r` radius, in camera px."""
    x: float = 0.0
    y: float = 0.0
    r: float = 10.0

    kind: str = "circle"

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        h, w = shape                        # broadcast — see RectRoi.mask
        dx2 = (np.arange(w, dtype=np.float64) - self.x) ** 2
        dy2 = (np.arange(h, dtype=np.float64) - self.y) ** 2
        return dx2[None, :] + dy2[:, None] <= self.r ** 2

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "circle", "name": self.name, "enabled": self.enabled,
                "x": self.x, "y": self.y, "r": self.r}


_KINDS = {"rect": RectRoi, "circle": CircleRoi}


def roi_from_dict(d: dict[str, Any]) -> _Roi:
    kind = d.get("kind", "rect")
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown ROI kind {kind!r}")
    return cls(**{k: v for k, v in d.items() if k != "kind"})


@dataclass
class RoiSet:
    """An ordered, editable collection of ROIs in camera pixels."""
    rois: list[_Roi] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rois)

    def __iter__(self) -> Iterator[_Roi]:
        return iter(self.rois)

    def __getitem__(self, i: int) -> _Roi:
        return self.rois[i]

    def add(self, roi: _Roi) -> _Roi:
        if not roi.name:
            roi.name = self._unique_name(roi.kind)
        self.rois.append(roi)
        return roi

    def remove(self, i: int) -> _Roi:
        return self.rois.pop(i)

    def clear(self) -> None:
        self.rois.clear()

    def _unique_name(self, stem: str) -> str:
        taken = {r.name for r in self.rois}
        n = 1
        while f"{stem}{n}" in taken:
            n += 1
        return f"{stem}{n}"

    # ── rasterising ──────────────────────────────────────────────────────────
    def mask(self, shape: tuple[int, int], *, enabled_only: bool = True
             ) -> np.ndarray:
        """Union of the ROIs as a camera-space bool mask."""
        out = np.zeros(shape, bool)
        for r in self.rois:
            if enabled_only and not r.enabled:
                continue
            out |= r.mask(shape)
        return out

    def clipped_mask(self, calib: DmdCalibration) -> tuple[np.ndarray, float]:
        """The mask restricted to what the DMD can reach → (mask, kept fraction).

        The fraction is returned rather than silently dropping the remainder:
        an ROI half outside the projector's field still *looks* drawn, and the
        operator has to be told that half of it will never be illuminated.
        """
        shape = (calib.cam_size[1], calib.cam_size[0])
        want = self.mask(shape)
        ok = want & calib.accessible_mask(shape)
        n = int(want.sum())
        return ok, (float(ok.sum()) / n if n else 1.0)

    def outside(self, calib: DmdCalibration) -> list[str]:
        """Names of ROIs that are not wholly inside the DMD's field."""
        shape = (calib.cam_size[1], calib.cam_size[0])
        reach = calib.accessible_mask(shape)
        out = []
        for r in self.rois:
            if not r.enabled:
                continue
            m = r.mask(shape)
            if m.any() and not m[reach].sum() == m.sum():
                out.append(r.name)
        return out

    def dmd_frame(self, calib: DmdCalibration) -> np.ndarray:
        """The device-sized binary frame that illuminates these ROIs."""
        shape = (calib.cam_size[1], calib.cam_size[0])
        return mask_from_roi(self.mask(shape), calib.dmd_to_cam,
                             calib.dmd_size[0], calib.dmd_size[1])

    # ── persistence ──────────────────────────────────────────────────────────
    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rois]

    @classmethod
    def from_list(cls, items) -> "RoiSet":
        return cls([roi_from_dict(d) for d in items or []])
