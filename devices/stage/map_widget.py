"""
Stage travel pictures: `StageMap` for X/Y, `ZGauge` for Z (focus).

Both show the hard travel, the soft limits inside it, the origin and the
current position; the map also shows this session's saved FOVs. The legend
is the panel's.
Nothing here commands motion: dragging the position dot only emits the
target, and the panel decides whether and how fast to move.
Z gets a gauge, not a third map axis, so it reads as depth rather than
another position on a table.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .settings import _BAD, _C_CUR, _C_FOV, _C_ORIGIN, _C_SOFT

_TRAVEL_EDGE = QColor("#8a8a8a")
_TRAVEL_FILL = QColor("#f4f4f4")
_TEXT        = _TRAVEL_EDGE.darker(160)
_SOFT_EDGE   = QColor(_C_SOFT)
_CURRENT     = QColor(_C_CUR)
_ORIGIN      = QColor(_C_ORIGIN)
_STALE       = QColor(_BAD)
_FOV         = QColor(_C_FOV)

_LEFT_VCENTER = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


class _TravelWidget(QWidget):
    """Shared drawing helpers."""

    def clear_position(self) -> None:
        self._pos = None
        self.update()

    def _small(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(6.5, f.pointSizeF() - 2.0))
        return f

    def _center_text(self, p: QPainter, text: str) -> None:
        p.setPen(QPen(_TRAVEL_EDGE))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)


class _MapXf:
    """Stage µm <-> widget px: a linear orientation (see
    `StageMap._orientation`), scaled so the travel fits `box` less `PAD_PX`."""

    PAD_PX = 6.0        # room for a marker on the travel edge (the dot is r=5)

    def __init__(self, m: tuple[float, float, float, float], xt, yt, box: QRectF):
        self._m, self._xt, self._yt = m, xt, yt
        a, b, c, d = m
        det = a * d - b * c
        self._inv = (d / det, -b / det, -c / det, a / det)
        pts = [self._screen(x, y) for x in xt for y in yt]
        sx = [q[0] for q in pts]
        sy = [q[1] for q in pts]
        w, h = max(sx) - min(sx), max(sy) - min(sy)
        pad = 2 * self.PAD_PX
        self._k = min(max(1.0, box.width() - pad) / w,
                      max(1.0, box.height() - pad) / h)
        self._s0 = ((max(sx) + min(sx)) / 2.0, (max(sy) + min(sy)) / 2.0)
        self._c = box.center()

    def _screen(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d = self._m
        return a * x + b * y, c * x + d * y

    def to_px(self, x_um: float, y_um: float) -> QPointF:
        """Clamped into the travel, so a marker past it sits on its edge."""
        x = min(max(x_um, self._xt[0]), self._xt[1])
        y = min(max(y_um, self._yt[0]), self._yt[1])
        sx, sy = self._screen(x, y)
        return QPointF(self._c.x() + (sx - self._s0[0]) * self._k,
                       self._c.y() + (sy - self._s0[1]) * self._k)

    def to_um(self, pt: QPointF) -> tuple[float, float]:
        """Inverse of `to_px` (unclamped)."""
        sx = (pt.x() - self._c.x()) / self._k + self._s0[0]
        sy = (pt.y() - self._c.y()) / self._k + self._s0[1]
        a, b, c, d = self._inv
        return a * sx + b * sy, c * sx + d * sy

    def poly(self, xlim, ylim) -> QPolygonF:
        (x0, x1), (y0, y1) = xlim, ylim
        return QPolygonF([self.to_px(x0, y0), self.to_px(x1, y0),
                          self.to_px(x1, y1), self.to_px(x0, y1)])


class StageMap(_TravelWidget):
    """Travel map. Feed it `set_axes()` once and `set_position()` per poll;
    `set_fovs()` when the saved FOVs change. While `set_draggable(True)`,
    dragging the position dot emits `drag_moved(x_um, y_um)` (inside the soft
    limits); the owner shows its chase via `set_target()` and ends a drag
    early with `cancel_drag()`."""

    drag_moved = pyqtSignal(float, float)

    # With _MapXf.PAD_PX: the square's top and bottom meet ZGauge's bar.
    _MARGIN_LEFT   = 0
    _MARGIN_RIGHT  = 0
    _MARGIN_TOP    = 14
    _MARGIN_BOTTOM = 14
    _GRAB_PX       = 12     # how close to the dot a press must land

    _TIP = ("Stage position within its travel, with this session's saved "
            "FOVs. Drag the blue dot to move the stage (speed-capped; Esc "
            "stops). Clicking elsewhere does nothing. With measured camera "
            "axes the map is turned to match the camera: drag right and the "
            "picture slides right.")
    _TIP_NO_DRAG = ("Stage position within its travel, with this session's "
                    "saved FOVs. Dragging needs a connected stage with a "
                    "valid frame.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._x = None
        self._y = None
        self._pos: tuple[float, float] | None = None
        self._invert_y = True
        self._cam_axes = None
        self._fovs: list[tuple[str, float, float]] = []
        self._active_fov = ""
        self._target: tuple[float, float] | None = None
        self._draggable = False
        self._dragging = False

        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)     # the open-hand cursor over the dot
        self.setToolTip(self._TIP_NO_DRAG)

    def resizeEvent(self, event) -> None:
        # The plot is square: no wider than tall, so neighbours sit beside it.
        self.setMaximumWidth(max(self.minimumWidth(), self.height()))
        super().resizeEvent(event)

    # ── inputs ──────────────────────────────────────────────────────────────
    def set_axes(self, x_axis, y_axis, invert_y: bool = True) -> None:
        self._x, self._y, self._invert_y = x_axis, y_axis, invert_y
        self.update()

    def set_position(self, x_um: float, y_um: float) -> None:
        self._pos = (x_um, y_um)
        self.update()

    def set_cam_axes(self, axes) -> None:
        """Measured `cam_axes.CamAxes` (or None): the map turns to match the
        camera, so a drag to the right slides the picture right."""
        self._cam_axes = axes
        self.update()

    def set_fovs(self, fovs: list[tuple[str, float, float]],
                 active: str = "") -> None:
        """(name, x_um, y_um) per FOV; `active` is drawn filled."""
        self._fovs, self._active_fov = list(fovs), active
        self.update()

    def set_target(self, xy: tuple[float, float] | None) -> None:
        self._target = xy
        self.update()

    def set_draggable(self, on: bool) -> None:
        self._draggable = on
        if not on:
            self.cancel_drag()
        self.setToolTip(self._TIP if on else self._TIP_NO_DRAG)

    def cancel_drag(self) -> None:
        """Ignore the rest of the current drag (until the next press)."""
        self._dragging = False
        self.unsetCursor()

    @property
    def dragging(self) -> bool:
        return self._dragging

    # ── mouse (drag the position) ───────────────────────────────────────────
    def _near_dot(self, pt: QPointF) -> bool:
        xf = self._frame()
        if xf is None or self._pos is None:
            return False
        grabbable = [self._pos] + ([self._target] if self._target else [])
        for xy in grabbable:
            c = xf.to_px(*xy)
            if (c.x() - pt.x()) ** 2 + (c.y() - pt.y()) ** 2 <= self._GRAB_PX ** 2:
                return True
        return False

    def _emit_drag(self, pt: QPointF) -> None:
        xf = self._frame()
        if xf is None:
            return
        x_um, y_um = xf.to_um(pt)
        (x0, x1), (y0, y1) = self._x.soft_limits_um(), self._y.soft_limits_um()
        self.drag_moved.emit(min(max(x_um, x0), x1), min(max(y_um, y0), y1))

    def mousePressEvent(self, event) -> None:
        if (self._draggable and event.button() == Qt.MouseButton.LeftButton
                and self._near_dot(event.position())):
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._emit_drag(event.position())
        elif self._draggable:
            if self._near_dot(event.position()):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── geometry ────────────────────────────────────────────────────────────
    def _orientation(self) -> tuple[float, float, float, float]:
        """Stage (dx, dy) -> screen (right, down), as (a, b, c, d):
        right = a·dx + b·dy, down = c·dx + d·dy. Measured camera axes, at
        unit scale, so the map turns like the picture; else +Y up (or down)."""
        if self._cam_axes is not None:
            ax, ay, bx, by = self._cam_axes
            s = (math.hypot(ax, ay) + math.hypot(bx, by)) / 2.0 or 1.0
            return ax / s, bx / s, ay / s, by / s
        return (1.0, 0.0, 0.0, -1.0) if self._invert_y else (1.0, 0.0, 0.0, 1.0)

    def _frame(self) -> _MapXf | None:
        """The µm <-> px map for this size, or None with nothing to draw."""
        if self._x is None or self._y is None:
            return None
        xt, yt = self._x.travel_limits_um(), self._y.travel_limits_um()
        if xt[1] <= xt[0] or yt[1] <= yt[0]:
            return None
        return _MapXf(self._orientation(), xt, yt, self._box())

    def _box(self) -> QRectF:
        """The largest square that fits inside the margins: at the left, so
        it lines up with the panel's other rows; centred vertically."""
        avail_w = max(1.0, self.width() - self._MARGIN_LEFT - self._MARGIN_RIGHT)
        avail_h = max(1.0, self.height() - self._MARGIN_TOP - self._MARGIN_BOTTOM)
        side = min(avail_w, avail_h)
        return QRectF(self._MARGIN_LEFT,
                      self._MARGIN_TOP + (avail_h - side) / 2.0, side, side)

    # ── painting ────────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._x is None or self._y is None:
            self._center_text(p, "No stage configured")
            return
        xf = self._frame()
        if xf is None:
            self._center_text(p, "No travel limits — calibrate")
            return
        xt, yt = self._x.travel_limits_um(), self._y.travel_limits_um()

        travel_poly = xf.poly(xt, yt)
        travel = travel_poly.boundingRect()
        p.setPen(QPen(_TRAVEL_EDGE, 1.5))
        p.setBrush(QBrush(_TRAVEL_FILL))
        p.drawPolygon(travel_poly)

        p.setPen(QPen(_SOFT_EDGE, 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(xf.poly(self._x.soft_limits_um(), self._y.soft_limits_um()))

        small = self._small()
        self._draw_fovs(p, xf, travel, small)

        if self._pos is not None:
            px, py = self._pos
            c = xf.to_px(px, py)
            p.setPen(QPen(_CURRENT.lighter(130), 0.8, Qt.PenStyle.DotLine))
            p.drawLine(xf.to_px(xt[0], py), xf.to_px(xt[1], py))
            p.drawLine(xf.to_px(px, yt[0]), xf.to_px(px, yt[1]))
            if self._target is not None:
                t = xf.to_px(*self._target)
                p.setPen(QPen(_CURRENT, 1.2, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawLine(c, t)
                p.drawEllipse(t, 6, 6)
            p.setPen(QPen(_CURRENT.darker(130), 1.2))
            p.setBrush(QBrush(_CURRENT))
            p.drawEllipse(c, 5, 5)

        if self._x.origin_set and self._y.origin_set:
            self._cross(p, xf.to_px(0.0, 0.0), _ORIGIN, 9)
        else:
            p.setPen(QPen(_STALE))
            p.setFont(small)
            p.drawText(travel.adjusted(4, 4, -4, -4),
                       int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
                       "0,0 not set")

    # ── drawing helpers ─────────────────────────────────────────────────────
    def _draw_fovs(self, p: QPainter, xf: _MapXf, travel: QRectF,
                   font: QFont) -> None:
        """A square per saved FOV, its name beside it (flipped left near the
        right edge); the active one filled."""
        p.setFont(font)
        fm = p.fontMetrics()
        for name, x_um, y_um in self._fovs:
            c = xf.to_px(x_um, y_um)
            active = name == self._active_fov
            self._square(p, c, _FOV, 5, filled=active)
            w = fm.horizontalAdvance(name)
            left = c.x() + 8 if c.x() + 8 + w <= travel.right() else c.x() - 8 - w
            f = QFont(font)
            f.setBold(active)
            p.setFont(f)
            p.setPen(QPen(_FOV.darker(120)))
            p.drawText(QRectF(left, c.y() - 8, w + 4, 16), _LEFT_VCENTER, name)
        p.setFont(font)

    def _square(self, p: QPainter, c: QPointF, color: QColor, r: int,
                filled: bool = False) -> None:
        p.setPen(QPen(color, 1.5))
        p.setBrush(QBrush(color) if filled else Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))

    def _cross(self, p: QPainter, c: QPointF, color: QColor, r: int) -> None:
        p.setPen(QPen(color, 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))


class ZGauge(_TravelWidget):
    """Z (focus) within its travel, as a vertical thermometer. `set_axis()`
    once, `set_position()` per poll.

    The origin ticks on the bar's left; the soft limits carry their µm values.
    """

    _MARGIN_TOP    = 20        # room for the top soft-limit number
    _MARGIN_BOTTOM = 20        # room for the bottom soft-limit number
    _BAR_WIDTH     = 26
    _SIDE_TICK     = 16        # origin tick length, past the bar edge
    _LABEL_W       = 56        # room for "1234 µm" beside the bar
    _RADIUS        = 4.0
    BAR_CENTER_X   = _SIDE_TICK + _BAR_WIDTH / 2   # for a caption under the bar

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ax = None
        self._pos: float | None = None

        self.setMinimumSize(self._SIDE_TICK + self._BAR_WIDTH + self._LABEL_W + 12,
                            220)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolTip("Z (focus) position within its travel. Display "
                        "only — clicking here doesn't move the stage.")

    # ── inputs ──────────────────────────────────────────────────────────────
    def set_axis(self, z_axis) -> None:
        self._ax = z_axis
        self.update()

    def set_position(self, z_um: float) -> None:
        self._pos = z_um
        self.update()

    # ── geometry ────────────────────────────────────────────────────────────
    def _bar(self) -> QRectF:
        h = max(1.0, self.height() - self._MARGIN_TOP - self._MARGIN_BOTTOM)
        return QRectF(self._SIDE_TICK, self._MARGIN_TOP, self._BAR_WIDTH, h)

    def _y_for(self, z_um: float, lim: tuple[float, float], bar: QRectF) -> float:
        """Screen y for a value in `lim`; bigger is always up (no invert_y
        for a single axis)."""
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

        small = self._small()
        p.setFont(small)
        p.setPen(QPen(_TEXT))
        # As wide as the tick margins allow, so "-12650" isn't clipped.
        lw = bar.width() + 2 * self._SIDE_TICK
        p.drawText(QRectF(bar.left() - self._SIDE_TICK, top - 17, lw, 16),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                   f"{hi:.0f}")
        p.drawText(QRectF(bar.left() - self._SIDE_TICK, bot + 1, lw, 16),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   f"{lo:.0f}")

        if self._ax.origin_set:
            self._side_tick(p, bar, self._y_for(0.0, zt, bar), _ORIGIN, "left")
        else:
            p.setPen(QPen(_STALE))
            p.drawText(bar.adjusted(-6, 0, 6, 0),
                       int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
                       "0 not\nset")

        if self._pos is not None:
            y = self._y_for(self._pos, zt, bar)
            p.setPen(QPen(_CURRENT.lighter(130), 0.8, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(bar.left(), y), QPointF(bar.right(), y))
            p.setPen(QPen(_CURRENT.darker(130), 1.4))
            p.setBrush(QBrush(_CURRENT))
            p.drawEllipse(QPointF(bar.center().x(), y), 5.5, 5.5)
            p.setPen(QPen(_TEXT))
            p.drawText(QRectF(bar.right() + 6, y - 7, self._LABEL_W, 14),
                       _LEFT_VCENTER, f"{self._pos:.0f} µm")

        if not self._ax.has_frame:
            p.setPen(QPen(_STALE))
            p.drawText(bar.adjusted(-6, -6, 6, 0),
                       int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter),
                       "no frame")

    # ── drawing helpers ─────────────────────────────────────────────────────
    def _side_tick(self, p: QPainter, bar: QRectF, y: float, color: QColor,
                   side: str) -> None:
        """A marker extending outward from one edge of the bar."""
        x0 = bar.left() if side == "left" else bar.right()
        x1 = x0 + (-self._SIDE_TICK + 4 if side == "left" else self._SIDE_TICK - 4)
        p.setPen(QPen(color, 1.8))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
