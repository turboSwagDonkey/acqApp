"""Stimulation ROIs — the model. No Qt (the editor is `roi_panel.py`).

ROIs are held in **camera pixels**, the space the operator draws in. Mirrors
come from `RoiSet.dmd_frame()`, which needs a `DmdCalibration`: without one
there's no answer, and guessing would aim light at the wrong place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

from acqApp.devices.dmd.calibration import (OFF, ON, DmdCalibration,
                                            apply_transform)


@dataclass
class _Roi:
    name: str = ""
    enabled: bool = True
    kind: str = "roi"

    def mask_at(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Coverage on the camera grid xs × ys -> (len(ys), len(xs)); a
        coarse grid is enough for a percentage."""
        raise NotImplementedError

    def contains(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        """Membership at SCATTERED points (the projection path's mirror
        positions); sampling a camera-sized grid cost 107 ms per rebuild."""
        raise NotImplementedError

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        h, w = shape
        return self.mask_at(np.arange(w, dtype=np.float64),
                            np.arange(h, dtype=np.float64))

    def boundary(self, n: int = 64) -> np.ndarray:
        """Points on the outline, for containment tests without rasterising."""
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


def _to_local(dx: np.ndarray, dy: np.ndarray, angle_deg: float) -> tuple:
    """Rotate centre-relative offsets into the rect's unrotated frame."""
    if not angle_deg:
        return dx, dy
    t = np.radians(angle_deg)
    c, s = np.cos(t), np.sin(t)
    return c * dx + s * dy, -s * dx + c * dy


@dataclass
class RectRoi(_Roi):
    """(x, y) is the centre, so rotating doesn't move it."""
    x: float = 0.0
    y: float = 0.0
    w: float = 10.0
    h: float = 10.0
    angle_deg: float = 0.0
    kind: str = "rect"

    def mask_at(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        # Broadcast, not np.mgrid: that is two 84 MB int64 grids per ROI per
        # drag at ORCA full frame.
        dx = np.asarray(xs, dtype=np.float64)[None, :] - self.x
        dy = np.asarray(ys, dtype=np.float64)[:, None] - self.y
        dx, dy = _to_local(dx, dy, self.angle_deg)
        return (np.abs(dx) <= self.w / 2.0) & (np.abs(dy) <= self.h / 2.0)

    def contains(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        dx = np.asarray(px, dtype=np.float64) - self.x
        dy = np.asarray(py, dtype=np.float64) - self.y
        dx, dy = _to_local(dx, dy, self.angle_deg)
        return (np.abs(dx) <= self.w / 2.0) & (np.abs(dy) <= self.h / 2.0)

    def boundary(self, n: int = 64) -> np.ndarray:
        """The four corners: exact, since a projective map keeps lines
        straight."""
        t = np.radians(self.angle_deg)
        c, s = np.cos(t), np.sin(t)
        hw, hh = self.w / 2.0, self.h / 2.0
        loc = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]])
        return np.column_stack((
            self.x + c * loc[:, 0] - s * loc[:, 1],
            self.y + s * loc[:, 0] + c * loc[:, 1]))

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

    def mask_at(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        dx2 = (np.asarray(xs, dtype=np.float64) - self.x) ** 2   # see RectRoi
        dy2 = (np.asarray(ys, dtype=np.float64) - self.y) ** 2
        return dx2[None, :] + dy2[:, None] <= self.r ** 2

    def contains(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        dx = np.asarray(px, dtype=np.float64) - self.x
        dy = np.asarray(py, dtype=np.float64) - self.y
        return dx * dx + dy * dy <= self.r ** 2

    def boundary(self, n: int = 64) -> np.ndarray:
        """`n` points around the rim; sampled (its projective image is a
        conic), 64 points resolve it to 0.1 % of r."""
        t = np.linspace(0.0, 2.0 * np.pi, max(8, n), endpoint=False)
        return np.column_stack((self.x + self.r * np.cos(t),
                                self.y + self.r * np.sin(t)))

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
        """Camera-space mask clipped to the reachable field -> (mask, kept).
        The exact answer `reach_fraction` estimates; not on the projection
        path."""
        shape = (calib.cam_size[1], calib.cam_size[0])
        want = self.mask(shape)
        ok = want & calib.accessible_mask(shape)
        n = int(want.sum())
        return ok, (float(ok.sum()) / n if n else 1.0)

    def reach_fraction(self, calib: DmdCalibration, *,
                       max_side: int = 512) -> float:
        """Share of the drawn area the DMD can illuminate — an ESTIMATE, for
        the status line on every drag.

        Grid capped at `max_side`, each ROI bounded to its bbox and the scan
        to their union: 1308 -> 190 us at four ROIs (2026-08-25).
        """
        w, h = calib.cam_size
        step = max(1, int(np.ceil(max(int(w), int(h)) / max(1, max_side))))
        xs = np.arange(0, int(w), step, dtype=np.float64)
        ys = np.arange(0, int(h), step, dtype=np.float64)
        want = np.zeros((ys.size, xs.size), bool)
        i0 = j0 = np.iinfo(np.int32).max        # union bbox, in grid indices
        i1 = j1 = 0
        for r in self.rois:
            if not r.enabled:
                continue
            b = r.boundary()
            a0 = max(0, int(np.searchsorted(xs, b[:, 0].min(), "left")) - 1)
            a1 = min(xs.size, int(np.searchsorted(xs, b[:, 0].max(), "right")) + 1)
            c0 = max(0, int(np.searchsorted(ys, b[:, 1].min(), "left")) - 1)
            c1 = min(ys.size, int(np.searchsorted(ys, b[:, 1].max(), "right")) + 1)
            if a1 <= a0 or c1 <= c0:
                continue                        # entirely off the grid
            want[c0:c1, a0:a1] |= r.mask_at(xs[a0:a1], ys[c0:c1])
            i0, i1 = min(i0, a0), max(i1, a1)
            j0, j1 = min(j0, c0), max(j1, c1)
        if i1 <= i0 or j1 <= j0:
            return 1.0
        iy, ix = np.nonzero(want[j0:j1, i0:i1])
        if not iy.size:
            return 1.0
        pts = np.column_stack((xs[ix + i0], ys[iy + j0]))
        return float(calib.accessible(pts).mean())

    def outside(self, calib: DmdCalibration) -> list[str]:
        """Names of ROIs not wholly inside the DMD's field. Geometric: a
        full-frame mask per ROI was ~90 ms per drag, and less accurate."""
        return [r.name for r in self.rois
                if r.enabled and not calib.accessible(r.boundary()).all()]

    def dim(self, calib: DmdCalibration) -> list[str]:
        """Names of ROIs at least partly outside the marked vignette circle
        (reachable but dim). Advisory; empty if no vignette is marked."""
        return [r.name for r in self.rois
                if r.enabled and not calib.well_lit(r.boundary()).all()]

    def contains(self, px: np.ndarray, py: np.ndarray, *,
                 enabled_only: bool = True) -> np.ndarray:
        """Union of the ROIs at scattered camera points."""
        out = np.zeros(np.shape(px), dtype=bool)
        for roi in self.rois:
            if enabled_only and not roi.enabled:
                continue
            out |= roi.contains(px, py)
        return out

    def dmd_frame(self, calib: DmdCalibration, *,
                  enabled_only: bool = True) -> np.ndarray:
        """The device-sized binary frame that illuminates these ROIs.

        Asks each MIRROR whether it lands in an ROI, not the reverse: a
        forward map leaves holes wherever the DMD is coarser than the camera.
        Only mirrors inside each ROI's mapped bbox are asked.
        """
        w, h = int(calib.dmd_size[0]), int(calib.dmd_size[1])
        cw, ch = calib.cam_size
        M = np.asarray(calib.dmd_to_cam, dtype=np.float64)
        out = np.zeros((h, w), dtype=bool)

        for roi in self.rois:
            if enabled_only and not roi.enabled:
                continue
            d = apply_transform(calib.cam_to_dmd, roi.boundary(64))
            x0 = max(0, int(np.floor(d[:, 0].min())))
            x1 = min(w, int(np.ceil(d[:, 0].max())) + 1)
            y0 = max(0, int(np.floor(d[:, 1].min())))
            y1 = min(h, int(np.ceil(d[:, 1].max())) + 1)
            if x1 <= x0 or y1 <= y0:
                continue                    # entirely off the panel
            x = np.arange(x0, x1, dtype=np.float64)[None, :]
            y = np.arange(y0, y1, dtype=np.float64)[:, None]
            den = M[2, 0] * x + M[2, 1] * y + M[2, 2]
            den = np.where(np.abs(den) < 1e-12, 1e-12, den)
            px = (M[0, 0] * x + M[0, 1] * y + M[0, 2]) / den
            py = (M[1, 0] * x + M[1, 1] * y + M[1, 2]) / den
            hit = roi.contains(px, py)
            hit &= (px >= 0) & (px < cw) & (py >= 0) & (py < ch)
            out[y0:y1, x0:x1] |= hit
        return np.where(out, ON, OFF)

    # ── persistence ──────────────────────────────────────────────────────────
    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rois]

    @classmethod
    def from_list(cls, items) -> "RoiSet":
        return cls([roi_from_dict(d) for d in items or []])
