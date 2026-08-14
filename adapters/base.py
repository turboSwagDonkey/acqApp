"""The shape every instrument takes, and the two widgets they all build from.

`ModuleAdapter` is the whole of what `MainWindow` knows about an instrument: it
calls these hooks in a fixed order and never asks what is behind them.
Subclasses override only what they have. The lifecycle is in `__init__.py`.
"""
from __future__ import annotations

from typing import Any

import pyqtgraph as pg
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from acqApp import style
from acqApp.closed_loop import SignalSource
from acqApp.acq.devices import (DeviceWorker, ModuleHost, OutputController,
                            RecordingOutput)


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


def _image_view():
    """Image + LUT bar in a row -> (image, hist, graphics_view, viewbox, row).

    The LUT bar makes contrast draggable; both cameras want exactly this layout.
    """
    img = pg.ImageItem()
    hist = pg.HistogramLUTWidget()
    hist.setImageItem(img)
    hist.setFixedWidth(86)
    gv = pg.GraphicsView()
    vb = pg.ViewBox(lockAspect=True, invertY=True)
    gv.setCentralItem(vb)
    vb.addItem(img)

    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(hist)
    lay.addWidget(gv)
    return img, hist, gv, vb, row


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
        # protocol they need, so metadata reads straight off the object instead
        # of through a getattr default that invents a value.
        self.worker: DeviceWorker | None = None
        self.controller: OutputController | None = None

    # ── construction (once, at startup) ───────────────────────────────────────
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

    def signal_sources(self) -> list[SignalSource]:
        """Live scalars this module offers a closed-loop rule. Declaring one is
        the whole cost of making a quantity triggerable."""
        return []

    def probe_kwargs(self) -> dict[str, Any]:
        """Extra arguments for probe.probe_all (e.g. the stage's serial port)."""
        return {}
