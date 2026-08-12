"""
DMD (Digital Micromirror Device) controller + settings panel.

DmdController      : the real Vialux ALP-4.2 (1024x768 on this rig), via dmd/alp.py.
MockDmdController  : renders patterns locally, no hardware needed.
SettingsPanel      : QWidget for pattern, geometry, timing and trigger settings.
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal, QRect, QSize
from PyQt6.QtGui import (
    QImage, QPixmap, QShortcut, QKeySequence, QPainter, QPen, QColor
)
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QPushButton, QWidget, QLabel, QFileDialog, QHBoxLayout, QVBoxLayout,
)
from acqApp import style
from acqApp.dmd import alp

# Fallback panel size before (or without) a device: this rig's ALP is XGA.
DEFAULT_W, DEFAULT_H = 1024, 768

FRAME_START = 0
FRAME_STOP = -1


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    on_time_ms:    float       = 100.0  # illumination on-time per pattern (ms)
    # The panel no longer exposes the timing controls and hardcodes this True:
    # the DMD holds one image until Stop. `on_time_ms` / `n_repeats` below are
    # only read on the cycling path, which is now reachable from code (and the
    # tests) but not from the UI — kept because the ALP timing rules it encodes
    # were expensive to establish, not because anything drives it today.
    static_hold:   bool        = True   # True = project one image, held
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever
    # ── geometry: how the pattern lands on the panel ──
    scale_pct:     float       = 100.0  # per cent of the source image's size
    rotation_deg:  float       = 0.0    # clockwise-positive
    offset_x:      float       = 0.0    # device px from the panel centre
    offset_y:      float       = 0.0
    invert:        bool        = False  # swap on/off mirrors
    all_on:        bool        = False  # turn all mirrors on
    fit:           bool        = False  # scale to fit and centre
    lib_dir:       str         = ""     # ALP API location


class DmdController(QObject):
    """The real ALP-4.2 device."""
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._sink: Callable[[int], None] | None = None
        self._pattern: np.ndarray | None = None
        self._running = False
        lib_dir, source = alp.resolve_lib_dir(self._s.lib_dir)
        self._dev = alp.AlpDevice(lib_dir)
        self._dev.open()
        print(f"[DMD] ALP {self._dev.width}x{self._dev.height} (API from {source})")
        if self._s.pattern_path or self._s.all_on:
            self.load_pattern(self._s.pattern_path)

    @property
    def device_name(self) -> str:
        return f"ALP-4.2 {self._dev.width}x{self._dev.height}"

    @property
    def resolution(self) -> tuple[int, int]:
        return self._dev.width, self._dev.height

    @property
    def on_pixels(self) -> int:
        return 0 if self._pattern is None else int((self._pattern > 0).sum())

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        self._sink = sink

    def _frame(self, idx: int) -> None:
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def apply_settings(self, settings: DmdSettings) -> None:
        geometry_changed = (
            (settings.scale_pct, settings.rotation_deg, settings.offset_x,
             settings.offset_y, settings.invert, settings.fit, settings.all_on)
            != (self._s.scale_pct, self._s.rotation_deg, self._s.offset_x,
                self._s.offset_y, self._s.invert, self._s.fit, self._s.all_on))
        self._s = settings
        if geometry_changed and (settings.pattern_path or settings.all_on):
            self.load_pattern(settings.pattern_path)

    def load_pattern(self, path: Path | None = None) -> None:
        w, h = self.resolution

        if self._s.all_on:
            self._pattern = np.full((h, w), 255, dtype=np.uint8)
            print(f"[DMD] all mirrors ON -> {w}x{h}, {self.on_pixels} mirrors on")
            return

        p = Path(path or self._s.pattern_path or "")
        if not p.is_file():
            print(f"[DMD] load_pattern: no such file ({p})")
            self._pattern = None
            return

        self._pattern = alp.build_frame(
            p, w, h, scale_pct=self._s.scale_pct,
            rotation_deg=self._s.rotation_deg, offset_x=self._s.offset_x,
            offset_y=self._s.offset_y, invert=self._s.invert, fit=self._s.fit)
        print(f"[DMD] {p.name} -> {w}x{h}, {self.on_pixels} mirrors on")

    def display(self) -> None:
        if self._pattern is None:
            print("[DMD] display: no pattern loaded — nothing to project")
            return
        if self.on_pixels == 0:
            # Every mirror off is a legal frame and a projector showing nothing.
            # It is also what a bad scale/offset produces, so say it out loud.
            print("[DMD] display: the frame is entirely dark — check scale, "
                  "offset and invert")

        if self._s.static_hold:
            illum, loop, repeats = None, True, 0
        else:
            illum = int(round(self._s.on_time_ms * 1000.0))
            if illum > alp.MAX_PICTURE_US:
                print(f"[DMD] on-time {self._s.on_time_ms:g} ms exceeds the "
                      f"ALP's {alp.MAX_PICTURE_US / 1000:g} ms limit — "
                      f"clamped. Use static hold for a longer exposure.")
                illum = alp.MAX_PICTURE_US
            repeats = max(0, int(self._s.n_repeats))
            loop = repeats == 0

        self._dev.project(self._pattern, illumination_us=illum,
                          loop=loop, repeats=repeats)
        self._running = True
        self.pattern_started.emit()
        self._frame(FRAME_START)
        if self._s.static_hold:
            how = "static hold"
        else:
            how = (f"{illum / 1000:g} ms on-time, "
                   + ("looping" if loop else f"{repeats} repeats"))
        print(f"[DMD] projecting — {how}, {self.on_pixels} mirrors on")

    def stop(self) -> None:
        if not self._running:
            return
        self._dev.halt()
        self._running = False
        self._frame(FRAME_STOP)
        self.pattern_stopped.emit()

    def close(self) -> None:
        self.stop()
        self._dev.close()

    def software_trigger(self) -> None:
        self._frame(FRAME_START)


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

    device_name = "mock (no DMD attached)"

    @property
    def resolution(self) -> tuple[int, int]:
        return DEFAULT_W, DEFAULT_H

    @property
    def on_pixels(self) -> int:
        return 0 if self._pattern is None else int((self._pattern > 0).sum())

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        self._sink = sink

    def apply_settings(self, settings: DmdSettings) -> None:
        reload = ((settings.pattern_path or settings.all_on) and settings != self._s)
        self._s = settings
        if reload:
            self.load_pattern(settings.pattern_path)

    def _frame(self, idx: int) -> None:
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def load_pattern(self, path: Path | None = None) -> None:
        if self._s.all_on:
            self._pattern = np.full((DEFAULT_H, DEFAULT_W), 255, dtype=np.uint8)
            print(f"[DMD mock] all mirrors ON -> {DEFAULT_W}x{DEFAULT_H}, "
                  f"{self.on_pixels} mirrors on")
            return

        p = Path(path or self._s.pattern_path or "")
        if p.is_file():
            try:
                self._pattern = alp.build_frame(
                    p, DEFAULT_W, DEFAULT_H, scale_pct=self._s.scale_pct,
                    rotation_deg=self._s.rotation_deg,
                    offset_x=self._s.offset_x, offset_y=self._s.offset_y,
                    invert=self._s.invert, fit=self._s.fit)
                return
            except Exception as e:
                print(f"[DMD mock] could not render {p.name}: {e}")

        # Fallback checkerboard
        tile = np.kron([[0, 255] * 8, [255, 0] * 8] * 8,
                       np.ones((4, 4), dtype=np.uint8)).astype(np.uint8)
        reps = (DEFAULT_H // tile.shape[0] + 1, DEFAULT_W // tile.shape[1] + 1)
        self._pattern = np.tile(tile, reps)[:DEFAULT_H, :DEFAULT_W]

    def display(self) -> None:
        if self._running:
            return
        self._running = True
        self.pattern_started.emit()
        self._frame(FRAME_START)
        if self._s.static_hold:
            return

        on_time = max(0.001, self._s.on_time_ms / 1000.0)

        def _loop():
            idx = 1
            while self._running:
                time.sleep(on_time)
                if self._running:
                    self._frame(idx)
                    idx += 1

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        was_running = self._running
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if was_running:
            self._frame(FRAME_STOP)
        self.pattern_stopped.emit()

    def close(self) -> None:
        self.stop()

    def software_trigger(self) -> None:
        self._frame(FRAME_START)


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits DmdSettings
    load_requested   = pyqtSignal(object)   # emits Path
    display_requested = pyqtSignal()
    stop_requested    = pyqtSignal()

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._res: tuple[int, int] = (DEFAULT_W, DEFAULT_H)
        self._pattern_path: Path | None = (
            Path(self._s.pattern_path) if self._s.pattern_path else None)
        self._shortcuts: list[QShortcut] = []
        self._build()
        self._init_shortcuts()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_preview()

    def set_device(self, name: str, resolution: tuple[int, int], real: bool) -> None:
        self._res = resolution
        w, h = resolution
        res_str = f"{w}x{h}"
        dev_label = name if res_str in name else f"{name} · {res_str}"
        dev_label += "" if real else " · nothing will be projected"
        
        self._lbl_dev.setText(dev_label)
        self._lbl_dev.setStyleSheet(
            f"color:{style.HEX['dmd'] if real else '#c86'};")
        self._update_preview()

    def _build(self) -> None:
        grp = QGroupBox("DMD settings")
        lay = QFormLayout(grp)
        lay.setSpacing(6)

        self._lbl_dev = QLabel("no device yet")
        self._lbl_dev.setWordWrap(True)
        lay.addRow("Device:", self._lbl_dev)

        # ── Inline Pattern + Browse Row ──────────────────────────────────────
        pat_w = QWidget()
        pat_lay = QHBoxLayout(pat_w)
        pat_lay.setContentsMargins(0, 0, 0, 0)
        pat_lay.setSpacing(6)

        self._lbl_pattern = QLabel("No pattern loaded")
        self._lbl_pattern.setWordWrap(True)
        self._btn_browse = QPushButton("Browse…")
        self._btn_browse.setFixedWidth(90)
        self._btn_browse.clicked.connect(self._browse)

        pat_lay.addWidget(self._lbl_pattern, 1)
        pat_lay.addWidget(self._btn_browse)
        lay.addRow("Pattern:", pat_w)

        # ── All ON toggle ────────────────────────────────────────────────────
        self._chk_all_on = QCheckBox("All ON (turn all mirrors on)")
        self._chk_all_on.setChecked(self._s.all_on)
        self._chk_all_on.toggled.connect(self._on_all_on_toggled)
        lay.addRow(self._chk_all_on)

        # ── Live preview box ─────────────────────────────────────────────────
        self._preview = QLabel("No preview")
        self._preview.setMinimumSize(200, 180)
        self._preview.setMaximumHeight(240)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            "color:#777; border:1px solid #333; border-radius:3px;")
        lay.addRow(self._preview)

        # ── Pattern Alignment (Geometry Card) ────────────────────────────────
        geom_grp = QGroupBox("Pattern Alignment")
        geom_lay = QGridLayout(geom_grp)
        geom_lay.setSpacing(6)
        
        geom_lay.setColumnStretch(1, 1)
        geom_lay.setColumnStretch(3, 1)

        # Fit Checkbox
        self._chk_fit = QCheckBox("Fit to panel (ignore scale/rotation/offset)")
        self._chk_fit.setChecked(self._s.fit)
        self._chk_fit.toggled.connect(self._on_fit_toggled)
        geom_lay.addWidget(self._chk_fit, 0, 0, 1, 4)

        # Row 1: Scale % | Rotation °
        geom_lay.addWidget(QLabel("Scale:"), 1, 0)
        self._spn_scale = QDoubleSpinBox()
        self._spn_scale.setRange(1.0, 1000.0)
        self._spn_scale.setDecimals(1)
        self._spn_scale.setSingleStep(1.0)
        self._spn_scale.setSuffix(" %")
        self._spn_scale.setValue(self._s.scale_pct)
        geom_lay.addWidget(self._spn_scale, 1, 1)

        geom_lay.addWidget(QLabel("Rot:"), 1, 2)
        self._spn_rot = QDoubleSpinBox()
        self._spn_rot.setRange(-360.0, 360.0)
        self._spn_rot.setDecimals(1)
        self._spn_rot.setSingleStep(0.5)
        self._spn_rot.setSuffix(" °")
        self._spn_rot.setValue(self._s.rotation_deg)
        self._spn_rot.setToolTip("Clockwise-positive rotation.")
        geom_lay.addWidget(self._spn_rot, 1, 3)

        # Row 2: Offset X | Offset Y
        geom_lay.addWidget(QLabel("Offset X:"), 2, 0)
        self._spn_dx = QDoubleSpinBox()
        self._spn_dx.setRange(-4000.0, 4000.0)
        self._spn_dx.setDecimals(0)
        self._spn_dx.setSingleStep(1.0)
        self._spn_dx.setSuffix(" px")
        self._spn_dx.setValue(self._s.offset_x)
        geom_lay.addWidget(self._spn_dx, 2, 1)

        geom_lay.addWidget(QLabel("Offset Y:"), 2, 2)
        self._spn_dy = QDoubleSpinBox()
        self._spn_dy.setRange(-4000.0, 4000.0)
        self._spn_dy.setDecimals(0)
        self._spn_dy.setSingleStep(1.0)
        self._spn_dy.setSuffix(" px")
        self._spn_dy.setValue(self._s.offset_y)
        self._spn_dy.setToolTip("Offset from center in device pixels.")
        geom_lay.addWidget(self._spn_dy, 2, 3)

        # Row 3: Invert Checkbox | Reset Button
        self._chk_invert = QCheckBox("Invert mirrors")
        self._chk_invert.setChecked(self._s.invert)
        geom_lay.addWidget(self._chk_invert, 3, 0, 1, 2)

        btn_reset_geom = QPushButton("Reset Alignment")
        btn_reset_geom.setToolTip("Reset scale to 100%, rotation to 0°, and offsets to (0, 0)")
        btn_reset_geom.clicked.connect(self._reset_geometry)
        geom_lay.addWidget(btn_reset_geom, 3, 2, 1, 2)

        # Row 4: Enable Keyboard Nudging Toggle
        self._chk_nudge = QCheckBox("Enable keyboard nudging")
        self._chk_nudge.setToolTip(
            "Nudge alignment with keys:\n"
            " • Arrows : Offset X/Y (1 px)\n"
            " • + / -  : Scale (1 %)\n"
            " • [ / ]  : Rotation (0.5 °)\n"
            "Hold Shift for 10x larger steps."
        )
        self._chk_nudge.toggled.connect(self._on_nudge_toggled)
        geom_lay.addWidget(self._chk_nudge, 4, 0, 1, 4)

        lay.addRow(geom_grp)

        # Trigger settings
        self._cmb_trig = QComboBox()
        self._cmb_trig.addItems(["Internal", "External", "Software"])
        self._cmb_trig.setCurrentText(self._s.trigger_mode)
        lay.addRow("Trigger:", self._cmb_trig)

        # Display / Stop buttons
        btn_row_w = QWidget()
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

        # Root layout uses QVBoxLayout with stretch at bottom to prevent empty gaps
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(grp)
        root.addStretch(1)

        self._on_all_on_toggled(self._chk_all_on.isChecked())
        self._update_preview()

        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy):
            w.valueChanged.connect(self._emit)
        for c in (self._chk_fit, self._chk_invert, self._chk_all_on):
            c.toggled.connect(self._emit)
        self._cmb_trig.currentTextChanged.connect(self._emit)

    def _init_shortcuts(self) -> None:
        """Configures keyboard shortcuts for alignment nudging."""
        def add_sc(key_seq: str, callback: Callable[[], None]) -> None:
            sc = QShortcut(QKeySequence(key_seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(callback)
            sc.setEnabled(False)
            self._shortcuts.append(sc)

        # Offsets
        add_sc("Left", lambda: self._nudge_spn(self._spn_dx, -1))
        add_sc("Right", lambda: self._nudge_spn(self._spn_dx, 1))
        add_sc("Up", lambda: self._nudge_spn(self._spn_dy, -1))
        add_sc("Down", lambda: self._nudge_spn(self._spn_dy, 1))

        add_sc("Shift+Left", lambda: self._nudge_spn(self._spn_dx, -10))
        add_sc("Shift+Right", lambda: self._nudge_spn(self._spn_dx, 10))
        add_sc("Shift+Up", lambda: self._nudge_spn(self._spn_dy, -10))
        add_sc("Shift+Down", lambda: self._nudge_spn(self._spn_dy, 10))

        # Scale
        add_sc("+", lambda: self._nudge_spn(self._spn_scale, 1))
        add_sc("=", lambda: self._nudge_spn(self._spn_scale, 1))
        add_sc("-", lambda: self._nudge_spn(self._spn_scale, -1))
        add_sc("Shift++", lambda: self._nudge_spn(self._spn_scale, 10))
        add_sc("Shift+=", lambda: self._nudge_spn(self._spn_scale, 10))
        add_sc("Shift+-", lambda: self._nudge_spn(self._spn_scale, -10))
        add_sc("Shift+_", lambda: self._nudge_spn(self._spn_scale, -10))

        # Rotation
        add_sc("[", lambda: self._nudge_spn(self._spn_rot, -0.5))
        add_sc("]", lambda: self._nudge_spn(self._spn_rot, 0.5))
        add_sc("Shift+[", lambda: self._nudge_spn(self._spn_rot, -5.0))
        add_sc("Shift+]", lambda: self._nudge_spn(self._spn_rot, 5.0))
        add_sc("{", lambda: self._nudge_spn(self._spn_rot, -5.0))
        add_sc("}", lambda: self._nudge_spn(self._spn_rot, 5.0))

    def _on_nudge_toggled(self, enabled: bool) -> None:
        for sc in self._shortcuts:
            sc.setEnabled(enabled)

    def _nudge_spn(self, spn: QDoubleSpinBox, delta: float) -> None:
        if not spn.isEnabled():
            return
        spn.setValue(spn.value() + delta)

    def _reset_geometry(self) -> None:
        self._spn_scale.setValue(100.0)
        self._spn_rot.setValue(0.0)
        self._spn_dx.setValue(0.0)
        self._spn_dy.setValue(0.0)
        self._chk_invert.setChecked(False)
        self._chk_fit.setChecked(False)

    def _emit(self, *_a) -> None:
        self._update_preview()
        self.settings_changed.emit(self.settings)

    def _on_all_on_toggled(self, on: bool) -> None:
        self._btn_browse.setEnabled(not on)
        self._chk_fit.setEnabled(not on)
        self._chk_invert.setEnabled(not on)

        fit_active = self._chk_fit.isChecked()
        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy):
            w.setEnabled(not on and not fit_active)

        self._update_preview()

    def _on_fit_toggled(self, on: bool) -> None:
        if self._chk_all_on.isChecked():
            return
        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy):
            w.setEnabled(not on)
        self._update_preview()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pattern", "", "Images (*.png *.bmp *.tif)"
        )
        if path:
            p = Path(path)
            self._pattern_path = p
            self.load_requested.emit(p)
            self._emit()

    def _update_preview(self) -> None:
        """Renders the pattern array with a padded thatched magenta border around DMD bounds."""
        pw = self._preview.width()
        ph = self._preview.height()
        if pw <= 1 or ph <= 1:
            return

        w, h = self._res
        p = self._pattern_path

        # Determine frame buffer
        if self._chk_all_on.isChecked():
            self._lbl_pattern.setText("All mirrors ON (Full Illumination)")
            frame = np.full((h, w), 255, dtype=np.uint8)
        elif p is None:
            self._lbl_pattern.setText("No pattern loaded")
            frame = np.zeros((h, w), dtype=np.uint8)
        elif not p.exists():
            self._lbl_pattern.setText(f"{p.name} — missing")
            frame = np.zeros((h, w), dtype=np.uint8)
        else:
            self._lbl_pattern.setText(p.name)
            try:
                s = self.settings
                frame = alp.build_frame(
                    p, w, h,
                    scale_pct=s.scale_pct,
                    rotation_deg=s.rotation_deg,
                    offset_x=s.offset_x,
                    offset_y=s.offset_y,
                    invert=s.invert,
                    fit=s.fit
                )
            except Exception as e:
                self._preview.setText(f"(render error: {e})")
                return

        frame = np.ascontiguousarray(frame)
        qimg = QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8)
        dmd_pixmap = QPixmap.fromImage(qimg)

        # 2px padding prevents painter stroke clipping on edges
        pad = 2
        avail_w = max(1, pw - 2 * pad)
        avail_h = max(1, ph - 2 * pad)

        target_size = QSize(w, h).scaled(avail_w, avail_h, Qt.AspectRatioMode.KeepAspectRatio)
        scaled_dmd = dmd_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

        # Base preview canvas with transparent background so the tab gray shows through
        canvas = QPixmap(pw, ph)
        canvas.fill(QColor(0, 0, 0, 0))

        painter = QPainter(canvas)

        # Center target rect within preview area
        x = (pw - target_size.width()) // 2
        y = (ph - target_size.height()) // 2
        dmd_rect = QRect(x, y, target_size.width(), target_size.height())

        # 1. Draw DMD pattern frame (only this active region is opaque/black/white)
        painter.drawPixmap(dmd_rect, scaled_dmd)

        # 2. Draw thatched (dashed) magenta outline around active DMD area
        magenta_color = QColor(style.HEX.get("dmd", "#e040fb"))
        pen = QPen(magenta_color, 1.5, Qt.PenStyle.DashLine)
        pen.setDashPattern([4, 4])  # 4px dash, 4px space
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(dmd_rect.adjusted(0, 0, -1, -1))

        painter.end()

        self._preview.setPixmap(canvas)

    @property
    def settings(self) -> DmdSettings:
        return DmdSettings(
            pattern_path=self._pattern_path,
            on_time_ms=self._s.on_time_ms,
            static_hold=True,
            trigger_mode=self._cmb_trig.currentText(),
            n_repeats=self._s.n_repeats,
            scale_pct=self._spn_scale.value(),
            rotation_deg=self._spn_rot.value(),
            offset_x=self._spn_dx.value(),
            offset_y=self._spn_dy.value(),
            invert=self._chk_invert.isChecked(),
            all_on=self._chk_all_on.isChecked(),
            fit=self._chk_fit.isChecked(),
            lib_dir=self._s.lib_dir,
        )