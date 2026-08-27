"""Experiment routines — the executor. Pure, and no Qt.

Everything that moves, lights up or writes a file arrives as a **callable**
(`RoutineHooks`), the way `devices/dmd/calibration.py` takes `project`/`grab`.
That is what lets all of it be driven against fakes before anything on the rig
actuates — §2's requirement, and the whole difficulty of this feature.

`tick()`, not a loop with sleeps: a state machine over `now()` and `frames()`,
so pause/resume/abort are transitions and a test steps a whole routine on a
fake clock.

**A device failure PAUSES; it does not abort** (operator, PLAN §6 (4)): motion
stopped, light off, capture untouched, the operator decides. Two consequences
the plan left open, decided here:

- **The interrupted step's data is kept and marked**, never discarded — with an
  animal on the rig, recorded frames are not ours to throw away. `StepRun`
  carries `interrupted` and `fault` into the file.
- **Resume repeats the step**, as a new `attempt`. A step means "this much
  capture under these conditions"; half of one does not. Both attempts stay in
  the file.

The engine never touches the Recorder: it emits `begin_step`/`end_step`, and
whether that rolls a file (`save_mode="per_step"`) or marks a boundary is the
adapter's business.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from acqApp.routines.settings import Routine, Step

# A move that never reports arrival must fault, not hang the routine forever.
MOVE_TIMEOUT_S = 30.0


class Phase:
    """Where the engine is. Strings, so they go into the file and the panel."""
    IDLE    = "idle"
    SETTLE  = "settle"      # moving / changing pattern / waiting to settle
    CAPTURE = "capture"
    PAUSED  = "paused"      # a fault, or the operator — resume/skip/abort
    DONE    = "done"


def _noop(*_a, **_k) -> None:
    return None


@dataclass(frozen=True)
class RoutineHooks:
    """What the engine is allowed to do, one callable per thing.

    Split rather than one fat device object (ISP): the engine needs a clock, a
    frame count, a stage, a projector and a file boundary, and a test supplies
    only the ones its case exercises. Defaults are inert, so an unloaded
    instrument is a no-op rather than an AttributeError mid-run.
    """
    now:         Callable[[], float]                       # session-clock seconds
    frames:      Callable[[], int | None] = lambda: None    # monotonic frame count
    move:        Callable[[float | None, float | None], None] = _noop
    moving:      Callable[[], bool] = lambda: False         # still travelling?
    stop_motion: Callable[[], None] = _noop
    set_pattern: Callable[[str], None] = _noop
    light:       Callable[[bool], None] = _noop
    begin_step:  Callable[["StepRun"], None] = _noop
    end_step:    Callable[["StepRun"], None] = _noop
    log:         Callable[[str], None] = _noop


@dataclass
class StepRun:
    """One execution of one step — what the file records about it."""
    index:   int                    # step's position in the routine, 0-based
    cycle:   int                    # 0-based repeat of the whole step list
    attempt: int                    # 1 first time; 2+ after a paused step repeats
    label:   str
    t0:      float                  # session-clock seconds at capture start
    frame0:  int | None
    t_end:       float | None = None
    frames:      int | None = None
    interrupted: bool = False
    fault:       str = ""

    def attrs(self, session_origin: float = 0.0) -> dict[str, Any]:
        """What one step execution records about itself.

        Named here once so the two save modes cannot disagree: `single` files
        the list of these as `routine_runs`, `per_step` will write each as its
        own file's attributes. That is what `per_step` pays for giving up "one
        HDF5 per session" as a *file* property — every step names the session
        origin and its own t0 **on the same clock**, so a folder reassembles
        onto one timebase. Files that each restart from zero are not relatable,
        and that is unrecoverable once the animal is off the rig.
        """
        return {
            "routine_session_origin":   float(session_origin),
            "routine_step_index":       self.index,
            "routine_step_cycle":       self.cycle,
            "routine_step_attempt":     self.attempt,
            "routine_step_label":       self.label,
            "routine_step_t0":          float(self.t0),
            "routine_step_frame0":      -1 if self.frame0 is None else self.frame0,
            "routine_step_interrupted": bool(self.interrupted),
            "routine_step_fault":       self.fault,
        }


class RoutineError(RuntimeError):
    """The engine was asked for a transition it cannot make."""


class RoutineEngine:
    """Runs a `Routine` through `RoutineHooks`, one `tick()` at a time."""

    def __init__(self, routine: Routine, hooks: RoutineHooks, *,
                 move_timeout_s: float = MOVE_TIMEOUT_S) -> None:
        self._r = routine
        self._h = hooks
        self._timeout = move_timeout_s
        self.runs: list[StepRun] = []
        self.fault = ""
        self._phase = Phase.IDLE
        self._i = 0
        self._cycle = 0
        self._attempt = 1
        self._run: StepRun | None = None
        self._open = False              # begin_step delivered, end_step owed
        self._issued_at = 0.0           # when this step's move went out
        self._arrived_at: float | None = None
        self._started_at: float | None = None   # session clock at start()

    # ── readout ───────────────────────────────────────────────────────────────
    @property
    def phase(self) -> str:
        return self._phase

    @property
    def running(self) -> bool:
        """Is the rig under this engine's control? PAUSED counts — the stage is
        stopped but the routine still owns it, so modules must stay put."""
        return self._phase in (Phase.SETTLE, Phase.CAPTURE, Phase.PAUSED)

    @property
    def step(self) -> Step | None:
        if not self._r.steps or self._phase in (Phase.IDLE, Phase.DONE):
            return None
        return self._r.steps[self._i]

    @property
    def position(self) -> tuple[int, int, int]:
        """(step index, cycle, attempt) — the first two 0-based."""
        return self._i, self._cycle, self._attempt

    def steps_done(self) -> int:
        """Completed step executions; a repeated attempt is not counted twice."""
        return sum(1 for r in self.runs if not r.interrupted)

    def progress(self) -> float:
        """0..1 through the current step, by its OWN unit. 0 while settling."""
        run, step = self._run, self.step
        if run is None or step is None or self._phase != Phase.CAPTURE:
            return 0.0
        if step.unit == "frames":
            n = self._frames()
            if n is None or run.frame0 is None:
                return 0.0
            got = float(n - run.frame0)
        else:
            got = self._h.now() - run.t0
        return max(0.0, min(1.0, got / step.length)) if step.length > 0 else 1.0

    def total_runs(self) -> int:
        """Step executions a clean run performs — steps x cycles."""
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
        done = self._cycle * len(self._r.steps) + self._i + self.progress()
        return max(0.0, min(1.0, done / total))

    def elapsed(self) -> float:
        """Seconds since start(), on the session clock. 0 before it."""
        if self._started_at is None:
            return 0.0
        return max(0.0, self._safe_value(self._h.now, self._started_at)
                   - self._started_at)

    # ── control ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._phase in (Phase.SETTLE, Phase.CAPTURE, Phase.PAUSED):
            raise RoutineError("already running")
        if not self._r.steps:
            raise RoutineError("the routine has no steps")
        self.runs = []
        self.fault = ""
        self._i = self._cycle = 0
        self._attempt = 1
        self._started_at = self._safe_value(self._h.now, 0.0)
        self._enter_step()

    def pause(self, reason: str = "paused by the operator") -> None:
        if self._phase not in (Phase.SETTLE, Phase.CAPTURE):
            return
        self._halt(reason)

    def resume(self) -> None:
        """Repeat the paused step from its start, as a fresh attempt."""
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._attempt += 1
        self._enter_step()

    def skip(self) -> None:
        """Give up on the paused step and go on to the next one."""
        if self._phase != Phase.PAUSED:
            raise RoutineError("not paused")
        self.fault = ""
        self._advance()

    def abort(self) -> None:
        """Stop for good. Capture is still the operator's to stop."""
        if self._phase in (Phase.SETTLE, Phase.CAPTURE):
            self._halt("aborted")
        self._safe(self._h.stop_motion)
        self._safe(self._h.light, False)
        self._phase = Phase.DONE
        self._h.log("routine aborted")

    # ── the tick ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        """Advance the state machine. Cheap, and safe to call at any rate."""
        if self._phase == Phase.SETTLE:
            self._tick_settle()
        elif self._phase == Phase.CAPTURE:
            self._tick_capture()

    def _tick_settle(self) -> None:
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
        self._begin_capture()

    def _tick_capture(self) -> None:
        step, run = self._r.steps[self._i], self._run
        if run is None:                  # cannot happen; not worth crashing over
            self._halt("internal: capturing with no step run")
            return
        if step.unit == "frames":
            n = self._frames()
            if n is None or run.frame0 is None:
                self._halt("the frame count went away mid-step")
                return
            done = (n - run.frame0) >= step.length
        else:
            done = (self._h.now() - run.t0) >= step.length
        if done:
            self._finish_run()
            self._advance()

    # ── step lifecycle ────────────────────────────────────────────────────────
    def _enter_step(self) -> None:
        """Blank, move, load the pattern, then wait it out. Emits no light."""
        step = self._r.steps[self._i]
        self._arrived_at = None
        try:
            # Blank BEFORE the move: a lit panel travelling across the sample is
            # a stimulus nobody asked for.
            self._h.light(False)
            if step.x_um is not None or step.y_um is not None:
                self._h.move(step.x_um, step.y_um)
            if step.pattern:
                self._h.set_pattern(step.pattern)
        except Exception as e:           # noqa: BLE001 — any device failure
            self._halt(f"step {self._i + 1} setup failed "
                       f"({type(e).__name__}: {e})")
            return
        self._issued_at = self._safe_value(self._h.now, 0.0)
        self._phase = Phase.SETTLE
        self._h.log(f"step {self._i + 1}/{len(self._r.steps)} "
                    f"(cycle {self._cycle + 1}/{max(1, self._r.cycles)}): "
                    f"{step.describe()}")

    def _begin_capture(self) -> None:
        step = self._r.steps[self._i]
        try:
            if step.project:
                self._h.light(True)
            frame0 = self._frames()
            if step.unit == "frames" and frame0 is None:
                raise RuntimeError("no frame count to measure a frames step by")
            run = StepRun(index=self._i, cycle=self._cycle,
                          attempt=self._attempt,
                          label=step.label or step.describe(),
                          t0=self._h.now(), frame0=frame0)
            self._run = run
            self._h.begin_step(run)      # the adapter may open a file here
            self._open = True
        except Exception as e:           # noqa: BLE001
            self._halt(f"step {self._i + 1} could not start "
                       f"({type(e).__name__}: {e})")
            return
        self._phase = Phase.CAPTURE

    def _finish_run(self, *, interrupted: bool = False, fault: str = "") -> None:
        """Close the open run — kept and marked, never dropped."""
        run = self._run
        if run is None:
            return
        run.t_end = self._safe_value(self._h.now, run.t0)
        n = self._frames()
        run.frames = None if (n is None or run.frame0 is None) else n - run.frame0
        run.interrupted = interrupted
        run.fault = fault
        self._safe(self._h.light, False)
        if self._open:
            self._safe(self._h.end_step, run)
        self.runs.append(run)
        self._run, self._open = None, False

    def _advance(self) -> None:
        self._attempt = 1
        self._i += 1
        if self._i >= len(self._r.steps):
            self._i = 0
            self._cycle += 1
        if self._cycle >= max(1, self._r.cycles):
            self._phase = Phase.DONE
            self._safe(self._h.light, False)
            self._h.log(f"routine finished — {self.steps_done()} step(s)")
            return
        self._enter_step()

    def _halt(self, reason: str) -> None:
        """Fault or operator pause: stop what actuates, keep what captures."""
        self.fault = reason
        self._safe(self._h.stop_motion)
        self._safe(self._h.light, False)
        self._finish_run(interrupted=True, fault=reason)
        self._phase = Phase.PAUSED
        self._h.log(f"routine paused: {reason}")

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
