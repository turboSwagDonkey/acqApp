"""The XY stage's adapter; calibration lives in `devices/stage/settings.py`.

The connection is "always-on" (build_controller/close_controller), not
per-session: positioning happens before Live view, and the poller needs no
session clock. Opening the port is safe; motion still needs a button press
(`SettingsPanel._call`). A session only adds recording the position.
"""
from __future__ import annotations

import time
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.acq.devices import ModuleHost
from acqApp.devices.stage.acquisition import StagePollWorker
from acqApp.devices.stage.control import MockStageController, StageController
from acqApp.devices.stage.panel import SettingsPanel as StageSettingsPanel
from acqApp.devices.stage.settings import load_settings as load_stage_settings


MOVE_GRACE_S = 0.1


class StageModule(ModuleAdapter):
    key = "stage"
    tab_label = "Stage"
    _move_issued_at = float("-inf")

    # Only what the panel owns; calibration stays in the shared config
    # (nested StageAxis objects don't fit this flat JSON).
    _PANEL_KEYS = ("port", "poll_hz", "frame_rotation_deg")

    def __init__(self, win: ModuleHost) -> None:
        super().__init__(win)
        # Its own display timer: the window's only ticks during a session.
        self._disp_timer = QTimer()
        self._disp_timer.setInterval(33)
        self._disp_timer.timeout.connect(self.update_display)

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
        try:
            s.frame_rotation_deg = float(saved.get("frame_rotation_deg"))
        except (TypeError, ValueError):
            pass
        self.panel = StageSettingsPanel(s)
        self.panel.settings_changed.connect(self._save)
        self.panel.save_fov_requested.connect(self.save_fov)
        return self.panel

    def _save(self, s) -> None:
        config.save_settings(self.key,
                             {k: getattr(s, k) for k in self._PANEL_KEYS})

    def probe_kwargs(self) -> dict[str, Any]:
        try:
            return {"stage_port": self.panel.settings.port}
        except Exception:
            return {}

    # ── connection ──
    def build_controller(self, emulate: bool) -> None:
        s = self._settings()
        ctrl = MockStageController(s) if emulate else StageController(s)
        try:
            ctrl.connect()
        except Exception as e:
            self.win.status(f"stage: could not connect ({e})")
            return
        self.win.status(f"stage: connected ({ctrl.backend_kind})")
        self.controller = ctrl
        self._adopt(StagePollWorker(ctrl, s.poll_hz))
        self.worker.start()
        self.panel.bind_controller(ctrl)
        self._disp_timer.start()

    def close_controller(self) -> None:
        # Each step independent, so a dead port can't strand a half teardown.
        self._disp_timer.stop()
        if self.panel is not None:
            self.panel.bind_controller(None)
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        if self.controller is not None:
            try:
                self.controller.close()
            except Exception as e:
                print(f"[stage] close failed ({type(e).__name__}: {e})")
            self.controller = None

    def start(self) -> None:
        pass    # running since build_controller

    def stop(self) -> None:
        pass    # the connection outlives the session

    def update_display(self) -> None:
        pos = self.worker.get_latest() if self.worker is not None else None
        if pos is not None:
            self.panel.set_readout(pos[0], pos[1],
                                   pos[2] if len(pos) > 2 else None)

    # ── saved FOVs ──
    def save_fov(self) -> None:
        """Save the panel's displayed position plus a camera snapshot. Reads
        only; never commands the stage or camera."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        from acqApp.devices.stage import fov_store

        if not self.panel.connected:
            QMessageBox.information(
                self.panel, "Stage not connected",
                "The stage isn't connected — check the port on the Stage "
                "tab and that the hardware is powered on.")
            return
        x_um, y_um, z_um = self.panel.current_position
        name, ok = QInputDialog.getText(self.panel, "Save FOV", "Name:")
        name = name.strip()
        if not ok or not name:
            return
        path = fov_store.save(
            name, x_um, y_um, z_um=z_um,
            camera_preset=self.win.camera_preset("voltage_cam"),
            png_bytes=self._snapshot_png())
        self.win.status(f'Saved FOV "{name}" at {x_um:.0f}, {y_um:.0f} µm '
                        f"({path.name})")

    def _snapshot_png(self) -> bytes | None:
        """The newest camera frame as the preview shows it (downsampled,
        1-99 % stretch), or None."""
        frame = self.win.latest_frame("voltage_cam")
        if frame is None:
            return None
        import io

        import numpy as np
        from PIL import Image

        from acqApp.adapters.base import DISP_DS

        small = frame[::DISP_DS, ::DISP_DS]
        lo, hi = np.percentile(small, (1, 99))
        span = max(float(hi) - float(lo), 1.0)
        img8 = np.clip((small.astype(np.float32) - lo) / span * 255.0,
                       0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(img8, mode="L").save(buf, format="PNG")
        return buf.getvalue()

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(pos: tuple[float, ...]) -> None:
            rec.put("stage_x_um", pos[0])
            rec.put("stage_y_um", pos[1])
            if len(pos) > 2:                 # only on a rig with Z
                rec.put("stage_z_um", pos[2])

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"stage_port": s.port, "stage_poll_hz": s.poll_hz,
                "stage_z_enabled": s.has_z}

    # ── what a routine may drive ──
    def stage_target(self):
        """None until connected, so a routine is refused up front."""
        return self if self.controller is not None else None

    def move_to(self, x_um: float | None, y_um: float | None,
               z_um: float | None = None) -> None:
        """MOTION, one axis at a time."""
        if self.controller is None:
            raise RuntimeError("stage not connected")
        for which, um in (("x", x_um), ("y", y_um), ("z", z_um)):
            if um is not None:
                self.controller.move_to_um(which, float(um))
        self._move_issued_at = time.monotonic()

    def is_moving(self) -> bool:
        """True for a short grace after a command: the status bit lags."""
        if self.controller is None:
            raise RuntimeError("stage not connected")
        if time.monotonic() - self._move_issued_at < MOVE_GRACE_S:
            return True
        return self.controller.is_moving()

    def stop_motion(self) -> None:
        """Guarded: called exactly when the link may already be dead."""
        if self.controller is not None:
            try:
                self.controller.stop_all()
            except Exception as e:      # noqa: BLE001
                print(f"[stage] stop_all failed ({type(e).__name__}: {e})")

    def _settings(self):
        """Live, so a recalibration reaches a routine; off disk before the
        panel exists."""
        return (self.panel.settings if self.panel is not None
                else load_stage_settings())

    def limits_um(self):
        s = self._settings()
        return (s.x.soft_limits_um(), s.y.soft_limits_um())

    def has_z(self) -> bool:
        return self._settings().has_z

    def z_limits_um(self):
        s = self._settings()
        return s.z.soft_limits_um() if s.has_z else None

    def active_fov_name(self) -> str:
        return self.panel.active_fov_name if self.panel is not None else ""
