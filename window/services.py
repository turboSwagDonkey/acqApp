"""The services module adapters call on the window (`acq.devices.ModuleHost`
documents them): sync, targets, camera calls, modes."""
from __future__ import annotations

from pathlib import Path

from acqApp import config
from acqApp.acq.sync import SyncController
from acqApp.window.common import MODE_NONE


class WindowServices:
    """MainWindow's ModuleHost side. Uses `_modules`, `_sync`, `_cam_handle`."""

    # ── Services the module adapters use (see ModuleHost) ─────────────────────

    @property
    def sync(self) -> SyncController:
        return self._sync

    @property
    def cam_handle(self):
        return self._cam_handle

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def module_keys(self) -> list[str]:
        return [m.key for m in self._modules]

    def signal_sources(self) -> list:
        return [s for m in self._modules for s in m.signal_sources()]

    def stage_target(self):
        return self._first("stage_target")

    def pattern_target(self):
        return self._first("pattern_target")

    def led_target(self):
        return self._first("led_target")

    def puffer_target(self):
        return self._first("puffer_target")

    def frame_rate_hz(self) -> float | None:
        return self._first("frame_rate_hz")

    def active_fov_name(self) -> str:
        stage = self.stage_target()
        return stage.active_fov_name() if stage is not None else ""

    def dcimg_enabled(self) -> bool:
        return bool(self._first("records_dcimg"))

    def dcimg_target(self, stream: str) -> Path | None:
        if not self.dcimg_enabled() or self._rec_path is None:
            return None
        return self._rec_path / f"{self._rec_path.name}_{stream}.dcimg"

    def _first(self, name: str):
        for m in self._modules:
            got = getattr(m, name)()
            if got is not None:
                return got
        return None

    def set_live(self, on: bool) -> bool:
        """Through the button, so the UI stays in step. Returns the previous state."""
        was = self._btn_run.isChecked()
        if bool(on) != was:
            self._btn_run.setChecked(bool(on))
        return was

    def set_recording(self, on: bool) -> bool:
        """Through the button (which starts the session if needed). Returns
        the previous state."""
        was = self._btn_rec.isChecked()
        if bool(on) != was:
            self._btn_rec.setChecked(bool(on))
        return was

    def is_recording(self) -> bool:
        return self._btn_rec.isChecked()

    def _module(self, key: str):
        for m in self._modules:
            if m.key == key:
                return m
        return None

    def _call(self, key: str, name: str, *args, default=None):
        """`module[key].name(*args)`, or `default` if the module isn't loaded
        or has no such method — modes.json keys are hand-edited."""
        m = self._module(key)
        fn = getattr(m, name, None) if m is not None else None
        return fn(*args) if fn is not None else default

    def camera_preset(self, key: str) -> str | None:
        return self._call(key, "preset_key")

    def _swap(self, key: str, get: str, put: str, value):
        """`module[key].put(value)`; returns the `get()` from before."""
        m = self._module(key)
        if m is None or not hasattr(m, put):
            return None
        prev = getattr(m, get)()
        getattr(m, put)(value)
        return prev

    def set_camera_preset(self, key: str, preset: str) -> str | None:
        """Returns the previous preset. Takes effect at the next session start."""
        return self._swap(key, "preset_key", "set_preset", preset)

    def camera_binning(self, key: str) -> int | None:
        return self._call(key, "binning")

    def set_camera_binning(self, key: str, n: int) -> int | None:
        """Returns the previous value. Takes effect at the next session start."""
        return self._swap(key, "binning", "set_binning", n)

    def set_camera_trigger(self, key: str, on: bool) -> bool | None:
        return self._call(key, "set_external_trigger", on)

    def set_camera_burst(self, key: str, n: int) -> bool | None:
        return self._call(key, "set_burst_frames", n)

    def add_routine_fov_step(self, fov: Path, roi: Path) -> None:
        self._call("routines", "add_fov_step", fov, roi)

    def camera_burst_frames(self, key: str) -> int | None:
        return self._call(key, "burst_frames_done")

    def rearm_camera_trigger(self, key: str) -> bool | None:
        return self._call(key, "rearm_trigger")

    def arm_camera_with_next_file(self, key: str) -> bool | None:
        return self._call(key, "arm_with_next_file")

    def seal_camera_file(self, key: str) -> bool | None:
        return self._call(key, "seal_dcimg")

    def camera_trigger_gate(self, key: str) -> tuple[int, int] | None:
        return self._call(key, "trigger_gate")

    def dcimg_frames(self, key: str) -> int | None:
        return self._call(key, "dcimg_frames")

    def camera_ready(self, key: str) -> bool:
        return self._call(key, "dcimg_ready", default=True)

    def set_mode(self, name: str) -> None:
        """Apply a modes.json recipe. Keys:
          dmd_all_on: true
          dmd_sub_sampling: n            (1-10, "1 in n" pixels off; 1 = off)
          camera_presets: {key: preset}  (a preset key, or "full")
          camera_rate_hz: {key: hz}      (0 = Max; exposure follows)
          camera_exposure_us: {key: us}  (legacy: read as rate 1e6/us)
          camera_binning: {key: n}       (1/2/4)
          camera_trigger: {key: bool}    (True = External edge)

        DMD keys, presets and binning take effect at the next DMD Display /
        session start; the rate applies at once; camera_trigger restarts live
        view itself (refused while recording). Unknown modules and presets
        are skipped."""
        from acqApp.devices.voltage_cam.presets import resolve_preset_key

        recipe = self._modes.get(name, {})
        dmd = self._module("dmd")
        if recipe.get("dmd_all_on") and dmd is not None:
            dmd.set_all_on()
        sub_sampling = recipe.get("dmd_sub_sampling")
        if (sub_sampling is not None and dmd is not None
                and hasattr(dmd, "set_sub_sampling")):
            dmd.set_sub_sampling(int(sub_sampling))
        for key, preset in recipe.get("camera_presets", {}).items():
            self.set_camera_preset(key, resolve_preset_key(preset))
        rates = {k: 1e6 / us for k, us in
                 recipe.get("camera_exposure_us", {}).items() if us > 0}
        rates.update(recipe.get("camera_rate_hz", {}))
        for key, hz in rates.items():
            self._call(key, "set_rate", hz)
        for key, n in recipe.get("camera_binning", {}).items():
            self.set_camera_binning(key, n)
        for key, on in recipe.get("camera_trigger", {}).items():
            self.set_camera_trigger(key, on)
        self.status(f"Mode: {name}")

    def _save_mode_as(self) -> None:
        """Capture the DMD's and voltage camera's live settings as a new
        modes.json entry."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        from acqApp.devices.voltage_cam.presets import (TRIGGER_MODES,
                                                        preset_alias)

        name, ok = QInputDialog.getText(self, "Save as preset",
                                        "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        if name == MODE_NONE:
            QMessageBox.warning(self, "Reserved name",
                                f'"{MODE_NONE}" is reserved for "no preset" '
                                f"— choose another name.")
            return

        # Re-read from disk: the file may have been hand-edited since startup.
        modes = config.load_modes()
        if name in modes:
            if QMessageBox.question(
                    self, "Replace preset",
                    f'A preset named "{name}" already exists — replace it '
                    f"with the current settings?") != QMessageBox.StandardButton.Yes:
                return

        recipe: dict = {}
        captured: list[str] = []

        dmd = self._module("dmd")
        if dmd is not None and dmd.panel is not None:
            from acqApp.devices.dmd.control import MODE_ALL_ON
            if dmd.panel.mode == MODE_ALL_ON:
                recipe["dmd_all_on"] = True
                captured.append("DMD all-on")
            n = dmd.panel.settings.sub_sampling
            if n > 1:
                recipe["dmd_sub_sampling"] = n
                captured.append(f"DMD sub-sampling 1 in {n}")

        vcam = self._module("voltage_cam")
        if vcam is not None:
            if hasattr(vcam, "preset_key"):
                key = vcam.preset_key()
                recipe["camera_presets"] = {"voltage_cam": preset_alias(key)}
                captured.append(f"voltage_cam preset {key!r}")
            if vcam.panel is not None:
                hz = vcam.panel.rate_request_hz
                recipe["camera_rate_hz"] = {"voltage_cam": hz}
                captured.append(f"voltage_cam rate {hz:g} Hz" if hz > 0
                                else "voltage_cam rate Max")
            if hasattr(vcam, "binning"):
                n = vcam.binning()
                recipe["camera_binning"] = {"voltage_cam": n}
                captured.append(f"voltage_cam binning {n}x{n}")
            if vcam.panel is not None:
                mode = vcam.panel.get_config().trigger_mode
                recipe["camera_trigger"] = {
                    "voltage_cam": mode == TRIGGER_MODES[1]}
                captured.append(f"voltage_cam trigger {mode!r}")

        if not captured:
            QMessageBox.information(
                self, "Nothing to capture",
                "Neither the DMD (in All ON) nor the voltage camera is "
                "loaded, so there's no current state to save. Load them "
                "(or set the DMD to All ON) and try again, or write the "
                "entry into modes.json by hand.")
            return

        modes[name] = recipe
        config.save_modes(modes)
        self._modes = modes
        if self._mode_combo.findText(name) < 0:
            self._mode_combo.addItem(name)
        self._mode_combo.setCurrentText(name)
        self.status(f'Saved preset "{name}": {", ".join(captured)}')

    def latest_frame(self, key: str):
        """The cached newest frame; never commands the camera."""
        return self._call(key, "last_frame")

    def latest_frame_preset(self, key: str) -> str | None:
        """The preset the cached frame was captured under — not
        `camera_preset`, which can name a switch not yet in effect."""
        return self._call(key, "last_frame_preset")

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        if self._save_panel is not None:
            self._save_panel.set_expected_rate(mbps, writer_mbps)

    def on_worker_error(self, msg: str) -> None:
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "device"
        self.status(f"{name}: {msg}")
        print(f"[worker error] {name}: {msg}")
