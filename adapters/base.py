"""The shape every instrument takes, and the two widgets they all build from.

`ModuleAdapter` is the whole of what `MainWindow` knows about an instrument: it
calls these hooks in a fixed order and never asks what is behind them.
Subclasses override only what they have. The lifecycle is in `__init__.py`.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout, QWidget

from acqApp import config, style
from acqApp.closed_loop import SignalSource
from acqApp.acq.devices import (DeviceWorker, LedTarget, ModuleHost, OutputController,
                            PatternTarget, PufferTarget, RecordingOutput, StageTarget)


PLOT_HISTORY = 600          # samples kept in each rolling plot
DISP_DS      = 4            # preview downsample stride
LEVELS_EVERY = 15           # recompute camera contrast levels every N ticks


# ── shared widget builders ────────────────────────────────────────────────────

def _plot(title: str, left: str, units: str, bottom: str, key: str):
    """A rolling trace in the subsystem's accent colour -> (widget, curve)."""
    pw = pg.PlotWidget(title=title)
    pw.setLabel("left", left, units=units)
    pw.setLabel("bottom", bottom)
    pw.showGrid(x=True, y=True, alpha=0.3)
    curve = pw.plot(pen=pg.mkPen(style.HEX[key], width=1.5))
    return pw, curve


class DragRectViewBox(pg.ViewBox):
    """A ViewBox where an armed left-drag draws a rectangle instead of panning.

    A real override, not a monkeypatch: earlier the pupil eye-region tool
    swapped `vb.mouseDragEvent` for a bound method and swapped it back on
    disarm — works, but depends on nobody else touching that attribute
    between calls. Subclassing needs no restoring — `set_draw_mode` just
    falls through to `super().mouseDragEvent()` when off, or for any button
    but Left, so panning and zooming are never something to remember.
    """

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


def _image_view(vb_cls: type[pg.ViewBox] = pg.ViewBox):
    """Image + LUT bar in a row ->
    (image, hist, chk_auto, graphics_view, viewbox, row).

    The LUT bar makes contrast draggable; both cameras want exactly this
    layout. `vb_cls` swaps in a ViewBox subclass (e.g. `DragRectViewBox`) for
    a caller that needs more than pan/zoom from it.

    `chk_auto` sits right above the LUT bar it controls, not off in the
    settings tab — operator is already looking at it. The caller wires
    it to the same auto-contrast path as the settings panel's own checkbox
    and keeps the two in sync (see voltage_cam/pupil_cam's central_widget).
    """
    img = pg.ImageItem()
    hist = pg.HistogramLUTWidget()
    hist.setImageItem(img)
    hist.setFixedWidth(86)
    gv = pg.GraphicsView()
    vb = vb_cls(lockAspect=True, invertY=True)
    gv.setCentralItem(vb)
    vb.addItem(img)

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
    return img, hist, chk_auto, gv, vb, row


