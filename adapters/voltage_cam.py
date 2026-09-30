"""The voltage camera's adapter — owns the window's central view."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from typing import Any

import numpy as np
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import CameraWorker
from acqApp.adapters.base import (DISP_DS, PLOT_HISTORY, ModuleAdapter,
                                 _image_view, _plot, led_controller)
from acqApp.devices.voltage_cam.acquisition import MockCameraWorker, OrcaFireWorker
from acqApp.devices.voltage_cam.led import LedController, MockLedController
from acqApp.devices.voltage_cam.presets import (AcqConfig, DEFAULT_PRESET,
                                                PRESET_KEYS, TRIGGER_MODES,
                                                WRITER_MBPS)
from acqApp.devices.voltage_cam.panel import SettingsPanel as CamSettingsPanel

_INT_TRIGGER, _EXT_TRIGGER = TRIGGER_MODES[0], TRIGGER_MODES[1]


class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"
    tab_label = "Voltage cam (primary)"
    plot_label = "ΔF/F"
    central_title = "Voltage camera — primary"

    worker: CameraWorker | None

    def __init__(self, win) -> None:
        super().__init__(win)
        self._curve = None
        self._y: list[float] = []
        self._f0: float | None = None
        self._auto_levels = True
        self._last_frame = None         # full-res, for the DMD's ROI editor
        # Fixed at build_session: the panel may already name next session's preset.
        self._last_frame_preset: str | None = None
        self._preview_buf: deque = deque(maxlen=1)

    def last_frame(self):
        return self._last_frame

    def last_frame_preset(self) -> str | None:
        return self._last_frame_preset

    # ── the sensor's capture area (structural: next Start) ──
    def preset_key(self) -> str:
        return self.panel.get_config().preset_key

    def set_preset(self, key: str) -> None:
        self.panel.set_preset(key)

    def set_rate(self, hz: float) -> None:
        """Hot, through the panel's own capture-rate wiring."""
        self.panel.set_rate(hz)

    def binning(self) -> int:
        return self.panel.get_config().binning

    def set_binning(self, n: int) -> None:
        self.panel.set_binning(n)

    def set_trigger_mode_manual(self) -> None:
        """Internal mode on the panel only — no restart (see `_start_session`)."""
        self.panel.set_trigger_mode(_INT_TRIGGER)

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = CamSettingsPanel(self._load_config())
        self.panel.target_hz_changed.connect(self._on_rate)
        for sig in (self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed,
                    self.panel.target_hz_changed,
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
        # At startup the central view is built first; on a hot-load, this is.
        # Whichever runs second applies the saved view prefs.
        if self._hist is not None:
            self._hist.setVisible(cfg.show_lut)
        if self._chk_auto_lut is not None:
            self._chk_auto_lut.setChecked(cfg.auto_levels)
        for sig in (self.panel.target_hz_changed, self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed):
            sig.connect(self._push_rate)
        self._push_rate()
        return self.panel

    # ── illumination ──
    def build_controller(self, emulate: bool) -> None:
        self.controller = led_controller(
            emulate, "primary_led", LedController, MockLedController,
            "primary LED")

    def _on_led(self, on: bool) -> None:
        if self.controller is not None:
            self.controller.set(on)

    def led_target(self):
        return self if self.controller is not None else None

    def set_led(self, on: bool) -> None:
        self._on_led(on)

    def _on_lut_visible(self, on: bool) -> None:
        if self._hist is not None:
            self._hist.setVisible(on)

    def _on_auto_levels(self, on: bool) -> None:
        self._auto_levels = bool(on)
        if on:
            self._reset_levels()
        self._sync_auto_to_lut(on)

    def _on_preview_avg(self, n: int) -> None:
        self._preview_buf = deque(maxlen=max(1, n))

    def build_plot(self) -> QWidget:
        pw, self._curve = _plot("ΔF/F", "ΔF/F", "%", "Frame", self.key)
        return pw

    def central_widget(self) -> QWidget:
        self._img, hist, chk_auto, gv, _vb, row, self._rec_dot = _image_view()
        self._hist = hist
        self._chk_auto_lut = chk_auto
        self._chk_auto_lut.toggled.connect(self._sync_auto_from_lut)
        if self.panel is not None:          # hot-load; see build_panel
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
        # Saved before capture rate existed: keep its exposure's rate.
        if "target_hz" not in config.load_settings("voltage_cam") \
                and cfg.exposure_us > 0:
            cfg.target_hz = round(1e6 / cfg.exposure_us, 1)
        return cfg.fit_exposure()

    def _save(self, *_a) -> None:
        config.save_settings("voltage_cam", asdict(self.panel.get_config()))

    def _push_rate(self, *_a) -> None:
        cfg = self.panel.get_config()
        self.win.set_expected_rate(
            cfg.frame_bytes * cfg.rate_hz / (1 << 20), WRITER_MBPS)

    def frame_rate_hz(self) -> float | None:
        return self.panel.get_config().rate_hz

    def _on_rate(self, hz: float) -> None:
        if self.worker is not None:
            self.worker.set_rate(hz)

    # ── what a routine may drive ──
    def set_external_trigger(self, on: bool) -> bool:
        """External edge (True) or Internal. Trigger mode is set at acquisition
        start, so a change restarts live view — refused (False) while
        recording."""
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
        """Hot re-gate inside the capture thread. False if nothing is capturing."""
        if self.worker is None:
            return False
        self.worker.rearm_trigger()
        return True

    def arm_with_next_file(self) -> bool:
        """False unless a .dcimg is open."""
        w = self.worker
        if w is None or not getattr(w, "dcimg_active", False):
            return False
        w.arm_with_next_file()
        return True

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        cfg = self.panel.get_config()
        self._last_frame_preset = cfg.preset_key
        # Reuse the startup handle: re-opening DCAM crashes natively.
        worker = (MockCameraWorker(cfg) if emulate
                  else OrcaFireWorker(0, cfg, cam=self.win.cam_handle))
        self._adopt(worker)
        if isinstance(worker, OrcaFireWorker):
            worker.drops_update.connect(
                lambda skipped, _buf: self.win.status(
                    f"camera dropped {skipped} frames — reading too slowly"))
            worker.timing_update.connect(self.panel.set_measured_rate)
        self.panel.set_running(True)
        self._y.clear()
        self._f0 = None
        self._reset_levels()
        self._preview_buf.clear()

    def start(self) -> None:
        super().start()
        if self.panel.get_config().led_follow_live:
            self._apply_led_follow(True)

    def stop(self) -> None:
        super().stop()
        self.panel.set_running(False)
        self.panel.set_measured_rate(None)
        if self.panel.get_config().led_follow_live:
            self._apply_led_follow(False)

    # ── display ──
    def update_display(self) -> None:
        self._sync_rec_dot()
        f = self.worker.get_latest() if self.worker is not None else None
        if f is None:
            return
        # Full-res: ROIs are in camera px.
        self._last_frame = f
        small = f[::DISP_DS, ::DISP_DS]

        # Preview-only averaging; the trace and the file use unaveraged frames.
        if self._preview_buf.maxlen and self._preview_buf.maxlen > 1:
            self._preview_buf.append(small)
            disp = (np.mean(self._preview_buf, axis=0, dtype=np.float32)
                     .astype(small.dtype, copy=False))
        else:
            disp = small

        self._paint(disp, self._auto_levels)

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

        target = self.win.dcimg_target(self.key)
        if target is not None:
            if getattr(self.worker, "supports_dcimg", False):
                self.worker.set_record_file(target)   # DCAM writes; no sink
                return
            self.win.status("No DCAM camera — recording TIFF, not DCIMG")

        def sink(item) -> None:
            # `at` is the camera's acquisition time; the index is its own
            # counter, so a drop shows as a jump.
            frame, at, index = item
            rec.put("voltage_cam", frame, at=at)
            if index is not None:
                rec.put("voltage_cam_index", float(index), at=at)

        self.worker.set_sink(sink)

    def dcimg_ready(self) -> bool:
        w = self.worker
        return True if w is None else bool(getattr(w, "dcimg_ready", True))

    def trigger_gate(self) -> tuple[int, int] | None:
        w = self.worker
        return None if w is None else getattr(w, "trigger_gate", None)

    def dcimg_frames(self) -> int | None:
        w = self.worker
        if w is None or not getattr(w, "dcimg_active", False):
            return None
        return w.dcimg_frames

    def detach_sink(self) -> None:
        # Base only clears the sink; an attached recorder would keep writing.
        if self.worker is not None and getattr(self.worker, "supports_dcimg", False):
            self.worker.set_record_file(None)
        super().detach_sink()

    def metadata(self) -> dict[str, Any]:
        cfg = self.panel.get_config()
        return {"cam_preset":      cfg.preset_key,
                "cam_binning":     cfg.binning,
                "cam_exposure_us": cfg.exposure_us,     # estimate; final = camera's
                "cam_rate_hz":     cfg.target_hz,       # requested; 0 = Max
                # Placeholder, so it exists if the app dies mid-recording.
                "cam_timestamp_source": self._timestamp_source()}

    def probe_kwargs(self) -> dict[str, Any]:
        # Skips a ~6.5 s re-enumeration on the GUI thread.
        return {"cam_open": self.win.cam_handle is not None}

    def _timestamp_source(self) -> str:
        return "unknown" if self.worker is None else self.worker.timestamp_source

    def final_metadata(self) -> dict[str, Any]:
        if self.worker is None:
            return {"cam_timestamp_source": "unknown"}
        out = {
            "cam_timestamp_source": self.worker.timestamp_source,
            # What fit_exposure actually set from the camera's own readout.
            "cam_exposure_us": self.worker._config.exposure_us,
        }
        if getattr(self.worker, "dcimg_frames", 0):
            out["cam_dcimg_frames"] = self.worker.dcimg_frames
            out["cam_dcimg_missing"] = self.worker.dcimg_missing
            out.update(self._dcimg_clock_span())
        else:
            # Camera-side drops; with a .dcimg, cam_dcimg_missing is the count.
            out["cam_dropped_frames"] = self.worker.skipped_frames
        return out

    def _dcimg_clock_span(self) -> dict[str, Any]:
        """The .dcimg's open/close on the session clock — its only alignment
        with the other streams (frame n ≈ t0 + n/rate)."""
        span = getattr(self.worker, "dcimg_span", None)
        if not span:
            return {}
        t0, t1 = span
        try:
            clock = self.win.sync.clock
            out = {"cam_dcimg_t0_s": round(clock.at(t0), 6)}
            if t1:
                out["cam_dcimg_t1_s"] = round(clock.at(t1), 6)
            return out
        except Exception:       # noqa: BLE001 — clock never started
            return {}
