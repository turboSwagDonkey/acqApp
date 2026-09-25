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
from acqApp.adapters.base import (DISP_DS, PLOT_HISTORY, ModuleAdapter,
                                 _image_view, _plot, led_controller)
from acqApp.devices.voltage_cam.acquisition import MockCameraWorker, OrcaFireWorker
from acqApp.devices.voltage_cam.led import LedController, MockLedController
from acqApp.devices.voltage_cam.presets import (AcqConfig, DEFAULT_PRESET,
                                                PRESET_KEYS, TRIGGER_MODES,
                                                WRITER_MBPS)
from acqApp.devices.voltage_cam.panel import SettingsPanel as CamSettingsPanel

# TRIGGER_MODES[i] rather than a second literal, so this and the panel's
# combo can't say different things about how the two modes are spelled.
_INT_TRIGGER, _EXT_TRIGGER = TRIGGER_MODES[0], TRIGGER_MODES[1]


class VoltageCamModule(ModuleAdapter):
    key = "voltage_cam"
    tab_label = "Voltage cam (primary)"
    plot_label = "ΔF/F"
    central_title = "Voltage camera — primary"

    worker: CameraWorker | None          # narrows ModuleAdapter.worker

    def __init__(self, win) -> None:
        super().__init__(win)
        self._curve = None
        self._y: list[float] = []
        self._f0: float | None = None
        self._auto_levels = True         # AcqConfig's default, until build_panel says otherwise
        self._last_frame = None         # full-res, for the DMD's ROI editor
        # The preset actually behind `_last_frame` — set once per session, at
        # build_session(), not read live off the panel: `preset_key()` mirrors
        # the combo and can already name NEXT session's preset (structural,
        # only takes effect at the next Start), so it can name a different
        # capture area than the frame currently buffered was captured under.
        self._last_frame_preset: str | None = None
        self._preview_buf: deque = deque(maxlen=1)   # recent preview frames, for averaging

    def last_frame(self):
        return self._last_frame

    def last_frame_preset(self) -> str | None:
        """The resolution preset `last_frame()` was actually captured under.

        For the DMD's ROI editor (`adapters/dmd.py.edit_rois`), which must
        shift a click by the SAME (hpos, vpos) the buffered frame's pixel
        (0, 0) sits at — using `preset_key()` there instead would silently
        mismatch a frame still queued under the old preset right after the
        operator changes the combo but before the next Start applies it.
        """
        return self._last_frame_preset

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

    def binning(self) -> int:
        return self.panel.get_config().binning

    def set_binning(self, n: int) -> None:
        """Change the binning factor (e.g. from a Mode preset). Structural —
        like the operator's own combo click, it only takes effect the next
        time the session (re)starts."""
        self.panel.set_binning(n)

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
        # central_widget() can't read the panel yet); a hot-load builds this
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
        self.controller = led_controller(
            emulate, "primary_led", LedController, MockLedController,
            "primary LED")

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
            self._reset_levels()
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
        routine taking one recording per edge. False if there's no running
        worker to ask (nothing is capturing, so there's nothing to re-arm).

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

    def arm_with_next_file(self) -> bool:
        """Have the next `.dcimg` swap re-arm the trigger too, so a routine's
        `trigger` step can re-arm without killing the recorder — see
        `OrcaFireWorker.arm_with_next_file`. False unless a .dcimg is open."""
        w = self.worker
        if w is None or not getattr(w, "dcimg_active", False):
            return False
        w.arm_with_next_file()
        return True

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        cfg = self.panel.get_config()
        self._last_frame_preset = cfg.preset_key    # the area THIS worker captures
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
        self._reset_levels()
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

        # DCAM's own recorder writes the frames, so there is no sink to set:
        # nothing reaches Python to put in one. Preview still works (the
        # driver keeps filling the ring), and the scalar streams are
        # unaffected — only this camera's frames leave the .h5/TIFF path.
        target = self.win.dcimg_target(self.key)
        if target is not None:
            if getattr(self.worker, "supports_dcimg", False):
                self.worker.set_record_file(target)
                return
            # Emulate: no DCAM behind the mock, so fall through to the sink
            # and record a TIFF. Said out loud — a session that silently
            # ignored the chosen format would be found in the file, later.
            self.win.status("No DCAM camera — recording TIFF, not DCIMG")

        def sink(item) -> None:
            """The worker sends (frame, acquired_at, index).

            `acquired_at` is when the CAMERA says it was taken, not when the
            batch reached us — that's what keeps recorded frame times at the
            true rate. The index is the camera's own counter, its own stream so
            a dropped frame shows as a jump rather than closing the gap.
            """
            frame, at, index = item
            rec.put("voltage_cam", frame, at=at)
            if index is not None:
                rec.put("voltage_cam_index", float(index), at=at)

        self.worker.set_sink(sink)

    def dcimg_ready(self) -> bool:
        """See `ModuleHost.camera_ready` — False while a .dcimg swap has the
        camera stopped. A worker that predates this is treated as ready."""
        w = self.worker
        return True if w is None else bool(getattr(w, "dcimg_ready", True))

    def dcimg_frames(self) -> int | None:
        """Frames the .dcimg holds so far, or None if one isn't open — what
        the routine engine counts in place of `Recorder.offered()`, which
        nothing increments while DCAM is writing the frames itself."""
        w = self.worker
        if w is None or not getattr(w, "dcimg_active", False):
            return None
        return w.dcimg_frames

    def detach_sink(self) -> None:
        # Close the .dcimg too; base only clears the sink, and a recorder left
        # attached keeps writing into a file the session has moved on from.
        if self.worker is not None and getattr(self.worker, "supports_dcimg", False):
            self.worker.set_record_file(None)
        super().detach_sink()

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
        out = {
            # "camera" = the camera's own per-frame stamps, "arrival" = the
            # times we read them; decides how far the frame timing is trusted.
            "cam_timestamp_source": self.worker.timestamp_source,
            # Discarded by the CAMERA because we read too slowly — gone from the
            # file, visible as a gap in voltage_cam_index.
            "cam_dropped_frames": self.worker.skipped_frames,
        }
        # Only when DCAM wrote the frames: the .dcimg is the only record of
        # how many there were, since none passed through a sink to be counted.
        if getattr(self.worker, "dcimg_frames", 0):
            out["cam_dcimg_frames"] = self.worker.dcimg_frames
            out["cam_dcimg_missing"] = self.worker.dcimg_missing
            out.update(self._dcimg_clock_span())
        return out

    def _dcimg_clock_span(self) -> dict[str, Any]:
        """When the .dcimg opened and closed, on the SESSION clock.

        Without this the file is unalignable: its frames carry DCAM's own
        timebase, and the routine boundaries in the CSV carry the shared
        one, with nothing in common. The worker stamps both ends on
        `perf_counter`, which is the timebase `Recorder.put(at=…)` already
        converts from, so the same `clock.at()` puts them on the same axis as
        every other stream. Frame n is then t0 + n/rate, to within the
        camera's own jitter — exact per-frame stamps would mean
        `dcamrec_copymetadata`, which is a bigger piece of work.
        """
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
        except Exception:       # noqa: BLE001 — clock never started; no axis
            return {}
