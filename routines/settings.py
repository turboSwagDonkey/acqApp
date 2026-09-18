"""Experiment routines — the protocol, and no Qt.

A routine is a list of atomic steps executed in order: move, start
displaying, wait, puff, wait for an external trigger. A **Recording** is a
separate, draggable bracket over a contiguous range of steps — "the camera
is capturing for these" — independent of what those steps individually do.
`Step`, `Recording` and `Routine` are what persists; `validate()` is what
refuses a run *before* it starts.

**One recording per external edge** is a `trigger` step inside a repeat
group, with the Recording bracket on the steps AFTER it: each repeat
re-arms the camera, waits for its edge, and only then opens the file. With
`save_mode="per_repeat"` that is a folder of one file per edge. The bracket
must not cover the trigger step itself — `validate()` refuses that, since
the file would be open while waiting, and re-arming restarts the camera's
acquisition underneath it.

Five kinds, one step class (`KINDS`): a step is one thing regardless of
which fields it uses, the same way `x_um=None` already meant "leave this
axis alone" before this redesign — a field a step's kind doesn't use just
stays at its default, the table renders it as "—", and nothing here reads
it. This keeps `Routine.to_dict()`'s `vars(s).copy()` round-trip and the
table's one-row-per-step model from needing kind-per-subclass dispatch at
the serialization boundary.

Two things here were the operator's calls (PLAN §6) and are load-bearing:

- **A Wait step's length is frames OR seconds, its author's choice**, never
  interconverted — at 106 Hz a rounded conversion sheds frames at every step
  boundary, so `unit` travels with `length` into the engine.
- **Validation is up front.** A stage target outside the soft limits is a
  refusal at the Start button, not a fault at step 7 of 12 with an animal on
  the rig.

**Old (pre-redesign) saved routines/templates auto-migrate** on load —
`Routine.from_dict` expands each old composite step (move+pattern+capture+
settle+puff bundled) into the atomic steps it implied, each wrapped in its
own `Recording` (an old step always captured — there was no "off" state).
See `_migrate_step`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

UNITS = ("frames", "seconds")
KINDS = ("move", "display", "wait", "puff", "trigger")

# key -> label. `single` keeps the one-file-per-session invariant; the other
# two trade it for a folder of files, rolled by `adapters/routines.py` at
# every RecordingRun boundary (`per_repeat`) or only when the covering Group
# actually changes (`per_group`, coarser — repeats of the same Group share a
# file). Provenance (which group/repeat/cycle) lives in each file's own
# metadata (RecordingRun.attrs()), not the filename.
SAVE_MODES: dict[str, str] = {
    "single":     "One file for the whole routine",
    "per_repeat": "One file per repeat (each recording run)",
    "per_group":  "One file per group",
}

# key -> label. `manual` is the button; `ttl` arms the routine on Start and
# holds it there until the voltage camera reports a frame it did not have at
# arm time — `adapters/routines.py` puts the camera in External edge mode
# itself before arming (ModuleHost.set_camera_trigger), so nothing here
# reads a DAQ line: the camera already IS the TTL input.
START_TRIGGERS: dict[str, str] = {
    "manual": "Manual — Click Start",
    "ttl":    "TTL — Wait for EXT Cam Frame",
}

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
    """One atomic action: move, start displaying, wait, or puff.

    Only the fields its `kind` uses are meaningful; the rest sit at their
    default the way `x_um=None` already meant "leave this axis alone"
    before this redesign — the table renders an unused field as "—" and
    nothing here reads it.
    """
    kind:     str = "wait"
    label:    str = ""
    comment:  str = ""                # free-text note; no effect on the run
    # move only
    x_um:     float | None = None
    y_um:     float | None = None
    z_um:     float | None = None     # focus; None on a rig with no Z stage
    fov:      str = ""                # name of the saved FOV x_um/y_um(/z_um)
                                       # came from, purely a display label —
                                       # "" once any axis is hand-edited, since
                                       # the numbers may no longer match that
                                       # spot.
    settle_s: float = 0.25            # after arrival, before the step ends
    # display only — "" means STOP displaying (light off), not "leave alone":
    # unlike the old composite Step, a Display step is a stated action, so
    # there is no "did not say" state left for it to mean.
    pattern:  str = ""
    # wait only — never converted between the two, see the module docstring.
    length:   float = 100.0
    unit:     str = "frames"

    def describe(self) -> str:
        """One line for the panel and the log: the label, then what it does."""
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
        elif self.kind == "wait":
            body = f"wait {self.length:g} {self.unit}"
        elif self.kind == "trigger":
            body = "wait for camera trigger"
        else:
            body = "puff"
        return f"{self.label} ({body})" if self.label else body


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


@dataclass
class Recording:
    """A contiguous run of steps the camera is capturing for — a draggable
    bracket over `Routine.steps`, independent of what those steps do.

    Same shape as `Group` and for the same reason: `start`/`end` are 0-based,
    inclusive indices rather than a membership list, so reordering steps
    elsewhere does not have to rewrite one. Unlike a `Group` there is no
    repeat count — a Recording that happens to sit inside a repeated `Group`
    is simply re-entered on every repeat (`recording_run_ids` below is what
    tells those repeats apart as separate recording runs).
    """
    start: int = 0
    end:   int = 0


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


def recording_region_at(routine: "Routine", step_index: int) -> int | None:
    """Which `routine.recordings` bracket (its index) covers `step_index`,
    or None if no Recording does. Shared by `recording_run_ids` below and by
    the engine, which needs to name the region a freshly-opened run belongs
    to — one implementation of "which bracket is this," not two that could
    drift apart."""
    return next((i for i, r in enumerate(routine.recordings)
                if r.start <= step_index <= r.end), None)


def group_region_at(routine: "Routine", step_index: int) -> int | None:
    """Which `routine.groups` range (its index) covers `step_index`, or None
    if no Group does. Same shape as `recording_region_at` — shared by
    `adapters/routines.py`'s per-group save mode (repeats of the SAME Group
    share one output file) and by `routines/timeline.py`, which used to do
    this lookup inline."""
    return next((i for i, g in enumerate(routine.groups)
                if g.start <= step_index <= g.end), None)


def group_repeat_at(routine: "Routine",
                    order: list[int]) -> list[tuple[int, int] | None]:
    """Parallel to `order` (a `play_order(routine)` result): at each position,
    `(1-based repeat number, total repeats)` if it falls inside a repeat
    Group, else None.

    For the operator's own progress reading (`adapters/routines.py`'s status
    text): a Group's steps repeat in place, so "step 1/2" alone reads
    identically on repeat 1, repeat 2, … repeat 100 of a `[trigger, wait]`
    pair — nothing else in the display says which one is running.

    Walks `order` itself, like `recording_run_ids` — a running count of steps
    seen so far for the step's own Group, divided by the Group's span, is the
    0-based repeat index. Needs no assumption about how `order` was built
    beyond "one Group's range appears as contiguous repeats", which is
    exactly what `play_order` guarantees.
    """
    n = len(routine.steps)
    valid = [g for g in routine.groups if 0 <= g.start <= g.end < n]
    group_of: dict[int, int] = {}     # step_index -> index into `valid`
    for gi, g in enumerate(valid):
        for step_i in range(g.start, g.end + 1):
            group_of[step_i] = gi

    seen = [0] * len(valid)           # steps of each group visited so far
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
    """Parallel to `order` (a `play_order(routine)` result): a run-serial
    number at each position that is inside some Recording, None elsewhere.

    Equal consecutive serials mean "still the same open recording run"; any
    change — None<->serial, OR a new serial even for the SAME Recording —
    is a boundary the engine must close/open on. A serial changes on any
    non-monotonic step (the order doubling back — a repeat group's range
    looping to its start) even when the surrounding Recording is identical,
    so a Recording drawn across an entire repeated Group's range still
    yields one run per repeat rather than one run merging all of them.

    Says nothing about `cycles` — this only covers ONE pass of `order` — the
    engine additionally keys on the current cycle number, since `cycles`
    repeats the whole pass outside this function's view.
    """
    ids: list[int | None] = []
    serial = -1
    prev_step: int | None = None
    prev_rec: int | None = None
    for step_i in order:
        rec = recording_region_at(routine, step_i)
        if rec is None:
            ids.append(None)
        else:
            new_run = (rec != prev_rec or prev_step is None
                       or step_i != prev_step + 1)
            if new_run:
                serial += 1
            ids.append(serial)
        prev_step, prev_rec = step_i, rec
    return ids


@dataclass
class Routine:
    """The whole protocol. `cycles` repeats the step list end to end."""
    name:          str = "routine"
    steps:         list[Step] = field(default_factory=list)
    groups:        list[Group] = field(default_factory=list)
    recordings:    list[Recording] = field(default_factory=list)
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
                "groups": [vars(g).copy() for g in self.groups],
                "recordings": [vars(r).copy() for r in self.recordings]}

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        """Rebuild from saved JSON, dropping anything that no longer fits.

        A stale or hand-edited file must not stop the app starting — the worst
        case is an empty routine, which `validate` then refuses to run.

        Each raw step is migrated independently (a `"kind"` key marks it as
        already-new-format; its absence marks a pre-redesign composite step —
        see `_migrate_step`). `old_to_new` maps every raw step's position to
        the range of new steps it became — identity (i, i) for one that
        needed no migration — so `groups`/`recordings`, whose indices refer to
        raw-step positions, can be remapped the same way regardless of
        whether the file was old or new.
        """
        if not isinstance(d, dict):
            return cls()
        raw_steps = [s for s in (d.get("steps") or ()) if isinstance(s, dict)]
        steps: list[Step] = []
        recordings: list[Recording] = []
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
                recordings.append(Recording(start=start, end=end))

        groups = _remap_ranges(d.get("groups") or (), Group, old_to_new)
        # Explicit "recordings" only exist in new-format files (old ones never
        # had the key) — same remap as groups, for the same reason: an index
        # here refers to a raw-step position, not a post-migration one.
        recordings.extend(
            _remap_ranges(d.get("recordings") or (), Recording, old_to_new))

        try:
            cycles = max(1, int(d.get("cycles", 1)))
        except (TypeError, ValueError):
            cycles = 1
        mode = d.get("save_mode")
        if mode == "per_step":            # retired name; closest equivalent
            mode = "per_repeat"
        trigger = d.get("start_trigger")
        return cls(name=str(d.get("name") or "routine"), steps=steps,
                   groups=groups, recordings=recordings, cycles=cycles,
                   save_mode=mode if mode in SAVE_MODES else "single",
                   start_trigger=trigger if trigger in START_TRIGGERS
                                 else "manual")


def _remap_ranges(raw_list, cls, old_to_new: dict[int, tuple[int, int]]) -> list:
    """Parse a list of `Group`/`Recording` dicts, remapping `start`/`end`
    (raw-step positions) through `old_to_new`. A range touching a raw step
    that was dropped or never existed is dropped too — the same "keep only
    what still makes sense" rule `from_dict` follows everywhere else."""
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
    """One pre-redesign composite step -> the atomic steps it implied.

    An old step always captured (there was no "off" state), which is why
    `Routine.from_dict` wraps whatever this returns in one `Recording`. A
    puff interval interleaves exactly against a seconds-unit length; a
    frames-unit length has no frame rate available here to convert a
    real-time interval against a frame-gated duration, so it falls back to
    one trailing Puff step instead — lossy, and only reachable by loading an
    old saved file (operator-accepted trade-off, nothing built after this
    redesign can lose anything this way).
    """
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
            out.append(Step(kind="wait", label=label if first else "",
                            length=chunk, unit="seconds"))
            first, remaining = False, remaining - chunk
            if remaining > 1e-9:
                out.append(Step(kind="puff"))
    else:
        out.append(Step(kind="wait", label=label, length=length, unit=unit))
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
    """What the loaded rig can actually do, as validation sees it.

    Built by the adapter from its neighbours, so a routine that projects is
    refused when the DMD is not loaded rather than half-running without light.
    """
    x_um:            tuple[float, float] | None = None    # stage soft limits
    y_um:            tuple[float, float] | None = None
    z_um:            tuple[float, float] | None = None    # None: no Z stage
    has_stage:       bool = False
    has_z:           bool = False   # the loaded stage has a Z (focus) axis
    has_dmd:         bool = False
    has_puffer:      bool = False
    has_frames:      bool = False   # a camera is loaded, so frames() ticks


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
    elif routine.start_trigger == "ttl" and not rig.has_frames:
        out.append("start trigger is TTL, but no camera is loaded to "
                   "receive it")

    for i, s in enumerate(routine.steps, start=1):
        at = f"step {i}"
        if s.kind not in KINDS:
            out.append(f"{at}: unknown kind {s.kind!r}")
            continue
        if s.kind == "move":
            if s.x_um is not None or s.y_um is not None or s.z_um is not None:
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
                    out.append(f"{at}: uses the DMD, which is not loaded")
                p = Path(s.pattern)
                if not p.is_file():
                    out.append(f"{at}: pattern {p.name!r} is not a file")
        elif s.kind == "wait":
            if s.unit not in UNITS:
                out.append(f"{at}: unknown unit {s.unit!r}")
            elif s.unit == "frames" and not rig.has_frames:
                # Nothing would ever end the step; it would sit there forever.
                out.append(f"{at}: measured in frames, but no camera is loaded")
            if not (s.length > 0):
                out.append(f"{at}: length = {s.length:g}; must be above zero")
            if s.unit == "frames" and s.length != int(s.length):
                out.append(f"{at}: {s.length:g} frames is not a whole number")
        elif s.kind == "puff":
            if not rig.has_puffer:
                out.append(f"{at}: uses the puffer, which is not loaded")
        elif s.kind == "trigger":
            if not rig.has_frames:
                # The edge is only ever observable as frames appearing, so
                # with no camera there is nothing that could end this step.
                out.append(f"{at}: waits for the camera's trigger, but no "
                           f"camera is loaded")
            if recording_region_at(routine, i - 1) is not None:
                # Two reasons, both hard. The point of the step is that the
                # recording starts ON the edge — inside a bracket the file is
                # already open while it waits, which is backwards. And arming
                # restarts the camera's acquisition, which must not happen
                # underneath an open file. Put the bracket after this step.
                out.append(f"{at}: waits for a trigger inside a recording "
                           f"bracket; move the bracket to start after this "
                           f"step, so the recording begins on the edge")

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

    spans = []
    for i, r in enumerate(routine.recordings, start=1):
        at = f"recording {i}"
        if not (0 <= r.start <= r.end < n):
            out.append(f"{at}: steps {r.start + 1}-{r.end + 1} is outside "
                       f"the routine's {n} step(s)")
            continue
        for lo, hi in spans:
            if r.start <= hi and lo <= r.end:
                out.append(f"{at}: steps {r.start + 1}-{r.end + 1} overlaps "
                           f"another recording")
                break
        spans.append((r.start, r.end))

    return out
