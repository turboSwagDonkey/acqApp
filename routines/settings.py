"""Experiment routines — the protocol, and no Qt.

A routine is a list of steps executed in order: put the stage here, put this
pattern up, capture this much, move on. `Step` and `Routine` are what persists;
`validate()` is what refuses a run *before* it starts.

Two things here were the operator's calls (PLAN §6) and are load-bearing:

- **A step's length is frames OR seconds, its author's choice**, never
  interconverted — at 106 Hz a rounded conversion sheds frames at every step
  boundary, so `unit` travels with `length` into the engine.
- **Validation is up front.** A stage target outside the soft limits is a
  refusal at the Start button, not a fault at step 7 of 12 with an animal on
  the rig.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from acqApp.devices.voltage_cam.presets import TRIGGER_MODES

UNITS = ("frames", "seconds")

# key -> label. `single` keeps the one-file-per-session invariant; `per_step`
# trades it for a folder, and pays for it in acq/writer attributes (see engine).
SAVE_MODES: dict[str, str] = {
    "single":   "One file for the whole routine",
    "per_step": "One file per step",
}

# key -> label. `manual` is the button; `ttl` arms the routine on Start and
# holds it there until the voltage camera reports a frame it did not have at
# arm time — the camera's own "External edge" trigger mode is what makes that
# frame arrive on a real TTL pulse rather than on its own clock, so nothing
# here reads a DAQ line: the camera already IS the TTL input.
START_TRIGGERS: dict[str, str] = {
    "manual": "Manual — click Start",
    "ttl":    "TTL — waits for the camera's externally-triggered frame",
}

# The camera setting a "ttl" start trigger depends on. TRIGGER_MODES[1] rather
# than a second literal, so the two cannot say different things about what
# "External edge" is spelled as.
CAM_EXT_TRIGGER = TRIGGER_MODES[1]

# A settle a routine may ask for. Not a safety limit — an obviously-wrong entry
# (3600 s between steps) is worth catching at validation.
MAX_SETTLE_S = 120.0


def pattern_label(path: str) -> str:
    """How a step's pattern reads in the table and the log.

    A saved ROI set (`devices/dmd/roi_store.py`) is a `<name>.roi.json`, not a
    device frame — naming it "ROI: <name>" rather than the raw filename reads
    as an ROI set at a glance, the way a plain image's own name does."""
    p = Path(path)
    if p.name.endswith(".roi.json"):
        return f"ROI: {p.name[:-len('.roi.json')]}"
    return p.name


@dataclass
class Step:
    """One leg of a routine: a place, a pattern, and how long to capture."""
    label:    str = ""
    x_um:     float | None = None     # None = leave this axis where it is
    y_um:     float | None = None
    fov:      str = ""                # name of the saved FOV x_um/y_um came
                                       # from, purely a display label — "" once
                                       # either axis is hand-edited, since the
                                       # numbers may no longer match that spot.
    pattern:  str = ""                # "" = leave the DMD's loaded pattern;
                                       # light follows it (engine.py) — a step
                                       # either shows something or it doesn't,
                                       # there is no "loaded but dark" state.
                                       # The illumination LED is independent of
                                       # both and simply tracks CAPTURE: the
                                       # camera recording is what wants light.
    length:   float = 100.0           # in `unit` — never converted
    unit:     str = "frames"
    settle_s: float = 0.25            # after the move/pattern, before capture
    # Seconds between air puffs during CAPTURE, on the session clock (never
    # frame-converted, unlike `length`/`unit`) — a puff is a timed physical
    # event regardless of what the step's own length is measured in. 0 = none.
    puff_interval_s: float = 0.0

    def describe(self) -> str:
        """One line for the panel and the log."""
        where = (f"FOV {self.fov}" if self.fov else
                ", ".join(f"{a}={v:.0f}um" for a, v in
                          (("x", self.x_um), ("y", self.y_um)) if v is not None))
        what = pattern_label(self.pattern) if self.pattern else ""
        bits = [b for b in (where, what,
                            f"puff/{self.puff_interval_s:g}s"
                            if self.puff_interval_s > 0 else "") if b]
        head = self.label or f"{self.length:g} {self.unit}"
        return f"{head}" + (f" ({'; '.join(bits)})" if bits else "")


