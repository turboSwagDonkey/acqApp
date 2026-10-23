"""The camera's live frames with the ROI outlines on top, stacked under the
`RoiEditor`: ROIs are drawn on a full-field snapshot, and this shows whether
they land inside what the camera captures now (a cropped preset is a band).

Read-only. Frames come from a callable, so this never knows which camera it
shows; an ROI not wholly inside the capture area is outlined in amber.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QRectF, QTimer
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from acqApp import style
from acqApp.devices.dmd.roi import RoiSet
from acqApp.devices.dmd.roi_panel import RoiEditor, snapshot_levels

_LIVE_MS = 50
_ROI_PEN = pg.mkPen("#00d0ff", width=2)
_BEYOND_PEN = pg.mkPen(style.WARN, width=2)

# (frame, its preset's (hpos, vpos) in sensor px, sensor px per frame px).
LiveFrame = tuple[np.ndarray, tuple[float, float], float]


class LiveImageFeed(QObject):
    """Puts `source()`'s newest frame into `img` every _LIVE_MS until
    `stop()`; `frame` is the one last shown. No source = a still image."""

    def __init__(self, parent: QObject, img: pg.ImageItem,
                 source: Callable[[], object] | None, frame) -> None:
        super().__init__(parent)
        self._img, self._source, self.frame = img, source, frame
        self._timer = QTimer(self)
        if source is not None:
            self._timer.timeout.connect(self.refresh)
            self._timer.start(_LIVE_MS)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def stop(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        f = self._source() if self._source is not None else None
        if f is not None and f is not self.frame:
            self.frame = f
            self._img.setImage(np.asarray(f), autoLevels=False)


class RoiLiveView(QWidget):
    """Polls `source() -> LiveFrame | None` while started."""

    def __init__(self, source: Callable[[], LiveFrame | None], parent=None):
        super().__init__(parent)
        self._source = source
        self._rois = RoiSet()
        self._frame = None                 # the frame object last shown
        self._geom = None                  # ((ox, oy), scale, (h, w)) shown at
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(_LIVE_MS)
        self._timer.timeout.connect(self.refresh)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._gv = pg.GraphicsLayoutWidget()
        self._vb = pg.ViewBox(lockAspect=True, invertY=True)
        self._gv.addItem(self._vb)
        self._img = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img)
        self._inside = pg.PlotCurveItem(pen=_ROI_PEN, connect="finite")
        self._outside = pg.PlotCurveItem(pen=_BEYOND_PEN, connect="finite")
        self._vb.addItem(self._inside)
        self._vb.addItem(self._outside)
        self._hist = pg.HistogramLUTWidget()
        self._hist.setImageItem(self._img)
        self._hist.setFixedWidth(86)       # the editor's, so the images align
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._hist)
        row.addWidget(self._gv, 1)
        root.addLayout(row, 1)
        self._lbl = QLabel()
        self._lbl.setWordWrap(True)
        root.addWidget(self._lbl)
        self.setMinimumHeight(140)
        self._show_status()

    # ── inputs ───────────────────────────────────────────────────────────────
    def set_rois(self, rois: RoiSet) -> None:
        self._rois = rois
        self._draw()

    def start(self) -> None:
        self._timer.start()
        self.refresh()

    def stop(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        got = self._source()
        if got is None or got[0] is self._frame:
            return
        frame, (ox, oy), scale = got
        self._frame = frame
        f = np.asarray(frame)
        geom = ((float(ox), float(oy)), float(scale), f.shape[:2])
        if geom == self._geom:              # keep the operator's contrast
            self._img.setImage(f, autoLevels=False)
            return
        lo, hi = snapshot_levels(f)
        self._img.setImage(f, autoLevels=False, levels=(lo, hi))
        self._hist.setLevels(lo, hi)
        self._geom = geom
        h, w = geom[2]
        self._img.setRect(QRectF(0.0, 0.0, w * scale, h * scale))
        self._draw()
        self._vb.autoRange()

    # ── drawing ──────────────────────────────────────────────────────────────
    def area(self) -> tuple[float, float, float, float] | None:
        """The capture area (x0, y0, w, h) in sensor px, None before a frame."""
        if self._geom is None:
            return None
        (ox, oy), scale, (h, w) = self._geom
        return ox, oy, w * scale, h * scale

    def _draw(self) -> None:
        area = self.area()
        if area is None:
            return
        parts: dict[bool, list] = {True: [], False: []}
        for r in self._rois:
            if r.enabled:
                b = r.boundary()
                parts[r.within(*area)].append(
                    np.vstack((b, b[:1], [[np.nan, np.nan]])))
        ox, oy = area[:2]
        for inside, curve in ((True, self._inside), (False, self._outside)):
            p = np.vstack(parts[inside]) if parts[inside] else np.empty((0, 2))
            curve.setData(p[:, 0] - ox, p[:, 1] - oy)
        self._show_status()

    def _show_status(self) -> None:
        area = self.area()
        if area is None:
            self._lbl.setStyleSheet(f"color:{style.muted()};")
            self._lbl.setText("Live camera: waiting for a frame (is Live view on?)")
            return
        x0, y0, w, h = area
        msg = (f"Live camera: capture area {w:.0f}×{h:.0f} px, rows "
               f"{y0:.0f}–{y0 + h:.0f} of the sensor")
        beyond = self._rois.beyond(*area)
        if beyond:
            msg += f" — beyond it (amber): {', '.join(beyond)}"
        elif len(self._rois):
            msg += " — every ROI inside it"
        self._lbl.setStyleSheet(f"color:{style.WARN if beyond else style.muted()};")
        self._lbl.setText(msg)


def attach_live_view(editor: RoiEditor,
                     source: Callable[[], LiveFrame | None]) -> RoiLiveView:
    """A `RoiLiveView` stacked under `editor`'s image, following its edits."""
    live = RoiLiveView(source)
    editor.add_view(live)
    editor.rois_changed.connect(live.set_rois)
    live.set_rois(editor.roi_set)
    return live
