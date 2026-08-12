"""
The air puffer's adapter: an output, so it has a controller and no worker.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.modules.base import ModuleAdapter
from acqApp.puffer.control import (MockPufferController, PufferController,
                                   PufferSettings, SettingsPanel as PufferPanel)


class PufferModule(ModuleAdapter):
    key = "puffer"
    tab_label = "Puffer"

    def build_panel(self) -> QWidget:
        self.panel = PufferPanel(config.load_dataclass(PufferSettings, self.key))
        self.panel.test_requested.connect(self.fire)
        self.panel.settings_changed.connect(self._on_settings)
        # Arm the shared-clock trigger bus: the puff fires at t = at_s and is
        # logged on the session clock (trigger_fired -> on_trigger -> fire).
        self.panel.schedule_requested.connect(
            lambda at_s, dur: self.win.sync.schedule_trigger(self.key, at_s, dur))
        self.panel.clear_schedule_requested.connect(self.win.sync.clear_triggers)
        return self.panel

    def build_controller(self, emulate: bool) -> None:
        # Built from the panel, so the configured DO line is the one that fires.
        s = self.panel.settings if self.panel is not None else None
        self.controller = (MockPufferController(s) if emulate
                           else PufferController(s))

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        if self.controller is not None:
            self.controller.apply_settings(s)

    def fire(self, duration: float | None = None) -> None:
        """Fire now. With no duration this uses the panel's Duration value —
        which is what "Test puff" is for: verifying the puff you scheduled."""
        if self.controller is not None:
            self.controller.fire(duration)

    def on_trigger(self, name: str, duration: float) -> None:
        if name == self.key:
            self.fire(duration)

    def attach_sink(self, rec) -> None:
        if self.controller is not None:
            self.controller.set_sink(lambda d: rec.put("puffer", d))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        return {"puffer_channel":    s.channel,
                "puffer_duration_s": s.duration_s}
