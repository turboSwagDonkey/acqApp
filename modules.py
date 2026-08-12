"""
Per-subsystem wiring for the main window.

Every instrument needs the same five things done to it, in the same order, every
session: build a settings panel, build a worker, update a display each tick,
attach a recording sink, and contribute metadata to the session file. Spelling
that out inline meant `MainWindow` carried six near-identical `if "foo" in
enabled:` branches in each of four methods — adding an instrument meant four
edits in four places, and the branches had already drifted apart.

Here each subsystem is one `ModuleAdapter` subclass that owns its whole
lifecycle, and `MainWindow` just iterates. Adding an instrument is a new class
plus one line in `ADAPTERS`.

The adapter talks to the window through a narrow surface (`status`, `add_dock`,
`register_pg_view`, `set_expected_rate`, `on_worker_error`, `cam_handle`,
`sync`) — it never reaches into the window's widgets, so the two files stay
independently readable.

Lifecycle, in call order
------------------------
    build_panel()      once, at startup -> the settings tab (or None)
    build_plot()       once, at startup -> the Signals tab (or None)
    build_views()      once, at startup -> central image / extra docks
    build_controller() at startup and whenever Emulate is toggled
    build_session()    per session: create the worker, but DO NOT start it
    start()            per session: start the worker — only after the shared
                       clock has reached t=0, so no sample is ever stamped
                       against an unstarted clock
    update_display()   ~30 Hz while running
    attach_sink()      when recording starts;  detach_sink() when it stops
    metadata()         when recording starts
    stop()             per session teardown
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from acqApp import config, style

from acqApp.voltage_cam.acquisition import OrcaFireWorker, MockCameraWorker
from acqApp.voltage_cam.settings     import SettingsPanel as CamSettingsPanel
from acqApp.voltage_cam.presets      import AcqConfig, PRESET_KEYS, DEFAULT_PRESET

from acqApp.wheel.acquisition import EncoderWorker, MockEncoderWorker
from acqApp.wheel.settings    import EncoderSettings, SettingsPanel as WheelSettingsPanel

from acqApp.pupil_cam.acquisition import MockPupilCameraWorker, PupilCameraWorker
from acqApp.pupil_cam.settings    import (PupilSettings,
                                          SettingsPanel as PupilSettingsPanel)
from acqApp.pupil_cam.control     import MockLedController, LedController
from acqApp.pupil_cam.track_worker import PupilTrackWorker, track_params

from acqApp.puffer.control import (MockPufferController, PufferController,
                                   PufferSettings, SettingsPanel as PufferPanel)

from acqApp.stage.acquisition import StagePollWorker
from acqApp.stage.control     import StageController, MockStageController
from acqApp.stage.settings    import (SettingsPanel as StageSettingsPanel,
                                      load_settings as load_stage_settings)

from acqApp.dmd.control import (DmdController, MockDmdController, DmdSettings,
                                SettingsPanel as DmdPanel)

PLOT_HISTORY = 600          # samples kept in each rolling plot
DISP_DS      = 4            # preview downsample stride
LEVELS_EVERY = 15           # recompute camera contrast levels every N display ticks


# ── shared widget builders ────────────────────────────────────────────────────

def _plot(title: str, left: str, units: str, bottom: str, key: str):
    """A rolling trace plot in the subsystem's accent colour -> (widget, curve)."""
    pw = pg.PlotWidget(title=title)
    pw.setLabel("left", left, units=units)
    pw.setLabel("bottom", bottom)
    pw.showGrid(x=True, y=True, alpha=0.3)
    curve = pw.plot(pen=pg.mkPen(style.HEX[key], width=1.5))
    return pw, curve


