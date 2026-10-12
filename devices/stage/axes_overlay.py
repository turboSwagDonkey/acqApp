"""Stage +X/+Y arrows on a camera view: the way the picture slides when the
stage moves along each axis (`cam_axes.CamAxes`). Drawn in image
coordinates at the lower-left; hidden until the axes are measured."""
from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtGui import QBrush, QColor, QFont, QPen, QTransform
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

from .cam_axes import CamAxes

_COLORS = {"X": "#ff6b6b", "Y": "#4cd964"}
_HEAD_DEG = 28.0


class AxesOverlay:
    """`update(axes, (h, w))` per displayed frame; redraws only on change."""

    def __init__(self, vb: pg.ViewBox):
        self._items = {}
        for name, color in _COLORS.items():
            pen = pg.mkPen(color, width=2.5)
            line = pg.PlotCurveItem(pen=pen, connect="pairs")
            # Screen-pixel text, centred, dark-outlined to read on any image.
            label = QGraphicsSimpleTextItem(f"+{name}")
            font = QFont()
            font.setBold(True)
            font.setPointSizeF(11.0)
            label.setFont(font)
            label.setBrush(QBrush(QColor(color)))
            label.setPen(QPen(QColor(0, 0, 0, 200), 0.8))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            r = label.boundingRect()
            label.setTransform(QTransform.fromTranslate(-r.width() / 2,
                                                        -r.height() / 2))
            for it in (line, label):
                it.setZValue(20)
                it.setVisible(False)
                vb.addItem(it, ignoreBounds=True)
            self._items[name] = (line, label)
        self._key = None

    def update(self, axes: CamAxes | None, shape: tuple[int, int]) -> None:
        key = (axes, shape[:2])
        if key == self._key:
            return
        self._key = key
        if axes is None:
            for line, label in self._items.values():
                line.setVisible(False)
                label.setVisible(False)
            return
        h, w = shape[:2]
        # Room for an arrow plus its label in any direction from the origin.
        length = 0.11 * min(h, w)
        ox, oy = 0.2 * min(h, w), h - 0.2 * min(h, w)
        for name, (vx, vy) in (("X", axes[0:2]), ("Y", axes[2:4])):
            n = math.hypot(vx, vy) or 1.0
            ux, uy = vx / n, vy / n
            ex, ey = ox + ux * length, oy + uy * length
            xs, ys = [ox, ex], [oy, ey]
            for sign in (+1, -1):
                a = math.radians(sign * _HEAD_DEG)
                hx = ux * math.cos(a) - uy * math.sin(a)
                hy = ux * math.sin(a) + uy * math.cos(a)
                xs += [ex, ex - hx * length * 0.3]
                ys += [ey, ey - hy * length * 0.3]
            line, label = self._items[name]
            line.setData(np.array(xs), np.array(ys))
            label.setPos(ox + ux * length * 1.45, oy + uy * length * 1.45)
            line.setVisible(True)
            label.setVisible(True)
