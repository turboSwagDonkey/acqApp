"""
The voltage camera's adapter — the module that owns the window's central view.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from typing import Any

import numpy as np
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import CameraWorker
from acqApp.adapters.base import (DISP_DS, LEVELS_EVERY, PLOT_HISTORY,
                                 ModuleAdapter, _image_view, _plot)
from acqApp.devices.voltage_cam.acquisition import MockCameraWorker, OrcaFireWorker
from acqApp.devices.voltage_cam.led import LedController, MockLedController
from acqApp.devices.voltage_cam.presets import (AcqConfig, DEFAULT_PRESET,
                                                PRESET_KEYS, TRIGGER_MODES,
                                                WRITER_MBPS)
from acqApp.devices.voltage_cam.panel import SettingsPanel as CamSettingsPanel

# TRIGGER_MODES[i] rather than a second literal, so this and the panel's
# combo cannot say different things about how the two modes are spelled.
_INT_TRIGGER, _EXT_TRIGGER = TRIGGER_MODES[0], TRIGGER_MODES[1]


class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"
    tab_label = "Voltage cam (primary)"
    plot_label = "ΔF/F"
    central_title = "Voltage camera — primary"

    worker: CameraWorker | None          # narrows ModuleAdapter.worker

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._hist = None                # the histogram/LUT bar, for show/hide
        self._curve = None
        self._y: list[float] = []
        self._f0: float | None = None
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0
        self._auto_levels = True         # AcqConfig's default, until build_panel says otherwise
        self._last_frame = None         # full-res, for the DMD's ROI editor
        self._preview_buf: deque = deque(maxlen=1)   # recent preview frames, for averaging

    def last_frame(self):
        return self._last_frame

    # ── the sensor's capture area ──
    def preset_key(self) -> str:
        return self.panel.get_config().preset_key

    def set_preset(self, key: str) -> None:
        """Change the resolution preset. Structural — like the operator's own
        combo click, it only takes effect the next time the session (re)starts;
        the caller decides whether that means a live-view restart."""
        self.panel.set_preset(key)

    def set_exposure(self, us: float) -> None:
        """Change exposure (e.g. from a Mode preset). Hot — like the
        operator's own spinbox edit, it takes effect immediately via the
        panel's existing exposure_changed -> _on_exposure wiring."""
        self.panel.set_exposure(us)

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = CamSettingsPanel(self._load_config())
        self.panel.exposure_changed.connect(self._on_exposure)
        for sig in (self.panel.exposure_changed, self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed,
                    self.panel.lut_visible_changed,
                    self.panel.auto_levels_changed,
                    self.panel.preview_avg_changed,
                    self.panel.led_follow_changed):
            sig.connect(self._save)
        self.panel.lut_visible_changed.connect(self._on_lut_visible)
        self.panel.auto_levels_changed.connect(self._on_auto_levels)
        self.panel.preview_avg_changed.connect(self._on_preview_avg)
        self.panel.led_toggled.connect(self._on_led)
        cfg = self.panel.get_config()
        self._auto_levels = cfg.auto_levels
        self._preview_buf = deque(maxlen=max(1, cfg.preview_avg))
        # Startup builds the central widget BEFORE the settings tab (so
        # central_widget() cannot read the panel yet); a hot-load builds this
        # panel first instead. Whichever runs second is what makes the saved
        # preference actually reach a `self._hist` that may already exist.
        if self._hist is not None:
            self._hist.setVisible(cfg.show_lut)
        if self._chk_auto_lut is not None:
            self._chk_auto_lut.setChecked(cfg.auto_levels)
        # The Save tab's capacity estimate is driven by the data rate, and the
        # data rate is what these three settings decide.
        for sig in (self.panel.exposure_changed, self.panel.resolution_changed,
                    self.panel.binning_changed):
            sig.connect(self._push_rate)
        self._push_rate()
        return self.panel

    # ── illumination ──
    def build_controller(self, emulate: bool) -> None:
        if emulate:
            self.controller = MockLedController()
            return
        # Not fitted is the operator's claim in rigs.json, so don't touch the
        # DAQ at all: opening a line on a rig that has no primary LED only
        # ever produced a multi-line nidaqmx traceback at every startup.
        if not config.rig_has("primary_led"):
            print(f"[main] primary LED not fitted on rig "
                  f"{config.active_rig() or '(none set)'} — using mock")
            self.controller = MockLedController()
            return
        chan = config.rig_channel("primary_led")
        try:
            self.controller = (LedController(chan) if chan
                               else LedController())
        except Exception as e:
            print(f"[main] primary LED unavailable ({e}) — using mock")
            self.controller = MockLedController()

    def _on_led(self, on: bool) -> None:
        if self.controller is not None:
            self.controller.set(on)

    # ── what an experiment routine may drive (acq.devices.LedTarget) ──
    def led_target(self):
        return self if self.controller is not None else None

    def set_led(self, on: bool) -> None:
        self._on_led(on)

    def _on_lut_visible(self, on: bool) -> None:
        if self._hist is not None:
            self._hist.setVisible(on)

    def _on_auto_levels(self, on: bool) -> None:
        self._auto_levels = bool(on)
        if on:                          # recompute fresh, not a stale cache
            self._levels = None
            self._level_ctr = 0
        self._sync_auto_to_lut(on)

    def _on_preview_avg(self, n: int) -> None:
        # A new maxlen needs a new deque — changing maxlen on an existing one
        # isn't supported, and reusing it would blend old-N and new-N frames.
        self._preview_buf = deque(maxlen=max(1, n))

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("ΔF/F", "ΔF/F", "%", "Frame", self.key)
        return pw

    def central_widget(self) -> QWidget:
        self._img, hist, chk_auto, gv, _vb, row = _image_view()
        self._hist = hist
        self._chk_auto_lut = chk_auto
        self._chk_auto_lut.toggled.connect(self._sync_auto_from_lut)
        # Guarded: at startup this runs BEFORE build_panel() (main.py builds
        # the central widget before the settings tab), so there may be no
        # panel yet — build_panel() then applies the saved preference itself
        # once it exists. A hot-load has the panel already, so this is the
        # one that actually matters there.
        if self.panel is not None:
            cfg = self.panel.get_config()
            self._hist.setVisible(cfg.show_lut)
            self._chk_auto_lut.setChecked(cfg.auto_levels)
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
        """Feed the acquisition rate, and the writer's ceiling, to the Save tab.
        The camera is the only module that knows both."""
        cfg = self.panel.get_config()
        self.win.set_expected_rate(
            cfg.frame_bytes * cfg.expected_hz / (1 << 20), WRITER_MBPS)

    def frame_rate_hz(self) -> float | None:
        """What this preset and exposure are expected to sustain. The routine
        panel turns "100 frames" into seconds with it; nothing records it."""
        return self.panel.get_config().expected_hz

    def _on_exposure(self, us: float) -> None:
        if self.worker is not None:
            self.worker.set_exposure(us)

    # (binning is structural: it only takes effect on the next Start, because the
    # panel locks resolution/binning/trigger for the whole session.)

    # ── what a routine may drive (ModuleHost.set_camera_trigger) ──
    def set_external_trigger(self, on: bool) -> bool:
        """Put the camera in External edge (True) or back to Internal
        (False), restarting live view to apply it if that's actually a
        change — trigger mode is a start-of-acquisition setting on this
        camera, not hot-changeable (devices/voltage_cam/acquisition.py sets
        it right before `start_acquisition()`).

        Returns whether the camera ended up in that mode. `False` if a
        restart was needed but a recording is already running — refused,
        unattempted, rather than interrupting it; this module always being
        loaded when this is called (unlike "no camera at all") is what lets
        the caller (`main.py`'s `set_camera_trigger` pooling, `None` only
        for THAT case) tell the two apart. Mirrors `adapters/dmd.py`'s
        `calibrate()`, the other place in the app that already stops/
        reconfigures/restarts this camera for a structural setting.
        """
        want = _EXT_TRIGGER if on else _INT_TRIGGER
        if self.panel.get_config().trigger_mode == want:
            return on
        if self.win.is_recording():
            return False
        was_live = self.win.set_live(False)
        self.panel.set_trigger_mode(want)
        self.win.set_live(was_live)
        return on

    def rearm_trigger(self) -> bool:
        """Re-gate the external trigger so the next edge is detectable, for a
        routine taking one recording per edge. False if there is no running
        worker to ask (nothing is capturing, so there is nothing to re-arm).

        Unlike `set_external_trigger` this is cheap and hot: it restarts only
        the camera's acquisition, inside the capture thread, leaving the
        session, the open file and live view alone. It does NOT put the camera
        into External edge mode — `set_external_trigger` does that once, before
        the routine starts.
        """
        # Both worker classes implement it (the mock as a no-op, for exactly
        # this parity), so only "nothing is capturing" has to be handled.
        if self.worker is None:
            return False
        self.worker.rearm_trigger()
        return True

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
        self._preview_buf.clear()   # don't average across a session boundary

    def start(self) -> None:
        super().start()
        if self.panel.get_config().led_follow_live:
            self._apply_led_follow(True)

    def stop(self) -> None:
        super().stop()
        self.panel.set_running(False)
        self.panel.set_measured_rate(None)          # back to the estimate
        if self.panel.get_config().led_follow_live:
            self._apply_led_follow(False)

    # ── display ──
    def update_display(self) -> None:
        f = self.worker.get_latest() if self.worker is not None else None
        if f is None:
            return
        # Kept at FULL resolution for the DMD's ROI editor: ROIs are in camera
        # px and the registration is measured in camera px, so handing over the
        # display's ¼-scale copy would put every ROI out by a factor of DISP_DS.
        self._last_frame = f
        small = f[::DISP_DS, ::DISP_DS]              # strided view, no copy

        # Preview-only averaging: blends recent DOWNSAMPLED frames for display.
        # The df/f trace below and every recorded frame still use `small`/`f`
        # unaveraged — this never touches what's measured or written.
        if self._preview_buf.maxlen and self._preview_buf.maxlen > 1:
            self._preview_buf.append(small)
            disp = (np.mean(self._preview_buf, axis=0, dtype=np.float32)
                     .astype(small.dtype, copy=False))
        else:
            disp = small

        if self._auto_levels:
            # The percentile is the costly part, so refresh contrast a couple
            # of times a second rather than every frame.
            if self._levels is None or self._level_ctr % LEVELS_EVERY == 0:
                lo, hi = np.percentile(disp, (1, 99))
                self._levels = (float(lo), float(hi))
            self._level_ctr += 1
            self._img.setImage(disp, autoLevels=False, levels=self._levels)
        else:
            # Read the LUT bar's own current levels back and pass them
            # explicitly — pyqtgraph only reliably re-renders the mapping
            # onto NEW frame data when setLevels() is actually called;
            # omitting `levels=` here (as if "leave it alone" were enough)
            # left the display stuck on stale contrast until the operator
            # dragged the LUT themselves, which is what really called it.
            levels = self._hist.item.getLevels() if self._hist is not None else None
            self._img.setImage(disp, autoLevels=False, levels=levels)

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

            `acquired_at` is when the CAMERA says it was taken, not when the
            batch reached us — that is what keeps recorded frame times at the
            true rate. The index is the camera's own counter, its own stream so
            a dropped frame shows as a jump rather than closing the gap.
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
                # Placeholder — no frame has settled it yet. Overwritten by
                # final_metadata(); here so the attribute exists at all if the
                # app dies mid-recording.
                "cam_timestamp_source": self._timestamp_source()}

    def probe_kwargs(self) -> dict[str, Any]:
        # The window opened the camera once at startup and holds the handle, so
        # the Devices window need not re-enumerate — which costs ~6.5 s on the
        # GUI thread, every refresh.
        return {"cam_open": self.win.cam_handle is not None}

    def _timestamp_source(self) -> str:
        """Off the worker — both twins declare it (`TimestampedWorker`).
        "unknown" means *no worker*, a different fact from one that
        failed to say."""
        return "unknown" if self.worker is None else self.worker.timestamp_source

    def final_metadata(self) -> dict[str, Any]:
        if self.worker is None:
            return {"cam_timestamp_source": "unknown"}
        return {
            # "camera" = the camera's own per-frame stamps, "arrival" = the
            # times we read them; decides how far the frame timing is trusted.
            "cam_timestamp_source": self.worker.timestamp_source,
            # Discarded by the CAMERA because we read too slowly — gone from the
            # file, visible as a gap in voltage_cam_index.
            "cam_dropped_frames": self.worker.skipped_frames,
        }
