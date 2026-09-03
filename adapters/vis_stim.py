"""
Visual stim's adapter — drifting-grating stimulus, ported from visStimCode's
guiVisStimDAQ.m/runStimManager.m.

The .m code gated blank/stim phases on pulses from an external MCC DAQ line;
this rig has no such line, so this adapter feeds the controller the shared
session clock's own tick instead (`self.win.sync.tick` — `acq/sync.py`,
10 Hz by default), the same timing sequence every other module already uses.
That tick only runs while the session is live, so Run puts live view on
itself if it is not already (mirrors adapters/routines.py's `_open_recording`
doing the same for the record button) and only turns it back off once the run
finishes if it is the one that turned it on.

Otherwise unlike a camera or encoder module, vis_stim owns no per-session
acquisition worker: the run state machine is always-on, built in
build_controller like puffer/DMD's controllers, matching guiVisStimDAQ.m
building its own state at GUI construction.
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
    # The run/progress status should stay visible while the operator is on a
    # camera's settings page — the same reasoning RoutinesModule documents
    # for its own window.
    own_window = True

    controller: VisStimController | None   # narrows ModuleAdapter.controller

    def __init__(self, win) -> None:
        super().__init__(win)
        self._rec = None
        # True only when Run turned live view on itself — the twin of
        # RoutinesModule's `_own_rec`, and for the same reason: "stop what you
        # started" differs from "stop the operator's live view".
        self._own_live = False

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
        self.controller = VisStimController(s)
        # One shared-clock tick = one "trigger" pulse (see control.py).
        self.win.sync.tick.connect(self.controller.on_tick)
        self.controller.progress_changed.connect(self.panel.set_progress)
        self.controller.run_state_changed.connect(self._on_run_state)
        self.controller.trial_boundary.connect(self._on_trial_boundary)

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

    def detach_sink(self) -> None:
        super().detach_sink()
        self._rec = None

    def _on_trial_boundary(self, index: int, opening: bool, _payload) -> None:
        """+index on the way in, -(index+1) on the way out — one scalar
        stream carries both edges, same convention as
        adapters/routines.py's `_put`. The trial's params and outcome go into
        final_metadata() instead: Writer.write() coerces every scalar stream
        via float(), so nothing but a number can go through rec.put()."""
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
            # The protocol as configured, in full — "which orientation was
            # trial 4" cannot be recovered from the file any other way.
            "vis_stim_params":            json.dumps(d["params"]),
            "vis_stim_loops":             json.dumps(d["loops"]),
            "vis_stim_started":           False,
        }

    def final_metadata(self) -> dict[str, Any]:
        stats = (self.controller.last_run_stats if self.controller is not None
                 else {"trials_total": 0, "trials_completed": 0, "aborted": False})
        log = self.controller.trial_log if self.controller is not None else []
        return {
            "vis_stim_started":          stats.get("trials_total", 0) > 0,
            "vis_stim_trials_total":     stats.get("trials_total", 0),
            "vis_stim_trials_completed": stats.get("trials_completed", 0),
            "vis_stim_aborted":          stats.get("aborted", False),
            # Every trial actually run, with its params and outcome — the
            # same role adapters/routines.py's `routine_runs` plays for steps.
            "vis_stim_trials":           json.dumps(log),
        }
