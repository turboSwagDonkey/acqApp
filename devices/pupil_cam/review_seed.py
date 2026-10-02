"""Pupil review: Auto's help, where the user marks the pupil on a few
frames when Auto is unsure. Split out of review_dialog.py."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt

from acqApp.devices.pupil_cam.track_worker import AutoTuneWorker

_SEED_FRAMES = 5        # frames the user marks when Auto needs help


class _SeedMixin:
    """Marks on `_SEED_FRAMES` frames seed a second Auto run."""

    @property
    def seeding(self) -> bool:
        return self._seed_frames is not None and not self._seed_bar.isHidden()

    def _seed_start(self) -> None:
        n = len(self.review)
        k = min(_SEED_FRAMES, n)
        self._seed_frames = [int(i) for i in
                             np.unique(np.linspace(n * 0.1, n * 0.9, k).astype(int))]
        self._seed_marks = []
        self._seed_bar.show()
        self._set_running()
        self._seed_show()

    def _seed_show(self) -> None:
        """Go to the next frame to mark."""
        k = len(self._seed_marks)
        self._clear_seed_roi()
        self.goto(self._seed_frames[k], force=True)
        self._btn_seed_next.setEnabled(False)
        self._lbl_seed.setText(
            f"Auto needs help ({k + 1}/{len(self._seed_frames)}): click the "
            f"pupil's centre. Resize the circle to fit it, if you like.")

    def _on_image_click(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or self.review is None:
            return
        if not self.seeding:
            if self._btn_pin_cr.isChecked():
                if self._vb.sceneBoundingRect().contains(ev.scenePos()):
                    p = self._vb.mapSceneToView(ev.scenePos())
                    self.toggle_pin(p.x(), p.y())
                    ev.accept()
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = self._vb.mapSceneToView(ev.scenePos())
        h, w = self.review.reader.height, self.review.reader.width
        r = self._seed_r0 or max(5.0, min(h, w) * 0.06)
        self._clear_seed_roi()
        self._seed_roi = pg.CircleROI((p.x() - r, p.y() - r), (2 * r, 2 * r),
                                      pen=pg.mkPen("#ffd166", width=2))
        self._seed_r0 = r
        self._vb.addItem(self._seed_roi)
        self._btn_seed_next.setEnabled(True)
        ev.accept()

    def toggle_pin(self, x: float, y: float) -> None:
        """Unpin the pin under (x, y), or pin the reflection there, sized to
        the bright blob on this frame (as the live tab does)."""
        st = self._read_settings()
        pins = list(st.cr_pins)
        for i, (px, py, pr) in enumerate(pins):
            if np.hypot(x - px, y - py) <= pr:
                pins.pop(i)
                self._ctl.set_pins(pins)
                return
        r = 8.0
        try:
            from acqApp.devices.pupil_cam.eyeloop_tracker import measure_reflection
            r = measure_reflection(self._data, (x, y), threshold=st.cr_threshold)
        except Exception:                               # noqa: BLE001 — no cv2
            pass
        pins.append((float(x), float(y), float(r)))
        self._ctl.set_pins(pins)

    def _draw_pins(self) -> None:
        pins = self._read_settings().cr_pins if self.review is not None else []
        if not pins:
            self._pin_curve.setData([], [])
            return
        th = np.linspace(0, 2 * np.pi, 33)
        xs, ys = [], []
        for px, py, pr in pins:
            xs += list(px + pr * np.cos(th)) + [np.nan]
            ys += list(py + pr * np.sin(th)) + [np.nan]
        self._pin_curve.setData(np.array(xs), np.array(ys))

    def _clear_seed_roi(self) -> None:
        if self._seed_roi is not None:
            self._vb.removeItem(self._seed_roi)
            self._seed_roi = None

    def _seed_mark(self):
        """(x, y, r or None) from the circle: r only if it was resized."""
        roi = self._seed_roi
        d = roi.size()[0]
        p = roi.pos()
        r = d / 2.0
        drawn = abs(r - self._seed_r0) > 0.5
        if drawn:
            self._seed_r0 = r       # the next circle starts at this size
        return (p.x() + r, p.y() + r, r if drawn else None)

    def _seed_next(self, skip: bool = False) -> None:
        if self._seed_frames is None:
            return
        self._seed_marks.append(None if skip or self._seed_roi is None
                                else self._seed_mark())
        if len(self._seed_marks) < len(self._seed_frames):
            self._seed_show()
            return
        self._clear_seed_roi()
        self._seed_bar.hide()
        marks = [m for m in self._seed_marks if m is not None]
        if not marks:
            self._seed_cancel()
            return
        frames = [np.array(self.review.reader.luma(i)) for i in self._seed_frames]
        st = self._read_settings()
        region = None if self._region_default else st.search_limit()
        self._auto_worker = AutoTuneWorker(frames, region, self._seed_marks)
        self._auto_worker.done.connect(self._on_auto)
        self._auto_worker.error.connect(self._on_auto_error)
        self._prog.setText("finding parameters from your marks…")
        self._set_running()
        self._auto_worker.start()

    def _seed_cancel(self) -> None:
        self._clear_seed_roi()
        self._seed_bar.hide()
        self._seed_frames = None
        self._seed_marks = []
        self._prog.setText("Auto cancelled.")
        self._set_running()
        self.goto(self._frame, force=True)
