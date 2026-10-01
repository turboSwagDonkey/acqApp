"""`ModuleAdapter` — all `MainWindow` knows about an instrument — and the
widgets they share. The lifecycle is in `__init__.py`."""
from __future__ import annotations

from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from acqApp import config, style
from acqApp.closed_loop import SignalSource
from acqApp.acq.devices import (DeviceWorker, LedTarget, ModuleHost, OutputController,
                            PatternTarget, PufferTarget, RecordingOutput, StageTarget)


PLOT_HISTORY = 600          # samples kept in each rolling plot
DISP_DS      = 4            # preview downsample stride
LEVELS_EVERY = 15           # recompute camera contrast levels every N ticks


# ── shared widget builders ────────────────────────────────────────────────────

def _plot(title: str, left: str, units: str, bottom: str, key: str):
    """-> (widget, curve), in the module's accent colour."""
    pw = pg.PlotWidget(title=title)
    pw.setLabel("left", left, units=units)
    pw.setLabel("bottom", bottom)
    pw.showGrid(x=True, y=True, alpha=0.3)
    curve = pw.plot(pen=pg.mkPen(style.HEX[key], width=1.5))
    return pw, curve


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


def _image_view(vb_cls: type[pg.ViewBox] = pg.ViewBox):
    """Image + LUT bar (with an Auto checkbox) in a row ->
    (image, hist, chk_auto, graphics_view, viewbox, row, rec_dot)."""
    img = pg.ImageItem()
    hist = pg.HistogramLUTWidget()
    hist.setImageItem(img)
    hist.setFixedWidth(86)
    gv = pg.GraphicsView()
    vb = vb_cls(lockAspect=True, invertY=True)
    gv.setCentralItem(vb)
    vb.addItem(img)
    rec_dot = RecDot(gv)

    chk_auto = QCheckBox("Auto")
    chk_auto.setToolTip("Auto contrast — same control as the settings tab's.")

    lut_col = QWidget()
    lut_lay = QVBoxLayout(lut_col)
    lut_lay.setContentsMargins(0, 0, 0, 0)
    lut_lay.setSpacing(2)
    lut_lay.addWidget(chk_auto, alignment=Qt.AlignmentFlag.AlignHCenter)
    lut_lay.addWidget(hist)

    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(lut_col)
    lay.addWidget(gv)
    return img, hist, chk_auto, gv, vb, row, rec_dot


def led_controller(emulate: bool, rig_key: str, real_cls, mock_cls, label: str):
    """A DAQ LED or its mock. A rig without the LED gets the mock without a
    DAQ attempt."""
    if emulate:
        return mock_cls()
    if not config.rig_has(rig_key):
        print(f"[main] {label} not fitted on rig "
              f"{config.active_rig() or '(none set)'} — using mock")
        return mock_cls()
    chan = config.rig_channel(rig_key)
    try:
        return real_cls(chan) if chan else real_cls()
    except Exception as e:
        print(f"[main] {label} unavailable ({e}) — using mock")
        return mock_cls()


# ── base ──────────────────────────────────────────────────────────────────────

class ModuleAdapter:
    """One instrument's whole contribution to the main window."""

    key: str = ""               # a config.MODULES key
    tab_label: str = ""
    plot_label: str = ""        # empty = no Signals tab
    central_title: str = ""

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
    # A panel used while another module's page is open (the routine) gets its
    # own window instead of a settings page.
    own_window: bool = False
    own_window_size: tuple[int, int] | None = None

    def build_panel(self) -> QWidget | None:
        return None

    def build_plot(self) -> QWidget | None:
        return None

    def build_views(self) -> None:
        """Any further UI, after the panels."""

    def central_widget(self) -> QWidget | None:
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
            self.controller.set(on)
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
        """Auto recomputes percentiles every LEVELS_EVERY ticks. Manual re-passes
        the LUT bar's own levels: omitting `levels=` left pyqtgraph showing
        stale contrast."""
        if auto:
            if self._levels is None or self._level_ctr % LEVELS_EVERY == 0:
                lo, hi = np.percentile(data, (1, 99))
                self._levels = (float(lo), float(hi))
            self._level_ctr += 1
            levels = self._levels
        else:
            levels = self._hist.item.getLevels() if self._hist is not None else None
        self._img.setImage(data, autoLevels=False, levels=levels)

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

    def busy_reason(self) -> str:
        """Why the module set must not change right now, or ""."""
        return ""

    def probe_kwargs(self) -> dict[str, Any]:
        return {}
