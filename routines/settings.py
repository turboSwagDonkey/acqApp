"""Experiment routines — the protocol. No Qt.

A routine is a list of atomic steps (`KINDS`), one `Step` class whose unused
fields stay at their defaults. Each `record` step is a one-step Recording.
One recording per external edge is `[trigger, record]` in a repeat group.

- A timed step's length is frames OR seconds, never converted: at 106 Hz a
  rounded conversion sheds frames at every boundary.
- `validate()` refuses a run up front, not at step 7 with an animal on the rig.
- Pre-redesign composite steps migrate on load (`_migrate_step`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

UNITS = ("frames", "seconds")
KINDS = ("move", "display", "wait", "record", "puff", "trigger")
TIMED_KINDS = ("wait", "record")      # run for `length` `unit`

# Files roll at each run boundary (per_repeat) or only when the Group changes
# (per_group). Provenance goes in each file's metadata, not its name.
SAVE_MODES: dict[str, str] = {
    "single":     "One file for the whole routine",
    "per_repeat": "One file per repeat (each recording run)",
    "per_group":  "One file per group",
}

# Not a safety limit; catches an obviously wrong entry.
MAX_SETTLE_S = 120.0


def pattern_label(path: str) -> str:
    """"ROI: <name>" for a saved ROI set, else the file name."""
    p = Path(path)
    if p.name.endswith(".roi.json"):
        return f"ROI: {p.name[:-len('.roi.json')]}"
    return p.name


@dataclass
class Step:
    kind:     str = "wait"
    label:    str = ""
    comment:  str = ""
    # move
    x_um:     float | None = None      # None leaves the axis alone
    y_um:     float | None = None
    z_um:     float | None = None
    fov:      str = ""                 # display label; "" once hand-edited
    settle_s: float = 0.25             # after arrival
    # display: "" means STOP displaying
    pattern:  str = ""
    # wait / record
    length:   float = 100.0
    unit:     str = "frames"

    def describe(self) -> str:
        if self.kind == "move":
            where = (f"FOV {self.fov}" if self.fov else
                     ", ".join(f"{a}={v:.0f}um" for a, v in
                               (("x", self.x_um), ("y", self.y_um),
                                ("z", self.z_um))
                               if v is not None) or "NA")
            body = f"move to {where}"
        elif self.kind == "display":
            body = (f"show {pattern_label(self.pattern)}" if self.pattern
                    else "stop displaying")
        elif self.kind in TIMED_KINDS:
            body = f"{self.kind} {self.length:g} {self.unit}"
        elif self.kind == "trigger":
            body = "wait for camera trigger"
        else:
            body = "puff"
        return f"{self.label} ({body})" if self.label else body


@dataclass
class Group:
    """Steps start..end (inclusive indices) repeated in place."""
    start:   int = 0
    end:     int = 0
    repeats: int = 2


@dataclass
class Recording:
    """Derived from a `record` step, never stored."""
    start: int = 0
    end:   int = 0


def play_order(routine: "Routine") -> list[int]:
    """One pass as step indices, each group repeated in place (`cycles`
    repeats the pass). Invalid groups are skipped, not raised on."""
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


def _region(spans, step_index: int) -> int | None:
    return next((i for i, r in enumerate(spans)
                if r.start <= step_index <= r.end), None)


def recording_region_at(routine: "Routine", step_index: int) -> int | None:
    """Index of the Recording covering `step_index`, or None."""
    return _region(routine.recordings, step_index)


def group_region_at(routine: "Routine", step_index: int) -> int | None:
    """Index of the Group covering `step_index`, or None."""
    return _region(routine.groups, step_index)


def group_repeat_at(routine: "Routine",
                    order: list[int]) -> list[tuple[int, int] | None]:
    """Parallel to `order`: (1-based repeat, total) inside a Group, else None."""
    n = len(routine.steps)
    valid = [g for g in routine.groups if 0 <= g.start <= g.end < n]
    group_of: dict[int, int] = {}
    for gi, g in enumerate(valid):
        for step_i in range(g.start, g.end + 1):
            group_of[step_i] = gi

    seen = [0] * len(valid)
    out: list[tuple[int, int] | None] = []
    for step_i in order:
        gi = group_of.get(step_i)
        if gi is None:
            out.append(None)
            continue
        g = valid[gi]
        span, reps = g.end - g.start + 1, max(1, g.repeats)
        out.append((seen[gi] // span + 1, reps))
        seen[gi] += 1
    return out


def recording_run_ids(routine: "Routine", order: list[int]) -> list[int | None]:
    """Parallel to `order`: a run serial inside a Recording, None outside.

    A serial changes whenever the order doubles back, so a Recording across
    a repeated Group yields one run per repeat. One pass only — the engine
    also keys on the cycle."""
    recs = routine.recordings
    ids: list[int | None] = []
    serial = -1
    prev_step: int | None = None
    prev_rec: int | None = None
    for step_i in order:
        rec = _region(recs, step_i)
        if rec is None:
            ids.append(None)
        else:
            if rec != prev_rec or prev_step is None or step_i != prev_step + 1:
                serial += 1
            ids.append(serial)
        prev_step, prev_rec = step_i, rec
    return ids


@dataclass
class Routine:
    """The whole protocol; `cycles` repeats the step list end to end. Always
    arms on Start (the camera's first edge starts step 1)."""
    name:          str = "routine"
    steps:         list[Step] = field(default_factory=list)
    groups:        list[Group] = field(default_factory=list)
    cycles:        int = 1
    save_mode:     str = "single"
    # Hold at a .dcimg roll (camera stopped ~0.9 s) and restart the step's
    # clock, or a seconds Wait counts down against a stopped camera.
    wait_for_camera: bool = True

    @property
    def recordings(self) -> list[Recording]:
        """One per `record` step; its index is the region id."""
        return [Recording(start=i, end=i)
                for i, s in enumerate(self.steps) if s.kind == "record"]

    def total_steps(self) -> int:
        return len(play_order(self)) * max(1, self.cycles)

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"name": self.name, "cycles": self.cycles,
                "save_mode": self.save_mode,
                "wait_for_camera": self.wait_for_camera,
                "steps": [vars(s).copy() for s in self.steps],
                "groups": [vars(g).copy() for g in self.groups]}

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        """Rebuild from saved JSON, dropping what no longer fits (worst case:
        an empty routine that `validate` refuses).

        A step without "kind" is pre-redesign and migrates to several steps;
        `old_to_new` maps each raw position to its new range so groups can be
        remapped."""
        if not isinstance(d, dict):
            return cls()
        raw_steps = [s for s in (d.get("steps") or ()) if isinstance(s, dict)]
        steps: list[Step] = []
        old_to_new: dict[int, tuple[int, int]] = {}
        for old_i, raw in enumerate(raw_steps):
            if "kind" in raw:
                kw = {k: v for k, v in raw.items()
                      if k in Step.__dataclass_fields__}
                try:
                    s = Step(**kw)
                except TypeError:
                    continue
                if s.kind not in KINDS:
                    continue
                old_to_new[old_i] = (len(steps), len(steps))
                steps.append(s)
            else:
                start = len(steps)
                steps.extend(_migrate_step(raw))
                end = len(steps) - 1
                if end < start:
                    continue
                old_to_new[old_i] = (start, end)

        groups = _remap_ranges(d.get("groups") or (), Group, old_to_new)
        if d.get("recordings"):
            print("[routines] this file has recording brackets, which no "
                  "longer exist — add Record steps where they were")

        try:
            cycles = max(1, int(d.get("cycles", 1)))
        except (TypeError, ValueError):
            cycles = 1
        mode = d.get("save_mode")
        if mode == "per_step":            # retired name
            mode = "per_repeat"
        return cls(name=str(d.get("name") or "routine"), steps=steps,
                   groups=groups, cycles=cycles,
                   save_mode=mode if mode in SAVE_MODES else "single",
                   wait_for_camera=bool(d.get("wait_for_camera", True)))


