"""
The experiment routine's adapter — the only code here that touches a real
stage or a real projector.

Everything that decides lives in `routines/`, Qt-free. This builds the
`RoutineHooks` pointing that engine at the loaded modules, reaching them
through `ModuleHost` (`stage_target`/`pattern_target`) rather than by importing
their adapters — the stage does not know routines exist.

**The tick runs on the GUI thread**, on a QTimer, not in a worker. It is
non-blocking, and the closed loop already goes out of its way to get actuation
*back* here (its `fired` signal is queued); this is that, without the hop. A
stage move is a short serial write; the position poller does the polling, on
its own thread.

**Save mode `per_step` is accepted and validated but not yet rolled**: step
boundaries go into the one session file as `/routine`. Rolling one means
re-entering `MainWindow._start_recording` mid-session, and main.py is the
operator's file. Until then both modes produce one relatable file.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.routines.engine import Phase, RoutineEngine, RoutineHooks
from acqApp.routines.panel import SettingsPanel as RoutinePanel
from acqApp.routines.settings import RigLimits, Routine, validate

# How often the engine is asked to advance. A step boundary lands within one
# tick of its true instant; at 106 fps that is under three frames, and the
# boundary itself is recorded from the clock, not from the tick.
TICK_MS = 25

# The stream a "frames" step counts. The voltage camera is the imaging path an
# experiment is about; the pupil camera watches the animal.
FRAME_STREAM = "voltage_cam"


class RoutinesModule(ModuleAdapter):
    """Wires `routines/` into this window.

    Owns no device, and reads its neighbours only through the host: an
    instrument becomes routine-drivable by declaring `stage_target` or
    `pattern_target`, and nothing here changes.
    """
    key = "routines"
    tab_label = "Routines"

    def __init__(self, win) -> None:
        super().__init__(win)
        self._engine: RoutineEngine | None = None
        self._timer: QTimer | None = None
        self._rec = None                # the Recorder, while recording
        self._filed = 0                 # step boundaries handed to the file
        self._n_steps = 0               # steps in the routine that is running

    # ── construction ──
    def build_panel(self) -> QWidget:
        saved = config.load_settings(self.key).get("routine")
        self.panel = RoutinePanel(Routine.from_dict(saved or {}))
        self.panel.settings_changed.connect(self._save)
        self.panel.start_requested.connect(self._start)
        self.panel.pause_requested.connect(self._pause)
        self.panel.resume_requested.connect(self._resume)
        self.panel.skip_requested.connect(self._skip)
        self.panel.abort_requested.connect(self._abort)
        # Parented to the panel, so it dies with the UI rather than ticking on
        # into an unloaded module.
        self._timer = QTimer(self.panel)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        return self.panel

    def _save(self, routine: Routine) -> None:
        config.save_settings(self.key, {"routine": routine.to_dict()})

    # ── what the engine is allowed to do ──
    def _rig(self) -> RigLimits:
        """What the loaded modules can actually do, for validation."""
        stage = self.win.stage_target()
        x = y = None
        if stage is not None:
            try:
                x, y = stage.limits_um()
            except Exception:            # noqa: BLE001 — a stage mid-teardown
                x = y = None
        return RigLimits(x_um=x, y_um=y, has_stage=stage is not None,
                         has_dmd=self.win.pattern_target() is not None,
                         has_frames=self._rec is not None
                         and FRAME_STREAM in self.win.module_keys())

    def _hooks(self) -> RoutineHooks:
        stage = self.win.stage_target()
        dmd = self.win.pattern_target()
        clock = self.win.sync.clock
        rec = self._rec

        def frames() -> int | None:
            # What reached the FILE, not what the camera produced — the two
            # differ exactly when the write path is what is falling behind.
            return None if rec is None else rec.offered(FRAME_STREAM)

        def noop_move(_x, _y) -> None:
            raise RuntimeError("no stage loaded")

        return RoutineHooks(
            now=clock.now,
            frames=frames,
            move=stage.move_to if stage is not None else noop_move,
            stop_motion=stage.stop_motion if stage is not None else (lambda: None),
            set_pattern=dmd.set_pattern if dmd is not None else (lambda _p: None),
            light=dmd.set_light if dmd is not None else (lambda _on: None),
            begin_step=self._on_step_begin,
            end_step=self._on_step_end,
            log=self.win.status,
        )

    # ── run control ──
    def _start(self) -> None:
        """Validate first, and say every reason it cannot run."""
        routine = self.panel.settings
        problems = validate(routine, self._rig())
        if self._rec is None:
            problems.insert(0, "not recording — start the recording first, so "
                                "the steps land in a file")
        if problems:
            self.panel.show_problems(problems)
            self.win.status(f"routine refused: {problems[0]}")
            return
        self._filed = 0
        self._n_steps = len(routine.steps)
        self._engine = RoutineEngine(routine, self._hooks())
        self._engine.start()
        self._timer.start()
        self.win.status(f"routine '{routine.name}' started — "
                        f"{routine.total_steps()} step(s)")

    def _pause(self) -> None:
        if self._engine is not None:
            self._engine.pause()

    def _resume(self) -> None:
        if self._engine is not None:
            self._engine.resume()

    def _skip(self) -> None:
        if self._engine is not None:
            self._engine.skip()

    def _abort(self) -> None:
        if self._engine is not None:
            self._engine.abort()
        self._stop_ticking()

    def _stop_ticking(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _tick(self) -> None:
        """The engine's heartbeat. Guarded: an exception out of a Qt slot
        aborts the process, and this one drives the stage."""
        eng = self._engine
        if eng is None:
            self._stop_ticking()
            return
        try:
            eng.tick()
        except Exception as e:           # noqa: BLE001
            self._stop_ticking()
            self.win.status(f"routine tick failed ({type(e).__name__}: {e})")
            return
        if eng.phase == Phase.DONE:
            self._stop_ticking()

    # ── the file ──
    def _on_step_begin(self, run) -> None:
        """A step started. One `/routine` entry per boundary, on the shared
        clock — which is what makes the steps locatable in the file."""
        self._put(run, opening=True)

    def _on_step_end(self, run) -> None:
        self._put(run, opening=False)

    def _put(self, run, *, opening: bool) -> None:
        rec = self._rec
        if rec is None:
            return
        # +index on the way in, -(index+1) on the way out: one scalar stream
        # carries both edges, and the sign says which without a second stream.
        # `+1` because step 0's opening edge would otherwise be its own closing.
        rec.put("routine", float(run.index + 1) if opening
                else -float(run.index + 1))
        self._filed += 1

    # ── session / recording ──
    def attach_sink(self, rec) -> None:
        self._rec = rec

    def detach_sink(self) -> None:
        super().detach_sink()
        # Recording stopped under a running routine: it has nowhere to put its
        # steps and its "100 frames" has nothing to count. Stop it rather than
        # let it drive the stage into a file that is not open.
        if self._engine is not None and self._engine.running:
            self._engine.abort()
            self._stop_ticking()
            self.win.status("routine aborted — the recording stopped")
        self._rec = None

    def stop(self) -> None:
        """Session teardown. The routine cannot outlive the clock it times by."""
        if self._engine is not None and self._engine.running:
            self._engine.abort()
        self._stop_ticking()
        self._engine = None
        super().stop()

    def busy_reason(self) -> str:
        if self._engine is not None and self._engine.running:
            return ("stop the routine first — it holds an index into the "
                    "loaded modules")
        return ""

    # ── display ──
    def update_display(self) -> None:
        eng = self._engine
        if eng is None or self.panel is None:
            return
        if eng.phase == Phase.PAUSED:
            self.panel.set_state(eng.phase, f"PAUSED — {eng.fault}")
            return
        if eng.phase == Phase.DONE:
            self.panel.set_state(eng.phase,
                                 f"finished — {eng.steps_done()} step(s)")
            return
        i, cycle, attempt = eng.position
        step = eng.step
        where = f"step {i + 1}/{self._n_steps}  cycle {cycle + 1}"
        if attempt > 1:
            where += f"  (attempt {attempt})"
        if eng.phase == Phase.SETTLE:
            where += " — settling"
        elif step is not None:
            where += (f" — capturing {eng.progress() * 100:.0f} % of "
                      f"{step.length:g} {step.unit}")
        self.panel.set_state(eng.phase, where)

    # ── metadata ──
    def metadata(self) -> dict[str, Any]:
        r = self.panel.settings
        return {
            # The protocol as configured, in full: "which stage position was
            # step 4" cannot be recovered from the file any other way.
            "routine_name":       r.name,
            "routine_cycles":     r.cycles,
            "routine_save_mode":  r.save_mode,
            "routine_n_steps":    len(r.steps),
            "routine_steps":      _steps_json(r),
            # A routine that was configured and never started, and one that ran,
            # leave the same step list. This is what tells them apart.
            "routine_started":    False,
        }

    def final_metadata(self) -> dict[str, Any]:
        eng = self._engine
        if eng is None:
            return {"routine_started": False, "routine_steps_done": 0,
                    "routine_steps_interrupted": 0, "routine_fault": "",
                    "routine_runs": "[]"}
        return {
            "routine_started":           True,
            "routine_steps_done":        eng.steps_done(),
            "routine_steps_interrupted": sum(1 for x in eng.runs if x.interrupted),
            # Every execution, not just the counts. `/routine` carries the
            # boundaries but only a signed index, so without this a step that
            # was interrupted and repeated is indistinguishable from one that
            # ran twice — and WHICH one faulted is recoverable from nothing
            # else in the file. Session origin 0.0: in `single` mode the file
            # IS the session, so its clock already starts there.
            "routine_runs": json.dumps([x.attrs() for x in eng.runs]),
            # Empty unless it ended paused — a routine that finished clean and
            # one that was left paused at step 7 look alike without this.
            "routine_fault":             eng.fault if eng.phase == Phase.PAUSED
                                         else "",
            "routine_boundaries":        self._filed,
        }


def _steps_json(r: Routine) -> str:
    """The step list as it will be read back — HDF5 attributes are scalars, so
    the protocol travels as one JSON string, as the DMD's ROIs do."""
    return json.dumps(r.to_dict()["steps"])