def led_controller(emulate: bool, rig_key: str, real_cls, mock_cls, label: str):
    """Open a DAQ-backed LED, or its mock twin, saying which and why.

    A rig whose profile says the LED isn't fitted gets the mock with no DAQ
    attempt at all: opening a line on a rig that has no such LED only ever
    produced a nidaqmx traceback at every startup. Both cameras' LEDs are
    wired this way and differ only in the three names.
    """
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

    key: str = ""               # matches a config.MODULES key
    tab_label: str = ""         # settings tab title
    plot_label: str = ""        # Signals tab title (empty = no plot)
    central_title: str = ""     # header shown over the window's central view

    def __init__(self, win: ModuleHost) -> None:
        self.win = win
        self.panel: QWidget | None = None
        # Declared, not duck-typed (devices.py). Subclasses narrow these to the
        # protocol they need, so metadata reads off the object instead of a
        # getattr default that invents a value.
        self.worker: DeviceWorker | None = None
        self.controller: OutputController | None = None
        # The "Auto" checkbox _image_view() builds into the LUT bar, for a
        # module with a preview — set by whoever calls central_widget()/
        # build_views(). See _sync_auto_to_lut/_sync_auto_from_lut.
        self._chk_auto_lut: QCheckBox | None = None
        # Preview state, shared by every camera-shaped module; _paint() below
        # is the only thing that reads them. A non-camera leaves them None.
        self._img = None                 # the pg.ImageItem being painted
        self._hist = None                # its LUT bar, for show/hide + levels
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0

    # ── construction (once, at startup) ───────────────────────────────────────
    # A panel that belongs in a window of its own rather than as a page of the
    # shared settings window. For a module that is *used* while another
    # module's page is open — a routine is driven while the camera it drives is
    # being adjusted, and a tab can't be in two places. The window reads this;
    # it never learns which module set it.
    own_window: bool = False
    # (w, h) that window opens at until the operator resizes it; None sizes it
    # from the panel.
    own_window_size: tuple[int, int] | None = None

    def build_panel(self) -> QWidget | None:
        """The settings tab for this module, or None."""
        return None

    def build_plot(self) -> QWidget | None:
        """The Signals-dock tab for this module, or None."""
        return None

    def build_views(self) -> None:
        """Any further UI. Called after the panels."""

    def central_widget(self) -> QWidget | None:
        """The window's central view, if this module owns it."""
        return None

    # ── persistent output controllers (rebuilt when Emulate is toggled) ───────
    def build_controller(self, emulate: bool) -> None:
        """Create the always-on output device (puffer, LED, DMD) for this mode."""

    def close_controller(self) -> None:
        if self.controller is not None:
            try:
                self.controller.close()
            except Exception:
                pass
            self.controller = None

    # ── illumination / preview (shared by every camera-shaped module) ─────────
    def _apply_led_follow(self, on: bool) -> None:
        """Fire the controller and sync the panel's own LED checkbox —
        called from start()/stop() by any module offering Follow Live
        view, so the checkbox still shows the truth without the caller
        toggling it manually (LedController.set() covers on/off either
        way — no need to pick between .on()/.off())."""
        if self.controller is not None:
            self.controller.set(on)
        if self.panel is not None:
            self.panel.set_led(on)

    def _sync_auto_from_lut(self, on: bool) -> None:
        """The LUT bar's own "Auto" checkbox changed -> push it to the
        settings tab's, which is the one actually wired to persist +
        apply (a no-op, not a loop, once the two already agree)."""
        if self.panel is not None:
            self.panel._chk_auto.setChecked(on)

    def _sync_auto_to_lut(self, on: bool) -> None:
        """The settings tab's "Auto contrast" changed -> push it to the
        LUT bar's own checkbox (a no-op, not a loop, once agreed)."""
        if self._chk_auto_lut is not None:
            self._chk_auto_lut.setChecked(on)

    def _reset_levels(self) -> None:
        """Drop the cached contrast so the next paint recomputes it — at a
        session boundary, or when Auto is switched back on, where reusing
        last session's percentiles shows the new frames at stale contrast."""
        self._levels = None
        self._level_ctr = 0

    def _paint(self, data, auto: bool) -> None:
        """Show one frame in `self._img` at the right contrast.

        Auto recomputes the percentile every LEVELS_EVERY ticks, not every
        frame — the percentile is the costly part, and contrast a couple of
        times a second is enough. Manual re-passes the LUT bar's OWN current
        levels rather than omitting `levels=`: pyqtgraph only reliably
        re-renders the mapping onto new frame data when setLevels() is
        actually called, and leaving it out stuck the display on stale
        contrast until the operator dragged the bar themselves.
        """
        if auto:
            if self._levels is None or self._level_ctr % LEVELS_EVERY == 0:
                lo, hi = np.percentile(data, (1, 99))
                self._levels = (float(lo), float(hi))
            self._level_ctr += 1
            levels = self._levels
        else:
            levels = self._hist.item.getLevels() if self._hist is not None else None
        self._img.setImage(data, autoLevels=False, levels=levels)

    # ── session ───────────────────────────────────────────────────────────────
    def build_session(self, emulate: bool) -> None:
        """Create this session's worker — but do NOT start it: the shared clock
        has to reach t=0 before any device pushes a timestamped sample."""

    def start(self) -> None:
        if self.worker is not None:
            self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def _adopt(self, worker):
        """Own a freshly built worker and surface its crashes — an escaping
        exception would take the process down (see acq/worker.py)."""
        worker.error.connect(self.win.on_worker_error)
        self.worker = worker
        return worker

    # ── display (~30 Hz while running) ────────────────────────────────────────
    def update_display(self) -> None:
        """Pull the newest sample and paint it. Only called while running."""

    def last_frame(self):
        """The last frame this module displayed, or None if it has none.

        Cached by the adapter rather than re-read from the worker, because
        `get_latest()` *consumes*: asking the worker would usually return None
        (the display tick got there first) and would otherwise steal a frame
        from this module's own preview. Cameras override; everything else has
        no frame to give.
        """
        return None

    # ── recording ─────────────────────────────────────────────────────────────
    def attach_sink(self, rec) -> None:
        """Route samples to the Recorder, which stamps them on the shared clock."""

    def detach_sink(self) -> None:
        if self.worker is not None:
            self.worker.set_sink(None)
        # The LED is an output controller but deliberately not a
        # RecordingOutput: its state is illumination, not an experimental event.
        if isinstance(self.controller, RecordingOutput):
            self.controller.set_sink(None)

    def metadata(self) -> dict[str, Any]:
        """Settings worth writing into the session file's attributes."""
        return {}

    def final_metadata(self) -> dict[str, Any]:
        """How the data actually came out, as opposed to how it was configured.
        Written just before the close, overwriting `metadata()`'s placeholder."""
        return {}

    # ── misc ──────────────────────────────────────────────────────────────────
    def on_trigger(self, name: str, duration: float) -> None:
        """A trigger fired on the shared bus — scheduled, or from the loop."""

    def on_modules_changed(self) -> None:
        """Another module was loaded or unloaded while this one was running.

        Anything derived from the *set* of neighbours goes stale here — the
        closed loop's source and target lists above all.
        """

    def signal_sources(self) -> list[SignalSource]:
        """Live scalars this module offers a closed-loop rule. Declaring one is
        the whole cost of making a quantity triggerable."""
        return []

    def stage_target(self) -> StageTarget | None:
        """If this module can be sent to an XY position, itself. Declaring it's
        the whole cost of letting an experiment routine drive it."""
        return None

    def pattern_target(self) -> PatternTarget | None:
        """If this module can put a pattern up and take it down, itself."""
        return None

    def led_target(self) -> LedTarget | None:
        """If this module can switch illumination on and off, itself."""
        return None

    def puffer_target(self) -> PufferTarget | None:
        """If this module can fire an air puff, itself."""
        return None

    def frame_rate_hz(self) -> float | None:
        """The rate this module's camera is configured to run at, or None.

        For *estimating* only — a step measured in frames is still never
        converted where it's recorded (`routines/settings.py`).
        """
        return None

    def busy_reason(self) -> str:
        """Why the module set must not change right now, or "".

        `set_modules` already refuses while recording; a running routine is the
        second such case, and asking every adapter keeps the window from
        learning what a routine is.
        """
        return ""

    def probe_kwargs(self) -> dict[str, Any]:
        """Extra arguments for probe.probe_all (e.g. the stage's serial port)."""
        return {}