@dataclass
class Group:
    """A contiguous run of steps that repeats as a unit, nested inside `cycles`.

    `start`/`end` are 0-based indices into `Routine.steps`, inclusive — a
    range, not a list of steps of its own, so reordering/inserting steps
    elsewhere in the table does not have to rewrite a membership list.
    """
    start:   int = 0
    end:     int = 0
    repeats: int = 2          # 1 would be a no-op; the UI starts useful


def play_order(routine: "Routine") -> list[int]:
    """One pass through `routine.steps`, each group's range repeated in
    place — indices into `routine.steps`. `cycles` repeats this whole list
    again, outside; groups nest inside one pass, not across cycles.

    Invalid or overlapping groups (validate() refuses those before a run)
    are skipped here rather than raising, so a stale/hand-edited routine
    still degrades to something playable instead of crashing the estimate.
    """
    n = len(routine.steps)
    order: list[int] = []
    groups = sorted((g for g in routine.groups if 0 <= g.start <= g.end < n),
                    key=lambda g: g.start)
    i = gi = 0
    while i < n:
        if gi < len(groups) and groups[gi].start == i:
            g = groups[gi]
            for _ in range(max(1, g.repeats)):
                order.extend(range(g.start, g.end + 1))
            i = g.end + 1
            gi += 1
        else:
            order.append(i)
            i += 1
    return order


@dataclass
class Routine:
    """The whole protocol. `cycles` repeats the step list end to end."""
    name:          str = "routine"
    steps:         list[Step] = field(default_factory=list)
    groups:        list[Group] = field(default_factory=list)
    cycles:        int = 1
    save_mode:     str = "single"
    start_trigger: str = "manual"

    def total_steps(self) -> int:
        return len(play_order(self)) * max(1, self.cycles)

    # ── persistence ───────────────────────────────────────────────────────────
    # Explicit rather than asdict(): this nests, and config.py's JSON is flat
    # enough that a silent shape change would come back as a stale routine.
    def to_dict(self) -> dict:
        return {"name": self.name, "cycles": self.cycles,
                "save_mode": self.save_mode,
                "start_trigger": self.start_trigger,
                "steps": [vars(s).copy() for s in self.steps],
                "groups": [vars(g).copy() for g in self.groups]}

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        """Rebuild from saved JSON, dropping anything that no longer fits.

        A stale or hand-edited file must not stop the app starting — the worst
        case is an empty routine, which `validate` then refuses to run.
        """
        if not isinstance(d, dict):
            return cls()
        steps = []
        for raw in d.get("steps") or ():
            if not isinstance(raw, dict):
                continue
            kw = {k: v for k, v in raw.items() if k in Step.__dataclass_fields__}
            try:
                steps.append(Step(**kw))
            except TypeError:
                continue
        groups = []
        for raw in d.get("groups") or ():
            if not isinstance(raw, dict):
                continue
            kw = {k: v for k, v in raw.items() if k in Group.__dataclass_fields__}
            try:
                groups.append(Group(**kw))
            except TypeError:
                continue
        try:
            cycles = max(1, int(d.get("cycles", 1)))
        except (TypeError, ValueError):
            cycles = 1
        mode = d.get("save_mode")
        trigger = d.get("start_trigger")
        return cls(name=str(d.get("name") or "routine"), steps=steps,
                   groups=groups, cycles=cycles,
                   save_mode=mode if mode in SAVE_MODES else "single",
                   start_trigger=trigger if trigger in START_TRIGGERS
                                 else "manual")


@dataclass(frozen=True)
class RigLimits:
    """What the loaded rig can actually do, as validation sees it.

    Built by the adapter from its neighbours, so a routine that projects is
    refused when the DMD is not loaded rather than half-running without light.
    """
    x_um:            tuple[float, float] | None = None    # stage soft limits
    y_um:            tuple[float, float] | None = None
    has_stage:       bool = False
    has_dmd:         bool = False
    has_puffer:      bool = False
    has_frames:      bool = False   # a camera is loaded, so frames() ticks
    cam_trigger_mode: str = ""      # the loaded camera's OWN trigger setting


