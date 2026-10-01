"""Experiment routines — the executor. Pure, no Qt.

Every actuation arrives as a callable (`RoutineHooks`), so a whole routine
runs against fakes on a fake clock. `tick()` is a state machine over `now()`
and `frames()`; pause/resume/abort are transitions.

Recording is an overlay: `recording_run_ids` says which steps a bracket
covers, `_update_recording` decides when to open/close one. Whether that rolls
a file is the adapter's business.

A device failure PAUSES (motion stopped, DMD and LED off, capture
untouched). The interrupted run is kept and marked; resume repeats the step
as a new attempt in a fresh recording run. A burst that stops short is the
exception: its run is marked the same way and the routine goes back to that
burst's trigger step for the next edge, pausing only after MAX_BURST_RETRIES
in a row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from acqApp.routines.settings import (Routine, Step, TIMED_KINDS, play_order,
                                      recording_region_at, recording_run_ids)

MOVE_TIMEOUT_S = 30.0

# Generous: a stimulus rig may be quiet for minutes. Only catches a dead line.
TRIGGER_TIMEOUT_S = 600.0

# A reported re-arm (`RoutineHooks.trigger_gate`) takes ~1-5 s.
REARM_TIMEOUT_S = 30.0

# Fallback trigger detection, for a camera without `trigger_gate`. The re-arm
# is asynchronous and in-flight frames keep arriving, so the count must hold
# still for SETTLE before a new frame counts as the edge; DRAIN is a floor
# above the capture loop's 0.5 s frame wait. An edge inside that window is
# indistinguishable from leftovers — the reason `trigger_gate` exists.
TRIGGER_DRAIN_S = 1.5
TRIGGER_SETTLE_S = 0.75

# A burst's frame count standing still this long mid-burst: the camera stopped.
# The adapter passes a tighter one from the frame rate: a stall must be seen
# before the next edge, whose burst would otherwise complete the count.
BURST_STALL_S = 5.0

# Short bursts retried in a row before the routine pauses instead.
MAX_BURST_RETRIES = 3


class Phase:
    """Where the engine is. Strings, so they go into the file and the panel."""
    IDLE    = "idle"
    ARMED   = "armed"       # before step 1, waiting for the first TTL frame
    RUNNING = "running"
    WAITING = "waiting"     # inside a `trigger` step, waiting for its edge
    PAUSED  = "paused"      # a fault, or the operator
    DONE    = "done"


def _noop(*_a, **_k) -> None:
    return None


def _ratio(got: float, total: float) -> float:
    return max(0.0, min(1.0, got / total)) if total > 0 else 1.0


@dataclass(frozen=True)
class RoutineHooks:
    """One callable per thing the engine may do; defaults are inert, so an
    unloaded instrument is a no-op."""
    now:            Callable[[], float]                       # session-clock seconds
    frames:         Callable[[], int | None] = lambda: None    # monotonic frame count
    move:           Callable[[float | None, float | None, float | None],
                             None] = _noop
    moving:         Callable[[], bool] = lambda: False
    stop_motion:    Callable[[], None] = _noop
    set_pattern:    Callable[[str], None] = _noop
    light:          Callable[[bool], None] = _noop
    led:            Callable[[bool], None] = _noop
    puff:           Callable[[], None] = _noop
    # Re-gates the camera; it latches after one edge otherwise.
    arm_trigger:    Callable[[], None] = _noop
    # (re-arms completed, frames since the last), or None if the camera can't
    # say. Replaces the settle heuristic when present.
    trigger_gate:   Callable[[], tuple[int, int] | None] = lambda: None
    # (session time, the record run this edge starts or None), per edge.
    # Missed edges can't be seen live; they're found by matching these
    # against the stim rig's log afterwards (saving/bpod_match.py).
    edge:           Callable[[float, "RecordingRun | None"], None] = _noop
    # Burst mode: real frames of the burst the last gate caught.
    burst_frames:   Callable[[], int | None] = lambda: None
    # Before `arm_trigger` when a `record` step follows: some recorders
    # (.dcimg) bind only while capture is stopped, and the file must exist
    # before the edge.
    prepare_recording: Callable[["RecordingRun"], None] = _noop
    begin_recording: Callable[["RecordingRun"], None] = _noop
    end_recording:   Callable[["RecordingRun"], None] = _noop
    log:            Callable[[str], None] = _noop


@dataclass
class RecordingRun:
    """One execution of one `Recording` bracket. `region` indexes
    `routine.recordings`; the indices are the steps this pass covered."""
    region:      int
    start_index: int
    end_index:   int
    cycle:       int
    attempt:     int                # 2+ after a paused step repeats
    t0:          float              # session clock
    frame0:      int | None
    t_end:       float | None = None
    frames:      int | None = None
    interrupted: bool = False
    fault:       str = ""

    def attrs(self, session_origin: float = 0.0) -> dict[str, Any]:
        """What the file records about this run. Origin and t0 share one
        clock, so a folder of rolled files reassembles onto one timebase."""
        return {
            "routine_recording_session_origin": float(session_origin),
            "routine_recording_region":         self.region,
            "routine_recording_start_index":    self.start_index,
            "routine_recording_end_index":      self.end_index,
            "routine_recording_cycle":          self.cycle,
            "routine_recording_attempt":        self.attempt,
            "routine_recording_t0":             float(self.t0),
            "routine_recording_frame0":         (-1 if self.frame0 is None
                                                 else self.frame0),
            "routine_recording_interrupted":    bool(self.interrupted),
            "routine_recording_fault":          self.fault,
        }


class RoutineError(RuntimeError):
    """The engine was asked for a transition it can't make."""


