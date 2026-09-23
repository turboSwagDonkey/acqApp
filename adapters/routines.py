"""
The experiment routine's adapter — the only code here that touches a real
stage or a real projector.

Everything that decides lives in `routines/`, Qt-free. This builds the
`RoutineHooks` pointing that engine at the loaded modules, reaching them
through `ModuleHost` (`stage_target`/`pattern_target`) rather than by importing
their adapters — the stage doesn't know routines exist.

**The tick runs on the GUI thread**, on a QTimer, not in a worker. It is
non-blocking, and the closed loop already goes out of its way to get actuation
*back* here (its `fired` signal is queued); this is that, without the hop. A
stage move is a short serial write; the position poller does the polling, on
its own thread.

**Start opens the recording itself** (`ModuleHost.set_recording`, the twin of
the DMD calibration's `set_live`): a routine can't run a step without a file
open. A recording this adapter started, it stops at the end; one the operator
started, it leaves alone.

**A "TTL" start trigger arms the camera itself, before opening the
recording** (`ModuleHost.set_camera_trigger`) — rather than trusting the
operator to have already set the voltage camera's own External edge mode on
its own tab, which can silently drift back to Internal between being set
and the routine actually arming. The camera reports a frame it didn't have
at arm time (`routines/engine.py`'s ARMED phase watches the frame count);
nothing here reads a DAQ line.

**A `trigger` STEP waits for an edge mid-routine**, which is how one
recording per edge is built (a repeat group over [trigger, wait], the
Recording bracket on the wait). Same physical line as the TTL start, so
`_start` puts the camera in External edge mode for either. Each such step
re-arms the camera through `ModuleHost.rearm_camera_trigger` — it latches,
so without that only a run's first edge would ever be seen — and that call
is asynchronous, which is what the engine's `TRIGGER_SETTLE_S` accounts for.

**Save modes `per_repeat`/`per_group` roll to a fresh file at a
`RecordingRun` boundary** (`_needs_roll`/`_roll_for`), via
`MainWindow.roll_recording()` — a plain stop-then-start of the recorder,
`main.py`'s only addition for this. The roll is deferred to right after
`RoutineEngine.tick()` returns, never run from inside the hook that
discovers it needs one: `begin_recording(run)` fires synchronously from
deep inside the engine's own call stack (`_advance -> _enter_step ->
_update_recording -> _open_recording`), which still has code to run after
the hook returns — calling back into the engine (`roll_recording`'s
failure path pauses it) from there would be overwritten by that code
before it ever took effect. `_tick()` is the only place both `eng.tick()`
and any resulting roll are guaranteed to run at the same, non-reentrant,
top-level frame.

**Every status message goes through `_status()`, to the console as well as
the window's status bar** (2026-09-17) — `MainWindow.status()` alone only
ever reached the bar, so a pause, a fault, or an uncaught tick exception
looked like the routine had gone silently quiet to an operator who works
from the console, which is this app's actual workflow at the rig.

**The panel names which REPEAT is running, not just which table row**
(`_repeat_suffix`, `routines/settings.py`'s `group_repeat_at`) — "step 1/2"
alone reads identically on repeat 1 and repeat 100 of a `[trigger, wait]`
pair, since a Group replays the same table rows in place; only the repeat
number actually changes.
"""
from __future__ import annotations

import json
import time
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.routines.engine import Phase, RoutineEngine, RoutineHooks
from acqApp.routines.estimate import clock, remaining
from acqApp.routines.panel import SettingsPanel as RoutinePanel
from acqApp.routines.settings import (RigLimits, Routine, group_region_at,
                                      group_repeat_at, play_order,
                                      recording_region_at, validate)

# A step boundary lands within one tick of its true instant; at 106 Hz that is
# under three frames, and the boundary itself is recorded from the clock, not
# from the tick.
TICK_MS = 25

# The voltage camera is the imaging path an experiment is about; the pupil
# camera watches the animal.
FRAME_STREAM = "voltage_cam"

# The estimate has to follow an exposure changed in another tab, but not at
# 30 Hz — the number is rebuilt from that panel's widgets each time.
RATE_EVERY = 30

