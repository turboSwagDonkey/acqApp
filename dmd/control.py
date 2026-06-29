"""
DMD (Digital Micromirror Device) controller + settings panel.

DmdController      : sends patterns to the DMD via the vendor SDK.
MockDmdController  : renders patterns locally, no hardware needed.
SettingsPanel      : QWidget for pattern, timing, and trigger settings.

TODO: replace the SDK stub with the actual vendor library.
      Common options:
        - Vialux ALP:  pyalp  /  ALP4lib
        - Texas Instruments LightCrafter: dlpc SDK via ctypes
        - Mightex:  MightexPolygon SDK
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QPushButton, QSpinBox, QWidget, QLabel, QFileDialog,
)
from acqApp import style


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    frame_rate:    float       = 30.0   # Hz
    exposure_us:   int         = 1000   # µs per frame
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever


class DmdController(QObject):
    """
    Stub for a real DMD vendor SDK.
    Replace _open / display / stop with actual SDK calls.
    """
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)   # pattern index

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s      = settings or DmdSettings()
        self._handle = None
        self._open()

    def _open(self) -> None:
        # TODO: initialise vendor SDK and store handle
        print("[DMD] open() — stub, no hardware initialised")

    def load_pattern(self, path: Path | None = None) -> None:
        """Upload a pattern image to the DMD."""
        p = path or self._s.pattern_path
        if p is None or not p.exists():
            print("[DMD] load_pattern: no valid path")
            return
        # TODO: vendor SDK upload call
        print(f"[DMD] load_pattern({p.name}) — stub")

    def display(self) -> None:
        """Start displaying the loaded pattern sequence."""
        # TODO: vendor SDK start call
        print("[DMD] display() — stub")
        self.pattern_started.emit()

    def stop(self) -> None:
        """Stop display."""
        # TODO: vendor SDK stop call
        print("[DMD] stop() — stub")
        self.pattern_stopped.emit()

    def close(self) -> None:
        self.stop()
        # TODO: vendor SDK close/uninit
        print("[DMD] close() — stub")

    def software_trigger(self) -> None:
        """Advance one frame when trigger_mode == 'Software'."""
        # TODO: vendor SDK trigger call
        pass


class MockDmdController(QObject):
    """Renders patterns in memory and logs events — no hardware."""
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s       = settings or DmdSettings()
        self._pattern: np.ndarray | None = None
        self._running = False
        self._thread:  threading.Thread | None = None

    def load_pattern(self, path: Path | None = None) -> None:
        p = path or self._s.pattern_path
        if p and p.exists():
            try:
                from PIL import Image
                self._pattern = np.array(Image.open(p).convert("L"))
                print(f"[DMD mock] loaded {p.name}  shape={self._pattern.shape}")
                return
            except Exception:
                pass
        # Fallback: checkerboard
        self._pattern = np.kron(
            [[0, 255] * 8, [255, 0] * 8] * 8,
            np.ones((4, 4), dtype=np.uint8),
        )
        print("[DMD mock] using fallback checkerboard pattern")

    def display(self) -> None:
        if self._running:
            return
        self._running = True
        self.pattern_started.emit()
        period = 1.0 / self._s.frame_rate

        def _loop():
            idx = 0
            while self._running:
                self.frame_displayed.emit(idx)
                idx += 1
                time.sleep(period)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        print("[DMD mock] display started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.pattern_stopped.emit()
        print("[DMD mock] display stopped")

    def close(self) -> None:
        self.stop()

    def software_trigger(self) -> None:
        self.frame_displayed.emit(0)


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits DmdSettings
    load_requested   = pyqtSignal(object)   # emits Path
    display_requested = pyqtSignal()
    stop_requested    = pyqtSignal()

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._build()

    def _build(self) -> None:
        grp = QGroupBox("DMD settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._lbl_pattern = QLabel("No pattern loaded")
        self._lbl_pattern.setWordWrap(True)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        lay.addRow("Pattern:", self._lbl_pattern)
        lay.addRow(btn_browse)

        self._spn_fps = QDoubleSpinBox()
        self._spn_fps.setRange(1.0, 500.0)
        self._spn_fps.setSuffix(" Hz")
        self._spn_fps.setValue(self._s.frame_rate)
        lay.addRow("Frame rate:", self._spn_fps)

        self._spn_exp = QSpinBox()
        self._spn_exp.setRange(100, 100_000)
        self._spn_exp.setSuffix(" µs")
        self._spn_exp.setValue(self._s.exposure_us)
        lay.addRow("Exposure:", self._spn_exp)

        self._cmb_trig = QComboBox()
        self._cmb_trig.addItems(["Internal", "External", "Software"])
        self._cmb_trig.setCurrentText(self._s.trigger_mode)
        lay.addRow("Trigger:", self._cmb_trig)

        self._spn_rep = QSpinBox()
        self._spn_rep.setRange(0, 9999)
        self._spn_rep.setSpecialValueText("∞  loop")
        self._spn_rep.setValue(self._s.n_repeats)
        lay.addRow("Repeats:", self._spn_rep)

        btn_row_w = QWidget()
        from PyQt5.QtWidgets import QHBoxLayout
        btn_row = QHBoxLayout(btn_row_w)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_display = QPushButton("Display")
        btn_display.setStyleSheet(style.solid_btn("sync"))
        btn_display.clicked.connect(self.display_requested)
        btn_stop = QPushButton("Stop")
        btn_stop.clicked.connect(self.stop_requested)
        btn_row.addWidget(btn_display)
        btn_row.addWidget(btn_stop)
        lay.addRow(btn_row_w)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pattern", "", "Images (*.png *.bmp *.tif)"
        )
        if path:
            p = Path(path)
            self._lbl_pattern.setText(p.name)
            self.load_requested.emit(p)

    @property
    def settings(self) -> DmdSettings:
        return DmdSettings(
            frame_rate=self._spn_fps.value(),
            exposure_us=self._spn_exp.value(),
            trigger_mode=self._cmb_trig.currentText(),
            n_repeats=self._spn_rep.value(),
        )
