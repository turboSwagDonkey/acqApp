"""
The DMD projector's adapter: an output that also answers to the trigger bus.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import ProjectorController
from acqApp.devices.dmd import alp
from acqApp.devices.dmd.control import DmdController, DmdSettings, MockDmdController
from acqApp.devices.dmd.panel import SettingsPanel as DmdPanel
from acqApp.adapters.base import ModuleAdapter


class DmdModule(ModuleAdapter):
    key = "dmd"
    tab_label = "DMD"

    controller: ProjectorController | None   # narrows ModuleAdapter.controller

    def build_panel(self) -> QWidget:
        self.panel = DmdPanel(self._settings())
        # Route through this adapter, not straight to the controller: the
        # controller is rebuilt whenever Emulate is toggled, and the panel binds
        # its signals only once.
        self.panel.load_requested.connect(self.load)
        self.panel.display_requested.connect(self.display)
        self.panel.stop_requested.connect(self.stop_display)
        self.panel.settings_changed.connect(self._save)
        return self.panel

    def _settings(self) -> DmdSettings:
        """Saved settings, defaulting to the standalone DMD app's alignment.

        Scale and rotation register the pattern to the optics, and that
        alignment lives in `dmdGUI_project`. A fresh install at 100 % / 0° would
        project in the wrong place while looking correctly configured — the same
        arrangement the stage has with `stage_control/config.json`.
        """
        s = config.load_dataclass(DmdSettings, self.key)
        saved = config.load_settings(self.key)
        shared = alp.sibling_config()
        if "scale_pct" not in saved and "defaultScale" in shared:
            s.scale_pct = float(shared["defaultScale"])
        if "rotation_deg" not in saved and "defaultRot" in shared:
            s.rotation_deg = float(shared["defaultRot"])
        return s

    def _save(self, s) -> None:
        d = asdict(s)
        d["pattern_path"] = str(s.pattern_path) if s.pattern_path else None
        config.save_settings(self.key, d)

    def build_controller(self, emulate: bool) -> None:
        s = self.panel.settings if self.panel is not None else DmdSettings()
        real = False
        if emulate:
            self.controller = MockDmdController(s)
        else:
            try:
                self.controller = DmdController(s)
                real = True
            except Exception as e:      # noqa: BLE001 — any ALP/driver failure
                # Most often the ALP is still held by the standalone
                # dmdGUI_project app: only one process can own it.
                print(f"[main] DMD unavailable ({type(e).__name__}: {e}) — "
                      f"using mock. If the standalone DMD app is open, close "
                      f"it and toggle Emulate off again.")
                self.controller = MockDmdController(s)
        if self.panel is not None:
            self.panel.set_device(self.controller.device_name,
                                  self.controller.resolution, real)
        # A pattern the panel is showing must be uploaded to this controller
        # too, or Display projects the fresh controller's default while the
        # panel names a file.
        path = s.pattern_path
        if path is not None and Path(path).is_file():
            self.controller.load_pattern(Path(path))

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

    def on_trigger(self, name: str, duration: float) -> None:
        """Project on a trigger — the closed loop's other output.

        The DMD has no pulse of its own: the ALP free-runs its sequence once
        started and holds until Stop. So a *timed* stimulus is display-now plus
        a single-shot stop, and `duration <= 0` leaves the pattern up.
        """
        if name != self.key:
            return
        self.display()
        if duration > 0:
            QTimer.singleShot(int(duration * 1000), self.stop_display)

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            # DMD frames are logged straight from its thread (no GUI-thread hop),
            # stamped on the shared clock like every other stream.
            self.controller.set_sink(lambda idx: rec.put("dmd", float(idx)))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        c = self.controller
        # Both twins declare device_name/resolution/on_pixels
        # (`ProjectorController`), so these are read, not guessed: the old
        # `getattr(c, "device_name", "none")` filed a session that really
        # projected as one that never did (§5b A1).
        w, h = c.resolution if c is not None else (0, 0)
        return {
            "dmd_on_time_ms":  s.on_time_ms,
            "dmd_static_hold": s.static_hold,
            "dmd_trigger":     s.trigger_mode,
            "dmd_repeats":     s.n_repeats,
            # What was projected, and where: without the geometry a recorded
            # stimulus cannot be located in the FOV afterwards, and without the
            # name a real session is indistinguishable from a mock one.
            "dmd_device":      c.device_name if c is not None else "none",
            "dmd_width":       w,
            "dmd_height":      h,
            "dmd_pattern":     s.pattern_path.name if s.pattern_path else "",
            "dmd_scale_pct":   s.scale_pct,
            "dmd_rotation_deg": s.rotation_deg,
            "dmd_offset_x":    s.offset_x,
            "dmd_offset_y":    s.offset_y,
            "dmd_invert":      s.invert,
            # An all-on frame ignores the pattern and the geometry above, so
            # without this the recorded scale/rotation would describe a
            # placement that was never used.
            "dmd_all_on":      s.all_on,
            "dmd_fit":         s.fit,
            # 0 mirrors on is a dark panel — a Display that "worked" and
            # projected nothing looks identical in every other field here.
            "dmd_on_pixels":   c.on_pixels if c is not None else 0,
        }