def _remap_ranges(raw_list, cls, old_to_new: dict[int, tuple[int, int]]) -> list:
    """Parse Group dicts, remapping start/end; drop any touching a lost step."""
    out = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        kw = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        try:
            obj = cls(**kw)
        except TypeError:
            continue
        lo, hi = old_to_new.get(obj.start), old_to_new.get(obj.end)
        if lo is None or hi is None:
            continue
        obj.start, obj.end = lo[0], hi[1]
        out.append(obj)
    return out


def _migrate_step(raw: dict) -> list[Step]:
    """A pre-redesign composite step -> atomic steps. It always captured, so
    its wait becomes `record`. A puff interval interleaves exactly in seconds;
    in frames (no rate to convert with) it becomes one trailing Puff."""
    out: list[Step] = []
    x, y = _opt_num(raw.get("x_um")), _opt_num(raw.get("y_um"))
    if x is not None or y is not None:
        out.append(Step(kind="move", x_um=x, y_um=y,
                        fov=str(raw.get("fov") or ""),
                        settle_s=_num(raw.get("settle_s"), 0.25)))
    pattern = str(raw.get("pattern") or "")
    if pattern:
        out.append(Step(kind="display", pattern=pattern))

    length = _num(raw.get("length"), 100.0)
    unit = raw.get("unit") if raw.get("unit") in UNITS else "frames"
    label = str(raw.get("label") or "")
    puff_iv = _num(raw.get("puff_interval_s"), 0.0)

    if puff_iv > 0 and unit == "seconds" and length > 0:
        remaining, first = length, True
        while remaining > 1e-9:
            chunk = min(puff_iv, remaining)
            out.append(Step(kind="record", label=label if first else "",
                            length=chunk, unit="seconds"))
            first, remaining = False, remaining - chunk
            if remaining > 1e-9:
                out.append(Step(kind="puff"))
    else:
        out.append(Step(kind="record", label=label, length=length, unit=unit))
        if puff_iv > 0:
            out.append(Step(kind="puff"))
    return out


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _opt_num(v) -> float | None:
    return None if v is None else _num(v, None)


