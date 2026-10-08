"""Drawing and editing stimulation ROIs over a snapshot. Model: `roi.py`.

The snapshot is handed in (`set_image`), so this never knows which camera
took it. The reachable field is outlined, not clamped: an ROI outside it is
only named by `_refresh_status()` — a stimulus that never arrives.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.ROI import Handle
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QKeySequence, QPainter,
                         QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QGraphicsEllipseItem, QGraphicsPathItem,
    QGraphicsRectItem, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.devices.dmd import roi_store
from acqApp.devices.dmd.calibration import DmdCalibration
from acqApp.devices.dmd.roi import (CircleRoi, PolyRoi, RectRoi, RoiSet,
                                    simplify_polygon)
from acqApp.devices.dmd.roi_picker import RoiSetPicker

# The field wears the DMD accent; ROI pens must read against a grey frame
# and stay distinct from it.
_FIELD_PEN = pg.mkPen(style.HEX["dmd"], width=2, style=Qt.PenStyle.DashLine)
# Distinct colour AND dash: dim, not unreachable, and it can overlap the field.
_VIGNETTE_PEN = pg.mkPen(style.WARN, width=2, style=Qt.PenStyle.DotLine)
_ROI_PEN = pg.mkPen("#00d0ff", width=2)
_ROI_HOVER = pg.mkPen("#4dff88", width=3)
_BAND_PEN = pg.mkPen("#00d0ff", width=1, style=Qt.PenStyle.DashLine)
_BAND_FILL = pg.mkBrush(0, 208, 255, 40)


def snapshot_levels(frame: np.ndarray) -> tuple[float, float]:
    """1st/99th-percentile contrast, not pyqtgraph's min/max autoLevels: an
    ORCA frame's signal is ~800 of 65535 counts, so two hot pixels black it
    out. Strided 4x4: a full-frame percentile sorts, 87 ms."""
    lo, hi = np.percentile(frame[::4, ::4], (1, 99))
    if hi <= lo:                        # a flat frame — fall back to the range
        lo, hi = float(frame.min()), float(frame.max()) or 1.0
    return float(lo), float(hi)


TOOLS = ("rectangle", "circle", "free", "pan")
_SHAPE_LABEL = {"rect": "Rectangle", "circle": "Circle", "poly": "Free-form"}
_TABLE_ROWS = 5                 # rows the ROI table always shows; more scroll


def circle_radius(centre, edge) -> float:
    """A circle drawn from its centre out to where the pointer is."""
    return float(np.hypot(edge[0] - centre[0], edge[1] - centre[1]))


class _DrawViewBox(pg.ViewBox):
    """A ViewBox where a left-drag makes an ROI (rectangle, circle or
    free-form tool) or pans (pan tool). A drag that starts on an existing ROI
    moves it in every tool: pyqtgraph hands it to the ROI item first.

    The tool is a visible choice, not a modifier: panning a 4432 px frame is
    how the target is found. The rubber band shows the shape the release
    creates: a rectangle between the two corners, a circle from its centre
    (where the drag began) out to the pointer, or the traced outline closed.
    """

    drawn = pyqtSignal(object, object)      # press (x, y), release (x, y), image px
    drawn_free = pyqtSignal(object)         # the traced [(x, y), ...], image px

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._draw = True
        self._trace: list = []              # the free-form stroke so far
        # Never name this `shape`: QGraphicsItem.shape() is a method Qt calls
        # during hit testing, and shadowing it with a string raises inside Qt's
        # own paint path, where the traceback names neither this class nor the
        # assignment.
        self.tool = "rectangle"
        self._rect = QGraphicsRectItem()
        self._ellipse = QGraphicsEllipseItem()
        self._stroke = QGraphicsPathItem()
        for it in (self._rect, self._ellipse, self._stroke):
            it.setPen(_BAND_PEN)
            it.setBrush(_BAND_FILL)
            it.setZValue(1e6)
            it.hide()
            # ignoreBounds: a half-drawn band must not move autoRange.
            self.addItem(it, ignoreBounds=True)

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._draw = tool != "pan"
        self.setCursor(Qt.CursorShape.CrossCursor if self._draw
                       else Qt.CursorShape.OpenHandCursor)
        if not self._draw:
            self._hide_band()
        self._trace.clear()

    def _hide_band(self) -> None:
        self._rect.hide()
        self._ellipse.hide()
        self._stroke.hide()

    def _show_stroke(self) -> None:
        """The outline so far, closed, in the shape the release will keep."""
        path = QPainterPath(self._trace[0])
        for p in self._trace[1:]:
            path.lineTo(p)
        path.closeSubpath()
        self._stroke.setPath(path)
        self._stroke.show()

    def _show_band(self, a, b) -> None:
        if self.tool == "rectangle":
            x0, x1 = sorted((a.x(), b.x()))
            y0, y1 = sorted((a.y(), b.y()))
            self._ellipse.hide()
            self._rect.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            self._rect.show()
        else:
            # The circle the release will make: centred on the press.
            self._rect.hide()
            r = circle_radius((a.x(), a.y()), (b.x(), b.y()))
            self._ellipse.setRect(QRectF(a.x() - r, a.y() - r, 2 * r, 2 * r))
            self._ellipse.show()

    def mouseDragEvent(self, ev, axis=None) -> None:
        if not self._draw or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(ev, axis=axis)
            return
        ev.accept()
        a = self.mapToView(ev.buttonDownPos())
        b = self.mapToView(ev.pos())
        if self.tool == "free":
            self._trace += [a, b] if not self._trace else [b]
            if ev.isFinish():
                pts = [(p.x(), p.y()) for p in self._trace]
                self._trace.clear()
                self._hide_band()
                self.drawn_free.emit(pts)
            else:
                self._show_stroke()
            return
        if ev.isFinish():
            self._hide_band()
            self.drawn.emit((a.x(), a.y()), (b.x(), b.y()))
        else:
            self._show_band(a, b)


_HANDLE_PX = 7                          # handle radius, screen px
_HANDLE_COLOUR = {"resize": QColor(255, 200, 0), "rotate": QColor(255, 90, 200)}
_rotate_cursor_cache: list = []


def _rotate_cursor() -> QCursor:
    """A circular arrow: Qt has no stock "rotate" cursor."""
    if not _rotate_cursor_cache:
        pm = QPixmap(28, 28)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, rad = 14.0, 8.0
        a_end = np.radians(40.0 + 270.0)        # the arc runs 40 -> 310 degrees
        end = np.array([c + rad * np.cos(a_end), c - rad * np.sin(a_end)])
        tan = np.array([-np.sin(a_end), -np.cos(a_end)])    # screen y points down
        nrm = np.array([-tan[1], tan[0]])
        head = QPolygonF([QPointF(*(end + 5.0 * tan)),
                          QPointF(*(end + 3.5 * nrm)), QPointF(*(end - 3.5 * nrm))])
        for colour, w in ((QColor(0, 0, 0), 4.0), (_HANDLE_COLOUR["rotate"], 2.0)):
            p.setPen(QPen(colour, w))
            p.setBrush(colour)
            p.drawArc(QRectF(c - rad, c - rad, 2 * rad, 2 * rad), 40 * 16, 270 * 16)
            p.drawPolygon(head)
        p.end()
        _rotate_cursor_cache.append(QCursor(pm, 14, 14))
    return _rotate_cursor_cache[0]


class _Handle(Handle):
    """A handle you can read at a glance. Filled, so it shows on any image;
    its shape and colour say what it does: a yellow diamond resizes, a pink
    circle rotates. The cursor says it again."""

    def __init__(self, typ, parent):
        self._role = "rotate" if "r" in (typ or "") else "resize"
        super().__init__(_HANDLE_PX, typ=typ, pen=pg.mkPen((20, 20, 20), width=2),
                         hoverPen=pg.mkPen("w", width=2), parent=parent)
        self._fill = QBrush(_HANDLE_COLOUR[self._role])
        self.setAcceptHoverEvents(True)             # a QGraphicsItem cursor needs it
        self.setCursor(_rotate_cursor() if self._role == "rotate"
                       else QCursor(Qt.CursorShape.SizeFDiagCursor))

    def paint(self, p, opt, widget) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(self.currentPen)
        p.setBrush(self._fill)
        p.drawPath(self.shape())


class _StyledHandles:
    """Mixin for a pyqtgraph ROI: every handle it grows is a `_Handle`."""

    def addHandle(self, info, index=None):
        if info.get("item") is None:
            info["item"] = _Handle(info["type"], self)
        return super().addHandle(info, index)


class _RectItem(_StyledHandles, pg.RectROI):
    pass


class _CircleItem(_StyledHandles, pg.CircleROI):
    pass


class _PolyItem(pg.ROI):
    """A traced outline on the image: drag it to move it, nothing else. No
    corner handles (they cluttered it); its size is set from the table row."""

    def __init__(self, points, **kw):
        self._poly = QPolygonF()
        super().__init__((0.0, 0.0), (1.0, 1.0), **kw)
        self.set_points(points)

    def set_points(self, points) -> None:
        """Replace the outline (local coordinates, the item at its origin)."""
        self.prepareGeometryChange()
        self._poly = QPolygonF([QPointF(x, y) for x, y in points])
        self.update()

    def local_points(self) -> list:
        return [(p.x(), p.y()) for p in self._poly]

    def reset(self, points) -> None:
        """Back at the origin with a new outline, without announcing it (the
        caller does): a half-moved one must not reach the model."""
        self.blockSignals(True)
        try:
            self.set_points(points)
            self.setPos((0.0, 0.0))
        finally:
            self.blockSignals(False)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addPolygon(self._poly)
        path.closeSubpath()
        return path

    def boundingRect(self) -> QRectF:
        return self.shape().boundingRect().adjusted(-2, -2, 2, 2)

    def paint(self, p, opt, widget=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(self.currentPen)
        p.drawPolygon(self._poly)


_NUM_COL0 = 2                   # the first numeric column, after Name and Shape


class _RoiTable(QTableWidget):
    """Enter on a selected row edits its number. Unhandled, Enter reaches the
    dialog the editor sits in and presses its default button (Save), which
    closes it."""

    def keyPressEvent(self, ev) -> None:
        if (ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and self.state() != QAbstractItemView.State.EditingState
                and self.currentRow() >= 0):
            row, col = self.currentRow(), max(self.currentColumn(), _NUM_COL0)
            item = self.item(row, col)
            if item is not None and item.flags() & Qt.ItemFlag.ItemIsEditable:
                self.setCurrentCell(row, col)
                self.edit(self.currentIndex())
            ev.accept()
            return
        super().keyPressEvent(ev)


class RoiEditor(QWidget):
    """Create, move, resize and delete ROIs over a camera snapshot."""

    rois_changed = pyqtSignal(object)      # emits the RoiSet

    def __init__(self, calib: DmdCalibration | None = None, parent=None, *,
                 offset: tuple[float, float] = (0.0, 0.0),
                 sensor: tuple[float, float] | None = None,
                 scale: float = 1.0):
        """`offset` = the capture preset's (hpos, vpos), the sensor px of the
        frame's (0, 0); `scale` = sensor px per frame px (binning). The model
        and calibration are in sensor px, pyqtgraph items preset-local: this
        is the one seam converting between them.
        """
        super().__init__(parent)
        self._calib = calib
        self._ox, self._oy = offset
        self._scale = float(scale)
        self._sensor = sensor              # full (w, h), drawn as the outer frame
        self._set = RoiSet()
        self._items: list = []             # pyqtgraph ROI items, index-aligned
        self._image: np.ndarray | None = None
        self._tool = "rectangle"
        self._build()

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # Fixed room for their text: a line more or less must never resize the
        # window (the same reason the table below is always five rows tall).
        self._hint = self._reserved_label(3)
        self._legend = self._reserved_label(1)
        root.addWidget(self._hint)
        root.addWidget(self._legend)

        self._gv = pg.GraphicsLayoutWidget()
        self._vb = _DrawViewBox(lockAspect=True, invertY=True)
        self._gv.addItem(self._vb)
        self._img = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img)
        self._frame = pg.PlotCurveItem(pen=pg.mkPen(style.muted(), width=1))
        self._vb.addItem(self._frame)
        if self._sensor is not None:
            w, h = self._sensor
            x0, y0 = -self._ox, -self._oy
            self._frame.setData([x0, x0 + w, x0 + w, x0, x0],
                                [y0, y0, y0 + h, y0 + h, y0])
        self._field = pg.PlotCurveItem(pen=_FIELD_PEN)
        self._vb.addItem(self._field)
        self._vignette = pg.PlotCurveItem(pen=_VIGNETTE_PEN)
        self._vb.addItem(self._vignette)
        self._vb.drawn.connect(self._on_drawn)
        self._vb.drawn_free.connect(self._on_free)

        self._hist = pg.HistogramLUTWidget()
        self._hist.setImageItem(self._img)
        self._hist.setFixedWidth(86)
        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.addWidget(self._hist)
        view_row.addWidget(self._gv, 1)
        root.addLayout(view_row, 1)

        bar = QHBoxLayout()
        self._tool_btns: dict[str, QPushButton] = {}
        group = QButtonGroup(self)          # exactly one tool is always armed
        for key, label, tip in (
                ("rectangle", "Rectangle",
                 "Drag on the image from one corner to the opposite one."),
                ("circle", "Circle",
                 "Drag on the image from the circle's centre out to its edge."),
                ("free", "Free-form",
                 "Trace any outline: hold the button and drag round the "
                 "target. Letting go closes it."),
                ("pan", "Pan",
                 "Drag to move the view instead of drawing. "
                 "The scroll wheel zooms in every tool.")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setStyleSheet(style.toggle_btn("dmd"))
            b.toggled.connect(lambda on, k=key: on and self._on_tool(k))
            group.addButton(b)
            bar.addWidget(b)
            self._tool_btns[key] = b
        bar.addSpacing(16)
        for label, tip, slot in (
                ("Delete", "Remove the selected ROI (Delete key).",
                 self._on_delete),
                ("Clear", "Remove every ROI.", self._on_clear)):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch()
        btn_save = QPushButton("Save…")
        btn_save.setToolTip(
            "Save this set under a name, into this session's quick list.")
        btn_save.clicked.connect(self._on_save_roi)
        btn_load = QPushButton("Load…")
        btn_load.setToolTip(
            "Load a set saved this session, or Browse older ones.")
        btn_load.clicked.connect(self._on_load_roi)
        bar.addWidget(btn_save)
        bar.addWidget(btn_load)
        root.addLayout(bar)

        self._build_table(root)
        for key in ("Delete", "Backspace"):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self._on_delete)
        self._tool_btns["rectangle"].setChecked(True)

        self._status = self._reserved_label(2)
        self._status.setText("no calibration — ROIs can't be projected")
        root.addWidget(self._status)
        self._draw_field()

    # ── inputs ───────────────────────────────────────────────────────────────
    def set_image(self, frame: np.ndarray, keep_view: bool = False) -> None:
        """Show the snapshot ROIs are drawn on (taken with the DMD all-on).
        `keep_view`: a live refresh, so the zoom and contrast stay put."""
        self._image = np.asarray(frame)
        if keep_view:
            self._img.setImage(self._image, autoLevels=False)
        else:
            lo, hi = snapshot_levels(self._image)
            self._img.setImage(self._image, autoLevels=False, levels=(lo, hi))
            self._hist.setLevels(lo, hi)
        # At its SENSOR extent: a binned frame has fewer px than it covers.
        h, w = self._image.shape[:2]
        self._img.setRect(QRectF(0.0, 0.0, w * self._scale, h * self._scale))
        if not keep_view:
            self._vb.autoRange()
            self._refresh_status()

    @property
    def can_project(self) -> bool:
        return self._calib is not None

    @property
    def roi_set(self) -> RoiSet:
        self._sync_from_items()
        return self._set

    def load(self, rois: RoiSet) -> None:
        self._set = rois
        self._rebuild_items()

    # ── the DMD field outline ────────────────────────────────────────────────
    def _draw_field(self) -> None:
        if self._calib is None:
            self._field.setData([], [])
            self._vignette.setData([], [])
            return
        c = self._calib.accessible_corners()
        self._field.setData(np.append(c[:, 0] - self._ox, c[0, 0] - self._ox),
                            np.append(c[:, 1] - self._oy, c[0, 1] - self._oy))
        if self._calib.vignette is None:
            self._vignette.setData([], [])
        else:
            cx, cy, r = self._calib.vignette
            t = np.linspace(0.0, 2.0 * np.pi, 96)
            self._vignette.setData(cx - self._ox + r * np.cos(t),
                                   cy - self._oy + r * np.sin(t))

    # ── tools ────────────────────────────────────────────────────────────────
    def set_tool(self, tool: str) -> None:
        """Arm "rectangle", "circle" or "pan"."""
        self._tool_btns[tool].setChecked(True)

    def _on_tool(self, tool: str) -> None:
        self._tool = tool
        self._vb.set_tool(tool)
        self._refresh_hint()

    @staticmethod
    def _reserved_label(lines: int) -> QLabel:
        """A muted, wrapping label that is always `lines` lines tall."""
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setStyleSheet(f"color:{style.muted()};")
        lbl.setFixedHeight(lbl.fontMetrics().lineSpacing() * lines + 4)
        return lbl

    def _refresh_hint(self) -> None:
        if self._tool == "pan":
            self._legend.setText("")
            msg = "Drag to pan, scroll to zoom. Pick a shape tool to draw."
        else:
            hx = {k: c.name() for k, c in _HANDLE_COLOUR.items()}
            self._legend.setText(
                f"Handles: <span style='color:{hx['resize']}'>◆</span> resize"
                f" &nbsp; <span style='color:{hx['rotate']}'>●</span> rotate"
                f" (rectangles)")
            what = {"rectangle": "a rectangle, corner to corner",
                    "circle": "a circle, centre outwards",
                    "free": "a free-form outline: letting go closes it"
                    }[self._tool]
            msg = (f"Drag on the image to draw {what}. Drag a shape to move it, "
                   f"a handle to resize or rotate it; a free-form outline moves "
                   f"as one piece, and its row below sets its size. "
                   f"Scroll to zoom; Pan moves the view.")
        if self._table.currentRow() >= 0:
            msg += "  Delete removes the selected ROI."
        self._hint.setText(msg)

    # ── add / remove ─────────────────────────────────────────────────────────
    def _add(self, roi) -> None:
        self._set.add(roi)
        self._rebuild_items()
        self._table.setCurrentCell(len(self._set) - 1, 0)
        self._emit()

    def _on_drawn(self, a, b) -> None:
        """A drag on the image became an ROI. Ignores a stray click."""
        x0, y0 = a[0] + self._ox, a[1] + self._oy
        x1, y1 = b[0] + self._ox, b[1] + self._oy
        if self._tool == "circle":
            r = circle_radius((x0, y0), (x1, y1))
            if r >= 2:                  # a click, not a drag
                self._add(CircleRoi(x=x0, y=y0, r=r))
            return
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 3 or h < 3:              # a click, not a drag
            return
        self._add(RectRoi(x=(x0 + x1) / 2.0, y=(y0 + y1) / 2.0, w=w, h=h))

    def _on_free(self, trace) -> None:
        """A traced stroke became a free-form ROI: thinned to a few corners
        that stay editable. Ignores a click or a sliver."""
        pts = np.asarray(trace, dtype=np.float64).reshape(-1, 2) + [self._ox, self._oy]
        if len(pts) < 3:
            return
        diag = float(np.hypot(*np.ptp(pts, axis=0)))
        poly = simplify_polygon(pts, tol=max(1.0, 0.01 * diag))
        x, y = poly[:, 0], poly[:, 1]
        area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
        if len(poly) < 3 or area < 9.0:
            return
        self._add(PolyRoi(points=poly.tolist()))

    def _on_delete(self) -> None:
        i = self._table.currentRow()
        if 0 <= i < len(self._set):
            self._set.remove(i)
            self._rebuild_items()
            self._emit()

    def _on_clear(self) -> None:
        self._set.clear()
        self._rebuild_items()
        self._emit()

    # ── save / load ──────────────────────────────────────────────────────────
    def _on_save_roi(self) -> None:
        self._sync_from_items()
        if not len(self._set):
            self._status.setText("Nothing to save — draw an ROI first.")
            return
        name, ok = QInputDialog.getText(self, "Save ROI set", "Name:")
        if not ok or not name.strip():
            return
        path = roi_store.save(name.strip(), self._set)
        self._status.setText(f"Saved {len(self._set)} ROI(s) to {path.name}")

    def _on_load_roi(self) -> None:
        dlg = RoiSetPicker(self)
        if not dlg.exec() or dlg.path is None:
            return
        try:
            rois = roi_store.load(dlg.path)
        except (OSError, ValueError, KeyError) as e:
            self._status.setText(
                f"Could not load {dlg.path.name} ({type(e).__name__})")
            return
        self.load(rois)
        self._emit()

    def _on_row(self, i: int) -> None:
        for j, it in enumerate(self._items):
            it.setPen(_ROI_HOVER if j == i else _ROI_PEN)
        self._refresh_hint()

    def _select_item(self, it) -> None:
        """A click or drag on an ROI on the image selects its table row."""
        if it in self._items:
            self._table.setCurrentCell(self._items.index(it), 0)

    # ── the ROI table: one row per ROI, its numbers editable in place ────────
    _FIELDS = (                 # key, header, lo, hi, kinds that use it
        ("x", "X", -1e5, 1e5, ("rect", "circle", "poly")),
        ("y", "Y", -1e5, 1e5, ("rect", "circle", "poly")),
        ("w", "W", 1.0, 1e5, ("rect", "poly")),
        ("h", "H", 1.0, 1e5, ("rect", "poly")),
        ("r", "Radius", 1.0, 1e5, ("circle",)),
        ("angle_deg", "Angle (°)", -360.0, 360.0, ("rect",)),
    )
    _NUM0 = _NUM_COL0

    def _build_table(self, root: QVBoxLayout) -> None:
        self._filling = False
        t = _RoiTable(0, self._NUM0 + len(self._FIELDS))
        t.setHorizontalHeaderLabels(
            ["Name", "Shape"] + [f[1] for f in self._FIELDS])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                          | QAbstractItemView.EditTrigger.SelectedClicked
                          | QAbstractItemView.EditTrigger.EditKeyPressed)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for c, tip in ((self._NUM0, "Centre, in sensor pixels."),
                       (self._NUM0 + 1, "Centre, in sensor pixels.")):
            t.horizontalHeaderItem(c).setToolTip(tip)
        t.currentCellChanged.connect(lambda row, *_a: self._on_row(row))
        t.itemChanged.connect(self._on_cell)
        self._table = t
        root.addWidget(t)
        # Always five rows tall, filled or not (more scroll): adding an ROI
        # must not resize the window.
        t.setFixedHeight(t.horizontalHeader().sizeHint().height()
                         + _TABLE_ROWS * t.verticalHeader().defaultSectionSize()
                         + 2 * t.frameWidth() + 2)

    def _fill_table(self) -> None:
        t = self._table
        self._filling = True
        try:
            t.setRowCount(0)
            t.setRowCount(len(self._set))
            for i in range(len(self._set)):
                for c in range(t.columnCount()):
                    t.setItem(i, c, QTableWidgetItem())
                self._fill_row(i)
        finally:
            self._filling = False
        self._refresh_hint()

    def _fill_row(self, i: int) -> None:
        """Write one ROI's numbers into its row; a column that doesn't apply
        to its shape reads "—" and can't be edited."""
        roi, t = self._set[i], self._table
        keep, self._filling = self._filling, True
        try:
            t.item(i, 0).setText(roi.name)
            t.item(i, 1).setText(_SHAPE_LABEL[roi.kind])
            for c, (key, _h, _lo, _hi, kinds) in enumerate(
                    self._FIELDS, start=self._NUM0):
                it = t.item(i, c)
                used = roi.kind in kinds
                it.setText(f"{getattr(roi, key):.1f}" if used else "—")
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                it.setFlags(flags | Qt.ItemFlag.ItemIsEditable if used else flags)
                it.setForeground(QBrush() if used else QBrush(QColor(style.muted())))
            for c in (0, 1):
                t.item(i, c).setFlags(Qt.ItemFlag.ItemIsEnabled
                                      | Qt.ItemFlag.ItemIsSelectable)
        finally:
            self._filling = keep

    def _on_cell(self, item) -> None:
        """A number typed into the table: into the model, then onto the image."""
        i, c = item.row(), item.column()
        if self._filling or c < self._NUM0 or not 0 <= i < len(self._set):
            return
        key, _h, lo, hi, kinds = self._FIELDS[c - self._NUM0]
        roi = self._set[i]
        if roi.kind in kinds:
            try:
                setattr(roi, key, min(hi, max(lo, float(item.text()))))
            except ValueError:              # not a number: put the old one back
                pass
            else:
                self._push_to_item(i)
        self._fill_row(i)

    def _item_state(self, roi) -> dict:
        """The pyqtgraph pos/size/angle of a model ROI: the inverse of
        `_sync_from_items`. RectROI's pos is its rotated origin corner."""
        if isinstance(roi, RectRoi):
            t = np.radians(roi.angle_deg)
            c, s = np.cos(t), np.sin(t)
            hx, hy = roi.w / 2.0, roi.h / 2.0
            return {"pos": (roi.x - self._ox - (c * hx - s * hy),
                            roi.y - self._oy - (s * hx + c * hy)),
                    "size": (roi.w, roi.h), "angle": roi.angle_deg}
        return {"pos": (roi.x - roi.r - self._ox, roi.y - roi.r - self._oy),
                "size": (2 * roi.r, 2 * roi.r), "angle": 0.0}

    def _push_to_item(self, i: int) -> None:
        """Model -> item after a typed edit; the item's finish signal then
        refreshes the table, status and listeners."""
        roi, it = self._set[i], self._items[i]
        if isinstance(roi, PolyRoi):
            it.reset(self._poly_local(roi))
            self._on_item_changed()             # a reset is silent
        else:
            it.setState(self._item_state(roi))

    def _poly_local(self, roi) -> list:
        return [[x - self._ox, y - self._oy] for x, y in roi.points]

    # ── pyqtgraph items ↔ model ──────────────────────────────────────────────
    def _rebuild_items(self) -> None:
        for it in self._items:
            self._vb.removeItem(it)
        self._items.clear()

        for roi in self._set:
            if isinstance(roi, PolyRoi):
                it = _PolyItem(self._poly_local(roi), pen=_ROI_PEN)
            elif isinstance(roi, RectRoi):
                st = self._item_state(roi)
                it = _RectItem(st["pos"], st["size"], angle=st["angle"],
                               pen=_ROI_PEN, rotatable=True)
                it.addRotateHandle([1, 0], [0.5, 0.5])
            else:
                st = self._item_state(roi)
                it = _CircleItem(st["pos"], st["size"], pen=_ROI_PEN)
            it.sigRegionChangeFinished.connect(self._on_item_changed)
            it.sigRegionChangeStarted.connect(
                lambda *_a, it=it: self._select_item(it))
            it.sigClicked.connect(lambda *_a, it=it: self._select_item(it))
            self._vb.addItem(it)
            self._items.append(it)
        self._fill_table()
        self._refresh_status()

    def _sync_from_items(self) -> None:
        """Read geometry back out of the pyqtgraph items into the model."""
        for roi, it in zip(self._set, self._items):
            pos, size = it.pos(), it.size()
            if isinstance(roi, PolyRoi):
                roi.points = [[float(x + pos[0] + self._ox),
                               float(y + pos[1] + self._oy)]
                              for x, y in it.local_points()]
            elif isinstance(roi, RectRoi):
                roi.w, roi.h = float(size[0]), float(size[1])
                roi.angle_deg = float(it.angle())
                # RectROI's pos() is its rotated origin corner, so the centre
                # has to come back through the same rotation.
                t = np.radians(roi.angle_deg)
                c, s = np.cos(t), np.sin(t)
                hx, hy = roi.w / 2.0, roi.h / 2.0
                roi.x = float(pos[0] + self._ox + c * hx - s * hy)
                roi.y = float(pos[1] + self._oy + s * hx + c * hy)
            else:
                roi.r = float(size[0]) / 2.0
                roi.x = float(pos[0]) + self._ox + roi.r
                roi.y = float(pos[1]) + self._oy + roi.r

    def _on_item_changed(self, *_a) -> None:
        self._sync_from_items()
        for i in range(len(self._set)):
            self._fill_row(i)
        self._refresh_status()
        self._emit()

    def _emit(self) -> None:
        self.rois_changed.emit(self._set)

    # ── status ───────────────────────────────────────────────────────────────
    def _refresh_status(self) -> None:
        if self._calib is None:
            self._status.setText(
                "No DMD calibration loaded — ROIs can be drawn but not "
                "projected. Run the calibration sweep first.")
            return
        if not len(self._set):
            self._status.setText(f"{self._calib.describe()} — no ROIs yet")
            return
        self._sync_from_items()
        # Estimates: this runs on every drag for a whole-number percentage.
        outside = self._set.outside(self._calib)
        dim = self._set.dim(self._calib)
        kept = self._set.reach_fraction(self._calib)
        msg = f"{len(self._set)} ROI(s); {100 * kept:.0f}% of the drawn area is "
        msg += "reachable by the DMD"
        if outside:
            msg += f" — outside the field: {', '.join(outside)}"
        if dim:
            msg += f" — dim (past the vignette): {', '.join(dim)}"
        self._status.setText(msg)
