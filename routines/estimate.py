"""How long a routine will take. No Qt.

A step's length is frames OR seconds and the two are never interconverted
(`settings.py`) — that's a *recording* rule. An estimate isn't a recording,
so here frames do become seconds, at a frame rate the caller supplies and the
result names. With no frame rate the frames stay frames and are reported
beside the seconds rather than folded into a number that would be wrong.

What's NOT counted: stage travel. Nothing knows how long a move takes until
`RoutineHooks.moving` is filled (PLAN §6), so every estimate is a floor and
says so.
"""
from __future__ import annotations

from dataclasses import dataclass

from acqApp.routines.settings import Routine, Step, TIMED_KINDS, play_order


@dataclass(frozen=True)
class Estimate:
    """A routine's cost, split into what's known and what's not."""
    seconds: float = 0.0        # settle + seconds-steps + converted frames
    frames:  float = 0.0        # frames left unconverted (no frame rate)
    moves:   int = 0            # steps that move the stage — travel is untimed
    lit:     int = 0            # steps with a pattern — light follows it
    hz:      float | None = None

    @property
    def complete(self) -> bool:
        """True when the whole routine is expressed in seconds."""
        return self.frames <= 0

    def text(self) -> str:
        """The duration as one phrase. "about" because moves aren't counted."""
        head = "about " if self.complete else "at least "
        out = head + clock(self.seconds)
        if self.frames:
            out += f" plus {self.frames:g} frames"
        return out


def step_seconds(step: Step, hz: float | None) -> tuple[float, float]:
    """One step as (seconds, unconverted frames), by its kind:

    - `move`: `settle_s` only — travel itself is untimed (see module docstring).
    - `wait`: `length`/`unit`, converted to seconds at `hz` if given.
    - `display`/`puff`: instant — nothing to time, the same way a puffer's
      own fire duration was never timed either.
    - `trigger`: **counted as zero**, though it's the one kind that can take
      arbitrarily long. How soon an external source sends its edge isn't ours
      to know, and guessing would be worse than plainly excluding it — so a
      routine built on trigger steps estimates only the capturing it does
      between them, and always finishes later than the figure shown.
    """
    if step.kind == "move":
        return max(0.0, step.settle_s), 0.0
    if step.kind not in TIMED_KINDS:
        return 0.0, 0.0
    if step.unit == "seconds":
        return max(0.0, step.length), 0.0
    if hz and hz > 0:
        return max(0.0, step.length) / hz, 0.0
    return 0.0, max(0.0, step.length)


def estimate(routine: Routine, hz: float | None = None) -> Estimate:
    """The whole routine, cycles included — a repeat group's range is counted
    once per repeat, via `play_order`."""
    order = play_order(routine)
    cycles = max(1, routine.cycles)
    secs = frames = 0.0
    for i in order:
        a, b = step_seconds(routine.steps[i], hz)
        secs += a
        frames += b
    return Estimate(
        seconds=secs * cycles,
        frames=frames * cycles,
        moves=sum(1 for i in order if routine.steps[i].kind == "move"
                  and (routine.steps[i].x_um is not None
                       or routine.steps[i].y_um is not None
                       or routine.steps[i].z_um is not None)),
        lit=sum(1 for i in order if routine.steps[i].kind == "display"
               and routine.steps[i].pattern),
        hz=hz if hz and hz > 0 else None,
    )


def remaining(routine: Routine, hz: float | None, order_pos: int, cycle: int,
              progress: float = 0.0) -> Estimate:
    """What's left from part-way through `play_order(routine)[order_pos]` of
    `cycle`.

    `order_pos` indexes the EXPANDED play order (a repeated group's range
    appears once per repeat), not `routine.steps` directly — so time still
    left in a repeat is counted, not just steps still left on the page.
    `progress` is 0..1 through the current step's capture (`RoutineEngine`),
    so the readout doesn't jump a whole step at a time.
    """
    order = play_order(routine)
    if not order:
        return Estimate(hz=hz)
    cycles = max(1, routine.cycles)
    order_pos = max(0, min(order_pos, len(order) - 1))
    cycle = max(0, min(cycle, cycles - 1))

    secs = frames = 0.0
    # The rest of this cycle, the current step counted by what's left of it.
    for pos in range(order_pos, len(order)):
        a, b = step_seconds(routine.steps[order[pos]], hz)
        share = 1.0 - max(0.0, min(1.0, progress)) if pos == order_pos else 1.0
        secs += a * share
        frames += b * share
    # Then every whole cycle after this one.
    whole = estimate(Routine(steps=routine.steps, groups=routine.groups,
                             cycles=1), hz)
    left = cycles - cycle - 1
    return Estimate(seconds=secs + whole.seconds * left,
                    frames=frames + whole.frames * left,
                    moves=whole.moves, lit=whole.lit, hz=whole.hz)


def clock(seconds: float) -> str:
    """Seconds as the operator reads a duration. 124 s isn't a duration.

    Rounded, because these are estimates: "23.3208 s" claims a precision the
    number doesn't have — no stage move is in it.
    """
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 90:
        return f"{round(seconds)} s"
    m, sec = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}:{sec:02d} min"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d} h"
