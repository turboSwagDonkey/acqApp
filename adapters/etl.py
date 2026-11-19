"""Path 2's electrically tunable lens: an AO setpoint that offsets path 2's
focal plane from path 1's, logged on the clock and drawn on the stage's Z
gauge (through `ModuleHost.focus_offset`)."""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.acq.devices import FocusOffset
from acqApp.adapters.base import ModuleAdapter
from acqApp.console import short_error
from acqApp.devices.etl.control import EtlController, MockEtlController
from acqApp.devices.etl.panel import SettingsPanel as EtlPanel
from acqApp.devices.etl.settings import EtlSettings
from acqApp.sidebar import MiniText


class EtlModule(ModuleAdapter):
    key = "etl"                 # also its rigs.json channel/hardware key
    tab_label = "ETL (path 2)"
    rides_on = "stage"          # its plane is a Z position: by the stage controls

    def __init__(self, win) -> None:
        super().__init__(win)
        self._real = False

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = EtlPanel(config.load_dataclass(EtlSettings, self.key))
        self.panel.settings_changed.connect(self._on_settings)
        return self.panel

    def _on_settings(self, s: EtlSettings) -> None:
        config.save_settings(self.key, asdict(s), keep=("min_volts", "max_volts"))
        self._write(s)

    def build_controller(self, emulate: bool) -> None:
        """Real only with a channel named in rigs.json: unset reads as
        fitted there, and a guessed AO line may be another device's."""
        s = self.panel.settings
        chan = config.rig_channel(self.key)
        self._real = False
        if emulate:
            self.controller = MockEtlController(chan or "", s)
        elif not config.rig_has(self.key) or chan is None:
            print(f"[main] ETL: no channel for rig "
                  f"{config.active_rig() or '(none set)'} — continuing without it")
            self.controller = MockEtlController("", s)
        else:
            try:
                self.controller = EtlController(chan, s)
                self._real = True
            except Exception as e:      # noqa: BLE001 — no DAQ, line busy
                print(f"[main] ETL: not available on {chan} — {short_error(e)}; "
                      f"continuing without it")
                self.controller = MockEtlController(chan, s)
        self._write(s)

    def _write(self, s: EtlSettings) -> None:
        # A slot: a DAQ error escaping it would abort the app.
        if self.controller is not None:
            try:
                self.controller.apply_settings(s)
            except Exception as e:      # noqa: BLE001
                self.win.status(f"ETL: {type(e).__name__}: {e}")
        self.panel.set_output(self._output_text())

    def _output_text(self) -> str:
        off = self.focus_offset()
        if off is None:
            return "—"
        um = "" if off.um is None else f"  ({off.um:+.1f} µm)"
        return f"{off.volts:.3f} V{um}" + ("" if self._real else "  (emulated)")

    # ── path 2's focal plane, for the stage's Z gauge ──
    def focus_offset(self) -> FocusOffset | None:
        """What was written, not what the panel asks: a failed write must not
        show as the plane moved."""
        v = self.controller.volts if self.controller is not None else None
        if v is None:
            return None
        s = replace(self.panel.settings, volts=v)
        return FocusOffset(volts=v, um=s.offset_um())

    # ── what a routine may drive (none call it yet) ──
    def set_volts(self, v: float) -> None:
        self.panel.set_volts(v)

    def set_offset_um(self, um: float) -> bool:
        """False without a calibration."""
        return self.panel.set_offset_um(um)

    # ── display ──
    def mini_view(self) -> QWidget:
        return MiniText(lambda: (self._output_text(), "path 2 focus"))

    # ── recording ──
    def attach_sink(self, rec) -> None:
        c = self.controller
        if c is None:
            return
        c.set_sink(lambda v: rec.put("etl_volts", v))
        if c.volts is not None:     # the file opens with the held value
            rec.put("etl_volts", c.volts)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        c = self.controller
        return {
            "etl_device":    "NI AO" if self._real else "mock",
            "etl_channel":   c.channel if c is not None else "",
            "etl_volts":     c.volts if c is not None and c.volts is not None
                             else s.clamped(),
            "etl_min_volts": s.limits[0],
            "etl_max_volts": s.limits[1],
            # The offset is derived: the calibration that made it goes too.
            "etl_offset_um":     s.offset_um() if s.calibrated else "",
            "etl_cofocal_volts": s.cofocal_volts,
            "etl_um_per_volt":   s.um_per_volt,
        }
