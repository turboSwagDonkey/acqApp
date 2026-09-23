"""
The XY stage's adapter. The calibration itself is shared with the standalone
`stage_control/` app and lives in `devices/stage/settings.py`.

The connection is built and torn down with `build_controller`/`close_controller`
— the "always-on" pair every simple output (puffer, LED, DMD) already uses —
rather than `build_session`/`stop`. Positioning the stage is something an
operator does before ever pressing Live view, unlike a camera's frame stream,
which only means anything once a session's shared clock exists; the stage's
own poll loop needs no clock at all (`StagePollWorker` times itself off
`time.perf_counter()`). Opening the port is safe on its own — CLAUDE.md's
line is "device open/config is safe", it's COMMANDING motion that needs an
explicit button press, and that gate (`SettingsPanel._call`) is unchanged.
Live/Record only adds recording the position that was already being read.
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
    # No plot: live position is the X/Y(/Z) readout in the Stage tab, and it's
    # recorded as stage_x_um / stage_y_um (+ stage_z_um on a rig with a Z
    # stage — see StagePollWorker's docstring).

    # Only what the panel itself owns. Calibration, soft limits and the origin
    # must keep coming from the shared stage_control config: StageSettings nests
    # two StageAxis objects, which don't survive this config's flat JSON.
    _PANEL_KEYS = ("port", "poll_hz", "frame_rotation_deg")

    def __init__(self, win: ModuleHost) -> None:
        super().__init__(win)
        # The panel's readout needs refreshing whether or not Live view is
        # running MainWindow's own shared display timer — that one only ticks
        # session-scoped modules. Same 30 Hz, its own clock.
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

    # ── connection (always-on: see the module docstring) ──
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
        self.worker.start()             # no session to wait on — see __init__
        self.panel.bind_controller(ctrl)
        self._disp_timer.start()

    def close_controller(self) -> None:
        # Unbind the controls first, then stop the poller, then close the
        # link — the reverse of build_controller. Reached by module unload,
        # Emulate real<->mock swap, and app close alike; a raise here (port
        # gone, stage unplugged) must not strand it half torn-down, so every
        # step is independent of the others succeeding.
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
        pass    # already running since build_controller — a session adds
                # recording (attach_sink), it doesn't own the connection

    def stop(self) -> None:
        pass    # the connection outlives the session; see close_controller

    def update_display(self) -> None:
        pos = self.worker.get_latest() if self.worker is not None else None
        if pos is not None:
            # A 3rd element is Z (see StagePollWorker's docstring); absent on
            # a rig with no Z stage.
            self.panel.set_readout(pos[0], pos[1],
                                   pos[2] if len(pos) > 2 else None)

    # ── saved FOVs (position + snapshot; devices/stage/fov_store.py) ──
    def save_fov(self) -> None:
        """Handle the Stage panel's "Save current as FOV…" button. Reads
        only: the panel's own last-displayed position (NOT the poller's
        `get_latest()` — see SettingsPanel.current_position for why that one
        is a single-use value the ~30 Hz display tick already drains, so a
        second reader here would lose the race almost every time) and a
        camera snapshot via `ModuleHost.latest_frame` — never commands the
        stage or the camera."""
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
        """An 8-bit, contrast-stretched PNG of the newest voltage_cam frame —
        the same downsample/percentile treatment the live preview already
        applies (adapters/voltage_cam.py), so a saved FOV looks like what the
        operator was seeing, not a raw linear 16-bit dump. None if no frame
        has arrived yet; a FOV without a picture is still useful, just not
        recognizable by eye."""
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
            """Position is a 2- or 3-vector -> that many scalar streams
            sharing the timebase. stage_z_um only appears in the recording at
            all on a rig with a Z stage — no empty/NaN column on the rest."""
            rec.put("stage_x_um", pos[0])
            rec.put("stage_y_um", pos[1])
            if len(pos) > 2:
                rec.put("stage_z_um", pos[2])

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"stage_port": s.port, "stage_poll_hz": s.poll_hz,
                "stage_z_enabled": s.has_z}

    # ── what an experiment routine may drive (acq.devices.StageTarget) ──
    def stage_target(self):
        """Itself, once connected. None before that, so a routine is refused
        rather than starting and failing at its first move."""
        return self if self.controller is not None else None

    def move_to(self, x_um: float | None, y_um: float | None,
               z_um: float | None = None) -> None:
        """MOTION. One axis at a time, because the controller commands one."""
        if self.controller is None:
            raise RuntimeError("stage not connected")
        for which, um in (("x", x_um), ("y", y_um), ("z", z_um)):
            if um is not None:
                self.controller.move_to_um(which, float(um))
        self._move_issued_at = time.monotonic()

    def is_moving(self) -> bool:
        """Still travelling? Reads as moving for a short grace after a command,
        since the status bit can lag the command by a few ms."""
        if self.controller is None:
            raise RuntimeError("stage not connected")
        if time.monotonic() - self._move_issued_at < MOVE_GRACE_S:
            return True
        return self.controller.is_moving()

    def stop_motion(self) -> None:
        """Stop both axes. Guarded, because `StageTarget` says it must be and
        because this runs when the stage has ALREADY failed — a dead serial
        link is exactly when it's called. The routine engine catches it too,
        but a contract the implementation leans on its caller to keep isn't
        one."""
        if self.controller is not None:
            try:
                self.controller.stop_all()
            except Exception as e:      # noqa: BLE001 — link gone, port gone
                print(f"[stage] stop_all failed ({type(e).__name__}: {e})")

    def _settings(self):
        """The SHARED calibration, read live so a recalibration reaches a
        routine too, and off disk before the panel exists."""
        return (self.panel.settings if self.panel is not None
                else load_stage_settings())

    def limits_um(self):
        """The soft limits the panel clamps to — see `_settings`."""
        s = self._settings()
        return (s.x.soft_limits_um(), s.y.soft_limits_um())

    def has_z(self) -> bool:
        """Whether this rig has a Z (focus) axis a routine may move."""
        return self._settings().has_z

    def z_limits_um(self):
        """Z's soft limits, live like `limits_um()`; None with no Z stage."""
        s = self._settings()
        return s.z.soft_limits_um() if s.has_z else None

    # ── what the Save panel may ask (acq.devices.ModuleHost.active_fov_name) ──
    def active_fov_name(self) -> str:
        return self.panel.active_fov_name if self.panel is not None else ""
