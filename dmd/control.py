"""
DMD (Digital Micromirror Device) controller + settings panel.

DmdController      : the real Vialux ALP-4.2 (1024x768 on this rig), via dmd/alp.py.
MockDmdController  : renders patterns locally, no hardware needed.
SettingsPanel      : QWidget for pattern, geometry, timing and trigger settings.

The geometry (scale / rotation / offset) is here rather than fixed at
"fit and centre" because it is how the projected pattern is aligned to the
optics — the standalone `dmdGUI_project` app is where that alignment is
normally found, and its saved scale/rotation are used as this panel's defaults
so acqApp starts from the same place instead of a different one.
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
from acqApp.dmd import alp

# Fallback panel size before (or without) a device: this rig's ALP is XGA.
DEFAULT_W, DEFAULT_H = 1024, 768

# Logged into /dmd at the boundaries of a projection. The device runs its own
# sequence clock once started, so those two instants are what we actually
# observe — see MockDmdController for the difference.
FRAME_START = 0
FRAME_STOP = -1


@dataclass
class DmdSettings:
    pattern_path:  Path | None = None   # .png / .bmp to upload
    on_time_ms:    float       = 100.0  # illumination on-time per pattern (ms)
    static_hold:   bool        = False  # True = project one image, held (one exposure)
    trigger_mode:  str         = "Internal"   # Internal | External | Software
    n_repeats:     int         = 0      # 0 = loop forever (ignored when static_hold)
    # ── geometry: how the pattern lands on the panel ──
    scale_pct:     float       = 100.0  # per cent of the source image's size
    rotation_deg:  float       = 0.0    # clockwise-positive, like the standalone GUI
    offset_x:      float       = 0.0    # device px from the panel centre
    offset_y:      float       = 0.0
    invert:        bool        = False  # swap on/off mirrors
    fit:           bool        = False  # scale to fit and centre (overrides the above)
    lib_dir:       str         = ""     # ALP API location; "" = look it up (alp.py)


class DmdController(QObject):
    """The real ALP-4.2 device.

    Opening raises if the ALP is already held by another process — the
    standalone `dmdGUI_project` app, most often — and the caller
    (`modules.DmdModule`) turns that into a mock controller plus a panel that
    says so. Silently doing nothing is what this class used to do, and it is
    the failure the operator cannot see from behind the rig.

    The device runs its own sequence clock once started, so what reaches the
    `/dmd` stream is the two instants we actually command: FRAME_START when
    Display is pressed and FRAME_STOP when it stops. On-time and repeats are in
    the metadata, so a cycling sequence is reconstructable between them.
    """
    pattern_started = pyqtSignal()
    pattern_stopped = pyqtSignal()
    frame_displayed = pyqtSignal(int)   # pattern index

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._sink: Callable[[int], None] | None = None
        self._pattern: np.ndarray | None = None     # the built frame, ready to send
        self._running = False
        lib_dir, source = alp.resolve_lib_dir(self._s.lib_dir)
        self._dev = alp.AlpDevice(lib_dir)
        self._dev.open()                # raises → the caller falls back to mock
        print(f"[DMD] ALP {self._dev.width}x{self._dev.height} "
              f"(API from {source})")
        if self._s.pattern_path:
            self.load_pattern(self._s.pattern_path)

    # ── description, for the panel and the session file ──
    @property
    def device_name(self) -> str:
        return f"ALP-4.2 {self._dev.width}x{self._dev.height}"

    @property
    def resolution(self) -> tuple[int, int]:
        return self._dev.width, self._dev.height

    @property
    def on_pixels(self) -> int:
        """Mirrors the current pattern would switch on. 0 means a dark panel —
        the one thing you cannot tell from a Display button that worked."""
        return 0 if self._pattern is None else int((self._pattern > 0).sum())

    def set_sink(self, sink: Callable[[int], None] | None) -> None:
        """Attach (or clear) a per-frame recording sink; receives pattern index."""
        self._sink = sink

    def _frame(self, idx: int) -> None:
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def apply_settings(self, settings: DmdSettings) -> None:
        """Adopt the panel's current settings. Called just before display(), so
        a timing or geometry change made after the pattern was loaded still
        takes effect — the frame is rebuilt from the source image."""
        geometry_changed = (
            (settings.scale_pct, settings.rotation_deg, settings.offset_x,
             settings.offset_y, settings.invert, settings.fit)
            != (self._s.scale_pct, self._s.rotation_deg, self._s.offset_x,
                self._s.offset_y, self._s.invert, self._s.fit))
        self._s = settings
        if geometry_changed and settings.pattern_path:
            self.load_pattern(settings.pattern_path)

    def load_pattern(self, path: Path | None = None) -> None:
        """Render a pattern image into the frame this device will project."""
        p = Path(path or self._s.pattern_path or "")
        if not p.is_file():
            print(f"[DMD] load_pattern: no such file ({p})")
            self._pattern = None
            return
        w, h = self.resolution
        self._pattern = alp.build_frame(
            p, w, h, scale_pct=self._s.scale_pct,
            rotation_deg=self._s.rotation_deg, offset_x=self._s.offset_x,
            offset_y=self._s.offset_y, invert=self._s.invert, fit=self._s.fit)
        print(f"[DMD] {p.name} -> {w}x{h}, {self.on_pixels} mirrors on")

    def display(self) -> None:
        """Project the loaded pattern."""
        if self._pattern is None:
            print("[DMD] display: no pattern loaded — nothing to project")
            return
        if self.on_pixels == 0:
            # Every mirror off is a legal frame and a projector showing nothing.
            # It is also what a bad scale/offset produces, so say it out loud.
            print("[DMD] display: the frame is entirely dark — check scale, "
                  "offset and invert")

        # Static hold leaves the timing at the device default and loops a
        # one-picture sequence: with ALP_BIN_UNINTERRUPTED that is a steady
        # image, not a flicker. Otherwise the on-time is the picture time.
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
        """Stop display."""
        if not self._running:
            return
        self._dev.halt()
        self._running = False
        self._frame(FRAME_STOP)
        self.pattern_stopped.emit()
        print("[DMD] display stopped")

    def close(self) -> None:
        self.stop()
        self._dev.close()

    def software_trigger(self) -> None:
        """Advance one frame when trigger_mode == 'Software'.

        Not wired to the ALP's trigger input yet: with a one-picture sequence
        there is nothing to advance to. Left as the event hook so the log still
        records that a trigger was issued.
        """
        self._frame(FRAME_START)


class MockDmdController(QObject):
    """Renders patterns in memory and logs events — no hardware.

    Runs the same frame builder as the real device at the same panel size, so
    "what would be projected" is answerable with nothing plugged in, and a
    geometry mistake shows up in Emulate mode rather than on the rig.

    It logs FRAME_START / FRAME_STOP like the real controller **and** a tick per
    on-time in between. Those ticks are honest here — this class really does
    emit them — but the ALP free-runs its own sequence once started, so the real
    stream carries only the two boundaries.
    """
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
        """Attach (or clear) a per-frame recording sink; receives pattern index."""
        self._sink = sink

    def apply_settings(self, settings: DmdSettings) -> None:
        """Adopt the panel's current settings (see DmdController.apply_settings)."""
        reload = (settings.pattern_path and settings != self._s)
        self._s = settings
        if reload:
            self.load_pattern(settings.pattern_path)

    def _frame(self, idx: int) -> None:
        """Report one displayed frame: record it (thread-direct, no GUI hop) and
        emit the Qt signal for any GUI listener."""
        sink = self._sink
        if sink is not None:
            sink(idx)
        self.frame_displayed.emit(idx)

    def load_pattern(self, path: Path | None = None) -> None:
        p = Path(path or self._s.pattern_path or "")
        if p.is_file():
            try:
                self._pattern = alp.build_frame(
                    p, DEFAULT_W, DEFAULT_H, scale_pct=self._s.scale_pct,
                    rotation_deg=self._s.rotation_deg,
                    offset_x=self._s.offset_x, offset_y=self._s.offset_y,
                    invert=self._s.invert, fit=self._s.fit)
                print(f"[DMD mock] {p.name} -> {DEFAULT_W}x{DEFAULT_H}, "
                      f"{self.on_pixels} mirrors on")
                return
            except Exception as e:                    # noqa: BLE001
                print(f"[DMD mock] could not render {p.name}: {e}")
        # Fallback: checkerboard, at the panel size the real device would use.
        tile = np.kron([[0, 255] * 8, [255, 0] * 8] * 8,
                       np.ones((4, 4), dtype=np.uint8)).astype(np.uint8)
        reps = (DEFAULT_H // tile.shape[0] + 1, DEFAULT_W // tile.shape[1] + 1)
        self._pattern = np.tile(tile, reps)[:DEFAULT_H, :DEFAULT_W]
        print("[DMD mock] using fallback checkerboard pattern")

    def display(self) -> None:
        if self._running:
            return
        self._running = True
        self.pattern_started.emit()
        self._frame(FRAME_START)
        # Static hold: project one image and leave it up endlessly until stop()
        # — no cadence, no cycling thread. On-time doesn't apply.
        if self._s.static_hold:
            print("[DMD mock] static hold — one image held until Stop")
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
        print(f"[DMD mock] display started ({self._s.on_time_ms:.0f} ms on-time)")

    def stop(self) -> None:
        was_running = self._running
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if was_running:
            self._frame(FRAME_STOP)
        self.pattern_stopped.emit()
        print("[DMD mock] display stopped")

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
        # Coerce: a path restored from JSON arrives as a str.
        self._pattern_path: Path | None = (
            Path(self._s.pattern_path) if self._s.pattern_path else None)
        self._build()

    def set_device(self, name: str, resolution: tuple[int, int],
                   real: bool) -> None:
        """Say which DMD (if any) is attached.

        The panel used to look identical whether or not there was hardware
        behind it, and with none, Display did nothing and said nothing — the
        one failure an operator cannot see from behind the rig.
        """
        w, h = resolution
        self._lbl_dev.setText(f"{name} · {w}x{h}"
                              + ("" if real else " · nothing will be projected"))
        self._lbl_dev.setStyleSheet(
            f"color:{style.HEX['dmd'] if real else '#c86'};")

    def _build(self) -> None:
        grp = QGroupBox("DMD settings")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._lbl_dev = QLabel("no device yet")
        self._lbl_dev.setWordWrap(True)
        lay.addRow("Device:", self._lbl_dev)

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

        # ── Geometry: where the pattern lands on the panel ───────────────────
        # This is the alignment to the optics. Defaults come from the standalone
        # dmdGUI_project's saved config when acqApp has none of its own, so the
        # first projection here matches the one the optics were aligned with
        # rather than starting from an arbitrary 100 %.
        self._chk_fit = QCheckBox("Fit to panel (ignore scale/rotation/offset)")
        self._chk_fit.setChecked(self._s.fit)
        self._chk_fit.toggled.connect(self._on_fit_toggled)
        lay.addRow(self._chk_fit)

        self._spn_scale = QDoubleSpinBox()
        self._spn_scale.setRange(1.0, 1000.0)
        self._spn_scale.setDecimals(1)
        self._spn_scale.setSuffix(" %")
        self._spn_scale.setValue(self._s.scale_pct)
        lay.addRow("Scale:", self._spn_scale)

        self._spn_rot = QDoubleSpinBox()
        self._spn_rot.setRange(-360.0, 360.0)
        self._spn_rot.setDecimals(1)
        self._spn_rot.setSuffix(" °")
        self._spn_rot.setValue(self._s.rotation_deg)
        self._spn_rot.setToolTip("Clockwise-positive, matching the standalone "
                                 "DMD app's rotation dial.")
        lay.addRow("Rotation:", self._spn_rot)

        self._spn_dx = QDoubleSpinBox()
        self._spn_dx.setRange(-4000.0, 4000.0)
        self._spn_dx.setDecimals(0)
        self._spn_dx.setSuffix(" px")
        self._spn_dx.setValue(self._s.offset_x)
        lay.addRow("Offset X:", self._spn_dx)

        self._spn_dy = QDoubleSpinBox()
        self._spn_dy.setRange(-4000.0, 4000.0)
        self._spn_dy.setDecimals(0)
        self._spn_dy.setSuffix(" px")
        self._spn_dy.setValue(self._s.offset_y)
        self._spn_dy.setToolTip("Offset of the pattern's centre from the "
                                "panel's centre, in device pixels.")
        lay.addRow("Offset Y:", self._spn_dy)

        self._chk_invert = QCheckBox("Invert (swap on/off mirrors)")
        self._chk_invert.setChecked(self._s.invert)
        lay.addRow(self._chk_invert)

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
        self._on_fit_toggled(self._chk_fit.isChecked())
        self._show_pattern(self._pattern_path)

        for w in (self._spn_on, self._spn_rep, self._spn_scale, self._spn_rot,
                  self._spn_dx, self._spn_dy):
            w.valueChanged.connect(self._emit)
        for c in (self._chk_static, self._chk_fit, self._chk_invert):
            c.toggled.connect(self._emit)
        self._cmb_trig.currentTextChanged.connect(self._emit)

    def _emit(self, *_a) -> None:
        self.settings_changed.emit(self.settings)

    def _on_fit_toggled(self, on: bool) -> None:
        # Fit computes scale and centres the pattern, so the manual controls
        # would be showing values that are not the ones being used.
        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy):
            w.setEnabled(not on)

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
            scale_pct=self._spn_scale.value(),
            rotation_deg=self._spn_rot.value(),
            offset_x=self._spn_dx.value(),
            offset_y=self._spn_dy.value(),
            invert=self._chk_invert.isChecked(),
            fit=self._chk_fit.isChecked(),
            lib_dir=self._s.lib_dir,        # no widget: resolved, not chosen
        )
