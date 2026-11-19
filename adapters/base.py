"""`ModuleAdapter` — all `MainWindow` knows about an instrument — and the
widgets they share. The lifecycle is in `__init__.py`."""
from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                             QVBoxLayout, QWidget)

from acqApp import config, style
from acqApp.console import short_error
from acqApp.acq.devices import (DeviceWorker, FocusOffset, LedTarget, ModuleHost,
                                OutputController, PatternTarget, PufferTarget,
                                RecordingOutput, SignalSource, StageTarget)


DISP_DS      = 4            # preview downsample stride
LEVELS_EVERY = 15           # recompute camera contrast levels every N ticks


# ── shared widget builders ────────────────────────────────────────────────────

class DragRectViewBox(pg.ViewBox):
    """A left-drag draws a rectangle while armed; otherwise a plain ViewBox."""

    dragged = pyqtSignal(float, float, float, float, bool)  # x0,y0,x1,y1,finished

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._draw = False

    def set_draw_mode(self, on: bool) -> None:
        self._draw = bool(on)
        self.setCursor(Qt.CursorShape.CrossCursor if on
                       else Qt.CursorShape.ArrowCursor)

    def mouseDragEvent(self, ev, axis=None) -> None:
        if not self._draw or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(ev, axis=axis)
            return
        ev.accept()
        a = self.mapSceneToView(ev.buttonDownScenePos())
        b = self.mapSceneToView(ev.scenePos())
        x0, x1 = sorted((a.x(), b.x()))
        y0, y1 = sorted((a.y(), b.y()))
        self.dragged.emit(x0, y0, x1, y1, ev.isFinish())


class RecDot(QLabel):
    """Red dot over a view's corner while recording. Parented to the
    GraphicsView and raised above its viewport, not laid out."""
    _SIZE = 14
    _MARGIN = 8

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setStyleSheet(
            f"background:#e53935; border-radius:{self._SIZE // 2}px; "
            f"border:1px solid #7a1f1f;")
        self.setToolTip("Recording")
        self.move(self._MARGIN, self._MARGIN)
        self.raise_()
        self.hide()


_LUT_H = 40                     # a horizontal LUT bar's height, px
_LUT_W = 64                     # an upright one's width (no axis or gradient)
_LUT_SMOOTH = 40                # the histogram is averaged over 1/N of its bins
_LUT_MEMORY = 8                 # refreshes (~6 s) whose spans the strip keeps in view


_LEVELS_DEADBAND = 0.04         # Auto ignores edge moves under this share of the range


def _settled_levels(old, new) -> tuple[float, float]:
    """Auto's new (lo, hi), or the old pair while both edges moved less than
    the deadband: percentile jitter doesn't shake the contrast or the bar."""
    lo, hi = float(new[0]), float(new[1])
    if old is not None:
        band = _LEVELS_DEADBAND * max(old[1] - old[0], 1e-9)
        if abs(lo - old[0]) < band and abs(hi - old[1]) < band:
            return old
    return lo, hi


def _level_text(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}"


