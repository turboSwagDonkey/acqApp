"""
The XY stage's adapter. The calibration itself is shared with the standalone
`stage_control/` app and lives in `acqApp/stage/settings.py`.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.modules.base import ModuleAdapter
from acqApp.stage.acquisition import StagePollWorker
from acqApp.stage.control import MockStageController, StageController
from acqApp.stage.settings import (SettingsPanel as StageSettingsPanel,
                                   load_settings as load_stage_settings)


class StageModule(ModuleAdapter):
    key = "stage"
    tab_label = "Stage"
    # No plot: live position is the X/Y readout in the Stage tab, and it is
    # recorded as stage_x_um / stage_y_um.

    # Everything else about the stage (axis calibration, soft limits, the
    # origin) lives in the config shared with the standalone stage_control app
    # and must keep coming from there — StageSettings nests two StageAxis
    # objects, which do not survive a round trip through this config's flat
    # JSON. Only the two fields the panel itself owns are persisted here.
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
            # Guarded for the same reason `ModuleAdapter.close_controller` is:
            # `MainWindow._on_run_toggled` stops every adapter in one unguarded
            # loop, so a serial close that raises here — an unplugged stage, a
            # port that went away mid-session — would abort the loop and leave
            # the modules after this one with their worker threads still
            # running. A stage we cannot close is not a reason to leave the
            # camera acquiring.
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