# Longest a routine will hold at a file roll waiting for capture to resume.
# A .dcimg roll measured ~0.9 s; an order of magnitude past that is a camera
# that isn't coming back, and the operator should be told rather than left
# watching a routine that looks alive.
HOLD_TIMEOUT_S = 10.0


class RoutinesModule(ModuleAdapter):
    """Wires `routines/` into this window.

    Owns no device, and reads its neighbours only through the host: an
    instrument becomes routine-drivable by declaring `stage_target` or
    `pattern_target`, and nothing here changes.
    """
    key = "routines"
    tab_label = "Routines"
    # Its own window: a routine is *run* from this panel, and operator is
    # watching the camera's page while it runs. Always loaded too
    # (`config.ALWAYS_ON`) — it owns no device, so there's nothing to unload.
    own_window = True
    own_window_size = (728, 936)

    def __init__(self, win) -> None:
        super().__init__(win)
        self._engine: RoutineEngine | None = None
        self._timer: QTimer | None = None
        self._rec = None                # the Recorder, while recording
        self._filed = 0                 # step boundaries handed to the file
        self._n_steps = 0               # steps in the routine that is running
        self._group_repeat: list[tuple[int, int] | None] = []   # see
                                         # group_repeat_at, indexed by
                                         # eng.order_position
        self._routine: Routine | None = None    # the one that is running
        self._rate_tick = 0
        # True only when Start opened the recording. What makes "stop what you
        # started" different from "stop the operator's recording".
        self._own_rec = False
        # ── file rolling (save_mode "per_repeat"/"per_group") ──
        self._pending_roll_run = None   # set by _on_recording_begin, acted on
                                         # after eng.tick() returns — see _tick
        self._file_group_key = None     # (cycle, group-or-None) of the open file
        self._filed_from = 0            # eng.runs[:_filed_from] already in a
                                         # closed file — see final_metadata
        self._pending_scope: dict[str, Any] = {}   # for the NEXT metadata() call
        self._rolling = False           # guards detach_sink()'s abort-on-stop
        self._routine_origin = 0.0      # session-clock t when Start was pressed
        # monotonic() when a file roll left the camera stopped, else None —
        # see _holding_for_camera.
        self._hold_t0: float | None = None
        # How many files THIS run has opened for each Recording bracket
        # (`routine.recordings` index) — the routine's own file-naming's
        # trial number, see `_trial_for`.
        self._trial_count: dict[int, int] = {}

    def _status(self, msg: str) -> None:
        """Every routine status message, everywhere in this file — including
        the engine's own `log` hook and every direct call below. The status
        bar alone was where "routine paused: <reason>" went, invisible to an
        operator watching the console, which is this app's actual workflow at
        the rig: a pause or an uncaught tick exception read as the routine
        just going silently quiet."""
        self.win.status(msg)
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
        # Parented to the panel, so it dies with the UI rather than ticking on
        # into an unloaded module.
        self._timer = QTimer(self.panel)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.panel.set_frame_rate(self.win.frame_rate_hz())
        return self.panel

    def _save(self, routine: Routine) -> None:
        config.save_settings(self.key, {"routine": routine.to_dict()})

    # ── what the engine is allowed to do ──
    def _rig(self) -> RigLimits:
        """What the loaded modules can actually do, for validation."""
        stage = self.win.stage_target()
        x = y = z = None
        has_z = False
        if stage is not None:
            try:
                x, y = stage.limits_um()
                has_z = stage.has_z()
                z = stage.z_limits_um() if has_z else None
            except Exception:            # noqa: BLE001 — a stage mid-teardown
                x = y = z = None
                has_z = False
        # `has_frames` is "a camera is loaded", not "a file is open": Start
        # opens the file itself, so the other reading refuses every routine
        # measured in frames.
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
            # What reached the FILE, not what the camera produced — the two
            # differ exactly when the write path is what is falling behind.
            #
            # Read `self._rec` per call, never captured: a `per_repeat`/
            # `per_group` roll swaps the Recorder out from under us
            # (`detach_sink`/`attach_sink`), and a closure holding the one that
            # was current at Start would freeze at the first roll — leaving a
            # frames-unit Wait, or a `trigger` step, watching a count that can
            # no longer move.
            #
            # A .dcimg is the same question asked of a different counter: DCAM
            # writes those frames itself, so `offered()` stays at 0 and every
            # frames-unit Wait and `trigger` step would stall on it. The
            # recorder's own count is the same quantity — frames in the file —
            # and rolls the same way, because a roll reopens it too.
            n = self.win.dcimg_frames(FRAME_STREAM)
            if n is not None:
                return n
            rec = self._rec
            return None if rec is None else rec.offered(FRAME_STREAM)

        def arm_trigger() -> None:
            # Put the camera back into its waiting state so the NEXT edge is
            # detectable; it latches otherwise (see
            # `ModuleHost.rearm_camera_trigger`). Raising here is right: the
            # engine turns it into a pause, and a `trigger` step that can't
            # re-arm would otherwise wait on an edge nothing can deliver.
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
            begin_recording=self._on_recording_begin,
            end_recording=self._on_recording_end,
            log=self._status,
        )

    # ── run control ──
    def _start(self) -> None:
        """Validate, open the recording if there's none, then run.

        In that order: a refused routine must not leave a file open behind it.
        """
        routine = self.panel.settings
        problems = validate(routine, self._rig())
        if problems:
            self.panel.show_problems(problems)
            self._status(f"routine refused: {problems[0]}")
            return


        # External edge mode is needed by a TTL start AND by any `trigger`
        # step mid-routine — those wait on the same physical line, so a
        # manual-start routine full of trigger steps needs it just as much.
        needs_ext = (routine.start_trigger == "ttl"
                     or any(s.kind == "trigger" for s in routine.steps))
        if needs_ext and not self._arm_camera_trigger():
            return

        self._routine = routine
        self._trial_count = {}
        if self._rec is None:
            first_index = min((r.start for r in routine.recordings), default=0)
            region = recording_region_at(routine, first_index) or 0
            fov, coords = self._fov_for(routine, first_index)
            self.win.set_routine_save_context(fov, self._trial_for(region), coords)
            if not self._open_recording():
                self.win.set_routine_save_context(None, None)
                return
        self._filed = 0
        self._pending_roll_run = None
        self._hold_t0 = None
        self._file_group_key = None
        self._filed_from = 0
        self._pending_scope = {}
        self._routine_origin = self.win.sync.clock.now()
        self._n_steps = len(routine.steps)
        self._group_repeat = group_repeat_at(routine, play_order(routine))
        self._engine = RoutineEngine(routine, self._hooks())
        self._engine.start(trigger=routine.start_trigger)
        self._timer.start()
        if routine.start_trigger == "ttl":
            self._status(f"routine '{routine.name}' armed — waiting for "
                         f"the camera's TTL trigger")
        else:
            self._status(f"routine '{routine.name}' started — "
                         f"{routine.total_steps()} step(s)")

    def _arm_camera_trigger(self) -> bool:
        """Put the voltage camera in External edge mode before the routine's
        own recording opens — so operator doesn't have to have already
        set it on the Voltage cam tab, and a mode that quietly drifted back
        to Internal since then doesn't leave the routine waiting forever.
        False (with a problem shown) if that isn't possible right now.

        For a TTL start AND for any `trigger` step, which wait on the same
        physical line; `_start` decides which routines need it.
        """
        ok = self.win.set_camera_trigger(FRAME_STREAM, True)
        if ok is True:
            return True
        msg = ("no camera loaded to put in External edge mode" if ok is None
              else "a recording is already running with the camera not in "
                   "External edge mode — stop it first")
        self.panel.show_problems([msg])
        self._status(f"routine refused: {msg}")
        return False

    def _open_recording(self) -> bool:
        """Start recording for this routine. False if it could not be started.

        `set_recording` goes through the Record button, so an unwritable save
        folder refuses here exactly as it would there.
        """
        was = self.win.set_recording(True)
        if self._rec is None:            # attach_sink never came: it refused
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
        """The FOV in effect at `step_index`: the last Move step at or
        before it. A saved FOV's name, or "custom" paired with the raw
        coordinates a step typed directly instead — `saving/config.py`'s
        `resolve_routine()` names the file from the first, and a sidecar
        (`write_routine_fov_sidecar`) keeps the second from being lost."""
        for i in range(min(step_index, len(routine.steps) - 1), -1, -1):
            s = routine.steps[i]
            if s.kind == "move":
                return (s.fov, None) if s.fov else \
                    ("custom", (s.x_um, s.y_um, s.z_um))
        return "custom", None

    def _trial_for(self, region: int) -> int:
        """Which file this is for Recording `region` (an index into
        `routine.recordings`) — every new file it opens counts, whatever the
        reason (a repeat group, a per_repeat/per_group roll)."""
        n = self._trial_count.get(region, 0) + 1
        self._trial_count[region] = n
        return n

    def _close_own_recording(self) -> None:
        """Stop a recording this adapter started; leave the operator's alone."""
        # Cleared regardless of ownership: a routine that ran inside a
        # recording the OPERATOR had already started never owns it, but the
        # FOV/trial context set for it must not leak into whatever the
        # operator records next.
        self.win.set_routine_save_context(None, None)
        if not self._own_rec:
            return
        self._own_rec = False            # before the call: detach_sink re-enters
        self.win.set_recording(False)

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
        self._close_own_recording()

    def _stop_ticking(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _holding_for_camera(self, eng) -> bool:
        """Whether the routine is held at a file roll waiting for capture.

        A `.dcimg` roll rebinds DCAM's recorder, which it will only do to a
        STOPPED camera — ~0.9 s in which no frame exists. The step that roll
        belongs to has already armed its clock, so ticking through the gap
        files a trial that is short by exactly that much. Hold instead, then
        restart the step's clock so it measures only time the camera was
        running. `Routine.wait_for_camera` turns this off; a TIFF roll never
        stops capture, so it never reaches here either way.
        """
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
        """The engine's heartbeat. Guarded: an exception out of a Qt slot
        aborts the process, and this one drives the stage.

        A pending file roll is handled HERE, after `eng.tick()` has fully
        returned — never from inside `_on_recording_begin` itself, which
        fires from deep inside the engine's own call stack and still has
        code to run once the hook returns (see the module docstring)."""
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
                # A .dcimg roll left the camera stopped for ~0.9 s. The step
                # this run belongs to armed its clock before the roll (see
                # the docstring above), so from here the engine must not tick
                # until frames exist again.
                if (self._routine is not None
                        and self._routine.wait_for_camera
                        and not self.win.camera_ready(FRAME_STREAM)):
                    self._hold_t0 = time.monotonic()
            else:
                eng.pause("could not open the next output file")
        if eng.phase == Phase.DONE:
            self._stop_ticking()
            # The routine is over; a file it opened has nothing left to record.
            self._close_own_recording()

    # ── the file ──
    def _on_recording_begin(self, run) -> None:
        """A recording bracket opened. One `/routine` entry per boundary, on
        the shared clock — which is what makes recordings locatable in the
        file.

        The very first run of the routine just establishes which (cycle,
        group) the already-open file covers — nothing to roll FROM yet.
        A later run whose save mode calls for a fresh file defers the
        actual roll to `_tick` instead of writing the boundary now — see
        the module docstring for why this can't happen inline."""
        if self._file_group_key is None:
            self._file_group_key = self._group_key_for(run)
        elif self._needs_roll(run):
            self._pending_roll_run = run
            return
        self._put(run, opening=True)

    def _on_recording_end(self, run) -> None:
        self._put(run, opening=False)

    def _needs_roll(self, run) -> bool:
        """Does `run` belong in a fresh file? Only called once a file is
        already open (see `_on_recording_begin`), so "single" and the
        first-ever run are handled by the caller, not here."""
        mode = self._routine.save_mode if self._routine is not None else "single"
        if mode == "per_repeat":
            return True
        if mode == "per_group":
            return self._group_key_for(run) != self._file_group_key
        return False

    def _group_key_for(self, run) -> tuple[int, int | None]:
        """(cycle, group-or-None) — what "the same file" means for
        save_mode="per_group": repeats of the same Group share a key,
        moving to a different Group/ungrouped region/cycle doesn't.

        Keyed on `run.start_index` alone, which is exactly right when each
        Group has its OWN Recording bracket (the natural setup, and the only
        one `routines/panel.py`'s per-step recording sticker can produce —
        it never spans more than one step). Edge
        case, not fixed here: if ONE Recording spans two step-index-ADJACENT
        Groups, `recording_run_ids` can merge a repeat of the first with the
        first repeat of the second into a single RecordingRun (its own
        "same step index continues forward" rule has no notion of a Group
        boundary) — that merged run's whole file is then labelled and rolled
        by whichever Group its start_index falls in, so a handful of the
        second Group's samples land in the first Group's file. Splitting a
        RecordingRun's own data across two files to fix this would break
        the one-run-one-file-slice invariant `final_metadata()` relies on
        (`eng.runs[self._filed_from:]`); not attempted."""
        group = (group_region_at(self._routine, run.start_index)
                if self._routine is not None else None)
        return (run.cycle, group)

    def _roll_for(self, run) -> bool:
        """Close the current file and open the next one, scoped to `run`.

        `_rolling` guards `detach_sink()`'s "recording stopped out from
        under a running routine -> abort" safety net against mistaking this
        deliberate swap for the operator having pulled the plug."""
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
        """Provenance for the file `run` is about to open — read once by
        `metadata()` right after `roll_recording()` calls it. The same
        (cycle, group) `per_group` keys files by, written out for the reader."""
        cycle, group = self._group_key_for(run)
        return {"routine_file_cycle": cycle,
                "routine_file_group": -1 if group is None else group}

    def _put(self, run, *, opening: bool) -> None:
        rec = self._rec
        if rec is None:
            return
        # +region on the way in, -(region+1) on the way out: one scalar stream
        # carries both edges, and the sign says which without a second stream.
        # `+1` because region 0's opening edge would otherwise be its own
        # closing. Two repeats of the SAME bracket sign identically — exactly
        # the ambiguity a repeated step already had before this redesign,
        # resolved the same way: `routine_runs` (below) carries cycle/attempt/
        # t0 for every execution, so the boundaries and the JSON reassemble
        # onto one story even though the raw stream alone can't tell repeats
        # apart.
        edge = float(run.region + 1)
        rec.put("routine", edge if opening else -edge)
        self._filed += 1

    # ── session / recording ──
    def attach_sink(self, rec) -> None:
        self._rec = rec

    def detach_sink(self) -> None:
        super().detach_sink()
        if self._rolling:
            # A deliberate file swap mid-routine (`_roll_for`): the OLD
            # recorder's sink is detached here, but who owns the recording
            # and whether the routine is still running are both unaffected
            # — attach_sink() is about to bring the new one in, and
            # `_own_rec` must survive to the routine's REAL end, or
            # `_close_own_recording()` there wrongly thinks it owns nothing.
            self._rec = None
            return
        # Recording stopped under a running routine: nowhere to put its steps,
        # nothing for "100 frames" to count. Stop it rather than let it drive
        # the stage into a closed file. Also reached when the routine closes
        # its own recording, where `_own_rec` is already False.
        if self._engine is not None and self._engine.running:
            self._engine.abort()
            self._stop_ticking()
            self._status("routine aborted — the recording stopped")
        self._own_rec = False
        self._rec = None
        self.win.set_routine_save_context(None, None)

    def stop(self) -> None:
        """Session teardown. The routine can't outlive the clock it times by."""
        if self._engine is not None and self._engine.running:
            self._engine.abort()
        self._stop_ticking()
        self._engine = None
        # Not `_close_own_recording`: the session is already coming down, and
        # `_stop_session` closes the recording before it gets here.
        self._own_rec = False
        super().stop()

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
        # The estimate follows a frame rate the operator may be changing in
        # another tab. Throttled: it costs that panel a config rebuild.
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
            self.panel.set_state(
                eng.phase,
                f"WAITING for the camera's trigger — step {i + 1}/"
                f"{self._n_steps}  cycle {cycle + 1}{self._repeat_suffix(eng)}",
                i)
            # No fraction: how long an external source takes is unknowable,
            # and a bar creeping along would imply otherwise.
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
        # What "step i is running" means depends on its kind — a Move doesn't
        # have a length to report a fraction of, a Wait does. A `trigger` step
        # reaches here only for the one tick between its edge landing and
        # `_advance()` processing it — briefly RUNNING, `eng.step` still the
        # trigger step — so it needs its own case rather than falling into
        # the puffer's label.
        if step is not None:
            if step.kind == "move":
                where += " — moving/settling"
            elif step.kind == "wait":
                where += (f" — waiting {eng.progress() * 100:.0f} % of "
                          f"{step.length:g} {step.unit}")
            elif step.kind == "display":
                where += " — displaying" if step.pattern else " — stopping display"
            elif step.kind == "trigger":
                where += " — trigger received"
            else:
                where += " — puffing"
        # The row is bolded in the table, so "which step is this" is answered
        # by looking at the protocol rather than by counting the label's index.
        self.panel.set_state(eng.phase, where, i)
        self.panel.set_progress(eng.overall_progress(), self._left(eng))

    def _repeat_suffix(self, eng) -> str:
        """Returns "  repeat N/M" if the running step sits inside a repeat
        Group, else "". Without this, "step 1/2" for a `[trigger, wait]` pair
        reads identically whether it's repeat 1 of 3 or repeat 3 of 3 — the
        ONLY thing in the whole display that would say otherwise is the
        progress bar's fraction, easy to miss on a routine that is otherwise
        silent between edges."""
        pos = eng.order_position
        rep = self._group_repeat[pos] if pos < len(self._group_repeat) else None
        return f"  repeat {rep[0]}/{rep[1]}" if rep else ""

    def _left(self, eng) -> str:
        """Elapsed, and what is left — a floor, since no move is timed."""
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
            # The protocol as configured, in full: "which stage position was
            # step 4" can't be recovered from the file any other way.
            "routine_name":          r.name,
            "routine_cycles":        r.cycles,
            "routine_save_mode":     r.save_mode,
            "routine_start_trigger": r.start_trigger,
            "routine_n_steps":       len(r.steps),
            "routine_steps":      _steps_json(r),
            # The same protocol, structured rather than a JSON string —
            # `_steps_json` above is right for HDF5's flat-attribute model,
            # but SplitWriter's settings JSON (acq/writer.py's _json_value)
            # keeps this nested so it reads back as a real object, not a
            # stringified blob, for "easily read and copied" split-mode
            # sessions.
            "routine_protocol":   r.to_dict(),
            # A routine that was configured and never started, and one that ran,
            # leave the same step list. This is what tells them apart.
            "routine_started":    False,
        }
        # Which (cycle, group) THIS file starts with — set by `_roll_for`
        # right before it calls `MainWindow.roll_recording()`, which is what
        # calls back here. Absent on the routine's first file: the whole
        # protocol above already says what it will do.
        meta.update(self._pending_scope)
        return meta

    def final_metadata(self) -> dict[str, Any]:
        eng = self._engine
        if eng is None:
            return {"routine_started": False, "routine_steps_done": 0,
                    "routine_recordings_interrupted": 0, "routine_fault": "",
                    "routine_runs": "[]"}
        # Scoped to what THIS file captured, not the whole routine — a rolled
        # file must not repeat runs a PRIOR file already reported. `single`
        # mode never rolls, so `_filed_from` stays 0 and this is the full
        # list, exactly as before file-rolling existed.
        file_runs = eng.runs[self._filed_from:]
        single = self._routine is None or self._routine.save_mode == "single"
        # `single` keeps session_origin 0.0 — the file IS the session, so its
        # clock already starts there. A rolled file names the ROUTINE's own
        # start on the shared clock instead, so every file it produces
        # reassembles onto one timebase.
        origin = 0.0 if single else self._routine_origin
        return {
            "routine_started":           True,
            # Atomic steps completed normally — see `RoutineEngine.steps_done`.
            # Whole-routine cumulative even when rolling ("progress as of
            # this file"), unlike routine_runs/_recordings_interrupted below.
            "routine_steps_done":        eng.steps_done(),
            # Recording brackets a pause/fault cut short, not atomic steps —
            # a routine with no Recordings at all can still fault mid-step and
            # report 0 here correctly, since nothing was ever open to interrupt.
            "routine_recordings_interrupted": sum(1 for x in file_runs
                                                  if x.interrupted),
            # Every execution THIS FILE covers, not just the counts. `/routine`
            # carries the boundaries but only a signed region index, so without
            # this a recording that was interrupted and repeated is
            # indistinguishable from one that ran twice — and WHICH one
            # faulted is recoverable from nothing else in the file.
            "routine_runs": json.dumps([x.attrs(session_origin=origin)
                                        for x in file_runs]),
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