def _image_view():
    """Image + LUT bar in a row -> (image, hist, graphics_view, viewbox, row).

    The LUT bar is what makes contrast draggable; both cameras want exactly this
    layout, so it lives here rather than being spelled out twice.
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
    """One instrument's whole contribution to the main window.

    Subclasses override only the hooks they need: a module with no plot simply
    doesn't implement `build_plot`, and a module with no worker (puffer, DMD)
    doesn't implement `build_session`.
    """

    key: str = ""               # matches a config.MODULES key
    tab_label: str = ""         # settings tab title
    plot_label: str = ""        # Signals tab title (empty = no plot)
    central_title: str = ""     # header shown over the window's central view

    def __init__(self, win) -> None:
        self.win = win
        self.panel: QWidget | None = None
        self.worker = None
        self.controller = None

    # ── construction (once, at startup) ───────────────────────────────────────
    def build_panel(self) -> QWidget | None:
        """The settings tab for this module, or None for no tab."""
        return None

    def build_plot(self) -> QWidget | None:
        """The Signals-dock tab for this module, or None for no plot."""
        return None

    def build_views(self) -> None:
        """Any further UI — an extra dock, an overlay. Called after the panels."""

    def central_widget(self) -> QWidget | None:
        """The window's central view, if this module owns it (the primary camera)."""
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
        """Create this session's worker — but do NOT start it.

        Starting is a separate step because the shared clock has to reach t=0
        before any device pushes a timestamped sample.
        """

    def start(self) -> None:
        if self.worker is not None:
            self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def _adopt(self, worker):
        """Take ownership of a freshly built worker and surface its crashes.

        A device thread that raises would otherwise escape QThread.run() and
        take the whole process down with it (see acq/worker.py).
        """
        worker.error.connect(self.win.on_worker_error)
        self.worker = worker
        return worker

    # ── display (~30 Hz while running) ────────────────────────────────────────
    def update_display(self) -> None:
        """Pull the newest sample and paint it. Called only while running."""

    # ── recording ─────────────────────────────────────────────────────────────
    def attach_sink(self, rec) -> None:
        """Route every sample to the Recorder, which stamps it on the shared clock."""

    def detach_sink(self) -> None:
        if self.worker is not None:
            self.worker.set_sink(None)
        if self.controller is not None and hasattr(self.controller, "set_sink"):
            self.controller.set_sink(None)

    def metadata(self) -> dict[str, Any]:
        """Settings worth writing into the session file's attributes."""
        return {}

    def final_metadata(self) -> dict[str, Any]:
        """Facts only known once the recording is over — how the data actually
        came out, as opposed to how it was configured. Written just before the
        file is closed, overwriting any placeholder from `metadata()`."""
        return {}

    # ── misc ──────────────────────────────────────────────────────────────────
    def on_trigger(self, name: str, duration: float) -> None:
        """A scheduled trigger fired on the shared clock."""

    def probe_kwargs(self) -> dict[str, Any]:
        """Extra arguments for probe.probe_all (e.g. the stage's serial port)."""
        return {}


# ── voltage camera ────────────────────────────────────────────────────────────

