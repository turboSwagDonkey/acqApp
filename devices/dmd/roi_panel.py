"""Drawing and editing stimulation ROIs over a snapshot. Model: `roi.py`.

Self-contained on purpose — it owns its image view rather than reaching into
the voltage camera's dock. The snapshot is handed in (`set_image`), so this
widget never knows which camera took it, and the DMD adapter can host it in a
dialog without the two modules importing each other.

The DMD's reachable field is drawn as an outline and enforced on drag: an ROI
outside it is not a small error, it is a stimulus that never arrives.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from acqApp.devices.dmd.calibration import DmdCalibration
from acqApp.devices.dmd.roi import CircleRoi, RectRoi, RoiSet

_FIELD_PEN = pg.mkPen("#ffbf00", width=2, style=Qt.PenStyle.DashLine)
_ROI_PEN = pg.mkPen("#00d0ff", width=2)
_ROI_HOVER = pg.mkPen("#4dff88", width=3)


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
        self._vb = self._gv.addViewBox(lockAspect=True, invertY=True)
        self._img = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img)
        self._field = pg.PlotCurveItem(pen=_FIELD_PEN)
        self._vb.addItem(self._field)
        root.addWidget(self._gv, 1)

        bar = QHBoxLayout()
        self._cmb = QComboBox()
        self._cmb.addItems(["rectangle", "circle"])
        bar.addWidget(self._cmb)
        for label, slot in (("Add", self._on_add), ("Delete", self._on_delete),
                            ("Clear", self._on_clear)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch()
        root.addLayout(bar)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row)
        root.addWidget(self._list)

        self._status = QLabel("no calibration — ROIs cannot be projected")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._draw_field()

    # ── inputs ───────────────────────────────────────────────────────────────
    def set_image(self, frame: np.ndarray) -> None:
        """Show the snapshot ROIs are drawn on (taken with the DMD all-on)."""
        self._image = np.asarray(frame)
        self._img.setImage(self._image, autoLevels=True)
        self._vb.autoRange()
        self._refresh_status()

    def set_calibration(self, calib: DmdCalibration | None) -> None:
        self._calib = calib
        self._draw_field()
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
        # Both are estimates on purpose — this runs on every drag, and the line
        # shows a whole-number percentage. `clipped_mask` is the exact one and
        # belongs on the projection path, not in a status bar.
        outside = self._set.outside(self._calib)
        kept = self._set.reach_fraction(self._calib)
        msg = f"{len(self._set)} ROI(s); {100 * kept:.0f}% of the drawn area is "
        msg += "reachable by the DMD"
        if outside:
            msg += f" — outside the field: {', '.join(outside)}"
        self._status.setText(msg)
