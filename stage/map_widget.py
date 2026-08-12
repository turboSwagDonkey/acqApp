"""
XY stage — travel map.

A read-only picture of where the stage is inside its own travel: the hard travel
rectangle, the soft-limit rectangle inside it, the origin, the session home, and
the current position. Display only — nothing here commands motion.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

_TRAVEL_EDGE = QColor("#8a8a8a")
_TRAVEL_FILL = QColor("#f4f4f4")
_SOFT_EDGE   = QColor("#b58900")
_CURRENT     = QColor("#1f77b4")
_ORIGIN      = QColor("#2ca02c")
_HOME        = QColor("#ff7f0e")
_STALE       = QColor("#c0392b")


class StageMap(QWidget):
    """Travel map. Feed it `set_axes()` once and `set_position()` per poll."""

    # Minimal margins to maximize map square size (edge labels removed)
    _MARGIN_LEFT   = 10     # Tight inset
    _MARGIN_RIGHT  = 85     # Room for vertical legend on the right
    _MARGIN_TOP    = 10     # Tight inset above box
    _MARGIN_BOTTOM = 10     # Tight inset below box

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

        # Hard travel
        travel = self._rect_for(xr, yr, xt, yt)
        p.setPen(QPen(_TRAVEL_EDGE, 1.5))
        p.setBrush(QBrush(_TRAVEL_FILL))
        p.drawRect(travel)

        # Soft limits (dashed)
        xs, ys = self._x.soft_limits_um(), self._y.soft_limits_um()
        soft = self._rect_for(xr, yr, xs, ys)
        pen = QPen(_SOFT_EDGE, 1.0, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(soft)

        # Current position & guide lines
        if self._pos is not None:
            c = self._to_px(self._pos[0], self._pos[1], xr, yr)
            p.setPen(QPen(_CURRENT.lighter(130), 0.8, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(travel.left(), c.y()), QPointF(travel.right(), c.y()))
            p.drawLine(QPointF(c.x(), travel.top()), QPointF(c.x(), travel.bottom()))
            p.setPen(QPen(_CURRENT.darker(130), 1.2))
            p.setBrush(QBrush(_CURRENT))
            p.drawEllipse(c, 5, 5)

        # Session home
        hx, hy = self._x.home_um(), self._y.home_um()
        if hx is not None and hy is not None:
            self._diamond(p, self._to_px(hx, hy, xr, yr), _HOME, 7)

        # Origin (0,0)
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

        # 1. Position dot
        p.setPen(QPen(_CURRENT.darker(130), 1.2))
        p.setBrush(QBrush(_CURRENT))
        p.drawEllipse(QPointF(lx + 4, ly), 4, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Position")

        # 2. Origin cross
        ly += spacing
        self._cross(p, QPointF(lx + 4, ly), _ORIGIN, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "0,0 (Origin)")

        # 3. Session Home diamond
        ly += spacing
        self._diamond(p, QPointF(lx + 4, ly), _HOME, 4)
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Home")

        # 4. Soft limits dashed rect
        ly += spacing
        pen = QPen(_SOFT_EDGE, 1.2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(lx + 1, ly - 4, 6, 6))
        p.setPen(QPen(_TRAVEL_EDGE.darker(160)))
        p.drawText(QRectF(lx + 14, ly - 7, 70, 14),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Soft limits")