class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"
    tab_label = "Voltage cam (primary)"
    plot_label = "ΔF/F"
    central_title = "Voltage camera — primary"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._curve = None
        self._y: list[float] = []
        self._f0: float | None = None
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = CamSettingsPanel(self._load_config())
        self.panel.exposure_changed.connect(self._on_exposure)
        for sig in (self.panel.exposure_changed, self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed):
            sig.connect(self._save)
        # The Save tab's capacity estimate is driven by the data rate, and the
        # data rate is what these three settings decide.
        for sig in (self.panel.exposure_changed, self.panel.resolution_changed,
                    self.panel.binning_changed):
            sig.connect(self._push_rate)
        self._push_rate()
        return self.panel

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("ΔF/F", "ΔF/F", "%", "Frame", self.key)
        return pw

    def central_widget(self) -> QWidget:
        self._img, hist, gv, _vb, row = _image_view()
        self.win.register_pg_view(gv)
        self.win.register_pg_view(hist)
        return row

    @staticmethod
    def _load_config() -> AcqConfig:
        cfg = config.load_dataclass(AcqConfig, "voltage_cam")
        if cfg.preset_key not in PRESET_KEYS:      # a preset may have been removed
            cfg.preset_key = DEFAULT_PRESET
        return cfg

    def _save(self, *_a) -> None:
        config.save_settings("voltage_cam", asdict(self.panel.get_config()))

    def _push_rate(self, *_a) -> None:
        """Feed the acquisition data rate to the Save tab's capacity estimate."""
        cfg = self.panel.get_config()
        self.win.set_expected_rate(cfg.frame_bytes * cfg.expected_fps / (1 << 20))

    def _on_exposure(self, us: float) -> None:
        if self.worker is not None:
            self.worker.set_exposure(us)

    # (binning is structural: it only takes effect on the next Start, because the
    # panel locks resolution/binning/trigger for the whole session.)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        cfg = self.panel.get_config()
        # Reuse the handle opened once at startup: re-opening a just-closed DCAM
        # device crashes the driver natively, and a fresh open costs ~7 s.
        worker = (MockCameraWorker(cfg) if emulate
                  else OrcaFireWorker(0, cfg, cam=self.win.cam_handle))
        self._adopt(worker)
        if isinstance(worker, OrcaFireWorker):
            worker.drops_update.connect(
                lambda skipped, _buf: self.win.status(
                    f"camera dropped {skipped} frames — reading too slowly"))
            # Show the camera's REAL measured rate, not the datasheet estimate.
            worker.timing_update.connect(self.panel.set_measured_rate)
        self.panel.set_running(True)
        self._y.clear()
        self._f0 = None
        self._levels = None
        self._level_ctr = 0

    def stop(self) -> None:
        super().stop()
        self.panel.set_running(False)
        self.panel.set_measured_rate(None)          # back to the estimate

    # ── display ──
    def update_display(self) -> None:
        f = self.worker.get_latest() if self.worker is not None else None
        if f is None:
            return
        small = f[::DISP_DS, ::DISP_DS]              # strided view, no copy
        # The percentile is the costly part, so refresh contrast a couple of
        # times a second rather than every frame.
        if self._levels is None or self._level_ctr % LEVELS_EVERY == 0:
            lo, hi = np.percentile(small, (1, 99))
            self._levels = (float(lo), float(hi))
        self._level_ctr += 1
        self._img.setImage(small, autoLevels=False, levels=self._levels)

        mean = float(small.mean())
        if self._f0 is None and mean != 0:
            self._f0 = mean
        df = (mean - self._f0) / self._f0 * 100 if self._f0 else 0.0
        self._y.append(df)
        del self._y[:-PLOT_HISTORY]
        self._curve.setData(self._y)

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(item) -> None:
            """The worker sends (frame, acquired_at, index).

            `acquired_at` is when the CAMERA says the frame was taken, which is
            not when this batch reached us — passing it through is what keeps
            the recorded frame times at the true frame rate. The index is the
            camera's own counter, recorded as its own stream so a dropped frame
            shows up as a jump rather than silently closing the gap.
            """
            frame, at, index = item
            rec.put("voltage_cam", frame, at=at)
            if index is not None:
                rec.put("voltage_cam_index", float(index), at=at)

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        cfg = self.panel.get_config()
        return {"cam_preset":      cfg.preset_key,
                "cam_binning":     cfg.binning,
                "cam_exposure_us": cfg.exposure_us,
                "cam_trigger":     cfg.trigger_mode,
                # Placeholder: no frame has arrived yet to settle it. Overwritten
                # by final_metadata(), and left here so the attribute still
                # exists if the app dies mid-recording.
                "cam_timestamp_source": getattr(self.worker, "timestamp_source",
                                                "unknown")}

    def final_metadata(self) -> dict[str, Any]:
        return {
            # Whether voltage_cam/timestamps are the camera's own per-frame
            # stamps ("camera") or the times we read them ("arrival") — the
            # difference decides how much the frame timing can be trusted.
            "cam_timestamp_source": getattr(self.worker, "timestamp_source",
                                            "unknown"),
            # Frames the CAMERA discarded because we read too slowly. These are
            # gone from the file; the gap is visible in voltage_cam_index.
            "cam_dropped_frames": getattr(self.worker, "skipped_frames", 0),
        }


# ── pupil camera ──────────────────────────────────────────────────────────────

