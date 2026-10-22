"""Visual stim's adapter (visStimCode's guiVisStimDAQ.m/runStimManager.m).

The controller's trigger is the shared clock tick (`win.sync.tick`), which
only runs while live, so Run turns live view on if needed and off again only
if it did. No per-session worker: the controller is always-on, built in
build_controller. Visuomotor reads the wheel through `win.signal_sources()`,
so without a wheel the drift just stays at zero.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.devices.vis_stim.control import VisStimController
from acqApp.devices.vis_stim.panel import SettingsPanel
from acqApp.devices.vis_stim.settings import VisStimSettings


class VisStimModule(ModuleAdapter):
    key = "vis_stim"
    tab_label = "Visual stim"

    controller: VisStimController | None   # narrows ModuleAdapter.controller

    def __init__(self, win) -> None:
        super().__init__(win)
        self._rec = None
        # Run turned live view on, so it turns it off (RoutinesModule's
        # `_own_rec` twin).
        self._own_live = False
        # This recording's runs: the controller's (trial_log, stats), trials logged before Record.
        self._runs: list[tuple[list, dict, int]] = []

    # ── construction ──
    def build_panel(self) -> QWidget:
        self.panel = SettingsPanel(self._settings())
        self.panel.settings_changed.connect(self._save)
        self.panel.run_requested.connect(self._run)
        self.panel.stop_requested.connect(self._stop)
        return self.panel

    def _settings(self) -> VisStimSettings:
        return VisStimSettings.from_dict(config.load_settings(self.key))

    def _save(self, s: VisStimSettings) -> None:
        config.save_settings(self.key, s.to_dict())
        if self.controller is not None:
            self.controller.apply_settings(s)

    # ── the always-on controller ──
    def build_controller(self, emulate: bool) -> None:
        s = self.panel.settings if self.panel is not None else VisStimSettings()
        self.controller = VisStimController(s, wheel_speed=self._read_wheel_speed)
        # One shared-clock tick = one trigger pulse.
        self.win.sync.tick.connect(self.controller.on_tick)
        self.controller.progress_changed.connect(self.panel.set_progress)
        self.controller.progress_fraction.connect(
            self.panel.set_progress_fraction)
        self.controller.run_state_changed.connect(self._on_run_state)
        self.controller.trial_boundary.connect(self._on_trial_boundary)

    def _read_wheel_speed(self) -> tuple[float, float] | None:
        """Looked up per call, so a hot-unloaded wheel is seen. The live
        EMA, not the ~1 s-lagged recorded speed."""
        for src in self.win.signal_sources():
            if src.key == "wheel_speed_live":
                return src.read()
        return None

    def close_controller(self) -> None:
        if self.controller is not None:
            try:
                self.win.sync.tick.disconnect(self.controller.on_tick)
            except (TypeError, RuntimeError):
                pass
        super().close_controller()

    def _run(self) -> None:
        if self.controller is None:
            return
        was_live = self.win.set_live(True)
        if self.controller.run():
            self._own_live = not was_live
        elif not was_live:
            self.win.set_live(False)   # refused (e.g. no trials) — put it back

    def _stop(self) -> None:
        if self.controller is not None:
            self.controller.stop()

    def _on_run_state(self, state: str) -> None:
        self.panel.set_run_state(state)
        if state == "PRIMING" and self._rec is not None:
            self._note_run()
        if state == "IDLE" and self._own_live:
            self._own_live = False
            self.win.set_live(False)

    def busy_reason(self) -> str:
        if self.controller is not None and self.controller.phase != "IDLE":
            return "stop the visual stimulus first — a run is in progress"
        return ""

    # ── recording ──
    def attach_sink(self, rec) -> None:
        self._rec = rec
        self._runs = []
        if self.controller is not None and self.controller.phase != "IDLE":
            # Already running: its earlier trials have no marks in this file.
            self._note_run(skip=len(self.controller.trial_log))

    def _note_run(self, skip: int = 0) -> None:
        c = self.controller
        self._runs.append((c.trial_log, c.last_run_stats, skip))

    def detach_sink(self) -> None:
        super().detach_sink()
        self._rec = None

    def _on_trial_boundary(self, index: int, opening: bool, _payload) -> None:
        """+(index+1) opening, -(index+1) closing, as routines' `_put`.
        Params and outcome go to final_metadata(): rec.put() takes floats."""
        rec = self._rec
        if rec is None:
            return
        rec.put("vis_stim", float(index + 1) if opening else -float(index + 1))

    def metadata(self) -> dict[str, Any]:
        s = self.panel.settings if self.panel is not None else VisStimSettings()
        d = s.to_dict()
        return {
            "vis_stim_trial_type":        d["trial_type"],
            "vis_stim_screen_index":      d["screen_index"],
            "vis_stim_stretch_to_screen": d["stretch_to_screen"],
            # The only record of e.g. "which orientation was trial 4".
            "vis_stim_params":            json.dumps(d["params"]),
            "vis_stim_loops":             json.dumps(d["loops"]),
            "vis_stim_started":           False,
        }

    def final_metadata(self) -> dict[str, Any]:
        """This recording's runs only; trial indices restart each run, as
        the /vis_stim marks do."""
        runs = self._runs
        kept = [(n, log[skip:]) for n, (log, _, skip) in enumerate(runs, 1)]
        return {
            "vis_stim_started":          bool(runs),
            "vis_stim_runs":             len(runs),
            "vis_stim_trials_total":     sum(st.get("trials_total", 0)
                                             for _, st, _ in runs),
            "vis_stim_trials_completed": sum(not rec.get("interrupted", False)
                                             for _, log in kept for rec in log),
            "vis_stim_aborted":          any(st.get("aborted", False)
                                             for _, st, _ in runs),
            # Every trial recorded, with params and outcome.
            "vis_stim_trials":           json.dumps(
                [{"run": n, **rec} for n, log in kept for rec in log]),
        }
