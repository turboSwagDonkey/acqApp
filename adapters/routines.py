"""
The routine's adapter: builds the `RoutineHooks` that point the Qt-free
engine at loaded modules, via `ModuleHost` targets.

- Ticks on the GUI thread (QTimer), non-blocking.
- Start arms the camera (External edge) and opens the recording; the end
  closes it. A recording the operator started is left alone.
- A `trigger` step re-arms the camera, which latches after one edge. With
  Trigger→Record pairs the camera bursts that Record's length per edge
  instead, re-arming itself; the Record ends on the burst.
- `per_repeat`/`per_group` roll to a fresh file at a run boundary. The roll
  is deferred to `_tick`, after `eng.tick()` returns: the begin hook fires
  inside the engine's call stack, and code still to run there would
  overwrite what a re-entrant call did.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.routines.banner import RoutineBanner
from acqApp.routines.engine import Phase, RoutineEngine, RoutineHooks
from acqApp.devices.voltage_cam.presets import BURST_TIMES_MAX
from acqApp.routines.estimate import burst_frames, clock, remaining
from acqApp.routines.panel import SettingsPanel as RoutinePanel
from acqApp.routines.settings import (RigLimits, Routine, TIMED_KINDS, group_region_at,
                                      group_repeat_at, play_order,
                                      recording_region_at, validate)

# Under three frames at 106 Hz; boundaries are stamped from the clock anyway.
TICK_MS = 25

FRAME_STREAM = "voltage_cam"

# Ticks between frame-rate refreshes (each rebuilds another panel's config).
RATE_EVERY = 30

# A .dcimg roll stops the camera ~0.9 s; ten times that is a camera that
# isn't coming back.
HOLD_TIMEOUT_S = 10.0

EDGE_LOG_HEADER = "edge,session_s,wall_time,fov,trial,path\n"


class RoutinesModule(ModuleAdapter):
    """Wires `routines/` into this window. Owns no device."""
    key = "routines"
    tab_label = "Routines"
    own_window = True
    own_window_size = (728, 936)

    def __init__(self, win) -> None:
        super().__init__(win)
        self._engine: RoutineEngine | None = None
        self._timer: QTimer | None = None
        self._rec = None                # the Recorder, while recording
        self._filed = 0                 # boundaries handed to the file
        self._n_steps = 0
        self._group_repeat: list[tuple[int, int] | None] = []  # by order_position
        self._routine: Routine | None = None
        self._rate_tick = 0
        self._own_rec = False           # Start opened the recording
        # ── file rolling ──
        self._prepared = None           # (cycle, step) whose .dcimg is open
        self._armed_with_file = False   # that swap re-armed the trigger too
        self._pending_roll_run = None   # acted on in _tick
        self._file_group_key = None     # (cycle, group-or-None) of the open file
        self._filed_from = 0            # eng.runs[:_filed_from] are in older files
        self._pending_scope: dict[str, Any] = {}   # for the next metadata()
        self._rolling = False           # see detach_sink
        self._routine_origin = 0.0
        self._hold_t0: float | None = None   # see _holding_for_camera
        self._trial_count: dict[int, int] = {}   # files opened per bracket
        # One row per trigger edge, for matching to the stim rig's log.
        self._edge_log = None                    # Path, created at first edge
        self._edge_n = 0
        self._pending_edge = None   # (n, t, wall, (cycle, step)) until its file opens

    def _status(self, msg: str) -> None:
        """Status bar, and the console (which the rig actually watches) for
        everything but per-step progress."""
        self.win.status(msg)
        if not msg.startswith("step "):
            print(f"[routines] {msg}")

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
        self.panel.status_message.connect(self._status)
        self._banner = RoutineBanner()   # top-level, so closed with the panel
        self.panel.state_shown.connect(self._banner.show_state)
        self.panel.destroyed.connect(self._banner.close)
        self._timer = QTimer(self.panel)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.panel.set_frame_rate(self.win.frame_rate_hz())
        return self.panel

    def _save(self, routine: Routine) -> None:
        config.save_settings(self.key, {"routine": routine.to_dict()})

    # ── what the engine is allowed to do ──
    def _rig(self) -> RigLimits:
        stage = self.win.stage_target()
        x = y = z = None
        has_z = False
        if stage is not None:
            try:
                x, y = stage.limits_um()
                has_z = stage.has_z()
                z = stage.z_limits_um() if has_z else None
            except Exception:            # noqa: BLE001 — stage mid-teardown
                x = y = z = None
                has_z = False
        # "A camera is loaded", not "a file is open": Start opens the file.
        return RigLimits(x_um=x, y_um=y, z_um=z, has_stage=stage is not None,
                         has_z=has_z,
                         has_dmd=self.win.pattern_target() is not None,
                         has_puffer=self.win.puffer_target() is not None,
                         has_frames=FRAME_STREAM in self.win.module_keys())

    def _hooks(self) -> RoutineHooks:
        stage = self.win.stage_target()
        dmd = self.win.pattern_target()
        led = self.win.led_target()
        puffer = self.win.puffer_target()
        clock = self.win.sync.clock

        def frames() -> int | None:
            # Frames in the FILE. Read self._rec per call: a roll replaces it.
            # A .dcimg is written by DCAM, so its own count stands in.
            n = self.win.dcimg_frames(FRAME_STREAM)
            if n is not None:
                return n
            rec = self._rec
            return None if rec is None else rec.offered(FRAME_STREAM)

        def arm_trigger() -> None:
            if self._armed_with_file:
                # `_prepare_recording` already re-armed inside the swap.
                self._armed_with_file = False
                return
            if self.win.rearm_camera_trigger(FRAME_STREAM) is not True:
                raise RuntimeError("the camera could not be re-armed for the "
                                   "next trigger")

        def noop_move(_x, _y, _z=None) -> None:
            raise RuntimeError("no stage loaded")

        return RoutineHooks(
            now=clock.now,
            frames=frames,
            move=stage.move_to if stage is not None else noop_move,
            moving=stage.is_moving if stage is not None else (lambda: False),
            stop_motion=stage.stop_motion if stage is not None else (lambda: None),
            set_pattern=dmd.set_pattern if dmd is not None else (lambda _p: None),
            light=dmd.set_light if dmd is not None else (lambda _on: None),
            led=led.set_led if led is not None else (lambda _on: None),
            puff=puffer.fire if puffer is not None else (lambda: None),
            arm_trigger=arm_trigger,
            trigger_gate=lambda: self.win.camera_trigger_gate(FRAME_STREAM),
            burst_frames=lambda: self.win.camera_burst_frames(FRAME_STREAM),
            edge=self._on_edge,
            prepare_recording=self._prepare_recording,
            begin_recording=self._on_recording_begin,
            end_recording=self._on_recording_end,
            log=self._status,
        )

    # ── run control ──
    def _first_file_is_doomed(self, routine: Routine) -> bool:
        """Whether the first file Start opens will be rolled away before it
        holds anything: a `trigger` step precedes the first bracket and the
        camera records .dcimg (the re-arm swaps files; a TIFF survives)."""
        if not self.win.dcimg_enabled():
            return False
        first_index = min((r.start for r in routine.recordings), default=0)
        return any(s.kind == "trigger" for s in routine.steps[:first_index])

    def _start(self) -> None:
        """Validate, arm, open the recording, run — refusals leave no file."""
        routine = self.panel.settings
        problems = validate(routine, self._rig())
        n_burst, why = burst_frames(routine, self.win.frame_rate_hz())
        if why:
            problems.append(why)
        elif n_burst > BURST_TIMES_MAX - 1:
            problems.append(f"a Record after a Trigger is {n_burst} frames; "
                            f"one burst holds at most {BURST_TIMES_MAX - 1}")
        if problems:
            self.panel.show_problems(problems)
            self._status(f"routine refused: {problems[0]}")
            return

        # Every routine owns the camera's gating (operator, 2026-09-28). The
        # flag stops `_start_session()` resetting it to manual.
        self.win.routine_arming_trigger(True)
        try:
            if not self._arm_camera_trigger():
                return
            burst_ok = self.win.set_camera_burst(FRAME_STREAM, n_burst)
            if burst_ok is False:
                msg = ("a recording is already running with a different "
                       "frames-per-edge — stop it first")
                self.panel.show_problems([msg])
                self._status(f"routine refused: {msg}")
                return
            if burst_ok is None:
                n_burst = 0             # no camera: nothing counts a burst

            self._routine = routine
            self._trial_count = {}
            self._edge_log = None
            self._edge_n = 0
            self._pending_edge = None
            if self._rec is None:
                if self._first_file_is_doomed(routine):
                    self.win.set_routine_save_context(None, None)
                else:
                    first_index = min((r.start for r in routine.recordings),
                                      default=0)
                    region = recording_region_at(routine, first_index) or 0
                    fov, coords = self._fov_for(routine, first_index)
                    self.win.set_routine_save_context(
                        fov, self._trial_for(region), coords)
                if not self._open_recording():
                    self.win.set_routine_save_context(None, None)
                    return
        finally:
            self.win.routine_arming_trigger(False)
        self._filed = 0
        self._pending_roll_run = None
        self._prepared = None
        self._armed_with_file = False
        self._hold_t0 = None
        self._file_group_key = None
        self._filed_from = 0
        self._pending_scope = {}
        self._routine_origin = self.win.sync.clock.now()
        self._n_steps = len(routine.steps)
        self._group_repeat = group_repeat_at(routine, play_order(routine))
        self._engine = RoutineEngine(routine, self._hooks(),
                                     burst_frames=n_burst)
        self._engine.start(trigger="ttl")
        if n_burst:
            self._status(f"camera bursts {n_burst} frames per edge")
        self._timer.start()
        if self._engine.phase == Phase.ARMED:
            self._status(f"routine '{routine.name}' armed — waiting for "
                         f"the camera's TTL trigger")
        else:
            self._status(f"routine '{routine.name}' started — its trigger "
                         f"steps take every edge, trial 1's included")

    def _arm_camera_trigger(self) -> bool:
        """External edge before the recording opens. None (no camera) is not
        a refusal; False (a running recording blocks the switch) is."""
        ok = self.win.set_camera_trigger(FRAME_STREAM, True)
        if ok is not False:
            return True
        msg = ("a recording is already running with the camera not in "
              "External edge mode — stop it first")
        self.panel.show_problems([msg])
        self._status(f"routine refused: {msg}")
        return False

    def _open_recording(self) -> bool:
        """Through the Record button, so it refuses exactly as that would."""
        was = self.win.set_recording(True)
        if self._rec is None:            # attach_sink never came
            self.panel.show_problems(
                ["could not start recording — check the Save page "
                 "(the status line says why)"])
            return False
        self._own_rec = not was
        if self._own_rec:
            self._status("recording started for the routine")
        return True

    def _fov_for(self, routine: Routine,
                step_index: int) -> tuple[str, tuple[float | None, float | None,
                                                     float | None] | None]:
        """The last Move at or before `step_index`: its saved FOV name, or
        ("custom", coords) for typed coordinates."""
        for i in range(min(step_index, len(routine.steps) - 1), -1, -1):
            s = routine.steps[i]
            if s.kind == "move":
                return (s.fov, None) if s.fov else \
                    ("custom", (s.x_um, s.y_um, s.z_um))
        return "custom", None

    def _trial_for(self, region: int) -> int:
        n = self._trial_count.get(region, 0) + 1
        self._trial_count[region] = n
        return n

    def _close_own_recording(self) -> None:
        """Stop recording and capture at the routine's end."""
        self.win.set_routine_save_context(None, None)
        self._own_rec = False            # before: detach_sink re-enters
        self.win.set_recording(False)
        self.win.set_live(False)
        self._flush_pending_edge()

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
        self.update_display()            # before the session (and timer) stops
        self._close_own_recording()

    def _stop_ticking(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _holding_for_camera(self, eng) -> bool:
        """Held at a .dcimg roll until capture resumes, then the Wait step's
        clock restarts, so the trial isn't short by the gap."""
        if self._hold_t0 is None:
            return False
        held = time.monotonic() - self._hold_t0
        if self.win.camera_ready(FRAME_STREAM):
            self._hold_t0 = None
            if eng.rearm_step():
                self._status(f"camera back after {held:.1f} s — the step's "
                             f"clock restarts from here")
            return False
        if held > HOLD_TIMEOUT_S:
            self._hold_t0 = None
            eng.pause(f"the camera did not come back within "
                      f"{HOLD_TIMEOUT_S:g} s of the file roll")
            return False
        return True

    def _tick(self) -> None:
        """Guarded: an exception out of a Qt slot aborts the process."""
        eng = self._engine
        if eng is None:
            self._stop_ticking()
            return
        if self._holding_for_camera(eng):
            return
        try:
            eng.tick()
        except Exception as e:           # noqa: BLE001
            self._stop_ticking()
            self._status(f"routine tick failed ({type(e).__name__}: {e})")
            return
        if self._pending_roll_run is not None:
            run, self._pending_roll_run = self._pending_roll_run, None
            if self._roll_for(run):
                self._put(run, opening=True)
                if (self._routine is not None
                        and self._routine.wait_for_camera
                        and not self.win.camera_ready(FRAME_STREAM)):
                    self._hold_t0 = time.monotonic()
            else:
                eng.pause("could not open the next output file")
        if eng.phase == Phase.DONE:
            self._stop_ticking()
            # Paint DONE first: closing the capture stops the display timer.
            self.update_display()
            self._close_own_recording()

    # ── the file ──
    def _prepare_recording(self, run) -> None:
        """With a .dcimg open, roll to the next recording's file and re-arm in
        the same swap: a plain re-arm would kill the recorder, and this way
        the edge's first frame lands in the new file. Raises to pause."""
        if self.win.dcimg_frames(FRAME_STREAM) is None:
            return
        if self.win.arm_camera_with_next_file(FRAME_STREAM) is not True:
            raise RuntimeError("the camera could not be armed with the next "
                               "file")
        if not self._roll_for(run):
            raise RuntimeError("could not open the next output file")
        self._prepared = (run.cycle, run.start_index)
        self._armed_with_file = True
        self._status("new .dcimg opened before the trigger step re-arms — a "
                     ".dcimg cannot span one")

    def _on_edge(self, t: float, run) -> None:
        """Log a trigger edge. Missed edges can't be seen live; the log is
        matched against the stim rig's afterwards (saving/bpod_match.py). The
        row waits for the file its recording opens, if one follows."""
        self._flush_pending_edge()
        self._edge_n += 1
        wall = time.strftime("%Y-%m-%dT%H:%M:%S")
        key = None if run is None else (run.cycle, run.start_index)
        self._pending_edge = (self._edge_n, t, wall, key, run)
        if run is None:
            self._flush_pending_edge()

    def _flush_pending_edge(self, run=None) -> None:
        """Write the pending edge; with `run`, as opening that run's file."""
        if self._pending_edge is None:
            return
        n, t, wall, key, edge_run = self._pending_edge
        self._pending_edge = None
        fov, trial, path = "", "", ""
        if run is not None and edge_run is not None:
            fov = self._fov_for(self._routine, run.start_index)[0]
            trial = self._trial_count.get(run.region, "")
            path = self.win.recording_path() or ""
        try:
            if self._edge_log is None:
                folder = Path(self.win.routine_folder())
                folder.mkdir(parents=True, exist_ok=True)
                self._edge_log = folder / (
                    f"routine_edges_{time.strftime('%Y%m%d_%H%M%S')}.csv")
                self._edge_log.write_text(EDGE_LOG_HEADER, encoding="utf-8")
            with open(self._edge_log, "a", encoding="utf-8") as fh:
                fh.write(f"{n},{t:.4f},{wall},{fov},{trial},{path}\n")
        except OSError as e:
            self._status(f"could not write the edge log ({e})")

    def _on_recording_begin(self, run) -> None:
        """One `/routine` entry per boundary. The first run just claims the
        open file; a later one needing a fresh file defers the roll to
        `_tick`."""
        if self._prepared == (run.cycle, run.start_index):
            self._prepared = None       # its file was opened before the edge
            self._file_group_key = self._group_key_for(run)
        elif self._file_group_key is None:
            self._file_group_key = self._group_key_for(run)
        elif self._needs_roll(run):
            self._pending_roll_run = run
            return
        self._put(run, opening=True)

    def _on_recording_end(self, run) -> None:
        self._put(run, opening=False)

    def _needs_roll(self, run) -> bool:
        mode = self._routine.save_mode if self._routine is not None else "single"
        if mode == "per_repeat":
            return True
        if mode == "per_group":
            return self._group_key_for(run) != self._file_group_key
        return False

    def _group_key_for(self, run) -> tuple[int, int | None]:
        """(cycle, group-or-None): what "same file" means for per_group.

        Known gap: one Recording spanning two adjacent Groups can merge
        their runs into one, filed under the first Group. Splitting it would
        break the one-run-one-file invariant `final_metadata` relies on."""
        group = (group_region_at(self._routine, run.start_index)
                if self._routine is not None else None)
        return (run.cycle, group)

    def _roll_for(self, run) -> bool:
        """Close the current file and open the next, scoped to `run`."""
        fov, coords = self._fov_for(self._routine, run.start_index)
        self.win.set_routine_save_context(fov, self._trial_for(run.region), coords)
        self._pending_scope = self._scope_for(run)
        self._rolling = True
        try:
            ok = self.win.roll_recording()
        finally:
            self._rolling = False
        if ok:
            self._filed_from = len(self._engine.runs)
            self._file_group_key = self._group_key_for(run)
        return ok

    def _scope_for(self, run) -> dict[str, Any]:
        cycle, group = self._group_key_for(run)
        return {"routine_file_cycle": cycle,
                "routine_file_group": -1 if group is None else group}

    def _put(self, run, *, opening: bool) -> None:
        rec = self._rec
        if rec is None:
            return
        # +(region+1) opens, -(region+1) closes; repeats are told apart by
        # `routine_runs`.
        edge = float(run.region + 1)
        rec.put("routine", edge if opening else -edge)
        self._filed += 1
        if (opening and self._pending_edge is not None
                and self._pending_edge[3] == (run.cycle, run.start_index)):
            self._flush_pending_edge(run)

    # ── session / recording ──
    def attach_sink(self, rec) -> None:
        self._rec = rec

    def detach_sink(self) -> None:
        super().detach_sink()
        if self._rolling:
            # Our own roll: the new sink is about to attach, ownership stays.
            self._rec = None
            return
        # Recording stopped under a running routine: stop the routine rather
        # than drive the stage into a closed file.
        if self._engine is not None and self._engine.running:
            self._engine.abort()
            self._stop_ticking()
            self._status("routine aborted — the recording stopped")
        self._own_rec = False
        self._rec = None
        self.win.set_routine_save_context(None, None)

    def stop(self) -> None:
        """Session teardown; `_stop_session` has already closed the file."""
        if self._engine is not None and self._engine.running:
            self._engine.abort()
        self._stop_ticking()
        self._engine = None
        self._own_rec = False
        super().stop()
        self._flush_pending_edge()

    def on_modules_changed(self) -> None:
        if self.panel is not None:
            self.panel.set_frame_rate(self.win.frame_rate_hz())

    def busy_reason(self) -> str:
        if self._engine is not None and self._engine.running:
            return ("stop the routine first — it holds an index into the "
                    "loaded modules")
        return ""

    # ── display ──
    def update_display(self) -> None:
        if self.panel is None:
            return
        self._rate_tick += 1
        if self._rate_tick >= RATE_EVERY:
            self._rate_tick = 0
            self.panel.set_frame_rate(self.win.frame_rate_hz())

        eng = self._engine
        if eng is None:
            return
        if eng.phase == Phase.ARMED:
            self.panel.set_state(eng.phase,
                                 "ARMED — waiting for the camera's TTL "
                                 "trigger", None)
            self.panel.set_progress(0.0, f"{clock(eng.elapsed())} waiting")
            return
        if eng.phase == Phase.WAITING:
            i, cycle, _ = eng.position
            what = ("WAITING for the camera's trigger" if eng.edge_ready else
                    "RE-ARMING the camera — an edge now is lost")
            self.panel.set_state(
                eng.phase,
                f"{what} — step {i + 1}/"
                f"{self._n_steps}  cycle {cycle + 1}{self._repeat_suffix(eng)}",
                i)
            self.panel.set_progress(eng.overall_progress(),
                                    f"{clock(eng.elapsed())} elapsed")
            return
        if eng.phase == Phase.PAUSED:
            self.panel.set_state(eng.phase, f"PAUSED — {eng.fault}",
                                 eng.position[0])
            self.panel.set_progress(eng.overall_progress(), self._left(eng))
            return
        if eng.phase == Phase.DONE:
            self.panel.set_state(eng.phase,
                                 f"finished — {eng.steps_done()} step(s)", None)
            self.panel.set_progress(1.0, f"took {clock(eng.elapsed())}")
            return
        i, cycle, attempt = eng.position
        step = eng.step
        where = (f"step {i + 1}/{self._n_steps}  cycle {cycle + 1}"
                f"{self._repeat_suffix(eng)}")
        if attempt > 1:
            where += f"  (attempt {attempt})"
        if step is not None:
            if step.kind == "move":
                where += " — moving/settling"
            elif step.kind in TIMED_KINDS:
                verb = "recording" if step.kind == "record" else "waiting"
                where += (f" — {verb} {eng.progress() * 100:.0f} % of "
                          f"{step.length:g} {step.unit}")
            elif step.kind == "display":
                where += " — displaying" if step.pattern else " — stopping display"
            elif step.kind == "trigger":
                # RUNNING for one tick between the edge and _advance().
                where += " — trigger received"
            else:
                where += " — puffing"
        self.panel.set_state(eng.phase, where, i)
        self.panel.set_progress(eng.overall_progress(), self._left(eng))

    def _repeat_suffix(self, eng) -> str:
        pos = eng.order_position
        rep = self._group_repeat[pos] if pos < len(self._group_repeat) else None
        return f"  repeat {rep[0]}/{rep[1]}" if rep else ""

    def _left(self, eng) -> str:
        """Elapsed and remaining — a floor, since moves aren't timed."""
        done = f"{clock(eng.elapsed())} elapsed"
        if self._routine is None:
            return done
        _i, cycle, _a = eng.position
        est = remaining(self._routine, self.panel.frame_rate,
                        eng.order_position, cycle, eng.progress())
        return f"{done} · {est.text()} left"

    # ── metadata ──
    def metadata(self) -> dict[str, Any]:
        r = self.panel.settings
        meta = {
            "routine_name":          r.name,
            "routine_cycles":        r.cycles,
            "routine_save_mode":     r.save_mode,
            "routine_start_trigger": "ttl",     # constant; kept for old readers
            "routine_n_steps":       len(r.steps),
            "routine_steps":      _steps_json(r),
            # Nested copy: SplitWriter's JSON keeps it an object.
            "routine_protocol":   r.to_dict(),
            "routine_started":    False,
        }
        # The (cycle, group) this file starts with, set by `_roll_for`.
        meta.update(self._pending_scope)
        return meta

    def final_metadata(self) -> dict[str, Any]:
        eng = self._engine
        if eng is None:
            return {"routine_started": False, "routine_steps_done": 0,
                    "routine_recordings_interrupted": 0, "routine_fault": "",
                    "routine_runs": "[]"}
        # Only the runs THIS file captured.
        file_runs = eng.runs[self._filed_from:]
        single = self._routine is None or self._routine.save_mode == "single"
        # A rolled file names the routine's start on the shared clock, so
        # its siblings reassemble onto one timebase.
        origin = 0.0 if single else self._routine_origin
        return {
            "routine_started":           True,
            "routine_steps_done":        eng.steps_done(),   # whole routine
            "routine_recordings_interrupted": sum(1 for x in file_runs
                                                  if x.interrupted),
            # The only record of which run faulted and was repeated.
            "routine_runs": json.dumps([x.attrs(session_origin=origin)
                                        for x in file_runs]),
            "routine_fault":             eng.fault if eng.phase == Phase.PAUSED
                                         else "",
            "routine_boundaries":        self._filed,
            "routine_edge_log":          (self._edge_log.name
                                          if self._edge_log else ""),
        }


def _steps_json(r: Routine) -> str:
    """HDF5 attributes are scalars, so the steps travel as one JSON string."""
    return json.dumps(r.to_dict()["steps"])