class PupilCamModule(ModuleAdapter):
    key = "pupil_cam"
    tab_label = "Pupil cam"
    plot_label = "Pupil"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._overlay = None
        self._curve = None
        self._y: list[float] = []
        # Tracking runs on its own thread (see pupil_cam/track_worker.py): it is
        # the only consumer of the camera worker's frames and hands the GUI each
        # frame together with the fit made from it.
        self._track: PupilTrackWorker | None = None
        self._theta = np.linspace(0, 2 * np.pi, 48)

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = PupilSettingsPanel(
            config.load_dataclass(PupilSettings, self.key))
        self.panel.exposure_changed.connect(self._on_exposure)
        self.panel.led_toggled.connect(self._on_led)
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        if self._track is not None:
            # Queued, not written: the tracker belongs to its own thread.
            self._track.configure(**track_params(s))

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("Pupil radius", "Radius", "px", "Frame", self.key)
        return pw

    def build_views(self) -> None:
        self._img, hist, gv, vb, row = _image_view()
        # Pupil frames are 8-bit, so pin the histogram to 0–255: the bar then
        # shows an absolute brightness scale instead of rescaling to each frame,
        # and the handles still drag to adjust contrast.
        self._img.setLevels((0, 255))
        hist.setHistogramRange(0, 255)
        hist.setLevels(0, 255)
        self.win.register_pg_view(hist)
        self.win.register_pg_view(gv)

        self._overlay = pg.PlotCurveItem(pen=pg.mkPen(style.HEX[self.key], width=2))
        vb.addItem(self._overlay)
        self.win.add_dock("Pupil cam", row, Qt.DockWidgetArea.RightDockWidgetArea,
                          accent=self.key)

    # ── controllers ──
    def build_controller(self, emulate: bool) -> None:
        if emulate:
            self.controller = MockLedController()
            return
        try:
            self.controller = LedController()
        except Exception as e:
            print(f"[main] eye-tracking LED unavailable ({e}) — using mock")
            self.controller = MockLedController()

    def _on_led(self, on: bool) -> None:
        if self.controller is not None:
            self.controller.set(on)

    def _on_exposure(self, us: float) -> None:
        # Hot-apply only if the running worker supports it (the real Basler does;
        # the mock has a fixed synthetic exposure).
        if self.worker is not None and hasattr(self.worker, "set_exposure"):
            self.worker.set_exposure(us)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings
        if emulate:
            cam = self._adopt(MockPupilCameraWorker(fps=s.fps))
        else:
            # cam=None → the worker opens/closes its own Basler on its thread.
            cam = self._adopt(PupilCameraWorker(exposure_us=s.exposure_us,
                                                fps=s.fps))
        # A fresh tracker per session, so no annulus lock is carried across one.
        self._track = PupilTrackWorker(cam.get_latest, history=PLOT_HISTORY)
        self._track.error.connect(self.win.on_worker_error)
        self._track.configure(**track_params(s))
        self._y.clear()

    def start(self) -> None:
        super().start()                 # camera first; the tracker idles until
        if self._track is not None:     # there is something to track
            self._track.start()

    def stop(self) -> None:
        if self._track is not None:     # stop the consumer before the producer
            self._track.stop()
            self._track = None
        super().stop()

    # ── display ──
    def update_display(self) -> None:
        if self._track is None:
            return
        radii = self._track.take_radii()
        if radii:
            self._y.extend(radii)
            del self._y[:-PLOT_HISTORY]
            self._curve.setData(self._y)

        pair = self._track.get_latest()
        if pair is None:
            return
        frame, res = pair               # the fit belongs to THIS frame
        # No `levels=` here: the LUT bar owns the levels, so forcing them every
        # frame would undo any contrast the user drags.
        self._img.setImage(frame, autoLevels=False)
        self._draw_outline(res)

    def _draw_outline(self, res) -> None:
        if res.center_x is None or res.radius is None:
            self._overlay.setData([], [])
            return
        th = self._theta                              # precomputed once
        cx, cy = float(res.center_x), float(res.center_y or 0.0)
        if res.axes is None:
            self._overlay.setData(cx + res.radius * np.cos(th),
                                  cy + res.radius * np.sin(th))
            return
        a, b = res.axes
        t = np.radians(float(res.angle or 0.0))
        ca, sa = np.cos(t), np.sin(t)
        u, v = a * np.cos(th), b * np.sin(th)
        self._overlay.setData(cx + u * ca - v * sa, cy + u * sa + v * ca)

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is not None:
            self.worker.set_sink(lambda fr: rec.put("pupil_cam", fr))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"pupil_exposure_us": s.exposure_us,
                "pupil_fps":         s.fps,
                "pupil_threshold":   s.threshold,
                "pupil_min_r":       s.min_r,
                "pupil_max_r":       s.max_r}


