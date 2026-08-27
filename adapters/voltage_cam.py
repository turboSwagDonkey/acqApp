"""
The voltage camera's adapter — the module that owns the window's central view.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import CameraWorker
from acqApp.adapters.base import (DISP_DS, LEVELS_EVERY, PLOT_HISTORY,
                                 ModuleAdapter, _image_view, _plot)
from acqApp.devices.voltage_cam.acquisition import MockCameraWorker, OrcaFireWorker
from acqApp.devices.voltage_cam.presets import AcqConfig, DEFAULT_PRESET, PRESET_KEYS, WRITER_MBPS
from acqApp.devices.voltage_cam.panel import SettingsPanel as CamSettingsPanel


class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"
    tab_label = "Voltage cam (primary)"
    plot_label = "ΔF/F"
    central_title = "Voltage camera — primary"

    worker: CameraWorker | None          # narrows ModuleAdapter.worker

    def __init__(self, win) -> None:
        super().__init__(win)
        self._img = None
        self._curve = None
        self._y: list[float] = []
        self._f0: float | None = None
        self._levels: tuple[float, float] | None = None
        self._level_ctr = 0
        self._last_frame = None         # full-res, for the DMD's ROI editor

    def last_frame(self):
        return self._last_frame

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
        """Feed the acquisition rate, and the writer's ceiling, to the Save tab.
        The camera is the only module that knows both."""
        cfg = self.panel.get_config()
        self.win.set_expected_rate(
            cfg.frame_bytes * cfg.expected_fps / (1 << 20), WRITER_MBPS)

    def frame_rate_hz(self) -> float | None:
        """What this preset and exposure are expected to sustain. The routine
        panel turns "100 frames" into seconds with it; nothing records it."""
        return self.panel.get_config().expected_fps

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
        # Kept at FULL resolution for the DMD's ROI editor: ROIs are in camera
        # px and the registration is measured in camera px, so handing over the
        # display's ¼-scale copy would put every ROI out by a factor of DISP_DS.
        self._last_frame = f
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
                "cam_trigger":     cfg.trigger_mode,
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
