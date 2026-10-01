"""The closed loop's adapter: wires the instrument-agnostic rule to the loaded
modules' signal sources and the shared trigger bus. Last in MODULES, so every
source exists before its panel asks; never reaches into another adapter."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.closed_loop import (ClosedLoopWorker, LoopSettings, SignalSource,
                                SettingsPanel as LoopPanel)
from acqApp.adapters.base import ModuleAdapter


class ClosedLoopModule(ModuleAdapter):
    key = "closed_loop"
    tab_label = "Closed loop"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._sources: dict[str, SignalSource] = {}
        self._reported_fires = 0        # last count shown on the status bar

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = LoopPanel(config.load_dataclass(LoopSettings, self.key))
        self._refresh_offers()
        self.panel.settings_changed.connect(self._on_settings)
        self.panel.armed_changed.connect(self._on_armed)
        return self.panel

    def on_modules_changed(self) -> None:
        if self.panel is not None:
            self._refresh_offers()

    def _refresh_offers(self) -> None:
        """Also per session: the wheel's units follow its scaling."""
        self._sources = {s.key: s for s in self.win.signal_sources()}
        self.panel.set_sources(list(self._sources.values()))
        self.panel.set_targets(self.win.module_keys())

    def _on_settings(self, s) -> None:
        config.save_settings(self.key, asdict(s))
        if self.worker is not None:
            self.worker.configure(s)

    def _on_armed(self, on: bool) -> None:
        if self.worker is not None:
            self.worker.set_armed(on)
        if not on:
            self.win.status("closed loop disarmed")
        elif self.worker is None:
            self.win.status("closed loop ARMED — it runs when a session starts")
        else:
            s = self.panel.settings
            self.win.status(f"closed loop ARMED — {s.source} {s.comparison} "
                            f"{s.threshold:g} fires the {s.target}")

    # ── session ──
    def build_session(self, emulate: bool) -> None:
        self._refresh_offers()
        s = self.panel.settings
        src = self._sources.get(s.source)
        if src is None:
            # Otherwise an armed rule that can never fire looks merely unmet.
            self.win.status("closed loop: no signal source loaded — rule idle")
            return
        self._reported_fires = 0
        worker = self._adopt(ClosedLoopWorker(src, s))
        worker.set_armed(self.panel.armed)
        worker.fired.connect(self._on_fired)

    def stop(self) -> None:
        super().stop()
        self.panel.clear_readout()

    def _on_fired(self, target: str, duration: float, value: float) -> None:
        """Queued onto the GUI thread only because `connect()` ran there (not a
        QObject); keep it to one call, the status line is in update_display."""
        self.win.sync.fire(target, duration)

    # ── display ──
    def update_display(self) -> None:
        latest = self.worker.get_latest() if self.worker is not None else None
        if latest is None:
            return
        self.panel.set_readout(*latest)
        n = latest[2]
        if n != self._reported_fires:
            self._reported_fires = n
            s = self.panel.settings
            self.win.status(f"closed loop → {s.target}  "
                            f"({latest[0]:+.3g}, {n} this session)")

    # ── recording ──
    def attach_sink(self, rec) -> None:
        if self.worker is None:
            return

        def sink(event: tuple[float | None, float]) -> None:
            # Stamped at the sample that caused the fire.
            value, at = event
            rec.put("closed_loop", float(value or 0.0), at=at)

        self.worker.set_sink(sink)

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings
        src = self._sources.get(s.source)
        return {
            # Never armed vs armed-but-unmet both leave /closed_loop empty.
            "loop_armed":        self.panel.armed,
            "loop_source":       s.source,
            "loop_source_units": src.units if src is not None else "",
            "loop_comparison":   s.comparison,
            "loop_threshold":    s.threshold,
            "loop_hold_s":       s.hold_s,
            "loop_refractory_s": s.refractory_s,
            "loop_retrigger":    s.retrigger,
            "loop_target":       s.target,
            "loop_duration_s":   s.duration_s,
            "loop_max_fires":    s.max_fires,
        }

    def final_metadata(self) -> dict[str, Any]:
        if self.worker is None:
            return {"loop_fires": 0, "loop_fires_session": 0}
        return {
            "loop_fires":         self.worker.recorded_fires,
            # Larger if it fired under Live view: actuated, unfiled.
            "loop_fires_session": self.worker.n_fires,
        }
