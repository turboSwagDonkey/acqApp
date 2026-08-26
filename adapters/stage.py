"""
The XY stage's adapter. The calibration itself is shared with the standalone
`stage_control/` app and lives in `devices/stage/settings.py`.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.devices.stage.acquisition import StagePollWorker
from acqApp.devices.stage.control import MockStageController, StageController
from acqApp.devices.stage.panel import SettingsPanel as StageSettingsPanel
from acqApp.devices.stage.settings import load_settings as load_stage_settings


class StageModule(ModuleAdapter):
    key = "stage"
    tab_label = "Stage"
    # No plot: live position is the X/Y readout in the Stage tab, and it is
    # recorded as stage_x_um / stage_y_um.

    # Only what the panel itself owns. Calibration, soft limits and the origin
    # must keep coming from the shared stage_control config: StageSettings nests
    # two StageAxis objects, which do not survive this config's flat JSON.
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
        if self.panel is not None:
            self.panel.bind_controller(None)
        if self.controller is not None:
            # `MainWindow._on_run_toggled` stops every adapter in one loop, so
            # a raise here (unplugged stage, port gone mid-session) would strand
            # every module after this one with its worker still running.
            try:
                self.controller.close()
            except Exception as e:
                print(f"[stage] close failed ({type(e).__name__}: {e})")
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

    # ── what an experiment routine may drive (acq.devices.StageTarget) ──
    def stage_target(self):
        """Itself, once connected. None before that, so a routine is refused
        rather than starting and failing at its first move."""
        return self if self.controller is not None else None

    def move_to(self, x_um: float | None, y_um: float | None) -> None:
        """MOTION. One axis at a time, because the controller commands one."""
        if self.controller is None:
            raise RuntimeError("stage not connected")
        for which, um in (("x", x_um), ("y", y_um)):
            if um is not None:
                self.controller.move_to_um(which, float(um))

    def stop_motion(self) -> None:
        if self.controller is not None:
            self.controller.stop_all()

    def limits_um(self):
        """The SHARED calibration's soft limits — the same ones the panel
        clamps to, read live so a recalibration reaches a routine too."""
        s = self.panel.settings if self.panel is not None else load_stage_settings()
        return (s.x.soft_limits_um(), s.y.soft_limits_um())