@dataclass(frozen=True)
class RigLimits:
    """What the loaded rig can do, as validation sees it."""
    x_um:            tuple[float, float] | None = None    # stage soft limits
    y_um:            tuple[float, float] | None = None
    z_um:            tuple[float, float] | None = None
    has_stage:       bool = False
    has_z:           bool = False
    has_dmd:         bool = False
    has_puffer:      bool = False
    has_frames:      bool = False   # a camera is loaded


def _limit_problem(axis: str, value: float,
                   limits: tuple[float, float] | None) -> str | None:
    if limits is None:
        return f"{axis} = {value:g} um but stage has no soft limits"
    lo, hi = min(limits), max(limits)
    if not (lo <= value <= hi):
        return f"{axis} = {value:g} um is outside soft limits [{lo:g}, {hi:g}]"
    return None


def validate(routine: Routine, rig: RigLimits) -> list[str]:
    """Everything wrong with running `routine` on `rig`; empty = may run."""
    out: list[str] = []
    if not routine.steps:
        out.append("routine has no steps")
    if routine.cycles < 1:
        out.append(f"cycles = {routine.cycles}; must be at least 1")
    if routine.save_mode not in SAVE_MODES:
        out.append(f"unknown save mode {routine.save_mode!r}")
    if not rig.has_frames:
        out.append("no camera is loaded to arm the routine's start on")

    for i, s in enumerate(routine.steps, start=1):
        at = f"step {i}"
        if s.kind not in KINDS:
            out.append(f"{at}: unknown kind {s.kind!r}")
            continue
        if s.kind == "move":
            if s.x_um is not None or s.y_um is not None or s.z_um is not None:
                if not rig.has_stage:
                    out.append(f"{at}: moves the stage, which isn't loaded")
                else:
                    for axis, v, lim in (("x", s.x_um, rig.x_um),
                                         ("y", s.y_um, rig.y_um)):
                        if v is None:
                            continue
                        p = _limit_problem(axis, v, lim)
                        if p:
                            out.append(f"{at}: {p}")
                    if s.z_um is not None:
                        if not rig.has_z:
                            out.append(f"{at}: moves Z, but this rig has no "
                                      f"Z stage")
                        else:
                            p = _limit_problem("z", s.z_um, rig.z_um)
                            if p:
                                out.append(f"{at}: {p}")
            if s.settle_s < 0:
                out.append(f"{at}: settle = {s.settle_s:g} s; must not be "
                           f"negative")
            elif s.settle_s > MAX_SETTLE_S:
                out.append(f"{at}: settle = {s.settle_s:g} s exceeds "
                           f"{MAX_SETTLE_S:g} s")
        elif s.kind == "display":
            if s.pattern:
                if not rig.has_dmd:
                    out.append(f"{at}: uses the DMD, which isn't loaded")
                p = Path(s.pattern)
                if not p.is_file():
                    out.append(f"{at}: pattern {p.name!r} isn't a file")
        elif s.kind in TIMED_KINDS:
            if s.unit not in UNITS:
                out.append(f"{at}: unknown unit {s.unit!r}")
            elif s.unit == "frames" and not rig.has_frames:
                out.append(f"{at}: measured in frames, but no camera is loaded")
            if not (s.length > 0):
                out.append(f"{at}: length = {s.length:g}; must be above zero")
            if s.unit == "frames" and s.length != int(s.length):
                out.append(f"{at}: {s.length:g} frames isn't a whole number")
        elif s.kind == "puff":
            if not rig.has_puffer:
                out.append(f"{at}: uses the puffer, which isn't loaded")
        elif s.kind == "trigger":
            if not rig.has_frames:
                out.append(f"{at}: waits for the camera's trigger, but no "
                           f"camera is loaded")

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
