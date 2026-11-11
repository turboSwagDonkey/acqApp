"""The DMD's settings panel. Never talks to the device: the panel emits, the
adapter routes, the controller (rebuilt on every Emulate toggle) projects."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QRect, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QImage, QKeySequence, QPainter, QPen,
                         QPixmap, QShortcut)
from PyQt6.QtWidgets import (
    QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from acqApp import style
from acqApp.widgets import (PillGroup, button_row, collapsible, pairs_grid,
                            pill, spin)
from acqApp.devices.dmd import alp, roi_store
from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, MODE_ALL_ON,
                                        MODE_PATTERN, MODE_ROI, DmdSettings,
                                        roi_frame, subsample_frame)

# Sub-sampling slider stops, dimmest first: 1 in n off keeps (n-1)/n, 1 = off.
_SUB_STEPS = (2, 3, 4, 5, 6, 7, 8, 9, 10, 1)

class _DraggablePreview(QLabel):
    """The preview label; click-drag nudges the pattern's offset. `dragged`
    is in PREVIEW px — the panel converts (it knows the scale)."""
    dragged = pyqtSignal(float, float)   # (dx, dy) in PREVIEW px

    resized = pyqtSignal()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._draggable = False
        self._drag_from = None
        # Ignored: the pixmap is drawn to the label's size, so its own size
        # must not feed back into the layout.
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Ignored)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def set_draggable(self, on: bool) -> None:
        self._draggable = on
        if not on:
            self._drag_from = None
        self.setCursor(Qt.CursorShape.SizeAllCursor if on
                       else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event) -> None:
        if self._draggable and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None:
            now = event.position()
            delta = now - self._drag_from
            self._drag_from = now
            self.dragged.emit(delta.x(), delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_from is not None:
            self._drag_from = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SettingsPanel(QWidget):
    settings_changed = pyqtSignal(object)   # emits DmdSettings
    load_requested   = pyqtSignal(object)   # emits Path
    # The adapter opens both: only it reaches the camera frame and controller.
    rois_edit_requested = pyqtSignal()
    calibrate_requested = pyqtSignal()
    # Cancels a pending debounced re-project; Live isn't a setting, so
    # settings_changed doesn't fire for it.
    live_toggled = pyqtSignal(bool)

    def __init__(self, settings: DmdSettings | None = None, parent=None):
        super().__init__(parent)
        self._s = settings or DmdSettings()
        self._res: tuple[int, int] = (DEFAULT_W, DEFAULT_H)
        self._pattern_path: Path | None = (
            Path(self._s.pattern_path) if self._s.pattern_path else None)
        self._pattern_cache: tuple[tuple, np.ndarray] | None = None
        # The built frame, keyed on what changes it (not the label size, so a
        # resize skips alp.build_frame, 2026-08-27).
        self._frame_cache: tuple[tuple, np.ndarray] | None = None
        self._preview_scale: float = 1.0    # device px per preview px
        self._rois: tuple = tuple(self._s.rois or ())
        self._calib_path: str = self._s.calib_path or ""
        # Name of a saved set a routine loaded; cleared once hand-edited.
        self._roi_pattern_name: str = ""
        self._shortcuts: list[QShortcut] = []
        self._build()
        self._init_shortcuts()

    def set_device(self, name: str, resolution: tuple[int, int], real: bool) -> None:
        self._res = resolution
        w, h = resolution
        res_str = f"{w}x{h}"
        dev_label = name if res_str in name else f"{name} · {res_str}"
        dev_label += "" if real else " · nothing will be projected"
        self._lbl_dev.setText(dev_label)
        self._lbl_dev.setStyleSheet(
            f"color:{style.HEX['dmd'] if real else style.WARN};")
        self._lbl_warn.setText(dev_label)
        self._lbl_warn.setVisible(not real)
        self._update_preview()

    def _build(self) -> None:
        """`self.view`, always shown: what the switch would project and its
        name. This panel, the settings: Show (the mode pills), that mode's
        controls, the rest under Advanced."""
        # Only without a real DMD: why the switch lights nothing.
        self._lbl_warn = QLabel()
        self._lbl_warn.setWordWrap(True)
        self._lbl_warn.setStyleSheet(f"color:{style.WARN};")
        self._lbl_warn.hide()

        # Pills, not a combo: what the switch projects should read at a glance.
        self._modes = PillGroup((
            ("All ON", MODE_ALL_ON, "Every mirror on — the full field."),
            ("Image", MODE_PATTERN, "A pattern file, placed by scale, "
                                    "rotation and offset."),
            ("ROIs", MODE_ROI, "Only the drawn ROIs, mapped through the "
                               "measured calibration.")), "dmd")
        self._rb = {k: self._modes.button(k)
                    for k in (MODE_ALL_ON, MODE_PATTERN, MODE_ROI)}
        self._rb.get(self._s.display_mode, self._rb[MODE_PATTERN]).setChecked(True)
        for rb in self._rb.values():
            rb.toggled.connect(self._on_mode_changed)

        self._preview = _DraggablePreview("No preview")
        self._preview.setMinimumSize(200, 140)
        self._preview.resized.connect(self._update_preview)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(f"color:{style.muted()};")
        self._preview.setToolTip(
            "What the DMD switch projects. In Image mode (Fit off), drag to "
            "move the pattern — the same offset as Offset X/Y.")
        self._preview.dragged.connect(self._on_preview_dragged)
        # What the preview shows, in words.
        self._lbl_pattern = QLabel("No pattern loaded")
        self._lbl_pattern.setWordWrap(True)
        self._lbl_pattern.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.view = QWidget()
        vroot = QVBoxLayout(self.view)
        vroot.setContentsMargins(4, 4, 4, 4)
        vroot.setSpacing(4)
        vroot.addWidget(self._preview, 1)
        vroot.addWidget(self._lbl_pattern)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(self._lbl_warn)
        root.addLayout(button_row(QLabel("Show:"), self._modes))
        self._image = self._build_image()
        self._roi = self._build_rois()
        root.addWidget(self._image)
        root.addWidget(self._roi)
        root.addWidget(self._build_advanced())
        root.addStretch(1)

        self._on_mode_changed()
        self._update_preview()

        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy,
                  self._sld_subsample):
            w.valueChanged.connect(self._emit)
        for c in (self._chk_fit, self._chk_invert):
            c.toggled.connect(self._emit)

    # ── Image mode: the pattern file and where it lands ──────────────────────
    def _build_image(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        self._btn_browse = QPushButton("Browse pattern…")
        self._btn_browse.setToolTip("A pattern image (.png/.bmp/.tif).")
        self._btn_browse.clicked.connect(self._browse)

        self._chk_fit = pill(
            "Fit to panel", "dmd", checked=self._s.fit,
            tip="Stretch the pattern to the panel, ignoring scale, rotation "
                "and offset.")
        self._chk_fit.toggled.connect(self._on_fit_toggled)

        self._spn_scale = spin(1.0, 1000.0, self._s.scale_pct,
                               decimals=1, step=1.0, suffix=" %")
        self._spn_scale.setToolTip("Pattern size, % of its natural size.")

        self._spn_rot = spin(-360.0, 360.0, self._s.rotation_deg,
                             decimals=1, step=0.5, suffix=" °",
                             tooltip="Clockwise-positive rotation.")

        self._spn_dx = spin(-4000.0, 4000.0, self._s.offset_x,
                            decimals=0, step=1.0, suffix=" px")
        self._spn_dx.setToolTip("Offset from center in device pixels.")

        self._spn_dy = spin(-4000.0, 4000.0, self._s.offset_y,
                            decimals=0, step=1.0, suffix=" px",
                            tooltip="Offset from center in device pixels.")

        btn_reset_geom = QPushButton("Reset")
        btn_reset_geom.setToolTip("Scale 100 %, rotation 0°, offsets (0, 0); "
                                  "Fit and Invert off.")
        btn_reset_geom.clicked.connect(self._reset_geometry)

        self._chk_nudge = pill(
            "Keyboard nudging", "dmd",
            tip="Nudge alignment with keys:\n"
                " • Arrows : Offset X/Y (1 px)\n"
                " • + / -  : Scale (1 %)\n"
                " • [ / ]  : Rotation (0.5 °)\n"
                "Hold Shift for 10x larger steps.")
        self._chk_nudge.toggled.connect(self._on_nudge_toggled)
        v.addLayout(button_row(self._btn_browse, None, self._chk_fit,
                               self._chk_nudge, right=(btn_reset_geom,)))
        v.addLayout(pairs_grid(
            ("Scale:", self._spn_scale, "Rotation:", self._spn_rot),
            ("Offset X:", self._spn_dx, "Offset Y:", self._spn_dy)))
        return box

    # ── rarely changed: folded ───────────────────────────────────────────────
    def _build_advanced(self) -> QGroupBox:
        adv = QGroupBox("Advanced")
        adv.setObjectName("advanced")       # style.accent_panel: underline when folded
        v = QVBoxLayout(adv)
        v.setSpacing(4)

        # Emits light unprompted, so never persisted: off every session.
        self._chk_live = pill(
            "Live update", "dmd",
            tip="While on, every change here re-projects onto the DMD a moment "
                "after you pause (debounced, not on every keystroke/drag tick) "
                "— no need to flip the switch. This actually emits light; "
                "leave it off unless you're actively aligning. Turning it off "
                "cancels a pending re-project immediately.")
        self._chk_live.toggled.connect(self.live_toggled.emit)
        self._chk_invert = pill("Invert mirrors", "dmd", checked=self._s.invert,
                                tip="Image mode: swap which mirrors are on and off.")

        self._sld_subsample = QSlider(Qt.Orientation.Horizontal)
        self._sld_subsample.setRange(0, len(_SUB_STEPS) - 1)
        self._sld_subsample.setPageStep(1)
        self._sld_subsample.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._sld_subsample.setToolTip(
            "Turn off 1 of every N mirrors the pattern would light, in an even "
            "diagonal spread, cutting total light without changing exposure "
            "time — a coarse ND filter made of missing mirrors. Left is "
            "dimmest: 1 in 2 off (half the light) through 1 in 10 off, then "
            "off (every mirror stays on).")
        self._lbl_subsample = QLabel()
        self._lbl_subsample.ensurePolished()        # the stylesheet's font, not the default
        self._lbl_subsample.setFixedWidth(
            self._lbl_subsample.fontMetrics().horizontalAdvance("1 in 10 off") + 4)
        self._lbl_subsample.setAlignment(Qt.AlignmentFlag.AlignRight
                                         | Qt.AlignmentFlag.AlignVCenter)
        self._sld_subsample.valueChanged.connect(self._show_subsample)
        self.set_sub_sampling(self._s.sub_sampling)
        self._show_subsample()

        self._lbl_dev = QLabel("no device yet")
        self._lbl_dev.setWordWrap(True)

        v.addLayout(button_row(self._chk_live, self._chk_invert))
        sub = QHBoxLayout()
        sub.setSpacing(6)
        sub.addWidget(QLabel("Sub-sampling:"))
        sub.addWidget(self._sld_subsample, 1)
        sub.addWidget(self._lbl_subsample)
        v.addLayout(sub)
        v.addLayout(button_row(QLabel("Device:"), self._lbl_dev))
        collapsible(adv, expanded=False)
        return adv

    # ── ROIs mode: the drawn regions and the calibration that maps them ──────
    def _build_rois(self) -> QWidget:
        """The editor itself is a separate window: it's an image view, and
        this panel lives in a scroll area."""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        self._lbl_rois = QLabel()
        self._lbl_rois.setToolTip(
            "Draw regions on a snapshot from the VOLTAGE camera — that's the "
            "imaging path the DMD projects into — and turn them into a mirror "
            "mask.\nTurning them into a mask needs a measured camera↔DMD "
            "registration; without one they can still be drawn and saved.")
        btn = QPushButton("Edit ROIs…")
        btn.setToolTip(
            "Open the editor on the voltage camera's newest frame, with the "
            "live camera under it.\nTake that frame full field with the DMD "
            "all-on to see the projected field — this button starts Live "
            "view but never commands the projector itself.")
        btn.clicked.connect(self.rois_edit_requested)
        btn_set = QPushButton("Load ROIs…")
        btn_set.setToolTip(
            "A saved ROI set (.roi.json) — the same ones the editor's Load… "
            "opens, without a trip through the editor.")
        btn_set.clicked.connect(self._browse)
        v.addLayout(button_row(self._lbl_rois, None, right=(btn, btn_set)))

        self._lbl_calib = QLabel()
        self._lbl_calib.setWordWrap(True)
        b_run = QPushButton("Calibrate…")
        b_run.setStyleSheet(style.solid_btn("dmd"))
        b_run.setToolTip(
            "Set the camera↔DMD transform by eye: the DMD lights a shape "
            "you steer onto a reticle over the live camera.\n"
            "THIS PROJECTS LIGHT until the window closes.")
        b_run.clicked.connect(self.calibrate_requested)
        b_load = QPushButton("Load…")
        b_load.setToolTip("A DmdCalibration JSON saved by Calibrate…")
        b_load.clicked.connect(self._pick_calib)
        self._btn_calib_clear = QPushButton("Clear")
        self._btn_calib_clear.clicked.connect(lambda: self._set_calib(""))
        self._btn_calib_clear.setToolTip("Forget the loaded calibration.")
        v.addLayout(button_row(self._lbl_calib, None, right=(
            b_run, b_load, self._btn_calib_clear)))
        self._show_rois()
        return box

    def _pick_calib(self) -> None:
        start = str(Path(self._calib_path).parent) if self._calib_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "DMD calibration", start, "Calibration (*.json);;All files (*)")
        if path:                        # empty = cancelled; must not clear
            self._set_calib(path)

    def _set_calib(self, path: str) -> None:
        self._calib_path = path
        self._show_rois()
        self._emit()

    def set_calib_path(self, path: str) -> None:
        """Adopt a calibration the adapter just measured or loaded."""
        self._set_calib(path)

    def set_pattern_path(self, path: Path) -> None:
        """Adopt a routine's pattern and switch to Image: `display()`
        re-applies the panel's settings, so a stale path or mode here would
        project the old thing."""
        self._pattern_path = Path(path)
        if not self._rb[MODE_PATTERN].isChecked():
            self._rb[MODE_PATTERN].setChecked(True)   # -> _on_mode_changed
        else:
            self._emit()

    def set_rois(self, rois) -> None:
        """Store what the editor produced. Called by the adapter, not the user."""
        self._rois = tuple(rois)
        self._roi_pattern_name = ""     # hand-edited — no longer "the" saved set
        self._show_rois()
        self._emit()

    def set_roi_pattern(self, name: str, rois: list) -> None:
        """Adopt a routine's ROI set and switch to ROIs (see
        `set_pattern_path`)."""
        self._rois = tuple(rois)
        self._roi_pattern_name = name
        self._show_rois()
        if not self._rb[MODE_ROI].isChecked():
            self._rb[MODE_ROI].setChecked(True)   # -> _on_mode_changed -> _emit
        else:
            self._emit()

    def set_all_on(self) -> None:
        """Switch to All ON; projects on the next switch-on (or Live update)."""
        if not self._rb[MODE_ALL_ON].isChecked():
            self._rb[MODE_ALL_ON].setChecked(True)   # -> _on_mode_changed -> _emit
        else:
            self._emit()

    def set_sub_sampling(self, n: int) -> None:
        """Set 1-in-N sub-sampling for any mode; projects on the next
        switch-on (or Live update)."""
        n = min(max(int(n), 1), 10)
        self._sld_subsample.setValue(_SUB_STEPS.index(n))

    def _sub_n(self) -> int:
        return _SUB_STEPS[self._sld_subsample.value()]

    def _show_subsample(self, *_a) -> None:
        n = self._sub_n()
        self._lbl_subsample.setText(f"1 in {n} off" if n > 1 else "off")

    def _show_rois(self) -> None:
        n = len(self._rois)
        base = f"ROIs: {n}" if n else "ROIs: none yet"
        self._lbl_rois.setText(
            f'{base} — "{self._roi_pattern_name}"' if self._roi_pattern_name
            else base)
        self._lbl_calib.setText(
            f"Calibration: {Path(self._calib_path).name}" if self._calib_path
            else "Calibration: none")
        self._btn_calib_clear.setEnabled(bool(self._calib_path))

    def _init_shortcuts(self) -> None:
        # On the settings and on the view: the image being nudged is in the
        # view, which a click on it focuses.
        self.view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        def add_sc(key_seq: str, callback: Callable[[], None]) -> None:
            for host in (self, self.view):
                sc = QShortcut(QKeySequence(key_seq), host)
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(callback)
                sc.setEnabled(False)
                self._shortcuts.append(sc)

        add_sc("Left", lambda: self._nudge_spn(self._spn_dx, -1))
        add_sc("Right", lambda: self._nudge_spn(self._spn_dx, 1))
        add_sc("Up", lambda: self._nudge_spn(self._spn_dy, -1))
        add_sc("Down", lambda: self._nudge_spn(self._spn_dy, 1))

        add_sc("Shift+Left", lambda: self._nudge_spn(self._spn_dx, -10))
        add_sc("Shift+Right", lambda: self._nudge_spn(self._spn_dx, 10))
        add_sc("Shift+Up", lambda: self._nudge_spn(self._spn_dy, -10))
        add_sc("Shift+Down", lambda: self._nudge_spn(self._spn_dy, 10))

        add_sc("+", lambda: self._nudge_spn(self._spn_scale, 1))
        add_sc("=", lambda: self._nudge_spn(self._spn_scale, 1))
        add_sc("-", lambda: self._nudge_spn(self._spn_scale, -1))
        add_sc("Shift++", lambda: self._nudge_spn(self._spn_scale, 10))
        add_sc("Shift+=", lambda: self._nudge_spn(self._spn_scale, 10))
        add_sc("Shift+-", lambda: self._nudge_spn(self._spn_scale, -10))
        add_sc("Shift+_", lambda: self._nudge_spn(self._spn_scale, -10))

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

    def _on_mode_changed(self, checked: bool = True) -> None:
        """Only the shown mode's controls are on screen. A switch fires
        `toggled` twice (off, then on); only the incoming button acts."""
        if not checked:
            return
        mode = self.mode
        pattern = mode == MODE_PATTERN
        self._image.setVisible(pattern)
        self._roi.setVisible(mode == MODE_ROI)
        self._chk_invert.setEnabled(pattern)
        self._set_alignment_enabled(pattern and not self._chk_fit.isChecked())
        self._update_preview()
        self.settings_changed.emit(self.settings)

    def _on_fit_toggled(self, on: bool) -> None:
        # The preview follows from `_emit`, also wired to this toggle.
        if self.mode == MODE_PATTERN:
            self._set_alignment_enabled(not on)

    def _set_alignment_enabled(self, on: bool) -> None:
        for w in (self._spn_scale, self._spn_rot, self._spn_dx, self._spn_dy):
            w.setEnabled(on)
        self._preview.set_draggable(on)

    def _on_preview_dragged(self, dx: float, dy: float) -> None:
        """Preview-px drag -> device-px offset; same sign as Qt's deltas.

        Scale is read once: the first setValue's `_emit` can reflow the
        layout and change it. `_spn_dx` is blocked so one tick fires one
        `_emit` (disk write, live re-project), not two."""
        scale = self._preview_scale
        with QSignalBlocker(self._spn_dx):
            self._spn_dx.setValue(self._spn_dx.value() + dx * scale)
        self._spn_dy.setValue(self._spn_dy.value() + dy * scale)

    def _browse(self) -> None:
        """A pattern file, or in ROIs mode a saved ROI set."""
        if self.mode == MODE_ROI:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select ROI set", str(roi_store.SESSION_DIR),
                "ROI sets (*.roi.json)")
            if path:
                try:
                    name, rois = roi_store.load_named(path)
                except (OSError, ValueError, KeyError) as e:
                    QMessageBox.warning(self, "ROI set not loaded",
                                        f"{Path(path).name} could not be read "
                                        f"({type(e).__name__}: {e}).")
                    return
                self.set_roi_pattern(name, rois.to_list())
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select pattern", "", "Images (*.png *.bmp *.tif)"
        )
        if path:
            p = Path(path)
            self._pattern_path = p
            self.load_requested.emit(p)
            self._emit()

    def _pattern_array(self, p: Path) -> np.ndarray:
        """The pattern file, decoded once per path + mtime + size: re-decoding
        per nudge was 4.7 ms of 21.8 and 20 disk reads/s (2026-08-25)."""
        from PIL import Image
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if self._pattern_cache is None or self._pattern_cache[0] != key:
            self._pattern_cache = (key, np.asarray(Image.open(p).convert("L")))
        return self._pattern_cache[1]

    def _update_preview(self) -> None:
        pw = self._preview.width()
        ph = self._preview.height()
        if pw <= 1 or ph <= 1:
            return

        w, h = self._res
        p = self._pattern_path

        mode = self.mode
        if mode == MODE_ALL_ON:
            self._lbl_pattern.setText("All mirrors ON (Full Illumination)")
            frame = subsample_frame(np.full((h, w), 255, dtype=np.uint8),
                                    self._sub_n())
        elif mode == MODE_ROI:
            n = len(self._rois)
            # Guarded: roi_frame prints why it can't, and this runs per resize.
            frame = (roi_frame(self.settings, w, h)
                     if n and self._calib_path else None)
            if frame is None:
                frame = np.zeros((h, w), dtype=np.uint8)
                self._lbl_pattern.setText(
                    "Needs a calibration to project the ROIs"
                    if n else "Nothing to project: draw or load ROIs")
            else:
                frame = subsample_frame(frame, self._sub_n())
                name = (f"{self._roi_pattern_name}: "
                        if self._roi_pattern_name else "")
                self._lbl_pattern.setText(
                    f"{name}{n} ROI(s) -> {int((frame > 0).sum())} mirrors")
        elif p is None:
            self._lbl_pattern.setText("No pattern loaded")
            frame = np.zeros((h, w), dtype=np.uint8)
        elif not p.exists():
            self._lbl_pattern.setText(f"{p.name} — missing")
            frame = np.zeros((h, w), dtype=np.uint8)
        else:
            self._lbl_pattern.setText(p.name)
            try:
                arr = self._pattern_array(p)
                s = self.settings
                # No pw/ph: a resize must not re-run build_frame (~17 ms).
                key = (self._pattern_cache[0], w, h, s.scale_pct,
                       s.rotation_deg, s.offset_x, s.offset_y, s.invert,
                       s.fit, s.sub_sampling)
                if self._frame_cache is None or self._frame_cache[0] != key:
                    built = subsample_frame(alp.build_frame(
                        arr, w, h, scale_pct=s.scale_pct,
                        rotation_deg=s.rotation_deg, offset_x=s.offset_x,
                        offset_y=s.offset_y, invert=s.invert, fit=s.fit),
                        s.sub_sampling)
                    self._frame_cache = (key, built)
                frame = self._frame_cache[1]
            except Exception as e:
                self._preview.setText(f"(render error: {e})")
                return

        frame = np.ascontiguousarray(frame)
        qimg = QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8)

        pad = 2                         # keeps the border stroke unclipped
        target = QSize(w, h).scaled(max(1, pw - 2 * pad), max(1, ph - 2 * pad),
                                    Qt.AspectRatioMode.KeepAspectRatio)
        self._preview_scale = w / max(1, target.width())
        scaled = QPixmap.fromImage(qimg).scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)

        canvas = QPixmap(pw, ph)
        canvas.fill(QColor(0, 0, 0, 0))         # the tab's grey shows through
        rect = QRect((pw - target.width()) // 2, (ph - target.height()) // 2,
                     target.width(), target.height())
        painter = QPainter(canvas)
        painter.drawPixmap(rect, scaled)
        pen = QPen(QColor(style.HEX.get("dmd", "#e040fb")), 1.5,
                   Qt.PenStyle.DashLine)
        pen.setDashPattern([4, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        # Nothing lit: say why inside the frame, not in a line under it.
        empty = not frame.any()
        if empty:
            painter.setFont(self._preview.font())
            painter.setPen(QColor(style.muted()))
            painter.drawText(rect.adjusted(12, 12, -12, -12),
                             int(Qt.AlignmentFlag.AlignCenter
                                 | Qt.TextFlag.TextWordWrap),
                             self._lbl_pattern.text())
        painter.end()
        self._lbl_pattern.setVisible(not empty)
        self._preview.setPixmap(canvas)

    @property
    def live(self) -> bool:
        """Whether Live update is on; never persisted, so off at start."""
        return self._chk_live.isChecked()

    @property
    def mode(self) -> str:
        for key, rb in self._rb.items():
            if rb.isChecked():
                return key
        return MODE_PATTERN

    @property
    def display_name(self) -> str:
        """What is selected, in a few words: file, ROI set or All on; '' = nothing."""
        mode = self.mode
        if mode == MODE_ALL_ON:
            return "All on"
        if mode == MODE_ROI:
            n = len(self._rois)
            return self._roi_pattern_name or (f"{n} ROI(s)" if n else "")
        return self._pattern_path.name if self._pattern_path else ""

    @property
    def settings(self) -> DmdSettings:
        return DmdSettings(
            pattern_path=self._pattern_path,
            scale_pct=self._spn_scale.value(),
            rotation_deg=self._spn_rot.value(),
            offset_x=self._spn_dx.value(),
            offset_y=self._spn_dy.value(),
            invert=self._chk_invert.isChecked(),
            sub_sampling=self._sub_n(),
            display_mode=self.mode,
            all_on=self.mode == MODE_ALL_ON,
            fit=self._chk_fit.isChecked(),
            lib_dir=self._s.lib_dir,
            rois=self._rois,
            calib_path=self._calib_path,
        )

    @property
    def calib_path(self) -> str:
        return self._calib_path

    @property
    def rois(self) -> tuple:
        return self._rois