# ── wheel encoder ─────────────────────────────────────────────────────────────

class WheelModule(ModuleAdapter):
    key = "wheel"
    tab_label = "Wheel"
    plot_label = "Wheel"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._plot_w = None
        self._curve = None
        self._y: list[float] = []
        self._units: str | None = None       # current Y-axis unit label

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = WheelSettingsPanel(
            config.load_dataclass(EncoderSettings, self.key))
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def build_plot(self) -> QWidget:
        self._plot_w, self._curve = _plot(
            "Wheel distance", "Distance", "m", "Sample", self.key)
        return self._plot_w

    def _on_settings(self, st) -> None:
        """Push live V/rev and wheel-diameter changes to a running worker.

        These two are the scaling constants for every wheel number in the
        session file, and both are still unmeasured on this rig — so they are
        also the values most likely to be silently wrong if they reset.
        """
        config.save_settings(self.key, asdict(st))
        if self.worker is not None:
            self.worker.set_scaling(st.volts_per_rev, st.wheel_dia_mm)

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings
        if emulate:
            self._adopt(MockEncoderWorker(s.volts_per_rev, s.wheel_dia_mm))
        else:
            self._adopt(EncoderWorker(s.channel, s.rate,
                                      s.volts_per_rev, s.wheel_dia_mm))
        self._y.clear()

    # ── display ──
    def update_display(self) -> None:
        sample = self.worker.get_latest() if self.worker is not None else None
        if sample is None:
            return
        v, speed, dist, _t = sample      # the worker already derived speed+distance
        self._y.append(self._show(v, speed, dist))
        del self._y[:-PLOT_HISTORY]
        self._curve.setData(self._y)

    def _show(self, v: float, speed: float, dist: float) -> float:
        """Pick units/labels for the current scaling, update the live readout,
        and return the value to plot. With no V/rev set there is nothing to
        derive, so it plots the raw voltage instead."""
        s = self.panel.settings
        if not s.volts_per_rev:
            self._axis("Voltage", "V")
            self._title(None, "")
            self.panel.set_readout(f"{v:.4f} V   (set V/rev to get speed)")
            return v
        if s.wheel_dia_mm:
            self._axis("Distance", "m")
            self._title(speed, "mm/s")
            self.panel.set_readout(
                f"speed {speed:+.1f} mm/s      net {dist / 1000:+.2f} m")
            return dist / 1000.0                 # plot net distance in metres
        self._axis("Distance", "rev")
        self._title(speed, "rev/s")
        self.panel.set_readout(f"speed {speed:+.2f} rev/s      net {dist:+.1f} rev")
        return dist                              # plot net distance in revolutions

    def _axis(self, name: str, units: str) -> None:
        """Relabel the Y axis only when the unit actually changes."""
        if self._units == units:
            return
        self._units = units
        self._plot_w.setLabel("left", name, units=units)

    def _title(self, speed: float | None, units: str) -> None:
        """Show the live speed as a number in the distance plot's title."""
        if speed is None:
            self._plot_w.setTitle("Wheel distance")
        else:
            prec = 1 if units == "mm/s" else 2
            self._plot_w.setTitle(
                f"Wheel distance   —   speed {speed:+.{prec}f} {units}")

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(sample: tuple[float, float, float, float | None]) -> None:
            """Raw voltage + derived speed + distance: three scalar streams
            sharing the one timebase.

            `at` is when the DAQ sampled the voltage, not when the block
            carrying it reached us — hardware-timed reads arrive in batches, so
            stamping on arrival would quantise the wheel's timebase to the read
            cadence exactly as it did for the camera (#1).
            """
            v, speed, dist, at = sample
            rec.put("wheel_voltage", v, at=at)
            rec.put("wheel_speed", speed, at=at)
            rec.put("wheel_distance", dist, at=at)

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        linear = bool(s.volts_per_rev and s.wheel_dia_mm)
        return {
            "wheel_channel":        s.channel,
            "wheel_rate_hz":        s.rate,
            "wheel_volts_per_rev":  s.volts_per_rev or 0.0,
            "wheel_dia_mm":         s.wheel_dia_mm or 0.0,
            "wheel_speed_units":    "mm/s" if linear else "rev/s",
            "wheel_distance_units": "mm"   if linear else "rev",
            "wheel_distance_mode":  "net_forward",   # signed; back-spin subtracts
            # Derived speed/distance are computed with look-ahead and so lag the
            # (live) voltage stream by this many seconds. _SIGN orients forward.
            "wheel_speed_lag_s":    EncoderWorker._LAG_S,
            "wheel_sign":           EncoderWorker._SIGN,
        }

    def final_metadata(self) -> dict[str, Any]:
        # Which timebase the samples actually carry is only known once the
        # worker has configured the board — and it decides whether the recorded
        # speed is a hardware measurement or a scheduler artefact.
        return {
            "wheel_timestamp_source": getattr(self.worker, "timestamp_source",
                                              "unknown"),
            "wheel_rate_actual_hz":   getattr(self.worker, "actual_rate", 0.0),
        }