def _quiet_lut(hist: pg.HistogramLUTWidget) -> None:
    """A LUT bar showing only what gets read: a smoothed grey histogram, the
    chosen range lit, its two edges as numbers. No axis, no gradient strip;
    right-click still opens the colour-map menu. Either orientation: upright,
    pyqtgraph turns the curve 90°, so levels run up y and counts along -x."""
    item = hist.item
    upright = item.orientation == "vertical"
    for part in (item.axis, item.gradient):
        item.layout.removeItem(part)
        part.hide()
    item.paint = lambda *_a: None   # it only draws the edges' leads to the gradient
    item.plot.setPen((205, 208, 218, 150))
    item.plot.setBrush((190, 194, 206, 60))
    item.region.setBrush((255, 255, 255, 26))
    item.region.setHoverBrush((255, 255, 255, 44))
    for ln in item.region.lines:
        ln.setPen(pg.mkPen("#e8c547", width=2))
        ln.setHoverPen(pg.mkPen("#ffe07a", width=3))
    font = QFont()
    font.setPointSize(8)
    # Inside the range, off the curve: its top corners, or upright its left
    # side (lo over its line, hi under its own).
    labels = ((pg.TextItem(anchor=(0, 1)), pg.TextItem(anchor=(0, 0))) if upright
              else (pg.TextItem(anchor=(0, 0)), pg.TextItem(anchor=(1, 0))))
    for t in labels:
        t.setColor("#e2e5ec")
        t.setFont(font)
        item.vb.addItem(t, ignoreBounds=True)
    hist.level_labels = labels

    def level_view() -> list:
        return item.vb.viewRange()[1 if upright else 0]

    def set_levels_view(lo: float, hi: float) -> None:
        (item.vb.setYRange if upright else item.vb.setXRange)(lo, hi, padding=0.08)

    def set_count_top(top: float) -> None:
        if upright:
            item.vb.setXRange(-top, 0, padding=0)
        else:
            item.vb.setYRange(0, top, padding=0)

    def place(*_a) -> None:
        far = item.vb.viewRange()[0][0] if upright else item.vb.viewRange()[1][1]
        for t, v in zip(labels, item.region.getRegion()):
            t.setText(_level_text(v))
            t.setPos(*((far, v) if upright else (v, far)))

    item.region.sigRegionChanged.connect(place)
    item.vb.sigRangeChanged.connect(place)

    base_changed = item.imageChanged
    prev: list = [None, None, 0.0]      # last curve x, y; the y top it set
    recent: deque = deque(maxlen=_LUT_MEMORY)   # the spans the view must hold
    zoomed = [False]        # the operator zoomed/panned: theirs until a double-click

    def changed(autoLevel: bool = False, autoRange: bool = False) -> None:
        base_changed(autoLevel=autoLevel, autoRange=autoRange)
        x, y = item.plot.getData()
        if y is not None and len(y) > 3:
            k = max(3, len(y) // _LUT_SMOOTH)
            y = np.convolve(y, np.ones(k) / k, mode="same")
            if prev[0] is not None:     # half the last curve: it eases, not flickers
                y = 0.5 * y + 0.5 * np.interp(x, prev[0], prev[1], left=0, right=0)
            prev[0], prev[1] = x, y
            item.plot.setData(x, y)
            _settle_ranges(x, y)
        place()

    def _settle_ranges(x, y) -> None:
        """Rescale only when the curve or an edge outgrows the view, or
        the view is far too big for them: steady between refreshes."""
        top, peak = prev[2], float(y.max())
        if peak > 0 and not 0.3 * top < peak < 0.85 * top:
            prev[2] = peak * 1.7        # headroom over the curve for the numbers
            set_count_top(prev[2])
        # The curve's bulk, not its extremes: a few outlier pixels move
        # those every frame.
        body = x[y > 0.01 * peak] if peak > 0 else x
        recent.append((float(body[0]), float(body[-1])))
        fit_levels()

    def fit_levels(*_a) -> None:
        """Hold what the last few refreshes spanned, and both edges: content
        that comes and goes (a bright spot, a dim patch) doesn't make the
        strip breathe. Also when an edge moves (Auto, a drag)."""
        if not recent or zoomed[0]:
            return
        lo, hi = item.region.getRegion()
        need = (min(lo, *(n[0] for n in recent)), max(hi, *(n[1] for n in recent)))
        v0, v1 = level_view()
        if need[0] < v0 or need[1] > v1 or need[1] - need[0] < 0.7 * (v1 - v0):
            set_levels_view(*need)

    item.region.sigRegionChangeFinished.connect(fit_levels)

    item.imageChanged = changed     # what `_paint`'s refresh calls
    item.vb.disableAutoRange()      # _settle_ranges alone moves the view

    item.vb.setMenuEnabled(False)
    vb_click = item.vb.mouseClickEvent

    def click(ev) -> None:
        if ev.button() == Qt.MouseButton.RightButton:
            ev.accept()
            item.gradient.showMenu(ev)
            return
        vb_click(ev)

    item.vb.mouseClickEvent = click

    def hand_zoom(*_a) -> None:
        zoomed[0] = True

    def double_click(ev) -> None:
        ev.accept()
        zoomed[0] = False
        fit_levels()

    item.vb.sigRangeChangedManually.connect(hand_zoom)
    item.vb.mouseDoubleClickEvent = double_click
    hist.zoom_is_manual = lambda: zoomed[0]
    hist.setToolTip("Pixel brightness. Drag the yellow edges to set black and "
                    "white (Auto sets them for you). Scroll to zoom, drag to "
                    "pan; double-click to fit again. Right-click: colour map.")


def _image_view(vb_cls: type[pg.ViewBox] = pg.ViewBox, *, horizontal: bool = False):
    """Image + LUT bar (with an Auto checkbox) beside it, or under it when
    `horizontal` (a wide sensor) -> (image, hist, chk_auto, graphics_view,
    viewbox, row, rec_dot)."""
    img = pg.ImageItem()
    hist = pg.HistogramLUTWidget(orientation="horizontal" if horizontal else "vertical")
    hist.setImageItem(img)
    # Per-frame histograms cost ~2 ms a frame; `_paint` refreshes it instead.
    img.sigImageChanged.disconnect(hist.item.imageChanged)
    if horizontal:
        hist.setFixedHeight(_LUT_H)
    else:
        hist.setFixedWidth(_LUT_W)
    _quiet_lut(hist)
    gv = pg.GraphicsView()
    vb = vb_cls(lockAspect=True, invertY=True)
    gv.setCentralItem(vb)
    vb.addItem(img)
    rec_dot = RecDot(gv)

    hint = QLabel("Press Live view")
    hint.setObjectName("idleHint")
    hint.setStyleSheet(f"color:{style.muted()};font-size:12pt;background:transparent")
    hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    QGridLayout(gv).addWidget(hint, 0, 0, Qt.AlignmentFlag.AlignCenter)
    img.sigImageChanged.connect(hint.hide)

    chk_auto = QCheckBox("Auto")
    chk_auto.setToolTip("Auto contrast — same control as the settings tab's.")

    lut_col = QWidget()
    lut_lay = (QHBoxLayout if horizontal else QVBoxLayout)(lut_col)
    lut_lay.setContentsMargins(0, 0, 0, 0)
    lut_lay.setSpacing(6 if horizontal else 2)
    lut_lay.addWidget(chk_auto, alignment=Qt.AlignmentFlag.AlignVCenter if horizontal
                      else Qt.AlignmentFlag.AlignHCenter)
    # The bar takes the length; hidden, the stretch does, so the checkboxes
    # before it stay together at the start.
    lut_lay.addWidget(hist, 1)
    lut_lay.addStretch(0)

    row = QWidget()
    lay = (QVBoxLayout if horizontal else QHBoxLayout)(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    if horizontal:              # image over its bar
        lay.addWidget(gv, 1)
        lay.addWidget(lut_col)
        # Hidden bar: the row keeps its height, so the image doesn't jump.
        lut_col.setMinimumHeight(_LUT_H)
    else:
        lay.addWidget(lut_col)
        lay.addWidget(gv)
    return img, hist, chk_auto, gv, vb, row, rec_dot


def led_controller(emulate: bool, rig_key: str, real_cls, mock_cls, label: str):
    """A DAQ LED or its mock. A rig without the LED gets the mock without a
    DAQ attempt."""
    if emulate:
        return mock_cls()
    if not config.rig_has(rig_key):
        print(f"[main] {label}: not fitted on rig "
              f"{config.active_rig() or '(none set)'} — continuing without it")
        return mock_cls()
    chan = config.rig_channel(rig_key)
    try:
        return real_cls(chan) if chan else real_cls()
    except Exception as e:
        where = f" on {chan}" if chan else ""
        print(f"[main] {label}: not available{where} — {short_error(e)}; "
              f"continuing without it")
        return mock_cls()


# ── base ──────────────────────────────────────────────────────────────────────

class ModuleAdapter:
    """One instrument's whole contribution to the main window."""

    key: str = ""               # a config.MODULES key
    tab_label: str = ""
    # Its own dock (settings folded over its view), else a General section.
    own_dock: bool = False
    # Another module's dock it rides on while that one is loaded: its panel's
    # `strip` on top of that view, its `calibration` in that Settings (else
    # a General section as usual).
    rides_on: str = ""
    image_format: str = ""      # its image stream's file: "avi", or "" = TIFF

    def __init__(self, win: ModuleHost) -> None:
        self.win = win
        self.panel: QWidget | None = None
        self.worker: DeviceWorker | None = None
        self.controller: OutputController | None = None
        self._chk_auto_lut: QCheckBox | None = None
        # Preview state for camera-shaped modules.
        self._img = None
        self._hist = None
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0
        self._rec_dot: QLabel | None = None

    # ── construction (once, at startup) ───────────────────────────────────────
    def build_panel(self) -> QWidget | None:
        return None

    def status_widget(self) -> QWidget | None:
        """A control for the window's status bar, beside Record. The same
        widget on every call; the window places it on load and removes it on
        unload."""
        return None

    def mini_view(self) -> QWidget | None:
        """The body of its sidebar card (a `sidebar.MiniView` polls; plain
        controls work too): built fresh per call, the card owns it. None =
        a title-only card, or no card for a module without a page."""
        return None

    def view_widget(self) -> QWidget | None:
        """Its live view, under its settings in its own dock; built once,
        after the panel."""
        return None

    # ── persistent output controllers (rebuilt when Emulate is toggled) ───────
    def build_controller(self, emulate: bool) -> None:
        """Create the always-on output device for this mode."""

    def close_controller(self) -> None:
        if self.controller is not None:
            try:
                self.controller.close()
            except Exception:
                pass
            self.controller = None

    # ── illumination / preview (camera-shaped modules) ────────────────────────
    def _apply_led_follow(self, on: bool) -> None:
        if self.controller is not None:
            try:                        # runs from Live's slots: must not abort
                self.controller.set(on)
            except Exception as e:                   # noqa: BLE001
                self.win.status(f"{self.tab_label} LED: {type(e).__name__}: {e}")
                return                  # the switch keeps showing the real state
        if self.panel is not None:
            self.panel.set_led(on)

    # The LUT bar's Auto and the settings tab's mirror each other; the
    # settings one persists. setChecked to the same value doesn't re-emit.
    def _sync_auto_from_lut(self, on: bool) -> None:
        if self.panel is not None:
            self.panel._chk_auto.setChecked(on)

    def _sync_auto_to_lut(self, on: bool) -> None:
        if self._chk_auto_lut is not None:
            self._chk_auto_lut.setChecked(on)

    def _reset_levels(self) -> None:
        self._levels = None
        self._level_ctr = 0

    def _paint(self, data, auto: bool) -> None:
        """Auto recomputes percentiles every LEVELS_EVERY ticks; the LUT bar's
        histogram refreshes on the same beat. Manual re-passes the bar's own
        levels: omitting `levels=` left pyqtgraph showing stale contrast."""
        refresh = self._level_ctr % LEVELS_EVERY == 0
        self._level_ctr += 1
        if auto:
            if self._levels is None or refresh:
                self._levels = _settled_levels(self._levels, np.percentile(data, (1, 99)))
            levels = self._levels
        else:
            levels = self._hist.item.getLevels() if self._hist is not None else None
        self._img.setImage(data, autoLevels=False, levels=levels)
        if self._hist is None:
            return
        if refresh and self._hist.isVisible():
            self._hist.item.imageChanged()
        elif auto and tuple(self._hist.item.getLevels()) != tuple(levels):
            # Auto moved between refreshes: the bar's edges follow at once.
            self._hist.item.region.setRegion(levels)

    def _sync_rec_dot(self) -> None:
        if self._rec_dot is not None:
            self._rec_dot.setVisible(self.win.is_recording())

    # ── session ───────────────────────────────────────────────────────────────
    def build_session(self, emulate: bool) -> None:
        """Create the worker but don't start it: the clock must reach t=0 first."""

    def start(self) -> None:
        if self.worker is not None:
            self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def _adopt(self, worker):
        """Own a worker and surface its errors (an escaping one aborts the process)."""
        worker.error.connect(self.win.on_worker_error)
        self.worker = worker
        return worker

    # ── display (~30 Hz while running) ────────────────────────────────────────
    def update_display(self) -> None:
        pass

    def last_frame(self):
        """The last displayed frame, cached: `get_latest()` consumes, so
        asking the worker would steal from the preview."""
        return None

    # ── recording ─────────────────────────────────────────────────────────────
    def attach_sink(self, rec) -> None:
        pass

    def detach_sink(self) -> None:
        if self.worker is not None:
            self.worker.set_sink(None)
        if isinstance(self.controller, RecordingOutput):
            self.controller.set_sink(None)

    def metadata(self) -> dict[str, Any]:
        return {}

    def final_metadata(self) -> dict[str, Any]:
        """How the data came out; overwrites `metadata()` just before close."""
        return {}

    # ── misc ──────────────────────────────────────────────────────────────────
    def on_trigger(self, name: str, duration: float) -> None:
        pass

    def on_modules_changed(self) -> None:
        """Neighbours changed; anything derived from the module set is stale."""

    def signal_sources(self) -> list[SignalSource]:
        return []

    def stage_target(self) -> StageTarget | None:
        return None

    def pattern_target(self) -> PatternTarget | None:
        return None

    def led_target(self) -> LedTarget | None:
        return None

    def puffer_target(self) -> PufferTarget | None:
        return None

    def frame_rate_hz(self) -> float | None:
        """For estimates only."""
        return None

    def focus_offset(self) -> FocusOffset | None:
        """Path 2's focal plane vs path 1's; only the ETL has one."""
        return None

    def records_dcimg(self) -> bool | None:
        """Whether its camera writes a .dcimg when recording; None = no camera."""
        return None

    def busy_reason(self) -> str:
        """Why the module set must not change right now, or ""."""
        return ""

    def probe_kwargs(self) -> dict[str, Any]:
        return {}
