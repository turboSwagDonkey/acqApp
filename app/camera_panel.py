"""
CameraPanel — QWidget that slots into the Camera dock.

Layout
------
  [ImageView preview — fills most of the panel]
  ─────────────────────────────────────────────
  Exposure: [____] ms   Binning: [1▼]   ROI: x0[_] x1[_] y0[_] y1[_]  [Clear ROI]
  [Start]  [Stop]   Status: ...
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

try:
    import pyqtgraph as pg
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

from acq.camera_worker import CameraWorker
from acq.clock import AbstractClock
from acq.ring_buffer import RingBuffer


class CameraPanel(QWidget):
    # Ring buffer sized for ~4 s at 100 fps full-frame; resize after Phase 0 result.
    DEFAULT_BUFFER_FRAMES = 400

    def __init__(
        self,
        clock: AbstractClock,
        ring_buffer: RingBuffer | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._buf = ring_buffer or RingBuffer(self.DEFAULT_BUFFER_FRAMES)
        self._worker: CameraWorker | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # Preview area
        if _HAS_PG:
            self._view = pg.ImageView()
            self._view.ui.roiBtn.hide()
            self._view.ui.menuBtn.hide()
            self._view.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            root.addWidget(self._view, stretch=1)
        else:
            lbl = QLabel("[pyqtgraph not installed — no live preview]")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(lbl, stretch=1)
            self._view = None

        # Controls
        ctrl = QHBoxLayout()
        root.addLayout(ctrl)

        # Exposure
        ctrl.addWidget(QLabel("Exposure:"))
        self._exp_spin = QDoubleSpinBox()
        self._exp_spin.setRange(0.1, 10_000.0)
        self._exp_spin.setValue(10.0)
        self._exp_spin.setSuffix(" ms")
        self._exp_spin.setDecimals(2)
        ctrl.addWidget(self._exp_spin)

        # Binning
        ctrl.addWidget(QLabel("Binning:"))
        self._bin_combo = QComboBox()
        for b in (1, 2, 4):
            self._bin_combo.addItem(str(b), userData=b)
        ctrl.addWidget(self._bin_combo)

        # ROI
        roi_box = QGroupBox("ROI (blank = full frame)")
        roi_lay = QHBoxLayout(roi_box)
        roi_lay.setContentsMargins(4, 2, 4, 2)
        self._roi_spins: dict[str, QSpinBox] = {}
        for label in ("x0", "x1", "y0", "y1"):
            roi_lay.addWidget(QLabel(label + ":"))
            sp = QSpinBox()
            sp.setRange(0, 99_999)
            sp.setValue(0)
            sp.setSpecialValueText("—")
            self._roi_spins[label] = sp
            roi_lay.addWidget(sp)
        clear_roi = QPushButton("Clear")
        clear_roi.clicked.connect(self._clear_roi)
        roi_lay.addWidget(clear_roi)
        ctrl.addWidget(roi_box)

        ctrl.addStretch()

        # Start / Stop
        self._start_btn = QPushButton("Start")
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._start)
        self._stop_btn.clicked.connect(self._stop)
        ctrl.addWidget(self._start_btn)
        ctrl.addWidget(self._stop_btn)

        # Status bar
        self._status_lbl = QLabel("Ready")
        root.addWidget(self._status_lbl)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _clear_roi(self) -> None:
        for sp in self._roi_spins.values():
            sp.setValue(0)

    def _start(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        exposure_s = self._exp_spin.value() / 1000.0
        binning = self._bin_combo.currentData()

        roi_vals = [self._roi_spins[k].value() for k in ("x0", "x1", "y0", "y1")]
        roi = tuple(roi_vals) if any(v > 0 for v in roi_vals) else None

        self._worker = CameraWorker(
            self._clock,
            self._buf,
            exposure=exposure_s,
            binning=binning,
            roi=roi,
        )
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.error.connect(self._on_error)
        self._worker.status.connect(self._on_status)
        self._worker.finished.connect(self._on_finished)

        self._worker.start()
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("Starting…")

    def _stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._stop_btn.setEnabled(False)

    def _on_frame(self, frame: np.ndarray) -> None:
        if self._view is not None:
            # ImageView expects (row, col); transpose if DCAM gives (col, row)
            self._view.setImage(
                frame,
                autoRange=False,
                autoLevels=False,
                autoHistogramRange=False,
            )

    def _on_error(self, msg: str) -> None:
        self._status_lbl.setText(f"ERROR: {msg}")
        QMessageBox.critical(self, "Camera Error", msg)

    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def _on_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------
    @property
    def ring_buffer(self) -> RingBuffer:
        return self._buf
