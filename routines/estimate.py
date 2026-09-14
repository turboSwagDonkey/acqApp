"""How long a routine will take. No Qt.

A step's length is frames OR seconds and the two are never interconverted
(`settings.py`) — that is a *recording* rule. An estimate is not a recording,
so here frames do become seconds, at a frame rate the caller supplies and the
result names. With no frame rate the frames stay frames and are reported
beside the seconds rather than folded into a number that would be wrong.

What is NOT counted: stage travel. Nothing knows how long a move takes until
`RoutineHooks.moving` is filled (PLAN §6), so every estimate is a floor and
says so.
"""
from __future__ import annotations

from dataclasses import dataclass

from acqApp.routines.settings import Routine, Step, play_order


@dataclass(frozen=True)
class Estimate:
    """A routine's cost, split into what is known and what is not."""
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
        """The duration as one phrase. "about" because moves are not counted."""
        head = "about " if self.complete else "at least "
        out = head + clock(self.seconds)
        if self.frames:
            out += f" plus {self.frames:g} frames"
        return out


def step_seconds(step: Step, hz: float | None) -> tuple[float, float]:
    """One step as (seconds, unconverted frames). Settle is always seconds."""
    secs = max(0.0, step.settle_s)
    if step.unit == "seconds":
        return secs + max(0.0, step.length), 0.0
    if hz and hz > 0:
        return secs + max(0.0, step.length) / hz, 0.0
    return secs, max(0.0, step.length)


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
        moves=sum(1 for i in order
                  if routine.steps[i].x_um is not None
                  or routine.steps[i].y_um is not None),
        lit=sum(1 for i in order if routine.steps[i].pattern),
        hz=hz if hz and hz > 0 else None,
    )


def remaining(routine: Routine, hz: float | None, order_pos: int, cycle: int,
              progress: float = 0.0) -> Estimate:
    """What is left from part-way through `play_order(routine)[order_pos]` of
    `cycle`.

    `order_pos` indexes the EXPANDED play order (a repeated group's range
    appears once per repeat), not `routine.steps` directly — so time still
    left in a repeat is counted, not just steps still left on the page.
    `progress` is 0..1 through the current step's capture (`RoutineEngine`),
    so the readout does not jump a whole step at a time.
    """
    order = play_order(routine)
    if not order:
        return Estimate(hz=hz)
    cycles = max(1, routine.cycles)
    order_pos = max(0, min(order_pos, len(order) - 1))
    cycle = max(0, min(cycle, cycles - 1))

    secs = frames = 0.0
    # The rest of this cycle, the current step counted by what is left of it.
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
    """Seconds as the operator reads a duration. 124 s is not a duration.

    Rounded, because these are estimates: "23.3208 s" claims a precision the
    number does not have — no stage move is in it.
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