# ── puffer ────────────────────────────────────────────────────────────────────

class PufferModule(ModuleAdapter):
    key = "puffer"
    tab_label = "Puffer"

    def build_panel(self) -> QWidget:
        self.panel = PufferPanel(config.load_dataclass(PufferSettings, self.key))
        self.panel.test_requested.connect(self.fire)
        self.panel.settings_changed.connect(self._on_settings)
        # Arm the shared-clock trigger bus: the puff fires at t = at_s and is
        # logged on the session clock (trigger_fired -> on_trigger -> fire).
        self.panel.schedule_requested.connect(
            lambda at_s, dur: self.win.sync.schedule_trigger(self.key, at_s, dur))
        self.panel.clear_schedule_requested.connect(self.win.sync.clear_triggers)
        return self.panel

    def build_controller(self, emulate: bool) -> None:
        # Built from the panel, so the configured DO line is the one that fires.
        s = self.panel.settings if self.panel is not None else None
        self.controller = (MockPufferController(s) if emulate
                           else PufferController(s))

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        if self.controller is not None:
            self.controller.apply_settings(s)

    def fire(self, duration: float | None = None) -> None:
        """Fire now. With no duration this uses the panel's Duration value —
        which is what "Test puff" is for: verifying the puff you scheduled."""
        if self.controller is not None:
            self.controller.fire(duration)

    def on_trigger(self, name: str, duration: float) -> None:
        if name == self.key:
            self.fire(duration)

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            self.controller.set_sink(lambda d: rec.put("puffer", d))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"puffer_channel":    s.channel,
                "puffer_duration_s": s.duration_s}


# ── XY stage ──────────────────────────────────────────────────────────────────

