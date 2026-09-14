"""
Stage travel visualizations: `StageMap` for X/Y, `ZGauge` for Z (focus).

Both are a read-only picture of where the stage is inside its own travel: the
hard travel extent, the soft-limit extent inside it, the origin, the session
home, and the current position. Display only — nothing here commands motion.

Two widgets rather than one made 3D: X/Y is a plane you travel across, Z is a
depth you travel through — the same distinction `panel.py`'s Motion grid vs.
`CalibrationDialog`'s two risk profiles already draw. A gauge, not a second
travel-map axis, reads as depth rather than another position on a table.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .settings import _BAD, _C_CUR, _C_HOME, _C_ORIGIN, _C_SOFT

_TRAVEL_EDGE = QColor("#8a8a8a")
_TRAVEL_FILL = QColor("#f4f4f4")
# Same colors settings.py's legend swatches use — one definition, not two
# hex literals kept in step by hand.
_SOFT_EDGE   = QColor(_C_SOFT)
_CURRENT     = QColor(_C_CUR)
_ORIGIN      = QColor(_C_ORIGIN)
_HOME        = QColor(_C_HOME)
_STALE       = QColor(_BAD)


class StageMap(QWidget):
    """Travel map. Feed it `set_axes()` once and `set_position()` per poll."""

    _MARGIN_LEFT   = 10
    _MARGIN_RIGHT  = 85     # room for the vertical legend
    _MARGIN_TOP    = 10
    _MARGIN_BOTTOM = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._x = None
        self._y = None
        self._pos: tuple[float, float] | None = None
        self._invert_y = True
        
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("Stage position within its travel. Display only — "
                        "clicking here does not move the stage.")

    # ── inputs ──────────────────────────────────────────────────────────────
    def set_axes(self, x_axis, y_axis, invert_y: bool = True) -> None:
        self._x, self._y, self._invert_y = x_axis, y_axis, invert_y
        self.update()

    def set_position(self, x_um: float, y_um: float) -> None:
        self._pos = (x_um, y_um)
        self.update()

    def clear_position(self) -> None:
        self._pos = None
        self.update()

    # ── geometry ────────────────────────────────────────────────────────────
    def _box(self) -> QRectF:
        """Enforces a 1:1 square canvas maximized within available widget area."""
        avail_w = max(1.0, self.width() - self._MARGIN_LEFT - self._MARGIN_RIGHT)
        avail_h = max(1.0, self.height() - self._MARGIN_TOP - self._MARGIN_BOTTOM)
        side = min(avail_w, avail_h)  # Strict square aspect ratio

        box_x = self._MARGIN_LEFT + (avail_w - side) / 2.0
        box_y = self._MARGIN_TOP + (avail_h - side) / 2.0
        return QRectF(box_x, box_y, side, side)

    def _to_px(self, x_um: float, y_um: float, xr, yr) -> QPointF:
        box = self._box()
        (x0, x1), (y0, y1) = xr, yr
        fx = (x_um - x0) / (x1 - x0) if x1 > x0 else 0.5
        fy = (y_um - y0) / (y1 - y0) if y1 > y0 else 0.5
        fx = min(max(fx, 0.0), 1.0)
        fy = min(max(fy, 0.0), 1.0)
        if self._invert_y:
            fy = 1.0 - fy
        return QPointF(box.left() + fx * box.width(),
                       box.top() + fy * box.height())

    def _rect_for(self, xr, yr, xlim, ylim) -> QRectF:
        a = self._to_px(xlim[0], ylim[0], xr, yr)
        b = self._to_px(xlim[1], ylim[1], xr, yr)
        return QRectF(a, b).normalized()

    # ── painting ────────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._x is None or self._y is None:
            self._center_text(p, "No stage configured")
            return

        xt, yt = self._x.travel_limits_um(), self._y.travel_limits_um()
        if xt[1] <= xt[0] or yt[1] <= yt[0]:
            self._center_text(p, "No travel limits — calibrate")
            return

        def pad(lim):
            span = lim[1] - lim[0]
            return (lim[0] - span * 0.04, lim[1] + span * 0.04)
        xr, yr = pad(xt), pad(yt)

        travel = self._rect_for(xr, yr, xt, yt)
        p.setPen(QPen(_TRAVEL_EDGE, 1.5))
        p.setBrush(QBrush(_TRAVEL_FILL))
        p.drawRect(travel)

        xs, ys = self._x.soft_limits_um(), self._y.soft_limits_um()
        soft = self._rect_for(xr, yr, xs, ys)
        pen = QPen(_SOFT_EDGE, 1.0, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(soft)

        if self._pos is not None:
            c = self._to_px(self._pos[0], self._pos[1], xr, yr)
            p.setPen(QPen(_CURRENT.lighter(130), 0.8, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(travel.left(), c.y()), QPointF(travel.right(), c.y()))
            p.drawLine(QPointF(c.x(), travel.top()), QPointF(c.x(), travel.bottom()))
            p.setPen(QPen(_CURRENT.darker(130), 1.2))
            p.setBrush(QBrush(_CURRENT))
            p.drawEllipse(c, 5, 5)

        hx, hy = self._x.home_um(), self._y.home_um()
        if hx is not None and hy is not None:
            self._diamond(p, self._to_px(hx, hy, xr, yr), _HOME, 7)

        if self._x.origin_set and self._y.origin_set:
            self._cross(p, self._to_px(0.0, 0.0, xr, yr), _ORIGIN, 9)
        else:
            p.setPen(QPen(_STALE))
            p.setFont(self._small())
            p.drawText(travel.adjusted(4, 4, -4, -4),
                       int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
                       "0,0 not set")

        self._legend(p, travel)

    # ── drawing helpers ─────────────────────────────────────────────────────
    def _small(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(6.5, f.pointSizeF() - 2.0))
        return f

    def _center_text(self, p: QPainter, text: str) -> None:
        p.setPen(QPen(_TRAVEL_EDGE))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)

    def _cross(self, p: QPainter, c: QPointF, color: QColor, r: int) -> None:
        p.setPen(QPen(color, 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))

    def _diamond(self, p: QPainter, c: QPointF, color: QColor, r: int) -> None:
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPointF(c.x(), c.y() - r), QPointF(c.x() + r, c.y()),
                      QPointF(c.x(), c.y() + r), QPointF(c.x() - r, c.y()))

    def _legend(self, p: QPainter, travel: QRectF) -> None:
        p.setFont(self._small())
        lx = travel.right() + 10
        
        spacing = 20
        total_h = 4 * spacing
        ly = travel.top() + max(0.0, (travel.height() - total_h) / 2.0) + 6

        p.setPen(QPen(_CURRENT.darker(130), 1.2))
        p.setBrush(QBrush(_CURRENT))
        p.drawEllipse(QPointF(lx + 4, ly), 4, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Position")

        ly += spacing
        self._cross(p, QPointF(lx + 4, ly), _ORIGIN, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "0,0 (Origin)")

        ly += spacing
        self._diamond(p, QPointF(lx + 4, ly), _HOME, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Home")

        ly += spacing
        pen = QPen(_SOFT_EDGE, 1.2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(lx + 1, ly - 4, 6, 6))
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Soft limits")


class ZGauge(QWidget):
    """Z (focus) position within its travel — a vertical thermometer, not a
    second travel-map axis: depth reads as depth. `set_axis()` once,
    `set_position()` per poll, same shape as `StageMap`.

    Origin sits on the bar's LEFT, home on its RIGHT — opposite sides so the
    two never draw on top of each other at a shared value — and the soft
    limits get their actual µm numbers at the ends of the dashed box, the
    same way a real gauge has marks that mean something rather than a
    colour band alone.
    """

    _MARGIN_TOP    = 20        # room for the top soft-limit number
    _MARGIN_BOTTOM = 20        # room for the bottom soft-limit number
    _BAR_WIDTH     = 26
    _SIDE_TICK     = 16        # origin/home tick length, past the bar edge
    _LABEL_W       = 56        # room for "1234 µm" beside the bar
    _RADIUS        = 4.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ax = None
        self._pos: float | None = None

        self.setMinimumSize(self._SIDE_TICK + self._BAR_WIDTH + self._LABEL_W + 12,
                            220)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolTip("Z (focus) position within its travel. Display "
                        "only — clicking here does not move the stage.")

    # ── inputs ──────────────────────────────────────────────────────────────
    def set_axis(self, z_axis) -> None:
        self._ax = z_axis
        self.update()

    def set_position(self, z_um: float) -> None:
        self._pos = z_um
        self.update()

    def clear_position(self) -> None:
        self._pos = None
        self.update()

    # ── geometry ────────────────────────────────────────────────────────────
    def _bar(self) -> QRectF:
        h = max(1.0, self.height() - self._MARGIN_TOP - self._MARGIN_BOTTOM)
        return QRectF(self._SIDE_TICK, self._MARGIN_TOP, self._BAR_WIDTH, h)

    def _y_for(self, z_um: float, lim: tuple[float, float], bar: QRectF) -> float:
        """Screen y for a value in `lim` — up on screen for a bigger number,
        the way a thermometer or a level reads, not StageMap's arbitrary
        invert_y (there's only one sensible "up" for a single axis). `bar`
        is the caller's own `_bar()`, passed in rather than recomputed —
        paintEvent already has it, and calls this up to four times a frame."""
        lo, hi = lim
        f = (z_um - lo) / (hi - lo) if hi > lo else 0.5
        f = min(max(f, 0.0), 1.0)
        return bar.bottom() - f * bar.height()

    # ── painting ────────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._ax is None:
            self._center_text(p, "No Z axis")
            return

        zt = self._ax.travel_limits_um()
        if zt[1] <= zt[0]:
            self._center_text(p, "No travel\nlimits")
            return

        bar = self._bar()
        p.setPen(QPen(_TRAVEL_EDGE, 1.5))
        p.setBrush(QBrush(_TRAVEL_FILL))
        p.drawRoundedRect(bar, self._RADIUS, self._RADIUS)

        lo, hi = self._ax.soft_limits_um()
        top, bot = self._y_for(hi, zt, bar), self._y_for(lo, zt, bar)
        p.setPen(QPen(_SOFT_EDGE, 1.2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(bar.left(), top, bar.width(), bot - top),
                          self._RADIUS, self._RADIUS)

        # The soft limits' own numbers — a coloured band alone doesn't say
        # HOW close to the edge "close" is.
        p.setFont(self._small())
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(bar.left() - 6, top - 13, bar.width() + 12, 12),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                   f"{hi:.0f}")
        p.drawText(QRectF(bar.left() - 6, bot + 1, bar.width() + 12, 12),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   f"{lo:.0f}")

        if self._ax.origin_set:
            self._side_tick(p, bar, self._y_for(0.0, zt, bar), _ORIGIN, "left")
        else:
            p.setPen(QPen(_STALE))
            p.setFont(self._small())
            p.drawText(bar.adjusted(-6, 0, 6, 0),
                       int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
                       "0 not\nset")

        hz = self._ax.home_um()
        if hz is not None:
            self._side_tick(p, bar, self._y_for(hz, zt, bar), _HOME, "right",
                            diamond=True)

        if self._pos is not None:
            y = self._y_for(self._pos, zt, bar)
            p.setPen(QPen(_CURRENT.lighter(130), 0.8, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(bar.left(), y), QPointF(bar.right(), y))
            p.setPen(QPen(_CURRENT.darker(130), 1.4))
            p.setBrush(QBrush(_CURRENT))
            p.drawEllipse(QPointF(bar.center().x(), y), 5.5, 5.5)
            p.setFont(self._small())
            p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
            p.drawText(QRectF(bar.right() + 6, y - 7, self._LABEL_W, 14),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       f"{self._pos:.0f} µm")

        if not self._ax.has_frame:
            p.setPen(QPen(_STALE))
            p.setFont(self._small())
            p.drawText(bar.adjusted(-6, -6, 6, 0),
                       int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter),
                       "no frame")

    # ── drawing helpers ─────────────────────────────────────────────────────
    def _small(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(6.5, f.pointSizeF() - 2.0))
        return f

    def _center_text(self, p: QPainter, text: str) -> None:
        p.setPen(QPen(_TRAVEL_EDGE))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)

    def _side_tick(self, p: QPainter, bar: QRectF, y: float, color: QColor,
                   side: str, *, diamond: bool = False) -> None:
        """A marker on one side of the bar, extending outward from its edge
        — origin on the left, home on the right, so the two never collide
        at a shared value the way sharing one edge would."""
        x0 = bar.left() if side == "left" else bar.right()
        x1 = x0 + (-self._SIDE_TICK + 4 if side == "left" else self._SIDE_TICK - 4)
        p.setPen(QPen(color, 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if diamond:
            r = 5
            xm = (x0 + x1) / 2.0
            p.drawPolygon(QPointF(xm, y - r), QPointF(xm + (x1 - xm), y),
                          QPointF(xm, y + r), QPointF(xm - (x1 - xm), y))
        else:
            p.drawLine(QPointF(x0, y), QPointF(x1, y))