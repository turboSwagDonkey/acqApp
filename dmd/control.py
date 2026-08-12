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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QPushButton, QSpinBox, QWidget, QLabel, QFileDialog,
)
from acqApp import style


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    on_time_ms:    float       = 100.0  # illumination on-time per pattern (ms)
    static_hold:   bool        = False  # True = project one image, held (one exposure)
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever (ignored when static_hold)


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
        self._sink: Callable[[int], None] | None = None
        self._open()

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        """Attach (or clear) a per-frame recording sink; receives pattern index."""
        self._sink = sink

    def apply_settings(self, settings: DmdSettings) -> None:
        """Adopt the panel's current settings. Called just before display(), so
        a timing change made after the pattern was loaded still takes effect."""
        self._s = settings

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
        sink = self._sink
        if sink is not None:
            sink(0)
        self.frame_displayed.emit(0)


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
        self._sink: Callable[[int], None] | None = None

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        """Attach (or clear) a per-frame recording sink; receives pattern index."""
        self._sink = sink

    def apply_settings(self, settings: DmdSettings) -> None:
        """Adopt the panel's current settings (see DmdController.apply_settings)."""
        self._s = settings

    def _frame(self, idx: int) -> None:
        """Report one displayed frame: record it (thread-direct, no GUI hop) and
        emit the Qt signal for any GUI listener."""
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

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
        # Static hold: project one image and leave it up endlessly until stop()
        # — no cadence, no cycling thread. On-time doesn't apply.
        if self._s.static_hold:
            self._frame(0)
            print("[DMD mock] static hold — one image held until Stop")
            return

        on_time = max(0.001, self._s.on_time_ms / 1000.0)

        def _loop():
            idx = 0
            while self._running:
                self._frame(idx)
                idx += 1
                time.sleep(on_time)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        print(f"[DMD mock] display started ({self._s.on_time_ms:.0f} ms on-time)")

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
        # Coerce: a path restored from JSON arrives as a str.
        self._pattern_path: Path | None = (
            Path(self._s.pattern_path) if self._s.pattern_path else None)
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

        # Live preview of the selected pattern image.
        self._preview = QLabel("No preview")
        self._preview.setMinimumSize(200, 350)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            "background:#111; color:#777; border:1px solid #333;")
        lay.addRow(self._preview)

        # Illumination on-time: how long each pattern is projected.
        self._spn_on = QDoubleSpinBox()
        self._spn_on.setRange(1.0, 600_000.0)      # 1 ms … 10 min
        self._spn_on.setDecimals(1)
        self._spn_on.setSuffix(" ms")
        self._spn_on.setValue(self._s.on_time_ms)
        lay.addRow("Illumination on-time:", self._spn_on)

        # Static hold: one consistent exposure of a single image (no cycling).
        self._chk_static = QCheckBox("Single static exposure (hold one image)")
        self._chk_static.setChecked(self._s.static_hold)
        self._chk_static.toggled.connect(self._on_static_toggled)
        lay.addRow(self._chk_static)

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
        from PyQt6.QtWidgets import QHBoxLayout
        btn_row = QHBoxLayout(btn_row_w)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_display = QPushButton("Display")
        btn_display.setStyleSheet(style.solid_btn("dmd"))
        btn_display.clicked.connect(self.display_requested)
        btn_stop = QPushButton("Stop")
        btn_stop.clicked.connect(self.stop_requested)
        btn_row.addWidget(btn_display)
        btn_row.addWidget(btn_stop)
        lay.addRow(btn_row_w)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

        self._on_static_toggled(self._chk_static.isChecked())
        self._show_pattern(self._pattern_path)

        for w in (self._spn_on, self._spn_rep):
            w.valueChanged.connect(self._emit)
        self._chk_static.toggled.connect(self._emit)
        self._cmb_trig.currentTextChanged.connect(self._emit)

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    def _on_static_toggled(self, on: bool) -> None:
        # A single held image has no cadence: on-time and repeat count are both
        # meaningless — the pattern just stays up until Stop.
        self._spn_on.setEnabled(not on)
        self._spn_rep.setEnabled(not on)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pattern", "", "Images (*.png *.bmp *.tif)"
        )
        if path:
            p = Path(path)
            self._show_pattern(p)
            self.load_requested.emit(p)
            self._emit()

    def _show_pattern(self, p: Path | None) -> None:
        """Adopt `p` as the selected pattern and reflect it in the panel.

        Also runs at build time for a pattern restored from the config. A file
        that has since been moved or deleted is reported as missing rather than
        left looking loaded — the DMD would otherwise sit dark with the panel
        naming a pattern.
        """
        self._pattern_path = p
        if p is None:
            self._lbl_pattern.setText("No pattern loaded")
            self._preview.setPixmap(QPixmap())
            self._preview.setText("No preview")
        elif p.exists():
            self._lbl_pattern.setText(p.name)
            self._update_preview(p)
        else:
            self._lbl_pattern.setText(f"{p.name} — missing")
            self._preview.setPixmap(QPixmap())
            self._preview.setText("(file not found)")

    def _update_preview(self, path: Path) -> None:
        pix = QPixmap(str(path))
        if pix.isNull():
            self._preview.setText("(cannot preview)")
            return
        self._preview.setPixmap(pix.scaled(
            self._preview.width(), self._preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    @property
    def settings(self) -> DmdSettings:
        return DmdSettings(
            pattern_path=self._pattern_path,
            on_time_ms=self._spn_on.value(),
            static_hold=self._chk_static.isChecked(),
            trigger_mode=self._cmb_trig.currentText(),
            n_repeats=self._spn_rep.value(),
        )
