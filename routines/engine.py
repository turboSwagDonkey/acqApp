"""Experiment routines — the executor. Pure, and no Qt.

Everything that moves, lights up or writes a file arrives as a **callable**
(`RoutineHooks`), the way `devices/dmd/calibration.py` takes `project`/`grab`.
That is what lets all of it be driven against fakes before anything on the rig
actuates — §2's requirement, and the whole difficulty of this feature.

`tick()`, not a loop with sleeps: a state machine over `now()` and `frames()`,
so pause/resume/abort are transitions and a test steps a whole routine on a
fake clock.

A step now completes on its own terms (`_enter_step` dispatches on
`step.kind`) and **recording is a separate overlay**: `Routine`/
`recording_run_ids` already say WHICH steps a `Recording` bracket covers;
this file only decides WHEN to open/close one as `self._i` moves through
`self._order`, via `_update_recording`. The engine never touches the
Recorder: it emits `begin_recording`/`end_recording`, and whether that
rolls a file (`save_mode="per_repeat"`/`"per_group"`) or marks a boundary
is the adapter's business.

**A device failure PAUSES; it does not abort** (operator, PLAN §6 (4)): motion
stopped, light off, capture untouched, the operator decides. Two consequences
the plan left open, decided here:

- **The interrupted recording's data is kept and marked**, never discarded —
  with an animal on the rig, recorded frames are not ours to throw away.
  `RecordingRun` carries `interrupted` and `fault` into the file.
- **Resume repeats the interrupted step**, as a new `attempt`, and — if that
  step is still inside a Recording — opens a **fresh** recording run rather
  than reattaching to the one the pause just closed (operator-confirmed):
  falls straight out of `_update_recording` comparing `(cycle, serial)` keys,
  since `resume()` clears `self._open_key` to nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from acqApp.routines.settings import (Routine, Step, play_order,
                                      recording_region_at, recording_run_ids)

# A move that never reports arrival must fault, not hang the routine forever.
MOVE_TIMEOUT_S = 30.0

# The same argument for a `trigger` step, but far more generous: an external
# source decides when, and a stimulus rig may legitimately be quiet for
# minutes. This only exists so a miswired line is eventually reported rather
# than hanging the routine forever.
TRIGGER_TIMEOUT_S = 600.0

# Dead time at the start of a `trigger` step, before an arriving frame counts
# as its edge. `arm_trigger` is asynchronous — the camera's capture loop acts
# on the request only when its own frame wait next expires — so frames already
# in flight keep being written for a moment, and would otherwise read as an
# edge that never happened. Must therefore exceed that loop's frame-wait
# timeout (`devices/voltage_cam/acquisition.py`'s `_WAIT_TIMEOUT`, 0.5 s).
#
# It is also the smallest gap between one recording ending and the next edge
# being detectable: an edge inside the window is ignored, which is the
# operator's "build for safety" choice over latching it.
TRIGGER_DRAIN_S = 1.5


class Phase:
    """Where the engine is. Strings, so they go into the file and the panel."""
    IDLE    = "idle"
    ARMED   = "armed"       # start_trigger="ttl": waiting for the camera's
                            # first externally-triggered frame
    RUNNING = "running"     # executing a step — the panel reads `step.kind`
                            # and `progress()` for what to say, not the phase
    WAITING = "waiting"     # inside a `trigger` step: the camera is re-armed
                            # and this is waiting for ITS edge. Distinct from
                            # ARMED, which happens once before step 1 and is
                            # about the routine as a whole; this recurs mid-run
                            # and the panel says which step it belongs to.
    PAUSED  = "paused"      # a fault, or the operator — resume/skip/abort
    DONE    = "done"


def _noop(*_a, **_k) -> None:
    return None


def _ratio(got: float, total: float) -> float:
    """`got/total`, clamped to 0..1; 1.0 if `total` is not positive."""
    return max(0.0, min(1.0, got / total)) if total > 0 else 1.0


@dataclass(frozen=True)
class RoutineHooks:
    """What the engine is allowed to do, one callable per thing.

    Split rather than one fat device object (ISP): the engine needs a clock, a
    frame count, a stage, a projector and a file boundary, and a test supplies
    only the ones its case exercises. Defaults are inert, so an unloaded
    instrument is a no-op rather than an AttributeError mid-run.
    """
    now:            Callable[[], float]                       # session-clock seconds
    frames:         Callable[[], int | None] = lambda: None    # monotonic frame count
    move:           Callable[[float | None, float | None, float | None],
                             None] = _noop
    moving:         Callable[[], bool] = lambda: False         # still travelling?
    stop_motion:    Callable[[], None] = _noop
    set_pattern:    Callable[[str], None] = _noop
    light:          Callable[[bool], None] = _noop
    led:            Callable[[bool], None] = _noop
    puff:           Callable[[], None] = _noop     # fires one puff, its own duration
    # Re-gates the camera so its NEXT edge is detectable; it latches otherwise,
    # so without this only a run's first edge would ever be seen. Inert by
    # default — an engine driven without a camera just waits on `frames`.
    arm_trigger:    Callable[[], None] = _noop
    begin_recording: Callable[["RecordingRun"], None] = _noop
    end_recording:   Callable[["RecordingRun"], None] = _noop
    log:            Callable[[str], None] = _noop


@dataclass
class RecordingRun:
    """One execution of one `Recording` bracket — what the file records
    about it. `region` indexes `routine.recordings`; `start_index`/
    `end_index` are the `routine.steps` span this particular pass actually
    covered (equal to the bracket's full span unless a pause cut it short)."""
    region:      int
    start_index: int
    end_index:   int
    cycle:       int                # 0-based repeat of the whole step list
    attempt:     int                # 1 first time; 2+ after a paused step repeats
    t0:          float              # session-clock seconds at open
    frame0:      int | None
    t_end:       float | None = None
    frames:      int | None = None
    interrupted: bool = False
    fault:       str = ""

    def attrs(self, session_origin: float = 0.0) -> dict[str, Any]:
        """What one recording run records about itself.

        Named here once so every save mode reads it the same way: `single`
        files the list of these as `routine_runs`, `per_repeat`/`per_group`
        write each (rolled file's slice) the same way — see
        `adapters/routines.py`'s `final_metadata()`. Every run names the
        session origin and its own t0 **on the same clock**, so a folder of
        several files reassembles onto one timebase.
        """
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
    """The engine was asked for a transition it cannot make."""


class RoutineEngine:
    """Runs a `Routine` through `RoutineHooks`, one `tick()` at a time."""

    def __init__(self, routine: Routine, hooks: RoutineHooks, *,
                 move_timeout_s: float = MOVE_TIMEOUT_S,
                 trigger_timeout_s: float = TRIGGER_TIMEOUT_S,
                 trigger_drain_s: float = TRIGGER_DRAIN_S) -> None:
        self._r = routine
        self._h = hooks
        self._timeout = move_timeout_s
        self._trig_timeout = trigger_timeout_s
        self._trig_drain = trigger_drain_s
        self.runs: list[RecordingRun] = []
        self.fault = ""
        self._phase = Phase.IDLE
        self._order: list[int] = []     # play_order(routine): a repeat
                                         # group's range appears once per repeat
        self._rec_ids: list[int | None] = []   # recording_run_ids(routine, order)
        self._pos = 0                   # position within self._order
        self._i = 0                     # self._order[self._pos] — the real
                                         # step index, what indexes routine.steps
        self._cycle = 0
        self._attempt = 1
        self._steps_completed = 0
        self._step_done = False         # this tick's step needs no more work
        self._dmd_on = False            # what the last Display step left lit
        self._open_run: RecordingRun | None = None
        self._open_key: tuple[int, int] | None = None   # (cycle, serial)
        self._issued_at = 0.0           # when this step's move went out
        self._arrived_at: float | None = None
        self._wait_t0 = 0.0
        self._wait_frame0: int | None = None
        self._started_at: float | None = None   # session clock at start()
        self._arm_frame0: int | None = None      # frame count when armed
        self._trig_frame0: int | None = None     # frame count when a `trigger`
                                                  # step re-armed the camera
        self._trig_t0 = 0.0                      # when it started waiting

    # ── readout ───────────────────────────────────────────────────────────────
    @property
    def phase(self) -> str:
        return self._phase

    @property
    def running(self) -> bool:
        """Is the rig under this engine's control? PAUSED counts — the stage is
        stopped but the routine still owns it, so modules must stay put.
        ARMED counts too — the recording it opened is already running. So does
        WAITING: mid-routine, holding at a `trigger` step for its edge."""
        return self._phase in (Phase.ARMED, Phase.RUNNING, Phase.WAITING,
                               Phase.PAUSED)

    @property
    def step(self) -> Step | None:
        if not self._r.steps or self._phase in (Phase.IDLE, Phase.DONE):
            return None
        return self._r.steps[self._i]

    @property
    def position(self) -> tuple[int, int, int]:
        """(step index, cycle, attempt) — the first two 0-based. The step
        index is into `routine.steps` (the table row), not the expanded
        play order — see `order_position` for that."""
        return self._i, self._cycle, self._attempt

    @property
    def order_position(self) -> int:
        """0-based position within `play_order(routine)` for this cycle —
        what `progress`/`remaining` need to account for a repeat group,
        since a table row number alone cannot say which repeat this is."""
        return self._pos

    def steps_done(self) -> int:
        """Atomic steps completed normally — a skipped/paused one is not
        counted, the same way a repeated attempt was never counted twice."""
        return self._steps_completed

    def progress(self) -> float:
        """0..1 through the current step, by its OWN terms. 0 outside RUNNING."""
        step = self.step
        if step is None or self._phase != Phase.RUNNING:
            return 0.0
        if step.kind == "wait":
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
        """Atomic step executions a clean run performs — the expanded play
        order (repeat groups included) x cycles."""
        return self._r.total_steps()

    def overall_progress(self) -> float:
        """0..1 through the WHOLE routine, the current step's fraction included.

        Position-based, not `steps_done()`: a repeated attempt is not backwards
        progress, and a bar that goes back would read as a fault.
        """
        total = self.total_runs()
        if total <= 0 or self._phase == Phase.IDLE:
            return 0.0
        if self._phase == Phase.DONE:
            return 1.0
        done = self._cycle * len(self._order) + self._pos + self.progress()
        return max(0.0, min(1.0, done / total))

    def elapsed(self) -> float:
        """Seconds since start(), on the session clock. 0 before it."""
        if self._started_at is None:
            return 0.0
        return max(0.0, self._safe_value(self._h.now, self._started_at)
                   - self._started_at)

    # ── control ───────────────────────────────────────────────────────────────
    def start(self, trigger: str = "manual") -> None:
        """Begin the routine. `trigger="ttl"` ARMS it instead of moving right
        away: the caller has already opened the recording, so the camera (in
        its own External-edge mode) is sitting there waiting for a pulse, and
        step 1 does not begin until a frame the camera did not have at arm
        time actually arrives — see `_tick_armed`.
        """
        if self._phase in (Phase.ARMED, Phase.RUNNING, Phase.PAUSED):
            raise RoutineError("already running")
        if not self._r.steps:
            raise RoutineError("the routine has no steps")
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
        if trigger == "ttl":
            self._arm_frame0 = self._frames()
            self._phase = Phase.ARMED
            self._h.log("routine armed — waiting for the camera's TTL trigger")
        else:
            self._enter_step()

    def pause(self, reason: str = "paused by the operator") -> None:
        # WAITING counts: a `trigger` step can sit there for minutes, and the
        # operator must be able to take the rig back rather than be held by an
        # edge that may never arrive.
        if self._phase not in (Phase.RUNNING, Phase.WAITING):
            return
        self._halt(reason)

    def resume(self) -> None:
        """Repeat the paused step from its start, as a fresh attempt. A
        Recording the step is still inside gets a fresh run too — `_halt`
        already closed the old one, and clearing `self._open_key` here means
        `_update_recording` cannot mistake this for "still the same run"."""
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._attempt += 1
        self._open_key = None
        self._enter_step()

    def skip(self) -> None:
        """Give up on the paused step and go on to the next one."""
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._advance()

    def abort(self) -> None:
        """Stop for good. Capture is still the operator's to stop."""
        if self._phase in (Phase.ARMED, Phase.RUNNING, Phase.WAITING):
            self._halt("aborted")
        self._safe(self._h.stop_motion)
        self._safe(self._h.light, False)
        self._phase = Phase.DONE
        self._h.log("routine aborted")

    # ── the tick ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        """Advance the state machine. Cheap, and safe to call at any rate."""
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
            elif step.kind == "wait":
                self._tick_wait()
            # display/puff always set _step_done in _enter_step and never
            # reach here.

    def _tick_armed(self) -> None:
        """Waiting for the TTL pulse: a frame the camera did not have when we
        armed. Not `> 0` — the camera may already have been mid-stream on some
        other trigger mode, and this must fire on ITS NEXT frame, not on one
        that arrived before the operator pressed Start."""
        n = self._frames()
        if n is None or self._arm_frame0 is None:
            self._halt("no frame count to detect the TTL trigger by")
            return
        if n > self._arm_frame0:
            self._enter_step()

    def _tick_trigger(self) -> None:
        """Inside a `trigger` step: a frame beyond the baseline is its edge.

        The baseline is not simply "the count at entry" — `arm_trigger` is
        asynchronous, so frames already in flight keep being written for a
        moment. For `TRIGGER_DRAIN_S` those RAISE the baseline instead of
        satisfying it; only afterwards does an increase mean a real edge. That
        window is also why an edge arriving while the routine was busy
        elsewhere is ignored rather than counted.
        """
        n = self._frames()
        if n is None:
            self._halt("no frame count to detect the trigger by")
            return
        t = self._h.now()
        if t - self._trig_t0 < self._trig_drain:
            self._trig_frame0 = max(self._trig_frame0, n)
            return
        if n > self._trig_frame0:
            self._phase = Phase.RUNNING
            self._step_done = True
            return
        if t - self._trig_t0 > self._trig_timeout:
            self._halt(f"no camera trigger within {self._trig_timeout:g} s")

    def _tick_move(self) -> None:
        step = self._r.steps[self._i]
        try:
            t = self._h.now()
            if self._arrived_at is None:
                if self._h.moving():
                    if t - self._issued_at > self._timeout:
                        self._halt(f"stage did not arrive within "
                                   f"{self._timeout:g} s")
                    return
                self._arrived_at = t     # settle counts from arrival, not issue
            if t - self._arrived_at < step.settle_s:
                return
        except Exception as e:           # noqa: BLE001 — any device failure
            self._halt(f"settle failed ({type(e).__name__}: {e})")
            return
        # Restore whatever the last Display step left lit — the move blanked
        # it on entry so a lit panel never travels across the sample.
        self._safe(self._h.light, self._dmd_on)
        self._step_done = True

    def _tick_wait(self) -> None:
        step = self._r.steps[self._i]
        if step.unit == "frames":
            n = self._frames()
            if n is None or self._wait_frame0 is None:
                self._halt("the frame count went away mid-step")
                return
            done = (n - self._wait_frame0) >= step.length
        else:
            done = (self._h.now() - self._wait_t0) >= step.length
        if done:
            self._step_done = True

    # ── step lifecycle ────────────────────────────────────────────────────────
    def _enter_step(self) -> None:
        """Open/close the recording bracket for this position, then act on
        the step itself: move, start/stop displaying, arm a wait, or puff.

        Recording opens BEFORE the step's own action is attempted — on
        purpose: the underlying camera records continuously regardless of
        whether any one step's setup succeeds, so a step that faults
        immediately inside a Recording bracket still gets a `RecordingRun`
        (opened, then closed `interrupted` with ~0 duration) rather than no
        record at all. A step OUTSIDE any bracket that faults the same way
        opens nothing, same as always.
        """
        if not self._update_recording():
            # A recording could not open, and `_update_recording` already
            # halted us — the step's own action must not run on top of that
            # pause, or the stage/DMD would move after the operator was just
            # told to decide. NOT a `self._phase == Phase.PAUSED` check: this
            # is also how `resume()` reaches here, with the phase already
            # PAUSED from the halt it is resuming FROM — that check could not
            # tell "just failed" from "already was", and would refuse to ever
            # leave PAUSED again once the routine had paused once (any pause,
            # not just this one).
            return
        step = self._r.steps[self._i]
        self._arrived_at = None
        self._step_done = False
        try:
            if step.kind == "move":
                # Blank BEFORE the move: a lit panel travelling across the
                # sample is a stimulus nobody asked for. `_tick_move` restores
                # `self._dmd_on` once the stage has arrived and settled.
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
            elif step.kind == "wait":
                self._wait_t0 = self._h.now()
                self._wait_frame0 = self._frames()
                if step.unit == "frames" and self._wait_frame0 is None:
                    raise RuntimeError(
                        "no frame count to measure a frames step by")
            elif step.kind == "puff":
                self._h.puff()
                self._step_done = True
            elif step.kind == "trigger":
                # Ask for the re-arm, then start the clock the drain window is
                # measured against — `_tick_trigger` keeps raising the baseline
                # for that long, which is what absorbs the frames still being
                # written while the camera's capture loop gets round to the
                # request.
                self._h.arm_trigger()
                self._trig_t0 = self._h.now()
                self._trig_frame0 = self._frames()
                if self._trig_frame0 is None:
                    raise RuntimeError(
                        "no frame count to detect a trigger by")
        except Exception as e:           # noqa: BLE001 — any device failure
            self._halt(f"step {self._i + 1} setup failed "
                       f"({type(e).__name__}: {e})")
            return
        self._issued_at = self._safe_value(self._h.now, 0.0)
        self._phase = (Phase.WAITING if step.kind == "trigger"
                       else Phase.RUNNING)
        # Position in the EXPANDED order, not the table row alone — inside a
        # repeat group "step 3" on its own does not say which repeat this is.
        self._h.log(f"step {self._i + 1} ({self._pos + 1}/{len(self._order)} "
                    f"this cycle, cycle {self._cycle + 1}/"
                    f"{max(1, self._r.cycles)}): {step.describe()}")

    def _advance(self) -> None:
        self._attempt = 1
        self._pos += 1
        if self._pos >= len(self._order):
            self._pos = 0
            self._cycle += 1
        if self._cycle >= max(1, self._r.cycles):
            self._close_open_run()       # whatever the last step left open
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
        if self._open_run is not None:
            self._close_recording(self._open_run, interrupted=True, fault=reason)
            self._open_key = None
        self._phase = Phase.PAUSED
        self._h.log(f"routine paused: {reason}")

    # ── recording brackets ───────────────────────────────────────────────────
    def _update_recording(self) -> bool:
        """Open/close a `RecordingRun` as `self._i` crosses a `Recording`'s
        edge. Keyed by `(cycle, serial)`, not the bare serial from
        `recording_run_ids` — a serial alone repeats every cycle (it only
        accounts for repeat GROUPS inside one pass of `play_order`), so
        without the cycle a Recording spanning steps that also span a cycle
        boundary would merge cycle 1 and cycle 2 into one run.

        Returns False only if opening a NEW recording failed — `_open_recording`
        already halted the engine in that case, and `_enter_step` must not run
        the step's own action on top of it. True otherwise (nothing to open,
        or the open succeeded).
        """
        serial = self._rec_ids[self._pos]
        key = None if serial is None else (self._cycle, serial)
        if key == self._open_key and self._open_run is not None:
            self._open_run.end_index = self._i
            return True
        if self._open_run is not None:
            self._close_recording(self._open_run)
        self._open_key = key
        if key is not None:
            return self._open_recording(recording_region_at(self._r, self._i))
        return True

    def _close_open_run(self) -> None:
        """Close whatever recording is open, with no successor to open —
        used when the routine ends, never mid-run (`_update_recording` owns
        that transition, since it may need to open a new one in the same
        breath)."""
        if self._open_run is not None:
            self._close_recording(self._open_run)
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
            self._halt(f"recording could not start "
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
        """Call a hook while tearing down. Something is already wrong; a second
        raise here would leave the light on."""
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