class StageModule(ModuleAdapter):
    key = "stage"
    tab_label = "Stage"
    # No plot: live position is the X/Y readout in the Stage tab, and it is
    # recorded as stage_x_um / stage_y_um.

    # Everything else about the stage (axis calibration, soft limits, the
    # origin) lives in the config shared with the standalone stage_control app
    # and must keep coming from there — StageSettings nests two StageAxis
    # objects, which do not survive a round trip through this config's flat
    # JSON. Only the two fields the panel itself owns are persisted here.
    _PANEL_KEYS = ("port", "poll_hz")

    def build_panel(self) -> QWidget:
        s = load_stage_settings()
        saved = config.load_settings(self.key)
        if isinstance(saved.get("port"), str) and saved["port"].strip():
            s.port = saved["port"]
        try:
            hz = float(saved.get("poll_hz"))
        except (TypeError, ValueError):
            pass
        else:
            if hz > 0:
                s.poll_hz = hz
        self.panel = StageSettingsPanel(s)
        self.panel.settings_changed.connect(self._save)
        return self.panel

    def _save(self, s) -> None:
        config.save_settings(self.key,
                             {k: getattr(s, k) for k in self._PANEL_KEYS})

    def probe_kwargs(self) -> dict[str, Any]:
        try:
            return {"stage_port": self.panel.settings.port}
        except Exception:
            return {}

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        s = self.panel.settings if self.panel is not None else load_stage_settings()
        ctrl = MockStageController(s) if emulate else StageController(s)
        try:
            ctrl.connect()
        except Exception as e:
            self.win.status(f"stage: could not connect ({e})")
            return
        self.controller = ctrl
        self._adopt(StagePollWorker(ctrl, s.poll_hz))
        self.panel.bind_controller(ctrl)

    def stop(self) -> None:
        super().stop()
        # Release the stage: unbind the controls first, then close the link.
        self.panel.bind_controller(None)
        if self.controller is not None:
            self.controller.close()
            self.controller = None

    def close_controller(self) -> None:
        pass        # the stage link is session-scoped, not an always-on output

    def update_display(self) -> None:
        xy = self.worker.get_latest() if self.worker is not None else None
        if xy is not None:
            self.panel.set_readout(xy[0], xy[1])

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(xy: tuple[float, float]) -> None:
            """Position is a 2-vector → two scalar streams sharing the timebase."""
            rec.put("stage_x_um", xy[0])
            rec.put("stage_y_um", xy[1])

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"stage_port": s.port, "stage_poll_hz": s.poll_hz}


# ── DMD ───────────────────────────────────────────────────────────────────────

class DmdModule(ModuleAdapter):
    key = "dmd"
    tab_label = "DMD"

    def build_panel(self) -> QWidget:
        self.panel = DmdPanel(config.load_dataclass(DmdSettings, self.key))
        # Route through this adapter, not straight to the controller: the
        # controller is rebuilt whenever Emulate is toggled, and the panel binds
        # its signals only once.
        self.panel.load_requested.connect(self.load)
        self.panel.display_requested.connect(self.display)
        self.panel.stop_requested.connect(self.stop_display)
        self.panel.settings_changed.connect(self._save)
        return self.panel

    def _save(self, s) -> None:
        d = asdict(s)
        d["pattern_path"] = str(s.pattern_path) if s.pattern_path else None
        config.save_settings(self.key, d)

    def build_controller(self, emulate: bool) -> None:
        self.controller = MockDmdController() if emulate else DmdController()
        # A pattern the panel is showing — restored from the config, or loaded
        # before Emulate was toggled — has to be uploaded to this controller
        # too, or Display projects whatever the fresh controller defaults to
        # while the panel names a file.
        path = self.panel.settings.pattern_path if self.panel is not None else None
        if path is not None and path.exists():
            self.controller.load_pattern(path)

    def load(self, path) -> None:
        if self.controller is not None:
            self.controller.load_pattern(path)

    def display(self) -> None:
        """Apply the panel's current settings, then start displaying."""
        if self.controller is not None:
            self.controller.apply_settings(self.panel.settings)
            self.controller.display()

    def stop_display(self) -> None:
        if self.controller is not None:
            self.controller.stop()

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            # DMD frames are logged straight from its thread (no GUI-thread hop),
            # stamped on the shared clock like every other stream.
            self.controller.set_sink(lambda idx: rec.put("dmd", float(idx)))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"dmd_on_time_ms":  s.on_time_ms,
                "dmd_static_hold": s.static_hold,
                "dmd_trigger":     s.trigger_mode}


# ── registry ──────────────────────────────────────────────────────────────────
# Keys must match config.MODULES; the window builds adapters in MODULES order.

ADAPTERS: dict[str, Callable[[Any], ModuleAdapter]] = {
    "voltage_cam": VoltageCamModule,
    "pupil_cam":   PupilCamModule,
    "wheel":       WheelModule,
    "puffer":      PufferModule,
    "stage":       StageModule,
    "dmd":         DmdModule,
}


def build_adapters(win, enabled) -> list[ModuleAdapter]:
    """Adapters for the enabled modules, in config.MODULES display order."""
    return [ADAPTERS[k](win) for k in config.MODULES
            if k in enabled and k in ADAPTERS]
