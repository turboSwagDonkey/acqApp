"""
The PMT/camera mirror's adapter. Manual only (operator): ThorImage drives
both switches over a port acqApp can't share, so no worker or controller,
just the operator's assertion logged to `/mirror`. No settings tab: a
Camera | PMT slide switch in the status bar.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.devices.mirror.settings import CAMERA, PMT, MirrorSettings
from acqApp.widgets import SlideSwitch

# Scalar streams are always written float64 (acq/writer.py) — the human
# label lives in metadata, the file gets this encoding instead.
_CODE = {CAMERA: 0.0, PMT: 1.0}

_TIP = ("Flip it after setting BOTH ThorImage switches (galvo, and the "
        "visualizer path): acqApp can't read either one itself, since "
        "ThorImage holds the controller's serial port while it runs.\n"
        "Left: camera (galvo out). Right: PMT (galvo in).")


class MirrorModule(ModuleAdapter):
    key = "mirror"
    tab_label = "Mirror"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._rec = None
        self._toggle_count = 0
        self._state = config.load_dataclass(MirrorSettings, self.key).state
        self._box: QWidget | None = None
        self.switch: SlideSwitch | None = None

    def status_widget(self) -> QWidget:
        if self._box is None:
            self.switch = SlideSwitch("Camera", "PMT", self.key)
            self.switch.setToolTip(_TIP)
            self.switch.setChecked(self._state == PMT)
            self.switch.toggled.connect(
                lambda on: self._on_toggle(PMT if on else CAMERA))
            self._box = QWidget()
            lay = QHBoxLayout(self._box)
            lay.setContentsMargins(6, 0, 6, 0)
            lay.setSpacing(6)
            lbl = QLabel("Mirror")
            lbl.setToolTip(_TIP)
            lay.addWidget(lbl)
            lay.addWidget(self.switch)
        return self._box

    @property
    def settings(self) -> MirrorSettings:
        return MirrorSettings(state=self._state)

    def _on_toggle(self, state: str) -> None:
        self._state = state
        config.save_settings(self.key, asdict(self.settings))
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
        return {"mirror_initial_state": self._state,
                "mirror_code_camera":   _CODE[CAMERA],
                "mirror_code_pmt":      _CODE[PMT]}

    def final_metadata(self) -> dict[str, Any]:
        return {"mirror_toggle_count": self._toggle_count}
