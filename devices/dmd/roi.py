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

from acqApp.devices.dmd.calibration import (OFF, ON, DmdCalibration,
                                            apply_transform)


@dataclass
class _Roi:
    name: str = ""
    enabled: bool = True

    kind: str = "roi"

    def mask_at(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Coverage at the given camera x and y coordinates → (len(ys), len(xs)).

        Explicit coordinates rather than a shape, so a caller that only wants a
        percentage can evaluate on a coarse grid instead of every pixel.
        """
        raise NotImplementedError

    def contains(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        """Membership at SCATTERED points, not on a grid.

        `mask_at` broadcasts a grid, which is right for drawing but wrong for
        the projection path: that asks "is this mirror inside" for ~786k
        mirrors whose camera positions are arbitrary, and building a
        camera-sized grid to sample from cost 107 ms per rebuild.
        """
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

    def mask_at(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        # Broadcast rather than np.mgrid: the editor rebuilds every ROI's mask
        # on every drag, and at ORCA full frame mgrid alone is two 84 MB int64
        # grids per ROI. Unrotated, the two conditions stay separable and only
        # the (H, W) bool is ever materialised.
        dx = np.asarray(xs, dtype=np.float64)[None, :] - self.x
        dy = np.asarray(ys, dtype=np.float64)[:, None] - self.y
        if self.angle_deg:
            t = np.radians(self.angle_deg)
            c, s = np.cos(t), np.sin(t)
            dx, dy = c * dx + s * dy, -s * dx + c * dy
        return (np.abs(dx) <= self.w / 2.0) & (np.abs(dy) <= self.h / 2.0)

    def contains(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        dx = np.asarray(px, dtype=np.float64) - self.x
        dy = np.asarray(py, dtype=np.float64) - self.y
        if self.angle_deg:
            t = np.radians(self.angle_deg)
            c, s = np.cos(t), np.sin(t)
            dx, dy = c * dx + s * dy, -s * dx + c * dy
        return (np.abs(dx) <= self.w / 2.0) & (np.abs(dy) <= self.h / 2.0)

    def boundary(self, n: int = 64) -> np.ndarray:
        """The four corners — and they are exact. A projective map takes
        straight lines to straight lines, so if the corners land inside the
        field, so does every edge between them."""
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
        """`n` points around the rim. Sampled, not exact: a circle's image under
        a projective map is a conic, so there is no finite exact set — but the
        rim is what can leave the field, and 64 points resolve it to 0.1 % of r."""
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

        The exact answer `reach_fraction` estimates, and the reference its test
        checks against. Not on the projection path: `dmd_frame` clips per mirror.
        """
        shape = (calib.cam_size[1], calib.cam_size[0])
        want = self.mask(shape)
        ok = want & calib.accessible_mask(shape)
        n = int(want.sum())
        return ok, (float(ok.sum()) / n if n else 1.0)

    def reach_fraction(self, calib: DmdCalibration, *,
                       max_side: int = 512) -> float:
        """Share of the drawn area the DMD can illuminate — an ESTIMATE.

        For the status line, recomputed on every drag event.

        Grid capped at `max_side`, then bounded twice as `dmd_frame` is: each
        ROI to its own bbox, the scan to their union. ROIs cover ~1 % of the
        grid, so the unbounded version spent its time on cells nothing reached
        — 1308 -> 190 us at four ROIs (2026-08-25), same answer.
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
        """Names of ROIs that are not wholly inside the DMD's field.

        Geometric, not rasterised. This used to build a full-camera mask **per
        ROI** and index it with another — ~90 ms each at ORCA full frame, on
        every drag — and pixel quantisation made it less accurate, not more.
        """
        return [r.name for r in self.rois
                if r.enabled and not calib.accessible(r.boundary()).all()]

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

        Asks each MIRROR where it lands and whether an ROI is there, rather
        than rasterising a camera-sized mask and sampling it — the old way built
        a 4432x2368 bool per ROI to read 786k values out of.

        And only the mirrors that could possibly be in each ROI: its camera
        boundary is mapped into mirror space and the search is confined to that
        block. ROIs cover a small share of the panel, so this is the difference
        between evaluating 786k mirrors per ROI and a few tens of thousands.

        Iterating mirrors (not ROI pixels) is still the point: a forward map
        leaves holes wherever the DMD is coarser than the camera, and a mask
        with holes is a stimulus with holes.
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
        return np.where(out, ON, OFF).astype(np.uint8)

    # ── persistence ──────────────────────────────────────────────────────────
    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rois]

    @classmethod
    def from_list(cls, items) -> "RoiSet":
        return cls([roi_from_dict(d) for d in items or []])
