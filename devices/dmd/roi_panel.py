"""Drawing and editing stimulation ROIs over a snapshot. Model: `roi.py`.

Owns its image view rather than reaching into the voltage camera's dock. The
snapshot is handed in (`set_image`), so this never knows which camera took it
and the DMD adapter can host it without the two modules importing each other.

The reachable field is outlined and enforced on drag: an ROI outside it is not
a small error, it is a stimulus that never arrives.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QGraphicsEllipseItem, QGraphicsRectItem, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.devices.dmd.calibration import DmdCalibration
from acqApp.devices.dmd.roi import CircleRoi, RectRoi, RoiSet

# The reachable field is the DMD's own boundary, so it wears the DMD accent —
# the same magenta the tab, its buttons and the panel preview use. The ROI pens
# stay independent colours: they must read against a grey camera frame AND be
# told apart from the field outline they sit inside.
_FIELD_PEN = pg.mkPen(style.HEX["dmd"], width=2, style=Qt.PenStyle.DashLine)
_ROI_PEN = pg.mkPen("#00d0ff", width=2)
_ROI_HOVER = pg.mkPen("#4dff88", width=3)
_BAND_PEN = pg.mkPen("#00d0ff", width=1, style=Qt.PenStyle.DashLine)
_BAND_FILL = pg.mkBrush(0, 208, 255, 40)


class _DrawViewBox(pg.ViewBox):
    """A ViewBox where a left-drag can mean "make an ROI here", not "pan".

    Placing an ROI used to take four gestures — pick a shape, press Add, drag it
    out of the middle of the field, size a handle — and it always started
    somewhere nobody asked for. Dragging where you want it is one.

    Gated on a toggle rather than a modifier key: panning and zooming a 4432 px
    frame is how you find the target in the first place, so the two cannot both
    own an unqualified left-drag.

    The rubber band is not decoration. Without it the drag produced nothing
    until release, so there was no way to tell whether the mode was even armed
    until after committing an ROI — and it is drawn in the SAME shape the
    release will create, so what is dragged is what appears.
    """

    drawn = pyqtSignal(object, object)      # (x0, y0), (x1, y1) in image px

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._draw = False
        # NOT `shape`: QGraphicsItem.shape() is a method Qt calls during hit
        # testing, and shadowing it with a string raises inside Qt's own paint
        # path — where the traceback names neither this class nor the assignment.
        self.roi_shape = "rectangle"
        self._rect = QGraphicsRectItem()
        self._ellipse = QGraphicsEllipseItem()
        for it in (self._rect, self._ellipse):
            it.setPen(_BAND_PEN)
            it.setBrush(_BAND_FILL)
            it.setZValue(1e6)
            it.hide()
            # ignoreBounds: a half-drawn band must not move autoRange.
            self.addItem(it, ignoreBounds=True)

    def set_draw_mode(self, on: bool) -> None:
        self._draw = bool(on)
        self.setCursor(Qt.CursorShape.CrossCursor if on
                       else Qt.CursorShape.ArrowCursor)
        if not on:
            self._hide_band()

    def _hide_band(self) -> None:
        self._rect.hide()
        self._ellipse.hide()

    def _show_band(self, a, b) -> None:
        x0, x1 = sorted((a.x(), b.x()))
        y0, y1 = sorted((a.y(), b.y()))
        if self.roi_shape.startswith("rect"):
            self._ellipse.hide()
            self._rect.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            self._rect.show()
        else:
            # The circle the release will make: same centre, same radius.
            self._rect.hide()
            r = ((x1 - x0) + (y1 - y0)) / 4.0
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            self._ellipse.setRect(QRectF(cx - r, cy - r, 2 * r, 2 * r))
            self._ellipse.show()

    def mouseDragEvent(self, ev, axis=None) -> None:
        if not self._draw or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(ev, axis=axis)
            return
        ev.accept()
        a = self.mapToView(ev.buttonDownPos())
        b = self.mapToView(ev.pos())
        if ev.isFinish():
            self._hide_band()
            self.drawn.emit((a.x(), a.y()), (b.x(), b.y()))
        else:
            self._show_band(a, b)


class RoiEditor(QWidget):
    """Create, move, resize and delete ROIs over a camera snapshot."""

    rois_changed = pyqtSignal(object)      # emits the RoiSet

    def __init__(self, calib: DmdCalibration | None = None, parent=None):
        super().__init__(parent)
        self._calib = calib
        self._set = RoiSet()
        self._items: list = []             # pyqtgraph ROI items, index-aligned
        self._image: np.ndarray | None = None
        self._build()

    # ── construction ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)

        self._gv = pg.GraphicsLayoutWidget()
        self._vb = _DrawViewBox(lockAspect=True, invertY=True)
        self._gv.addItem(self._vb)
        self._img = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img)
        self._field = pg.PlotCurveItem(pen=_FIELD_PEN)
        self._vb.addItem(self._field)
        self._vb.drawn.connect(self._on_drawn)

        # A LUT bar, as the live preview has. The percentile default is right
        # far more often than not, but a dim ROI still needs a drag to find.
        self._hist = pg.HistogramLUTWidget()
        self._hist.setImageItem(self._img)
        self._hist.setFixedWidth(86)
        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.addWidget(self._hist)
        view_row.addWidget(self._gv, 1)
        root.addLayout(view_row, 1)

        bar = QHBoxLayout()
        self._cmb = QComboBox()
        self._cmb.addItems(["rectangle", "circle"])
        bar.addWidget(self._cmb)
        self._btn_draw = QPushButton("Draw")
        self._btn_draw.setCheckable(True)
        self._btn_draw.setToolTip(
            "Drag on the image to place an ROI where you want it.\n"
            "Off, dragging pans the view as usual.")
        self._btn_draw.setStyleSheet(style.toggle_btn("dmd"))
        self._btn_draw.toggled.connect(self._vb.set_draw_mode)
        self._cmb.currentTextChanged.connect(
            lambda t: setattr(self._vb, "roi_shape", t))
        bar.addWidget(self._btn_draw)
        for label, slot in (("Add", self._on_add), ("Delete", self._on_delete),
                            ("Clear", self._on_clear)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch()
        root.addLayout(bar)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row)
        # An index, not the main event: it used to take half the window while
        # the image it describes was squeezed into the top.
        self._list.setMaximumHeight(110)
        root.addWidget(self._list)

        self._status = QLabel("no calibration — ROIs cannot be projected")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{style.muted()};")
        root.addWidget(self._status)
        self._draw_field()

    # ── inputs ───────────────────────────────────────────────────────────────
    def set_image(self, frame: np.ndarray) -> None:
        """Show the snapshot ROIs are drawn on (taken with the DMD all-on).

        Contrast from the 1st/99th percentile, NOT pyqtgraph's autoLevels,
        which stretches to min/max: on a real ORCA frame the signal lives in
        ~800 counts of 65535, so two hot pixels at 65000 collapse the image to
        black. That is why the editor looked far worse than the view it was
        opened from.
        """
        self._image = np.asarray(frame)
        # Strided, not the whole frame: np.percentile sorts, and at full
        # frame that is 87 ms for a contrast estimate. 1/16 of 10.5 Mpx is
        # still 650k samples, and the live preview does the same.
        lo, hi = np.percentile(self._image[::4, ::4], (1, 99))
        if hi <= lo:                    # a flat frame — fall back to the range
            lo, hi = float(self._image.min()), float(self._image.max()) or 1.0
        self._img.setImage(self._image, autoLevels=False,
                           levels=(float(lo), float(hi)))
        self._hist.setLevels(float(lo), float(hi))
        self._vb.autoRange()
        self._refresh_status()


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
            return
        c = self._calib.accessible_corners()
        self._field.setData(np.append(c[:, 0], c[0, 0]),
                            np.append(c[:, 1], c[0, 1]))

    # ── add / remove ─────────────────────────────────────────────────────────
    def _default_centre(self) -> tuple[float, float, float]:
        """Place a new ROI in the middle of the reachable field, not the image:
        dropping it where it cannot be projected is never what was meant."""
        if self._calib is not None:
            c = self._calib.accessible_corners()
            span = float(min(np.ptp(c[:, 0]), np.ptp(c[:, 1])))
            return float(c[:, 0].mean()), float(c[:, 1].mean()), max(8.0, span / 8)
        if self._image is not None:
            h, w = self._image.shape[:2]
            return w / 2.0, h / 2.0, max(8.0, min(w, h) / 8)
        return 50.0, 50.0, 20.0

    def _on_add(self) -> None:
        cx, cy, s = self._default_centre()
        if self._cmb.currentText().startswith("rect"):
            roi = RectRoi(x=cx, y=cy, w=2 * s, h=2 * s)
        else:
            roi = CircleRoi(x=cx, y=cy, r=s)
        self._set.add(roi)
        self._rebuild_items()
        self._list.setCurrentRow(len(self._set) - 1)
        self._emit()

    def _on_drawn(self, a, b) -> None:
        """A drag on the image became an ROI. Ignores a stray click."""
        x0, y0 = a
        x1, y1 = b
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 3 or h < 3:              # a click, not a drag
            return
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if self._cmb.currentText().startswith("rect"):
            roi = RectRoi(x=cx, y=cy, w=w, h=h)
        else:
            # (w + h) / 4, matching the band exactly — the radius the drag
            # previewed is the radius it makes. max() would overflow the drag
            # on the short side and min() would collapse a sloppy one.
            roi = CircleRoi(x=cx, y=cy, r=(w + h) / 4.0)
        self._set.add(roi)
        self._rebuild_items()
        self._list.setCurrentRow(len(self._set) - 1)
        self._emit()

    def _on_delete(self) -> None:
        i = self._list.currentRow()
        if 0 <= i < len(self._set):
            self._set.remove(i)
            self._rebuild_items()
            self._emit()

    def _on_clear(self) -> None:
        self._set.clear()
        self._rebuild_items()
        self._emit()

    def _on_row(self, i: int) -> None:
        for j, it in enumerate(self._items):
            it.setPen(_ROI_HOVER if j == i else _ROI_PEN)

    # ── pyqtgraph items ↔ model ──────────────────────────────────────────────
    def _rebuild_items(self) -> None:
        for it in self._items:
            self._vb.removeItem(it)
        self._items.clear()
        self._list.clear()

        for roi in self._set:
            if isinstance(roi, RectRoi):
                it = pg.RectROI([roi.x - roi.w / 2, roi.y - roi.h / 2],
                                [roi.w, roi.h], pen=_ROI_PEN, rotatable=True)
                it.addRotateHandle([1, 0], [0.5, 0.5])
            else:
                it = pg.CircleROI([roi.x - roi.r, roi.y - roi.r],
                                  [2 * roi.r, 2 * roi.r], pen=_ROI_PEN)
            it.sigRegionChangeFinished.connect(self._on_item_changed)
            self._vb.addItem(it)
            self._items.append(it)
            self._list.addItem(QListWidgetItem(self._describe(roi)))
        self._refresh_status()

    def _describe(self, roi) -> str:
        if isinstance(roi, RectRoi):
            return (f"{roi.name}  rect  ({roi.x:.0f}, {roi.y:.0f})  "
                    f"{roi.w:.0f}x{roi.h:.0f}"
                    + (f"  {roi.angle_deg:.0f}°" if roi.angle_deg else ""))
        return f"{roi.name}  circle  ({roi.x:.0f}, {roi.y:.0f})  r={roi.r:.0f}"

    def _sync_from_items(self) -> None:
        """Read geometry back out of the pyqtgraph items into the model."""
        for roi, it in zip(self._set, self._items):
            pos, size = it.pos(), it.size()
            if isinstance(roi, RectRoi):
                roi.w, roi.h = float(size[0]), float(size[1])
                roi.angle_deg = float(it.angle())
                # RectROI's pos() is its rotated origin corner, so the centre
                # has to come back through the same rotation.
                t = np.radians(roi.angle_deg)
                c, s = np.cos(t), np.sin(t)
                hx, hy = roi.w / 2.0, roi.h / 2.0
                roi.x = float(pos[0] + c * hx - s * hy)
                roi.y = float(pos[1] + s * hx + c * hy)
            else:
                roi.r = float(size[0]) / 2.0
                roi.x = float(pos[0]) + roi.r
                roi.y = float(pos[1]) + roi.r

    def _on_item_changed(self, *_a) -> None:
        self._sync_from_items()
        for i, roi in enumerate(self._set):
            self._list.item(i).setText(self._describe(roi))
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
        # Estimates on purpose: this runs on every drag for a whole-number
        # percentage. `dmd_frame` is the exact answer, on the projection path.
        outside = self._set.outside(self._calib)
        kept = self._set.reach_fraction(self._calib)
        msg = f"{len(self._set)} ROI(s); {100 * kept:.0f}% of the drawn area is "
        msg += "reachable by the DMD"
        if outside:
            msg += f" — outside the field: {', '.join(outside)}"
        self._status.setText(msg)
