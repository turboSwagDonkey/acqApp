"""How long a routine will take. No Qt.

Unlike a recording (`settings.py`), an estimate converts frames to seconds, at
the caller's frame rate; with none, frames are reported beside the seconds.
Stage travel isn't timed, so every estimate is a floor.
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
        """The duration as one phrase."""
        head = "about " if self.complete else "at least "
        out = head + clock(self.seconds)
        if self.frames:
            out += f" plus {self.frames:g} frames"
        return out


def step_seconds(step: Step, hz: float | None) -> tuple[float, float]:
    """One step as (seconds, unconverted frames). A move counts its settle
    only; display/puff are instant. A trigger counts as zero though it can
    take arbitrarily long: when the edge comes isn't ours to know."""
    if step.kind == "move":
        return max(0.0, step.settle_s), 0.0
    if step.kind not in TIMED_KINDS:
        return 0.0, 0.0
    if step.unit == "seconds":
        return max(0.0, step.length), 0.0
    if hz and hz > 0:
        return max(0.0, step.length) / hz, 0.0
    return 0.0, max(0.0, step.length)


def _one_pass(routine: Routine, order: list[int],
              hz: float | None) -> Estimate:
    secs = frames = 0.0
    moves = lit = 0
    for i in order:
        s = routine.steps[i]
        a, b = step_seconds(s, hz)
        secs += a
        frames += b
        if s.kind == "move" and (s.x_um is not None or s.y_um is not None
                                 or s.z_um is not None):
            moves += 1
        elif s.kind == "display" and s.pattern:
            lit += 1
    return Estimate(seconds=secs, frames=frames, moves=moves, lit=lit,
                    hz=hz if hz and hz > 0 else None)


def estimate(routine: Routine, hz: float | None = None) -> Estimate:
    """The whole routine, cycles and group repeats included."""
    one = _one_pass(routine, play_order(routine), hz)
    cycles = max(1, routine.cycles)
    return Estimate(seconds=one.seconds * cycles, frames=one.frames * cycles,
                    moves=one.moves, lit=one.lit, hz=one.hz)


def remaining(routine: Routine, hz: float | None, order_pos: int, cycle: int,
              progress: float = 0.0) -> Estimate:
    """What's left from `progress` (0..1) through position `order_pos` of
    the expanded `play_order` in `cycle`, so repeats still ahead count."""
    order = play_order(routine)
    if not order:
        return Estimate(hz=hz)
    cycles = max(1, routine.cycles)
    order_pos = max(0, min(order_pos, len(order) - 1))
    cycle = max(0, min(cycle, cycles - 1))

    secs = frames = 0.0
    for pos in range(order_pos, len(order)):
        a, b = step_seconds(routine.steps[order[pos]], hz)
        share = 1.0 - max(0.0, min(1.0, progress)) if pos == order_pos else 1.0
        secs += a * share
        frames += b * share
    whole = _one_pass(routine, order, hz)
    left = cycles - cycle - 1
    return Estimate(seconds=secs + whole.seconds * left,
                    frames=frames + whole.frames * left,
                    moves=whole.moves, lit=whole.lit, hz=whole.hz)


def clock(seconds: float) -> str:
    """Seconds as the operator reads a duration, rounded: an estimate has
    no sub-second precision."""
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


def burst_frames(routine: Routine, hz: float | None) -> tuple[int, str]:
    """Frames per edge for a camera in burst mode: the one length every
    Record right after a Trigger shares, at `hz` for a seconds Record (the
    camera counts pulses, not time). (0, "") if the routine has no such pair;
    (0, why) if they disagree."""
    lengths = set()
    for a, b in zip(routine.steps, routine.steps[1:]):
        if a.kind != "trigger" or b.kind != "record":
            continue
        if b.unit == "frames":
            lengths.add(int(round(b.length)))
        elif hz and hz > 0:
            lengths.add(int(round(b.length * hz)))
        else:
            return 0, "no frame rate to turn a seconds Record into frames"
    if not lengths:
        return 0, ""
    if len(lengths) > 1:
        return 0, (f"every Record after a Trigger must be the same length "
                   f"(one burst per edge); these come to "
                   f"{', '.join(str(n) for n in sorted(lengths))} frames")
    n = lengths.pop()
    return (n, "") if n > 0 else (0, "a Record after a Trigger is empty")