def _limit_problem(axis: str, value: float,
                   limits: tuple[float, float] | None) -> str | None:
    if limits is None:
        return f"{axis} = {value:g} um but the stage has no soft limits"
    lo, hi = min(limits), max(limits)
    if not (lo <= value <= hi):
        return f"{axis} = {value:g} um is outside the soft limits [{lo:g}, {hi:g}]"
    return None


def validate(routine: Routine, rig: RigLimits) -> list[str]:
    """Everything wrong with running `routine` on `rig`, worst first-ish.

    An empty list means it may run. Every check here is one that would
    otherwise surface mid-run, which on this rig means mid-experiment.
    """
    out: list[str] = []
    if not routine.steps:
        out.append("the routine has no steps")
    if routine.cycles < 1:
        out.append(f"cycles = {routine.cycles}; must be at least 1")
    if routine.save_mode not in SAVE_MODES:
        out.append(f"unknown save mode {routine.save_mode!r}")
    if routine.start_trigger not in START_TRIGGERS:
        out.append(f"unknown start trigger {routine.start_trigger!r}")
    elif routine.start_trigger == "ttl":
        if not rig.has_frames:
            out.append("start trigger is TTL, but no camera is loaded to "
                       "receive it")
        elif rig.cam_trigger_mode != CAM_EXT_TRIGGER:
            out.append(f"start trigger is TTL, but the camera's trigger is "
                       f"set to {rig.cam_trigger_mode or 'unknown'!r} — set "
                       f"it to {CAM_EXT_TRIGGER!r} on the Voltage cam page")

    for i, s in enumerate(routine.steps, start=1):
        at = f"step {i}"
        if s.unit not in UNITS:
            out.append(f"{at}: unknown unit {s.unit!r}")
        elif s.unit == "frames" and not rig.has_frames:
            # Nothing would ever end the step; it would sit there forever.
            out.append(f"{at}: measured in frames, but no camera is loaded")
        if not (s.length > 0):
            out.append(f"{at}: length = {s.length:g}; must be above zero")
        if s.unit == "frames" and s.length != int(s.length):
            out.append(f"{at}: {s.length:g} frames is not a whole number")
        if s.settle_s < 0:
            out.append(f"{at}: settle = {s.settle_s:g} s; must not be negative")
        elif s.settle_s > MAX_SETTLE_S:
            out.append(f"{at}: settle = {s.settle_s:g} s exceeds {MAX_SETTLE_S:g} s")

        if s.x_um is not None or s.y_um is not None:
            if not rig.has_stage:
                out.append(f"{at}: moves the stage, which is not loaded")
            else:
                for axis, v, lim in (("x", s.x_um, rig.x_um),
                                     ("y", s.y_um, rig.y_um)):
                    if v is None:
                        continue
                    p = _limit_problem(axis, v, lim)
                    if p:
                        out.append(f"{at}: {p}")

        if s.pattern and not rig.has_dmd:
            out.append(f"{at}: uses the DMD, which is not loaded")
        if s.pattern and not Path(s.pattern).is_file():
            out.append(f"{at}: pattern {Path(s.pattern).name!r} is not a file")
        if s.puff_interval_s < 0:
            out.append(f"{at}: puff interval = {s.puff_interval_s:g} s; "
                       f"must not be negative")
        elif s.puff_interval_s > 0 and not rig.has_puffer:
            out.append(f"{at}: schedules air puffs, but the puffer is not loaded")

    n = len(routine.steps)
    spans: list[tuple[int, int]] = []
    for i, g in enumerate(routine.groups, start=1):
        at = f"repeat group {i}"
        if not (0 <= g.start <= g.end < n):
            out.append(f"{at}: steps {g.start + 1}-{g.end + 1} is outside "
                       f"the routine's {n} step(s)")
            continue
        if g.repeats < 1:
            out.append(f"{at}: repeats = {g.repeats}; must be at least 1")
        for lo, hi in spans:
            if g.start <= hi and lo <= g.end:
                out.append(f"{at}: steps {g.start + 1}-{g.end + 1} overlaps "
                           f"another repeat group")
                break
        spans.append((g.start, g.end))

    return out

