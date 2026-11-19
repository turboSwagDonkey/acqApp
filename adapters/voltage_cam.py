"""The voltage camera's adapter — owns the window's central view."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from typing import Any

import numpy as np
from PyQt6.QtWidgets import QCheckBox, QLabel, QWidget

from acqApp import config
from acqApp.acq.devices import CameraWorker
from acqApp.adapters.base import (DISP_DS, LEVELS_EVERY, ModuleAdapter,
                                 _image_view, led_controller)
from acqApp.devices.stage.axes_overlay import AxesOverlay
from acqApp.devices.voltage_cam.acquisition import MockCameraWorker, OrcaFireWorker
from acqApp.devices.voltage_cam.led import LedController, MockLedController
from acqApp.devices.voltage_cam import overlay
from acqApp.devices.voltage_cam.presets import (AcqConfig, BINNING_OPTIONS,
                                                DEFAULT_BINNING, DEFAULT_PRESET,
                                                PRESET_KEYS, TRIGGER_MODES,
                                                WRITER_MBPS, burst_stale,
                                                burst_stale_indices)
from acqApp.devices.voltage_cam.panel import SettingsPanel as CamSettingsPanel
from acqApp.sidebar import MiniText

_INT_TRIGGER, _EXT_TRIGGER = TRIGGER_MODES[0], TRIGGER_MODES[1]


def _path_name(key: str) -> str:
    """The picker column `key` sits in ("Path 1"), else its module label."""
    for title, keys in config.module_groups():
        if key in keys:
            return title
    return config.MODULES.get(key, key)


class RunningMean:
    """Mean of the last `n` frames, O(1) frames a push: a running sum, exact
    for integer frames. Frames are copied, as a reused buffer would corrupt
    the sum on eviction."""

    def __init__(self, n: int = 1) -> None:
        self.maxlen = max(1, int(n))
        self._buf: deque = deque()
        self._sum: np.ndarray | None = None

    def clear(self) -> None:
        self._buf.clear()
        self._sum = None

    def push(self, frame: np.ndarray) -> np.ndarray:
        frame = np.array(frame)
        if self._sum is None or self._sum.shape != frame.shape:
            self.clear()
            acc = np.int64 if np.issubdtype(frame.dtype, np.integer) else np.float64
            self._sum = np.zeros(frame.shape, acc)
        self._buf.append(frame)
        self._sum += frame
        if len(self._buf) > self.maxlen:
            self._sum -= self._buf.popleft()
        return (self._sum / len(self._buf)).astype(frame.dtype, copy=False)


class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"         # also its stream, settings section and file suffix
    tab_label = "Voltage cam (primary)"
    own_dock = True
    device_index = 0            # DCAM index unless rigs.json device_ids names one
    meta_prefix = "cam"         # its settings.json keys: cam_preset, …
    led_rig_key = "primary_led"
    overlay_partner = "voltage_cam_2"   # the camera its Overlay blends in
    overlay_default = "green"           # its colour until one is picked
    # Its view holds the Overlay toggle and, overlaid, both cameras in one;
    # the partner's view then hides.
    overlay_host = True

    worker: CameraWorker | None

    def __init__(self, win) -> None:
        super().__init__(win)
        self._auto_levels = True
        self._last_frame = None         # full-res, for the DMD's ROI editor
        # Fixed at build_session: the panel may already name next session's preset.
        self._last_frame_preset: str | None = None
        self._preview_buf = RunningMean(1)
        self._chk_overlay: QCheckBox | None = None     # the host's view only
        self._lbl_legend: QLabel | None = None
        self._overlaid = False          # last paint was a composite (LUT hidden)
        self._ov_levels = None          # (own, partner) stretch, refreshed like _levels
        self._ov_ctr = 0
        self._dcimg_sealed = False      # see seal_dcimg
        # This recording asked DCAM for a .dcimg; outlives detach_sink for
        # final_metadata, and a worker's counts outlive the recording.
        self._rec_dcimg = False

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
        if "file_format" not in config.load_settings(self.key):
            self._save()        # before the Save tab drops its old copy
        self.panel.target_hz_changed.connect(self._on_rate)
        for sig in (self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed,
                    self.panel.burst_changed, self.panel.target_hz_changed,
                    self.panel.lut_visible_changed,
                    self.panel.auto_levels_changed,
                    self.panel.preview_avg_changed,
                    self.panel.led_follow_changed,
                    self.panel.format_changed,
                    self.panel.overlay_color_changed):
            sig.connect(self._save)
        self.panel.lut_visible_changed.connect(self._on_lut_visible)
        self.panel.auto_levels_changed.connect(self._on_auto_levels)
        self.panel.preview_avg_changed.connect(self._on_preview_avg)
        self.panel.overlay_color_changed.connect(lambda _c: self._sync_overlay())
        self.panel.led_toggled.connect(self._on_led)
        cfg = self.panel.get_config()
        self._auto_levels = cfg.auto_levels
        self._preview_buf = RunningMean(cfg.preview_avg)
        for sig in (self.panel.target_hz_changed, self.panel.resolution_changed,
                    self.panel.binning_changed, self.panel.trigger_changed):
            sig.connect(self._push_rate)
        self._push_rate()
        return self.panel

    # ── illumination ──
    def build_controller(self, emulate: bool) -> None:
        self.controller = led_controller(
            emulate, self.led_rig_key, LedController, MockLedController,
            f"{self.tab_label} LED")

    def _on_led(self, on: bool) -> None:
        # A slot: a DAQ error escaping it would abort the app mid-session.
        try:
            self.set_led(on)
        except Exception as e:                       # noqa: BLE001
            self.win.status(f"{self.tab_label} LED: {type(e).__name__}: {e}")

    def led_target(self):
        return self if self.controller is not None else None

    def set_led(self, on: bool) -> None:
        if self.controller is not None:
            self.controller.set(on)

    def _on_lut_visible(self, on: bool) -> None:
        if self._hist is not None and not self._overlaid:   # _end_overlay restores it
            self._hist.setVisible(on)

    def _on_auto_levels(self, on: bool) -> None:
        self._auto_levels = bool(on)
        if on:
            self._reset_levels()
        self._sync_auto_to_lut(on)

    def _on_preview_avg(self, n: int) -> None:
        self._preview_buf = RunningMean(n)

    def view_widget(self) -> QWidget:
        # The Orca's sensor is wider than tall: the bar goes under the image.
        self._img, hist, chk_auto, gv, vb, row, self._rec_dot = _image_view(
            horizontal=True)
        self._axes_overlay = AxesOverlay(vb)
        self._hist = hist
        self._chk_auto_lut = chk_auto
        self._chk_auto_lut.toggled.connect(self._sync_auto_from_lut)
        cfg = self.panel.get_config()
        self._hist.setVisible(cfg.show_lut)
        self._chk_auto_lut.setChecked(cfg.auto_levels)
        if self.overlay_host:
            # Under Auto, in the LUT column (which stays when the LUT bar hides).
            self._chk_overlay = QCheckBox("Overlay")
            self._lbl_legend = QLabel()
            self._lbl_legend.setVisible(False)
            lut = chk_auto.parentWidget().layout()
            lut.insertWidget(1, self._chk_overlay)
            lut.insertWidget(2, self._lbl_legend)
            self._chk_overlay.setChecked(cfg.overlay)
            self._chk_overlay.toggled.connect(self._on_overlay)
            self._sync_overlay()
        return row

    # ── the other path's camera, blended in ──
    def overlay_color(self) -> str:
        # Asked by the other camera, maybe before this panel is built.
        return (self.panel.overlay_color if self.panel is not None
                else self.overlay_default)

    def _on_overlay(self, on: bool) -> None:
        self.panel.set_overlay(on)
        self._save()
        self._ov_levels = None
        if not on:
            self._end_overlay()
        self._sync_overlay()

    def _merged(self) -> bool:
        """Overlay on with the partner loaded: one view for both."""
        chk = self._chk_overlay
        return (chk is not None and chk.isChecked()
                and self.overlay_partner in self.win.module_keys())

    def _sync_overlay(self) -> None:
        """The toggle's state and tooltip, the legend, and the partner's view
        hidden while the two are one."""
        if self._chk_overlay is None:
            return
        here = self.overlay_partner in self.win.module_keys()
        self._chk_overlay.setEnabled(here)
        other = config.MODULES.get(self.overlay_partner, self.overlay_partner)
        self._chk_overlay.setToolTip(
            f"Combine this view and {other}'s into one, each camera in its own "
            "colour (Overlay colour, in each camera's settings); its own view "
            "hides meanwhile. Display only; no registration yet: the other "
            "frame is just resized to fit."
            if here else f"Load {other} to overlay it here.")
        merged = self._merged()
        self.win.set_view_shown(self.overlay_partner, not merged)
        self._refresh_legend()
        self._lbl_legend.setVisible(merged)

    def _partner_color(self) -> str:
        return (self.win.camera_overlay_color(self.overlay_partner)
                or overlay.DEFAULT_COLOR)

    def _refresh_legend(self) -> None:
        """Which colour is which path, under the toggle."""
        if self._lbl_legend is None:
            return
        def swatch(name: str) -> str:
            rgb = overlay.OVERLAY_COLORS.get(name, (1.0, 1.0, 1.0))
            return "#%02x%02x%02x" % tuple(int(255 * v) for v in rgb)
        self._lbl_legend.setText(
            f'<span style="color:{swatch(self.overlay_color())}">■</span> '
            f'{_path_name(self.key)}<br>'
            f'<span style="color:{swatch(self._partner_color())}">■</span> '
            f'{_path_name(self.overlay_partner)}')

    def on_modules_changed(self) -> None:
        self._sync_overlay()

    def _end_overlay(self) -> None:
        """Back to the plain image: its LUT bar and its own levels."""
        if self._overlaid:
            self._overlaid = False
            if self._hist is not None:
                self._hist.setVisible(self.panel.get_config().show_lut)
            self._reset_levels()

    def _partner_frame(self):
        """The partner's newest frame at preview scale, or None (overlay off,
        partner not loaded or no frame yet)."""
        chk = self._chk_overlay
        if chk is None or not chk.isChecked() or not chk.isEnabled():
            return None
        f = self.win.latest_frame(self.overlay_partner)
        return None if f is None else f[::DISP_DS, ::DISP_DS]

    def _paint_overlay(self, own, other) -> None:
        if self._ov_levels is None or self._ov_ctr % LEVELS_EVERY == 0:
            self._ov_levels = (overlay.levels(own), overlay.levels(other))
        self._ov_ctr += 1
        rgb = overlay.composite(own, other, self.overlay_color(),
                                self._partner_color(), *self._ov_levels)
        if not self._overlaid:
            self._overlaid = True
            self._refresh_legend()      # the partner's panel exists by now
            if self._hist is not None:
                self._hist.setVisible(False)    # one LUT can't speak for two colours
        self._img.setImage(rgb, autoLevels=False, levels=(0.0, 1.0))

    @classmethod
    def _load_config(cls) -> AcqConfig:
        cfg = config.load_dataclass(AcqConfig, cls.key)
        saved = config.load_settings(cls.key)
        if cfg.preset_key not in PRESET_KEYS:      # a preset may have been removed
            cfg.preset_key = DEFAULT_PRESET
        if cfg.binning not in BINNING_OPTIONS:     # the panel's combo would raise
            cfg.binning = DEFAULT_BINNING
        if "overlay_color" not in saved:           # each path its own default
            cfg.overlay_color = cls.overlay_default
        # Saved before capture rate existed: keep its exposure's rate.
        if "target_hz" not in saved and cfg.exposure_us > 0:
            cfg.target_hz = round(1e6 / cfg.exposure_us, 1)
        # The format used to be chosen on the Save tab.
        if "file_format" not in saved:
            old = config.load_settings("saving").get("orca_format")
            if old in ("dcimg", "tiff"):
                cfg.file_format = old
        return cfg.fit_exposure()

    def records_dcimg(self) -> bool:
        return self.panel.get_config().file_format == "dcimg"

    def _save(self, *_a) -> None:
        config.save_settings(self.key, asdict(self.panel.get_config()))

    def _push_rate(self, *_a) -> None:
        cfg = self.panel.get_config()
        self.win.set_expected_rate(
            cfg.frame_bytes * cfg.rate_hz / (1 << 20), WRITER_MBPS, self.key)

    def frame_rate_hz(self) -> float | None:
        return self.panel.get_config().rate_hz

    def mini_view(self) -> QWidget:
        return MiniText(self._mini_text)

    def _mini_text(self) -> tuple[str, str, str]:
        """State, then a warning if any, else the size and rate (short: it
        shares a title bar; the rest is in its tooltip)."""
        cfg = self.panel.get_config()
        keep = self.panel.writable_fraction(cfg)
        what = f"{cfg.preset_key} · {cfg.rate_hz:g} Hz"
        state = "● live" if self.worker is not None else "idle"
        if keep < 1:
            return state, f"⚠ {100 * keep:.0f}% of frames writable", what
        return state, what, ""

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
            return True         # False would read as "refused: recording"
        return self._restart_with(lambda: self.panel.set_trigger_mode(want))

    def set_burst_frames(self, n: int) -> bool:
        """Frames per edge; structural like the trigger mode."""
        if self.panel.get_config().burst_frames == n:
            return True
        return self._restart_with(lambda: self.panel.set_burst_frames(n))

    def _restart_with(self, change) -> bool:
        if self.win.is_recording():
            return False
        was_live = self.win.set_live(False)
        try:
            change()
        finally:
            self.win.set_live(was_live)
        return True

    def burst_frames_done(self) -> int | None:
        """Real frames of the burst the last gate caught; None outside burst."""
        w = self.worker
        return None if w is None else getattr(w, "burst_frames_since_gate", None)

    def _burst_rule(self) -> tuple[int, bool] | None:
        w = self.worker
        return None if w is None else getattr(w, "burst_rule", None)

    def rearm_trigger(self) -> bool:
        """Hot re-gate inside the capture thread. False if nothing is capturing."""
        if self.worker is None:
            return False
        self.worker.rearm_trigger()
        return True

    def _dcimg_recording(self) -> bool:
        """This recording writes a .dcimg: open, or sealed awaiting its roll."""
        w = self.worker
        return w is not None and (self._dcimg_sealed
                                  or getattr(w, "dcimg_active", False))

    def arm_with_next_file(self) -> bool:
        """False unless this recording writes a .dcimg."""
        if not self._dcimg_recording():
            return False
        self.worker.arm_with_next_file()
        return True

    def seal_dcimg(self) -> bool:
        """Close the open .dcimg now, leaving the recording open. A burst-mode
        camera re-arms itself, so an edge before the next roll would add a
        burst to a finished file (rig, 2026-09-30: 2830 frames for 2500).
        False if no .dcimg is open."""
        w = self.worker
        if w is None or not getattr(w, "dcimg_active", False):
            return False
        w.set_record_file(None)
        self._dcimg_sealed = True
        return True

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        cfg = self.panel.get_config()
        self._last_frame_preset = cfg.preset_key
        # Reuse the kept handle: re-opening DCAM crashes natively.
        if emulate:
            worker = MockCameraWorker(cfg, seed=self.device_index)
        else:
            index = self._index()
            worker = OrcaFireWorker(index, cfg, cam=self.win.camera_handle(index))
        self._adopt(worker)
        if isinstance(worker, OrcaFireWorker):
            worker.drops_update.connect(
                lambda skipped, _buf: self.win.status(
                    f"camera dropped {skipped} frames — reading too slowly"))
            worker.timing_update.connect(self.panel.set_measured_rate)
        self.panel.set_running(True)
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
        disp = (self._preview_buf.push(small) if self._preview_buf.maxlen > 1
                else small)

        other = self._partner_frame()
        if other is not None:
            self._paint_overlay(disp, other)
        else:
            self._end_overlay()
            self._paint(disp, self._auto_levels)
        stage = self.win.stage_target()
        self._axes_overlay.update(
            stage.camera_axes() if stage is not None else None, disp.shape)

    # ── recording ──
    def attach_sink(self, rec) -> None:
        self._dcimg_sealed = self._rec_dcimg = False
        if self.worker is None:
            return

        target = self.win.dcimg_target(self.key)
        if target is not None:
            if getattr(self.worker, "supports_dcimg", False):
                self.worker.set_record_file(target)   # DCAM writes; no sink
                self._rec_dcimg = True
                return
            self.win.status("No DCAM camera — recording TIFF, not DCIMG")

        def sink(item) -> None:
            # `at` is the camera's acquisition time; the index is its own
            # counter, so a drop shows as a jump.
            frame, at, index = item
            rec.put(self.key, frame, at=at)
            if index is not None:
                rec.put(f"{self.key}_index", float(index), at=at)
                rule = self._burst_rule()
                if rule and burst_stale(int(index), *rule):
                    rec.put(f"{self.key}_stale", float(index), at=at)

        self.worker.set_sink(sink)

    def dcimg_ready(self) -> bool:
        w = self.worker
        return True if w is None else bool(getattr(w, "dcimg_ready", True))

    def trigger_gate(self) -> tuple[int, int] | None:
        w = self.worker
        return None if w is None else getattr(w, "trigger_gate", None)

    def dcimg_frames(self) -> int | None:
        """Frames in this recording's .dcimg (frozen once sealed)."""
        if not self._dcimg_recording():
            return None
        return self.worker.dcimg_frames

    def detach_sink(self) -> None:
        self._dcimg_sealed = False
        # Base only clears the sink; an attached recorder would keep writing.
        # A TIFF recording has none: closing one anyway restarts capture.
        w = self.worker
        if w is not None and (self._rec_dcimg
                              or getattr(w, "dcimg_active", False)):
            w.set_record_file(None)
        super().detach_sink()

    def _prefixed(self, meta: dict[str, Any]) -> dict[str, Any]:
        """`cam_x` -> `<meta_prefix>_x`, so two cameras' keys don't collide."""
        if self.meta_prefix == "cam":
            return meta
        return {f"{self.meta_prefix}_{k[4:]}" if k.startswith("cam_") else k: v
                for k, v in meta.items()}

    def metadata(self) -> dict[str, Any]:
        return self._prefixed(self._metadata())

    def final_metadata(self) -> dict[str, Any]:
        return self._prefixed(self._final_metadata())

    def _metadata(self) -> dict[str, Any]:
        cfg = self.panel.get_config()
        return {"cam_preset":      cfg.preset_key,
                "cam_binning":     cfg.binning,
                "cam_exposure_us": cfg.exposure_us,     # estimate; final = camera's
                "cam_rate_hz":     cfg.target_hz,       # requested; 0 = Max
                "cam_burst_frames": cfg.burst_frames if cfg.burst else 0,
                # Placeholder, so it exists if the app dies mid-recording.
                "cam_timestamp_source": self._timestamp_source()}

    def probe_kwargs(self) -> dict[str, Any]:
        # Skips a ~6.5 s re-enumeration on the GUI thread.
        return {"cam_open": self.win.cam_handle is not None}

    def _timestamp_source(self) -> str:
        return "unknown" if self.worker is None else self.worker.timestamp_source

    def _final_metadata(self) -> dict[str, Any]:
        if self.worker is None:
            return {"cam_timestamp_source": "unknown"}
        out = {
            "cam_timestamp_source": self.worker.timestamp_source,
            # What fit_exposure actually set from the camera's own readout.
            "cam_exposure_us": self.worker._config.exposure_us,
        }
        rule = self._burst_rule()
        if rule:
            out["cam_burst_leading_stale"] = rule[1]
        if self._rec_dcimg:
            out["cam_dcimg_frames"] = self.worker.dcimg_frames
            out["cam_dcimg_missing"] = self.worker.dcimg_missing
            out.update(self._dcimg_clock_span())
            if rule:
                # A .dcimg starts at a capture start, so indices are its own.
                out["cam_dcimg_stale_frames"] = json.dumps(burst_stale_indices(
                    self.worker.dcimg_frames, *rule))
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

    def _index(self) -> int:
        got = config.rig_device_id(self.key)
        return self.device_index if got is None else got


class VoltageCam2Module(VoltageCamModule):
    """Path 2's Orca: its own stream, files, settings and LED. Routines, the
    window's LED/rate lookups and the probe still mean path 1's camera."""
    key = "voltage_cam_2"
    tab_label = "Camera (path 2)"
    device_index = 1
    meta_prefix = "cam2"
    led_rig_key = "primary_led_2"
    overlay_partner = "voltage_cam"
    overlay_default = "magenta"     # complements path 1's green
    overlay_host = False            # path 1's view holds the combined one

    def on_modules_changed(self) -> None:
        # Path 1 gone mid-overlay: nothing would ever show this view again.
        if self.overlay_partner not in self.win.module_keys():
            self.win.set_view_shown(self.key, True)

    def build_controller(self, emulate: bool) -> None:
        # Unset means fitted in rigs.json: without a named line, path 1's
        # default would be driven.
        if config.rig_channel(self.led_rig_key) is None:
            self.controller = MockLedController()
        else:
            super().build_controller(emulate)

    def led_target(self):
        return None

    def frame_rate_hz(self) -> float | None:
        return None

    def probe_kwargs(self) -> dict[str, Any]:
        return {}