class RoutineEngine:
    """Runs a `Routine` through `RoutineHooks`, one `tick()` at a time."""

    def __init__(self, routine: Routine, hooks: RoutineHooks, *,
                 move_timeout_s: float = MOVE_TIMEOUT_S,
                 trigger_timeout_s: float = TRIGGER_TIMEOUT_S,
                 trigger_drain_s: float = TRIGGER_DRAIN_S,
                 trigger_settle_s: float = TRIGGER_SETTLE_S,
                 burst_frames: int = 0,
                 burst_stall_s: float = BURST_STALL_S) -> None:
        """`burst_frames` > 0: the camera captures that many frames per edge,
        so a Record right after a Trigger ends on the burst, not the clock."""
        self._r = routine
        self._burst_n = burst_frames
        self._burst_stall = burst_stall_s
        self._after_edge = False        # the next step is the edge's burst
        self._in_burst = False          # this Record step is a burst
        self._burst_seen = 0
        self._burst_moved_at = 0.0
        self._edge_at = (0, 0)          # (order position, cycle) of the last edge
        self._burst_retries = 0         # short bursts in a row
        self._h = hooks
        self._timeout = move_timeout_s
        self._trig_timeout = trigger_timeout_s
        self._trig_drain = trigger_drain_s
        self._trig_settle = trigger_settle_s
        self.runs: list[RecordingRun] = []
        self.fault = ""
        self._phase = Phase.IDLE
        self._order: list[int] = []     # play_order(routine)
        self._rec_ids: list[int | None] = []
        self._pos = 0                   # index into self._order
        self._i = 0                     # self._order[self._pos], a table row
        self._cycle = 0
        self._attempt = 1
        self._steps_completed = 0
        self._step_done = False
        self._dmd_on = False            # what the last Display step left lit
        self._open_run: RecordingRun | None = None
        self._open_key: tuple[int, int] | None = None   # (cycle, serial)
        self._issued_at = 0.0
        self._arrived_at: float | None = None
        self._wait_t0 = 0.0
        self._wait_frame0: int | None = None
        self._started_at: float | None = None
        self._arm_frame0: int | None = None
        self._trig_frame0: int | None = None
        self._trig_t0 = 0.0
        self._trig_still_since = 0.0
        self._trig_gated = False
        self._gate_seq0: int | None = None       # re-arm count before ours

    # ── readout ───────────────────────────────────────────────────────────────
    @property
    def phase(self) -> str:
        return self._phase

    @property
    def running(self) -> bool:
        """The routine owns the rig — PAUSED and ARMED included."""
        return self._phase in (Phase.ARMED, Phase.RUNNING, Phase.WAITING,
                               Phase.PAUSED)

    @property
    def step(self) -> Step | None:
        if not self._r.steps or self._phase in (Phase.IDLE, Phase.DONE):
            return None
        return self._r.steps[self._i]

    @property
    def position(self) -> tuple[int, int, int]:
        """(table row, cycle, attempt), the first two 0-based."""
        return self._i, self._cycle, self._attempt

    @property
    def order_position(self) -> int:
        """0-based position within `play_order` — tells repeats apart."""
        return self._pos

    @property
    def edge_ready(self) -> bool:
        """WAITING with the camera actually gated: an edge now is caught.
        False while the re-arm (camera stopped) is still in progress."""
        if self._phase != Phase.WAITING:
            return False
        if self._gate_seq0 is not None:
            gate = self._safe_value(self._h.trigger_gate, None)
            return gate is not None and gate[0] > self._gate_seq0
        return self._trig_gated

    @property
    def has_trigger_steps(self) -> bool:
        return any(s.kind == "trigger" for s in self._r.steps)

    def steps_done(self) -> int:
        return self._steps_completed

    def progress(self) -> float:
        """0..1 through the current step. 0 outside RUNNING."""
        step = self.step
        if step is None or self._phase != Phase.RUNNING:
            return 0.0
        if step.kind in TIMED_KINDS:
            if self._in_burst:
                got = self._safe_value(self._h.burst_frames, None)
                return _ratio(float(got or 0), self._burst_n)
            if step.unit == "frames":
                n = self._frames()
                if n is None or self._wait_frame0 is None:
                    return 0.0
                got = float(n - self._wait_frame0)
            else:
                got = self._h.now() - self._wait_t0
            return _ratio(got, step.length)
        if step.kind == "move":
            if self._arrived_at is None:
                return 0.0
            return _ratio(self._h.now() - self._arrived_at, step.settle_s)
        return 1.0 if self._step_done else 0.0

    def total_runs(self) -> int:
        return self._r.total_steps()

    def overall_progress(self) -> float:
        """0..1 through the whole routine. Position-based, so a repeated
        attempt never moves the bar backwards."""
        total = self.total_runs()
        if total <= 0 or self._phase == Phase.IDLE:
            return 0.0
        if self._phase == Phase.DONE:
            return 1.0
        done = self._cycle * len(self._order) + self._pos + self.progress()
        return max(0.0, min(1.0, done / total))

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._safe_value(self._h.now, self._started_at)
                   - self._started_at)

    # ── control ───────────────────────────────────────────────────────────────
    def start(self, trigger: str = "manual") -> None:
        """`trigger="ttl"` arms instead: step 1 waits for a frame the camera
        didn't have yet. Not when the routine has trigger steps: the rig
        sends one edge per trial, and arming would swallow trial 1's."""
        if self._phase in (Phase.ARMED, Phase.RUNNING, Phase.PAUSED):
            raise RoutineError("already running")
        if not self._r.steps:
            raise RoutineError("routine has no steps")
        self.runs = []
        self.fault = ""
        self._order = play_order(self._r)
        self._rec_ids = recording_run_ids(self._r, self._order)
        self._pos = self._cycle = 0
        self._i = self._order[0]
        self._attempt = 1
        self._steps_completed = 0
        self._dmd_on = False
        self._open_run = None
        self._open_key = None
        self._started_at = self._safe_value(self._h.now, 0.0)
        if trigger == "ttl" and not self.has_trigger_steps:
            self._arm_frame0 = self._frames()
            self._phase = Phase.ARMED
            self._h.log("routine armed — waiting for the camera's TTL trigger")
        else:
            self._enter_step()

    def pause(self, reason: str = "paused by operator") -> None:
        if self._phase not in (Phase.RUNNING, Phase.WAITING):
            return
        self._halt(reason)

    def resume(self) -> None:
        """Repeat the paused step as a fresh attempt, in a fresh run."""
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._attempt += 1
        self._open_key = None
        self._burst_retries = 0
        # The pause blanked the DMD; a Move restores it on arrival itself.
        if self._r.steps[self._i].kind != "move":
            self._safe(self._h.light, self._dmd_on)
        self._enter_step()

    def skip(self) -> None:
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._advance()

    def abort(self) -> None:
        """Stop for good. The adapter stops capture."""
        if self._phase in (Phase.ARMED, Phase.RUNNING, Phase.WAITING):
            self._halt("aborted")
        self._safe(self._h.stop_motion)
        self._safe(self._h.light, False)
        self._phase = Phase.DONE
        self._h.log("routine aborted")

    # ── the tick ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        if self._phase == Phase.ARMED:
            self._tick_armed()
        elif self._phase == Phase.WAITING:
            self._tick_trigger()
        elif self._phase == Phase.RUNNING:
            if self._step_done:
                self._step_done = False
                self._steps_completed += 1
                self._advance()
                return
            step = self._r.steps[self._i]
            if step.kind == "move":
                self._tick_move()
            elif step.kind in TIMED_KINDS:
                self._tick_wait()

    def _tick_armed(self) -> None:
        # Not `> 0`: the camera may already have been streaming at Start.
        n = self._frames()
        if n is None or self._arm_frame0 is None:
            self._halt("no frame count to detect TTL trigger by")
            return
        if n > self._arm_frame0:
            self._enter_step()

    def _tick_trigger(self) -> None:
        """Fallback (no `trigger_gate`): wait for the count to go still, then
        treat the next frame as the edge. A count that never settles faults
        rather than inventing an edge."""
        if self._gate_seq0 is not None:
            self._tick_trigger_gated()
            return
        n = self._frames()
        if n is None:
            self._halt("no frame count to detect the trigger by")
            return
        t = self._h.now()

        if not self._trig_gated:
            if n != self._trig_frame0:      # still draining
                self._trig_frame0 = n
                self._trig_still_since = t
            elif (t - self._trig_still_since >= self._trig_settle
                    and t - self._trig_t0 >= self._trig_drain):
                self._trig_gated = True
            if not self._trig_gated and t - self._trig_t0 > self._trig_timeout:
                self._halt("camera never stopped producing frames after "
                           "the trigger re-arm — it isn't re-arming")
            return

        if n > self._trig_frame0:
            self._edge_seen()
            return
        if t - self._trig_t0 > self._trig_timeout:
            self._halt(f"no camera trigger within {self._trig_timeout:g} s")

    def _edge_seen(self) -> None:
        """The trigger step's edge arrived."""
        self._safe(self._h.edge, self._safe_value(self._h.now, 0.0),
                   self._next_record_run())
        self._after_edge = self._burst_n > 0
        self._edge_at = (self._pos, self._cycle)
        self._phase = Phase.RUNNING
        self._step_done = True

    def _tick_trigger_gated(self) -> None:
        """Once the re-arm count passes ours, any frame is the edge. An edge
        while the camera is stopped is lost; the step waits for the next."""
        gate = self._h.trigger_gate()
        if gate is None:
            self._halt("the camera stopped reporting its trigger state")
            return
        seq, since = gate
        rearmed = seq > self._gate_seq0
        if rearmed and since > 0:
            self._edge_seen()
            return
        waited = self._h.now() - self._trig_t0
        if not rearmed:
            limit = min(REARM_TIMEOUT_S, self._trig_timeout)
            if waited > limit:
                self._halt(f"the camera did not finish the trigger re-arm "
                           f"within {limit:g} s")
        elif waited > self._trig_timeout:
            self._halt(f"no camera trigger within {self._trig_timeout:g} s")

    def _tick_move(self) -> None:
        step = self._r.steps[self._i]
        try:
            t = self._h.now()
            if self._arrived_at is None:
                if self._h.moving():
                    if t - self._issued_at > self._timeout:
                        self._halt(f"stage didn't arrive within "
                                   f"{self._timeout:g} s")
                    return
                self._arrived_at = t     # settle counts from arrival
            if t - self._arrived_at < step.settle_s:
                return
        except Exception as e:           # noqa: BLE001
            self._halt(f"settle failed ({type(e).__name__}: {e})")
            return
        # The move blanked the light; restore what Display last left lit.
        self._safe(self._h.light, self._dmd_on)
        self._step_done = True

    def _tick_wait(self) -> None:
        step = self._r.steps[self._i]
        if self._in_burst:
            self._tick_burst()
            return
        if step.unit == "frames":
            n = self._frames()
            if n is None or self._wait_frame0 is None:
                self._halt("frame count went away mid-step")
                return
            done = (n - self._wait_frame0) >= step.length
        else:
            done = (self._h.now() - self._wait_t0) >= step.length
        if done:
            self._step_done = True

    def _tick_burst(self) -> None:
        """Done at N real frames. Frames come from the camera's own count,
        so file rolls and preview skips don't move it."""
        got = self._safe_value(self._h.burst_frames, None)
        if got is None:
            self._halt("the camera stopped reporting its burst")
            return
        t = self._h.now()
        if got != self._burst_seen:
            self._burst_seen, self._burst_moved_at = got, t
        if got >= self._burst_n:
            self._burst_retries = 0
            self._step_done = True
        elif t - self._burst_moved_at > self._burst_stall:
            self._retry_burst(f"burst stopped at {got}/{self._burst_n} frames")

    def _retry_burst(self, reason: str) -> None:
        """Mark the short burst's run and wait for the next edge at its
        trigger step, whose re-arm opens the next file. Out of retries, it
        pauses there instead, so resume waits for an edge too."""
        self._close_open_run(interrupted=True, fault=reason)
        self._pos, self._cycle = self._edge_at
        self._i = self._order[self._pos]
        self._steps_completed -= 1      # the trigger step runs again
        self._attempt = 1
        self._burst_retries += 1
        if self._burst_retries > MAX_BURST_RETRIES:
            self._halt(f"{reason} ({MAX_BURST_RETRIES} retries in a row)")
            return
        self._h.log(f"{reason} — marked bad; retrying on the next edge "
                    f"({self._burst_retries}/{MAX_BURST_RETRIES})")
        self._enter_step()

    # ── step lifecycle ────────────────────────────────────────────────────────
    def _enter_step(self) -> None:
        """Open/close the recording for this position, then act on the step.
        Recording first, so a step that faults at once still leaves a run."""
        if not self._update_recording():
            # Already halted. Not a phase check: resume() arrives here PAUSED.
            return
        step = self._r.steps[self._i]
        self._arrived_at = None
        self._step_done = False
        # A repeat after a pause has no edge of its own: back to the clock.
        self._in_burst = (self._after_edge and step.kind == "record"
                          and self._attempt == 1)
        self._after_edge = False
        self._burst_seen = 0
        self._burst_moved_at = self._safe_value(self._h.now, 0.0)
        try:
            if step.kind == "move":
                # Never travel lit; `_tick_move` restores it on arrival.
                self._h.light(False)
                if (step.x_um is not None or step.y_um is not None
                        or step.z_um is not None):
                    self._h.move(step.x_um, step.y_um, step.z_um)
            elif step.kind == "display":
                if step.pattern:
                    self._h.set_pattern(step.pattern)
                    self._h.light(True)
                else:
                    self._h.light(False)
                self._dmd_on = bool(step.pattern)
                self._step_done = True
            elif step.kind in TIMED_KINDS:
                self._wait_t0 = self._h.now()
                self._wait_frame0 = self._frames()
                if step.unit == "frames" and self._wait_frame0 is None:
                    raise RuntimeError(
                        "no frame count to measure a frames step by")
            elif step.kind == "puff":
                self._h.puff()
                self._step_done = True
            elif step.kind == "trigger":
                # Read before asking, so our re-arm is the one that moves it.
                gate = self._h.trigger_gate()
                self._gate_seq0 = None if gate is None else gate[0]
                nxt = self._next_record_run()
                if nxt is not None:
                    self._h.prepare_recording(nxt)
                self._h.arm_trigger()
                if nxt is not None:
                    # Lit before the edge: the camera starts on it, the
                    # software only a tick or more later (rig: 18-42 frames).
                    self._h.led(True)
                self._trig_t0 = self._trig_still_since = self._h.now()
                self._trig_gated = False
                self._trig_frame0 = self._frames()
                if self._trig_frame0 is None:
                    raise RuntimeError(
                        "no frame count to detect a trigger by")
        except Exception as e:           # noqa: BLE001
            self._halt(f"step {self._i + 1} setup failed "
                       f"({type(e).__name__}: {e})")
            return
        self._issued_at = self._safe_value(self._h.now, 0.0)
        self._phase = (Phase.WAITING if step.kind == "trigger"
                       else Phase.RUNNING)
        self._h.log(f"step {self._i + 1} ({self._pos + 1}/{len(self._order)} "
                    f"this cycle, cycle {self._cycle + 1}/"
                    f"{max(1, self._r.cycles)}): {step.describe()}")

    def _next_record_run(self) -> RecordingRun | None:
        """The run a `record` step right after this position will open."""
        pos, cycle = self._pos + 1, self._cycle
        if pos >= len(self._order):
            pos, cycle = 0, cycle + 1
        if cycle >= max(1, self._r.cycles):
            return None
        i = self._order[pos]
        if self._r.steps[i].kind != "record":
            return None
        return RecordingRun(region=recording_region_at(self._r, i),
                            start_index=i, end_index=i, cycle=cycle,
                            attempt=1, t0=self._h.now(), frame0=None)

    def rearm_step(self) -> bool:
        """Restart the current Wait step's clock, after a .dcimg roll stopped
        the camera mid-step. Wait steps only: a trigger step's baseline must
        not move, or it would swallow its own edge."""
        if not self._order:
            return False
        step = self._r.steps[self._i]
        if step.kind not in TIMED_KINDS:
            return False
        self._wait_t0 = self._h.now()
        self._wait_frame0 = self._frames()
        return True

    def _advance(self) -> None:
        self._attempt = 1
        self._pos += 1
        if self._pos >= len(self._order):
            self._pos = 0
            self._cycle += 1
        if self._cycle >= max(1, self._r.cycles):
            self._close_open_run()
            self._phase = Phase.DONE
            self._safe(self._h.light, False)
            self._h.log(f"routine finished — {self.steps_done()} step(s)")
            return
        self._i = self._order[self._pos]
        self._enter_step()

    def _halt(self, reason: str) -> None:
        """Fault or operator pause: stop what actuates, keep what captures."""
        self.fault = reason
        self._safe(self._h.stop_motion)
        self._safe(self._h.light, False)
        self._safe(self._h.led, False)      # a trigger step lit it early
        self._close_open_run(interrupted=True, fault=reason)
        self._phase = Phase.PAUSED
        self._h.log(f"routine paused: {reason}")

    # ── recording brackets ───────────────────────────────────────────────────
    def _update_recording(self) -> bool:
        """Open/close a run as `self._i` crosses a bracket edge. Keyed by
        (cycle, serial): the serial alone would merge a bracket spanning a
        cycle boundary into one run. False only if opening failed (halted)."""
        serial = self._rec_ids[self._pos]
        key = None if serial is None else (self._cycle, serial)
        if key == self._open_key and self._open_run is not None:
            self._open_run.end_index = self._i
            return True
        self._close_open_run()
        self._open_key = key
        if key is not None:
            return self._open_recording(recording_region_at(self._r, self._i))
        return True

    def _close_open_run(self, **kw) -> None:
        if self._open_run is not None:
            self._close_recording(self._open_run, **kw)
            self._open_key = None

    def _open_recording(self, region: int) -> bool:
        try:
            run = RecordingRun(region=region, start_index=self._i,
                               end_index=self._i, cycle=self._cycle,
                               attempt=self._attempt, t0=self._h.now(),
                               frame0=self._frames())
            self._h.led(True)
            self._h.begin_recording(run)
            self._open_run = run
            return True
        except Exception as e:           # noqa: BLE001
            self._halt(f"recording couldn't start "
                       f"({type(e).__name__}: {e})")
            return False

    def _close_recording(self, run: RecordingRun, *, interrupted: bool = False,
                         fault: str = "") -> None:
        run.t_end = self._safe_value(self._h.now, run.t0)
        n = self._frames()
        run.frames = None if (n is None or run.frame0 is None) else n - run.frame0
        run.interrupted = interrupted
        run.fault = fault
        self._safe(self._h.led, False)
        self._safe(self._h.end_recording, run)
        self.runs.append(run)
        self._open_run = None

    # ── helpers ───────────────────────────────────────────────────────────────
    def _frames(self) -> int | None:
        return self._safe_value(self._h.frames, None)

    @staticmethod
    def _safe(fn: Callable, *args) -> None:
        """Teardown call: a second raise here would leave the light on."""
        try:
            fn(*args)
        except Exception:                # noqa: BLE001
            pass

    @staticmethod
    def _safe_value(fn: Callable, default):
        try:
            return fn()
        except Exception:                # noqa: BLE001
            return default
