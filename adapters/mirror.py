"""
The PMT/camera mirror's adapter. Manual only (PLAN.md §6): two ThorImage
switches (galvo in/out, visualizer path camera/PMT) move together and are
driven by ThorImage over a serial connection acqApp cannot share, so there
is no worker and no controller here — just a panel the operator clicks to
say which way they set both, logged to `/mirror` like `/dmd`'s boundaries.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.devices.mirror.panel import SettingsPanel as MirrorPanel
from acqApp.devices.mirror.settings import CAMERA, PMT, MirrorSettings

# Scalar streams are always written float64 (acq/writer.py) — the human
# label lives in metadata, the file gets this encoding instead.
_CODE = {CAMERA: 0.0, PMT: 1.0}


class MirrorModule(ModuleAdapter):
    key = "mirror"
    tab_label = "Mirror"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._rec = None
        self._toggle_count = 0

    def build_panel(self) -> QWidget:
        self.panel = MirrorPanel(config.load_dataclass(MirrorSettings, self.key))
        self.panel.state_changed.connect(self._on_toggle)
        return self.panel

    def _on_toggle(self, state: str) -> None:
        config.save_settings(self.key, asdict(self.panel.settings))
        if self._rec is not None:
            self._toggle_count += 1
            self._rec.put("mirror", _CODE[state])
        self.win.status(f"mirror: operator set {state}"
                        + ("" if self._rec is not None else
                           " (not recording — not logged)"))

    # ── recording ──
    def attach_sink(self, rec) -> None:
        self._toggle_count = 0
        self._rec = rec

    def detach_sink(self) -> None:
        super().detach_sink()
        self._rec = None

    def metadata(self) -> dict[str, Any]:
        return {"mirror_initial_state": self.panel.settings.state,
                "mirror_code_camera":   _CODE[CAMERA],
                "mirror_code_pmt":      _CODE[PMT]}

    def final_metadata(self) -> dict[str, Any]:
        return {"mirror_toggle_count": self._toggle_count}
