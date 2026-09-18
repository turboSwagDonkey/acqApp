"""Experiment routines: the protocol, and the engine that executes it.

This is the first feature in the app whose whole purpose is to **actuate** — it
drives the stage and puts light on the sample without an operator watching each
step. So everything that decides is Qt-free and callable-driven, and this drives
all of it against a fake rig on a fake clock: no window, no device, ~1 s.

A routine is a list of ATOMIC steps (move / display / wait / puff), not the
pre-redesign composite step that bundled all of them — see routines/settings.py
and routines/engine.py's module docstrings for the shape. A **Recording** is a
separate, draggable bracket over a contiguous range of steps ("the camera is
capturing for these"), independent of what those steps individually do.

What it defends, in the order the decisions were made (PLAN §6):

  * **A step's length is frames OR seconds and the two are never converted.**
    At 106 Hz a rounded conversion sheds frames at every step boundary. The
    control is a camera running off its nominal rate: a converting engine ends
    the seconds step at a visibly different frame count.
  * **Validation is up front.** A stage target outside the soft limits is a
    refusal at the Start button, not a fault at step 7 of 12.
  * **A fault pauses, it does not abort** — motion stopped, light off, capture
    untouched — and the interrupted recording's data is **kept and marked**,
    with resume repeating the paused step as a fresh attempt, in a FRESH
    recording run.
  * **A Recording drawn across a repeated Group, or across a cycle boundary,
    yields one run per repeat/cycle** — never one run merging them all. This
    is the trickiest correctness point in the whole redesign.
  * **Old (pre-redesign) saved routines auto-migrate** into atomic steps, each
    wrapped in its own Recording, with Group ranges remapped onto the new,
    expanded step indices.
  * **The light is never on while the stage travels**, and a Move restores
    whatever a Display step last left lit, once arrived and settled.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_routines.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from _harness import Report, isolate_user_state, make_window, pump, qt_app

from acqApp.routines.engine import Phase, RoutineEngine, RoutineError, RoutineHooks
from acqApp.routines.settings import (KINDS, UNITS, Group, Recording,
                                      RigLimits, Routine, Step, play_order,
                                      recording_run_ids, validate)

FULL_RIG = RigLimits(x_um=(-5000.0, 5000.0), y_um=(-5000.0, 5000.0),
                     has_stage=True, has_dmd=True, has_frames=True,
                     has_puffer=True)
# The same rig, but with a Z (focus) axis too — most rigs don't have one,
# which is why FULL_RIG above stays Z-less as the common case.
Z_RIG = RigLimits(x_um=FULL_RIG.x_um, y_um=FULL_RIG.y_um,
                  z_um=(-500.0, 500.0), has_stage=True, has_z=True,
                  has_dmd=True, has_frames=True, has_puffer=True)
# Same rig, but with a camera loaded — the one condition a TTL start trigger
# needs at validation time (arming the camera itself is `adapters/
# routines.py`'s job now, not something validate() checks for statically).
TTL_RIG = RigLimits(x_um=FULL_RIG.x_um, y_um=FULL_RIG.y_um, has_stage=True,
                    has_dmd=True, has_frames=True, has_puffer=True)
DT = 0.01                      # the worker's tick, near enough


# ── the fake rig ──────────────────────────────────────────────────────────────

class FakeRig:
    """A stage, a projector, a puffer and a camera, on a clock the test drives.

    Records every actuating call in `log`, which is how the ordering checks
    (light off before a move, light on only while displaying) are made at all.
    """

    def __init__(self, hz: float = 106.0, travel_s: float = 0.0) -> None:
        self.t = 0.0
        self.hz = hz
        self.travel_s = travel_s
        self.log: list[tuple] = []
        self.lit = False
        self.led_on = False
        self.frames_running = True
        self._arrive_at: float | None = None
        self.fail_move = False
        self.fail_light = False
        self.fail_begin = False        # begin_recording (the FILE) raises
        self.fail_arm = False          # arm_trigger raises
        self.puffed = 0
        self.begun: list = []
        self.ended: list = []
        # ── external trigger ──
        # `gated` is the camera sitting in External edge mode with no edge yet:
        # the clock keeps running but no frames are produced. That is what a
        # `trigger` step waits out, and `_gated_total` is the frame-time lost
        # to it, so `frames()` still matches `t` exactly whenever nothing ever
        # gated — which is every pre-existing test in this file.
        self.gated = False
        self.rearms = 0
        self._gated_total = 0.0

    # clock + camera
    def now(self) -> float:
        return self.t

    def frames(self) -> int | None:
        if not self.frames_running:
            return None
        return int((self.t - self._gated_total) * self.hz)

    def advance(self, dt: float = DT) -> None:
        self.t += dt
        if self.gated:
            self._gated_total += dt

    # external trigger
    def arm_trigger(self) -> None:
        """What the camera adapter does: re-gate, so the next edge shows up as
        frames starting again."""
        self.rearms += 1
        self.log.append(("arm_trigger",))
        if self.fail_arm:
            raise RuntimeError("camera could not be re-armed")
        self.gated = True

    def fire_trigger(self) -> None:
        """The external edge arrives: frames start flowing again."""
        self.gated = False

    # stage
    def move(self, x, y, z=None) -> None:
        self.log.append(("move", x, y, z))
        if self.fail_move:
            raise RuntimeError("serial link down")
        self._arrive_at = self.t + self.travel_s

    def moving(self) -> bool:
        return self._arrive_at is not None and self.t < self._arrive_at

    def stop_motion(self) -> None:
        self.log.append(("stop_motion",))
        self._arrive_at = None

    # projector
    def set_pattern(self, path: str) -> None:
        self.log.append(("pattern", path))

    def light(self, on: bool) -> None:
        if self.fail_light and on:
            raise RuntimeError("ALP not ready")
        self.log.append(("light", bool(on)))
        self.lit = bool(on)

    def led(self, on: bool) -> None:
        self.log.append(("led", bool(on)))
        self.led_on = bool(on)

    # puffer
    def puff(self) -> None:
        self.log.append(("puff",))
        self.puffed += 1

    # file boundaries
    def begin_recording(self, run) -> None:
        if self.fail_begin:
            raise RuntimeError("file could not open")
        self.log.append(("begin", run.region, run.cycle, run.attempt))
        self.begun.append(run)

    def end_recording(self, run) -> None:
        self.log.append(("end", run.region, run.cycle, run.attempt))
        self.ended.append(run)

    def hooks(self) -> RoutineHooks:
        return RoutineHooks(now=self.now, frames=self.frames, move=self.move,
                            moving=self.moving, stop_motion=self.stop_motion,
                            set_pattern=self.set_pattern, light=self.light,
                            led=self.led, puff=self.puff,
                            arm_trigger=self.arm_trigger,
                            begin_recording=self.begin_recording,
                            end_recording=self.end_recording,
                            log=lambda _m: None)


def drive(eng: RoutineEngine, rig: FakeRig, *, limit_s: float = 60.0,
          until=None) -> None:
    """Tick the engine on the fake clock until it stops, or `until(eng)`."""
    while rig.t < limit_s:
        if until is not None and until(eng):
            return
        if eng.phase in (Phase.DONE, Phase.IDLE, Phase.PAUSED):
            return
        rig.advance()
        eng.tick()


# ── validation ────────────────────────────────────────────────────────────────

def check_validation(r: Report, tmp: Path) -> None:
    """Everything that must be refused before the Start button does anything."""
    pattern = tmp / "spot.png"
    pattern.write_bytes(b"not really a png, but it is a file")

    good = Routine(steps=[Step(kind="move", x_um=100.0, y_um=-200.0),
                         Step(kind="display", pattern=str(pattern)),
                         Step(kind="wait", length=100, unit="frames"),
                         Step(kind="puff")])
    r.check(validate(good, FULL_RIG) == [],
            "control: a valid routine (one of each kind) on a full rig is "
            "accepted")

    good_ttl = Routine(steps=[Step(kind="wait", length=1, unit="seconds")],
                       start_trigger="ttl")
    r.check(validate(good_ttl, TTL_RIG) == [],
            "control: a TTL start trigger is accepted once a camera is "
            "loaded to receive it")

    cases = [
        ("a move step outside the soft limits",
         Routine(steps=[Step(kind="move", x_um=9_000.0)]), FULL_RIG,
         "soft limits"),
        ("a move step targeting Z on a rig with no Z stage",
         Routine(steps=[Step(kind="move", z_um=10.0)]), FULL_RIG,
         "no Z stage"),
        ("a Z target outside Z's own soft limits",
         Routine(steps=[Step(kind="move", z_um=9_000.0)]), Z_RIG,
         "soft limits"),
        ("a frames wait with no camera loaded",
         Routine(steps=[Step(kind="wait", length=100, unit="frames")]),
         RigLimits(), "no camera"),
        ("a display step with a pattern but no DMD loaded",
         Routine(steps=[Step(kind="display", pattern=str(pattern))]),
         RigLimits(), "DMD"),
        ("a move step with a target but no stage loaded",
         Routine(steps=[Step(kind="move", x_um=10.0)]), RigLimits(), "stage"),
        ("a puff step with no puffer loaded",
         Routine(steps=[Step(kind="puff")]), RigLimits(), "puffer"),
        ("a zero-length wait",
         Routine(steps=[Step(kind="wait", length=0, unit="seconds")]),
         FULL_RIG, "above zero"),
        ("a fractional frame count",
         Routine(steps=[Step(kind="wait", length=10.5, unit="frames")]),
         FULL_RIG, "whole"),
        ("a negative settle on a move step",
         Routine(steps=[Step(kind="move", settle_s=-1.0)]), FULL_RIG,
         "negative"),
        ("a pattern file that is not there",
         Routine(steps=[Step(kind="display", pattern=str(tmp / "gone.png"))]),
         FULL_RIG, "not a file"),
        ("an empty routine", Routine(steps=[]), FULL_RIG, "no steps"),
        ("an unrecognised kind",
         Routine(steps=[Step(kind="teleport")]), FULL_RIG, "unknown kind"),
        ("a TTL start trigger with no camera loaded",
         Routine(steps=[Step(kind="wait", length=1, unit="seconds")],
                start_trigger="ttl"), RigLimits(), "no camera"),
        # A trigger step is only observable as frames appearing, so with no
        # camera nothing could ever end it.
        ("a trigger step with no camera loaded",
         Routine(steps=[Step(kind="trigger")]), RigLimits(), "no camera"),
        # The recording must start ON the edge, and re-arming restarts the
        # camera's acquisition — neither is safe underneath an open file.
        ("a trigger step inside a recording bracket",
         Routine(steps=[Step(kind="trigger"),
                       Step(kind="wait", length=1, unit="seconds")],
                recordings=[Recording(start=0, end=1)]),
         FULL_RIG, "inside a recording bracket"),
    ]
    for label, routine, rig, needle in cases:
        problems = validate(routine, rig)
        r.check(any(needle in p for p in problems),
                f"refused: {label} ({problems[:1] or 'NOTHING SAID'})")

    # The limits are per axis, and y must not be checked against x's.
    tall = RigLimits(x_um=(-100.0, 100.0), y_um=(-9000.0, 9000.0),
                     has_stage=True, has_frames=True)
    r.check(validate(Routine(steps=[Step(kind="move", y_um=5000.0)]),
                    tall) == [],
            "a target legal on ITS axis is not refused by the other axis")
    r.check(validate(Routine(steps=[Step(kind="move", x_um=5000.0)]),
                    tall) != [],
            "…and the same number on the narrow axis still is")

    # A Z target inside Z's own limits, on a rig that has one, is accepted —
    # the control for the two Z refusal cases above.
    r.check(validate(Routine(steps=[Step(kind="move", x_um=10.0, y_um=10.0,
                                        z_um=50.0)]), Z_RIG) == [],
            "control: an (x, y, z) move within every axis's own limits, on "
            "a rig with a Z stage, is accepted")

    # Persistence round trip: the panel saves this into acqapp_local.json.
    src = Routine(name="grid", cycles=3, save_mode="per_repeat",
                  start_trigger="ttl",
                  steps=[Step(kind="move", label="a", x_um=1.0, settle_s=0.1),
                         Step(kind="wait", label="b", length=5, unit="frames"),
                         Step(kind="display", label="c", pattern="p.png"),
                         Step(kind="puff", label="d")],
                  groups=[Group(start=1, end=2, repeats=2)],
                  recordings=[Recording(start=0, end=3)])
    back = Routine.from_dict(src.to_dict())
    r.check(back == src, "a routine survives the JSON round trip unchanged")
    r.check(Routine.from_dict({"start_trigger": "nonsense"}).start_trigger
            == "manual",
            "an unrecognised saved start trigger falls back to manual")
    r.check(Routine.from_dict({"save_mode": "per_step"}).save_mode
            == "per_repeat",
            "a saved routine from before the per-group/per-repeat split "
            "migrates its old 'per_step' mode to the closest equivalent, "
            "rather than silently falling back to single")
    r.check(Routine.from_dict(
        {"steps": [{"kind": "wait", "gone": 1, "label": "x"}],
         "cycles": "nonsense"}).steps[0].label == "x",
            "a stale saved (new-format) step drops unknown keys rather than "
            "raising")


def check_groups(r: Report) -> None:
    """A repeat group re-runs a contiguous range of steps, nested inside
    `cycles` — steps [A, B, C] with B..C x3 plays A, B, C, B, C, B, C. A
    Recording bracket is the same start/end-range shape, validated the same
    way, and independent of Group."""
    routine = Routine(steps=[Step(kind="wait", label="A"),
                             Step(kind="wait", label="B"),
                             Step(kind="wait", label="C")],
                      groups=[Group(start=1, end=2, repeats=3)])
    order = play_order(routine)
    r.check(order == [0, 1, 2, 1, 2, 1, 2],
            f"A once, B-C three times, in place ({order})")
    r.check(routine.total_steps() == 7,
            f"total_steps() counts the expanded order ({routine.total_steps()})")

    # ── validate(): groups ──
    r.check(validate(routine, FULL_RIG) == [],
            "control: a valid group on a full rig is accepted")
    bad_range = Routine(steps=[Step(kind="wait")],
                        groups=[Group(start=0, end=5, repeats=2)])
    r.check(any("outside" in p for p in validate(bad_range, FULL_RIG)),
            "a group referencing steps past the end of the routine is refused")
    overlap = Routine(steps=[Step(kind="wait"), Step(kind="wait"), Step(kind="wait")],
                      groups=[Group(start=0, end=1, repeats=2),
                              Group(start=1, end=2, repeats=2)])
    r.check(any("overlaps" in p for p in validate(overlap, FULL_RIG)),
            "two groups sharing a step are refused")
    zero_repeat = Routine(steps=[Step(kind="wait"), Step(kind="wait")],
                          groups=[Group(start=0, end=1, repeats=0)])
    r.check(any("repeats" in p for p in validate(zero_repeat, FULL_RIG)),
            "a group that repeats zero times is refused, not silently a no-op")

    # ── validate(): recordings (same range shape, independent of groups) ──
    bad_rec = Routine(steps=[Step(kind="wait")],
                      recordings=[Recording(start=0, end=5)])
    r.check(any("outside" in p for p in validate(bad_rec, FULL_RIG)),
            "a recording bracket past the end of the routine is refused")
    overlap_rec = Routine(steps=[Step(kind="wait"), Step(kind="wait"),
                                Step(kind="wait")],
                         recordings=[Recording(start=0, end=1),
                                     Recording(start=1, end=2)])
    r.check(any("overlaps another recording" in p
               for p in validate(overlap_rec, FULL_RIG)),
            "two recordings sharing a step are refused")
    ok_rec = Routine(steps=[Step(kind="wait"), Step(kind="wait")],
                     recordings=[Recording(start=0, end=0),
                                 Recording(start=1, end=1)])
    r.check(validate(ok_rec, FULL_RIG) == [],
            "control: two ADJACENT (non-overlapping) recordings are accepted")

    # ── the engine actually plays that order ──
    rig = FakeRig()
    routine2 = Routine(steps=[Step(kind="wait", label="A", length=0.05,
                                   unit="seconds"),
                              Step(kind="wait", label="B", length=0.05,
                                   unit="seconds"),
                              Step(kind="wait", label="C", length=0.05,
                                   unit="seconds")],
                       groups=[Group(start=1, end=2, repeats=3)])
    eng = RoutineEngine(routine2, rig.hooks())
    eng.start()
    drive(eng, rig)
    r.check(eng.phase == Phase.DONE, "the group's expansion runs to the end")
    r.check(eng.steps_done() == 7,
            f"the engine executes the group's expanded order, 7 atomic steps "
            f"({eng.steps_done()})")
    r.check(eng.total_runs() == 7,
            f"total_runs() matches what actually ran ({eng.total_runs()})")

    # ── progress accounts for the repeats, not just the table row ──
    rig2 = FakeRig()
    eng2 = RoutineEngine(routine2, rig2.hooks())
    eng2.start()
    # Drive to partway through the SECOND execution of step B (order pos 3).
    drive(eng2, rig2, until=lambda e: e.order_position == 3
                                      and e.phase == Phase.RUNNING)
    r.check(eng2.order_position == 3,
            f"order_position tracks the expanded order, not the table row "
            f"(step index {eng2.position[0]}, order pos {eng2.order_position})")
    r.check(0.0 < eng2.overall_progress() < 1.0,
            "overall_progress reflects the repeat, not just 3 table rows")


def check_group_repeat_at(r: Report) -> None:
    """`group_repeat_at` — what fixed the routine panel showing "step 1/2"
    identically on every one of a `trigger` step's repeats, with nothing
    anywhere saying which repeat was running (rig, 2026-09-17)."""
    from acqApp.routines.settings import group_repeat_at

    routine = Routine(steps=[Step(kind="trigger"),
                             Step(kind="wait", length=8.0, unit="seconds")],
                      groups=[Group(start=0, end=1, repeats=3)],
                      recordings=[Recording(start=1, end=1)])
    order = play_order(routine)
    rep = group_repeat_at(routine, order)
    r.check(order == [0, 1, 0, 1, 0, 1],
            f"control: the trigger+wait pair plays 3 times ({order})")
    r.check(rep == [(1, 3), (1, 3), (2, 3), (2, 3), (3, 3), (3, 3)],
            f"…and each pass is numbered 1/3, 2/3, 3/3 — same repeat number "
            f"for both steps of one pass ({rep})")

    # No group at all: every position is None, not a spurious (1, 1).
    plain = Routine(steps=[Step(kind="wait"), Step(kind="wait")])
    r.check(group_repeat_at(plain, play_order(plain)) == [None, None],
            "a routine with no repeat group reports no repeat anywhere")

    # A step OUTSIDE the group (the operator's Move-then-repeat shape) is
    # None; only the grouped range is numbered.
    with_move = Routine(
        steps=[Step(kind="move", label="FOV"), Step(kind="trigger"),
              Step(kind="wait", length=1.0, unit="seconds")],
        groups=[Group(start=1, end=2, repeats=2)])
    rep2 = group_repeat_at(with_move, play_order(with_move))
    r.check(rep2 == [None, (1, 2), (1, 2), (2, 2), (2, 2)],
            f"the ungrouped Move stays None; only the repeated pair is "
            f"numbered ({rep2})")


def check_recording_repeats(r: Report) -> None:
    """The trickiest correctness point in the redesign: a Recording drawn
    across an ENTIRE repeated Group's range must produce N separate
    RecordingRuns, never one merging them all (`recording_run_ids`'s
    docstring) — and the same holds across a CYCLE boundary, via the
    engine's own `(cycle, serial)` key in `_update_recording`.
    """
    # ── across a repeated Group ──
    routine = Routine(steps=[Step(kind="wait", label="A", length=0.05,
                                  unit="seconds"),
                             Step(kind="wait", label="B", length=0.05,
                                  unit="seconds"),
                             Step(kind="wait", label="C", length=0.05,
                                  unit="seconds")],
                      groups=[Group(start=1, end=2, repeats=3)],
                      recordings=[Recording(start=1, end=2)])
    order = play_order(routine)
    r.check(order == [0, 1, 2, 1, 2, 1, 2], f"fixture: A once, B-C x3 ({order})")
    ids = recording_run_ids(routine, order)
    r.check(ids == [None, 0, 0, 1, 1, 2, 2],
            f"a NEW serial every time the order doubles back into the "
            f"bracket, not one for the whole repeated range ({ids})")

    rig = FakeRig()
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)
    r.check(eng.phase == Phase.DONE, "the routine ran to the end")
    r.check(len(eng.runs) == 3 and [x.region for x in eng.runs] == [0, 0, 0],
            f"the SAME Recording (region 0) opens THREE separate runs, one "
            f"per repeat, never one merged run ({len(eng.runs)} run(s))")
    r.check(not any(x.interrupted for x in eng.runs), "…all three clean")
    r.check(all(a.t_end is not None and a.t_end <= b.t0 + 1e-9
               for a, b in zip(eng.runs, eng.runs[1:])),
            "…and they do not overlap in time — each is its own pass through "
            "the bracket")

    # ── across a CYCLE boundary ──
    routine2 = Routine(steps=[Step(kind="wait", label="A", length=0.05,
                                   unit="seconds"),
                              Step(kind="wait", label="B", length=0.05,
                                   unit="seconds")],
                       recordings=[Recording(start=0, end=1)],
                       cycles=2)
    order2 = play_order(routine2)
    ids2 = recording_run_ids(routine2, order2)
    r.check(ids2 == [0, 0],
            f"one pass alone sees a single serial — cycles are outside "
            f"play_order's view ({ids2})")

    rig2 = FakeRig()
    eng2 = RoutineEngine(routine2, rig2.hooks())
    eng2.start()
    drive(eng2, rig2)
    r.check(eng2.phase == Phase.DONE, "the 2-cycle routine ran to the end")
    r.check(len(eng2.runs) == 2 and [x.cycle for x in eng2.runs] == [0, 1],
            f"the SAME bracket (serial 0 in both cycles) still opens once "
            f"PER CYCLE — the engine keys on (cycle, serial), not the "
            f"serial alone ({len(eng2.runs)} run(s))")
    r.check([x.region for x in eng2.runs] == [0, 0],
            "…both runs are of the same Recording, just different cycles")
    r.check(not any(x.interrupted for x in eng2.runs), "…both clean")


# ── frames and seconds are different units ────────────────────────────────────

def check_units(r: Report) -> None:
    """The operator's first decision: a step ends on the unit it was GIVEN.

    The control is a camera running below its nominal rate. An engine that
    converted seconds to frames at the nominal rate would end the seconds step
    at a frame count this one demonstrably does not produce.
    """
    nominal, actual = 106.0, 97.0
    rig = FakeRig(hz=actual)
    routine = Routine(steps=[Step(kind="wait", label="A", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="B", length=1.5,
                                  unit="seconds")],
                      recordings=[Recording(start=0, end=0),
                                  Recording(start=1, end=1)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    r.check(eng.phase == Phase.DONE and len(eng.runs) == 2,
            f"both steps ran (phase={eng.phase}, {len(eng.runs)} runs)")
    a, b = eng.runs
    r.check(a.frames is not None and 100 <= a.frames <= 102,
            f"the frames step ended on its FRAME count ({a.frames})")
    r.check(abs((a.t_end - a.t0) - 100 / actual) < 3 * DT,
            f"…and took the time that implies at {actual:g} Hz "
            f"({a.t_end - a.t0:.3f} s)")

    held = b.t_end - b.t0
    r.check(abs(held - 1.5) <= 2 * DT,
            f"the seconds step ended on its DURATION ({held:.3f} s)")
    naive = int(1.5 * nominal)
    r.check(b.frames is not None and abs(b.frames - naive) > 8,
            f"control: it did NOT stop at the nominal-rate conversion "
            f"({b.frames} frames, a converting engine would say {naive})")
    r.check(b.frames is not None and abs(b.frames - 1.5 * actual) < 4,
            f"…it stopped where the real rate puts it ({b.frames} frames)")


# ── the order of operations within a step ─────────────────────────────────────

def check_order(r: Report, tmp: Path) -> None:
    """Blank, move, settle, restore whatever a Display step last lit — for a
    Move step. Pattern then light — for a Display step. Neither for a Wait or
    a Puff step: those kinds have nothing to travel or settle around."""
    pattern = tmp / "bar.png"
    pattern.write_bytes(b"x")

    # ── Display sets pattern, then light; a following Move blanks it before
    #    travelling, then restores it once arrived and settled ──
    rig = FakeRig(travel_s=0.30)
    routine = Routine(steps=[Step(kind="display", pattern=str(pattern)),
                             Step(kind="move", x_um=50.0, y_um=60.0,
                                  z_um=70.0, settle_s=0.25)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    kinds = [e[0] for e in rig.log]
    r.check(kinds[:4] == ["pattern", "light", "light", "move"],
            f"pattern, light-on, then the move's blank, then the move itself "
            f"({kinds[:4]})")
    r.check(rig.log[3] == ("move", 50.0, 60.0, 70.0),
            f"a step's z_um reaches the move hook alongside x/y "
            f"({rig.log[3]})")
    r.check(rig.log[1] == ("light", True) and rig.log[2] == ("light", False),
            "the display's light-on is immediately followed by the move's "
            "blank")
    r.check(rig.log[-2] == ("light", True),
            "the move restores whatever the display last left lit, once "
            "arrived and settled")
    r.check(rig.log[-1] == ("light", False),
            "…and the light goes off again when the routine finishes")

    # The control that matters on a rig with a sample under the objective.
    lit = False
    travelling_lit = False
    for e in rig.log:
        if e[0] == "light":
            lit = e[1]
        if e[0] == "move" and lit:
            travelling_lit = True
    r.check(not travelling_lit, "the light is never on when a move is issued")

    r.check(rig.t >= 0.30 + 0.25 - DT,
            f"the routine did not finish before travel AND settle elapsed "
            f"({rig.t:.3f} s)")

    # A Move step with no target sends no move() at all — it is a pure
    # settle, still blanking and restoring the light around it.
    rig2 = FakeRig()
    routine2 = Routine(steps=[Step(kind="display", pattern=str(pattern)),
                              Step(kind="move", settle_s=0.1)])
    eng2 = RoutineEngine(routine2, rig2.hooks())
    eng2.start()
    drive(eng2, rig2)
    r.check("move" not in [e[0] for e in rig2.log],
            f"a move step with no target issues no move command "
            f"({rig2.log})")
    r.check(rig2.log == [("pattern", str(pattern)), ("light", True),
                         ("light", False), ("light", True), ("light", False)],
            f"…only the blank-then-restore around its settle, plus the "
            f"display before it and the final shutdown ({rig2.log})")

    # Wait and Puff touch neither move nor light — only the final shutdown
    # (unconditional, any kind) does.
    rig3 = FakeRig()
    routine3 = Routine(steps=[Step(kind="wait", length=0.05, unit="seconds"),
                              Step(kind="puff")])
    eng3 = RoutineEngine(routine3, rig3.hooks())
    eng3.start()
    drive(eng3, rig3)
    r.check([e[0] for e in rig3.log] == ["puff", "light"],
            f"wait/puff neither blank nor restore the light — only the "
            f"final shutdown does ({rig3.log})")
    r.check(rig3.puffed == 1, "the puff step fired exactly once")

    # CONTROL: with no travel and no settle, a move finishes almost instantly.
    quick = FakeRig()
    e4 = RoutineEngine(Routine(steps=[Step(kind="move", x_um=1.0,
                                           settle_s=0.0)]), quick.hooks())
    e4.start()
    drive(e4, quick)
    r.check(e4.phase == Phase.DONE and quick.t < 3 * DT,
            f"control: no travel and no settle finishes almost immediately "
            f"({quick.t:.3f} s)")


def check_move_timeout(r: Report) -> None:
    """A stage that never reports arrival must pause, not hang forever."""
    rig = FakeRig(travel_s=1e9)          # never arrives
    eng = RoutineEngine(Routine(steps=[Step(kind="move", x_um=10.0)]),
                        rig.hooks(), move_timeout_s=2.0)
    eng.start()
    drive(eng, rig, limit_s=10.0)
    r.check(eng.phase == Phase.PAUSED and "did not arrive" in eng.fault,
            f"a stage that never arrives pauses the routine ({eng.fault!r})")
    r.check(("stop_motion",) in rig.log, "…and motion is stopped")
    r.check(not rig.lit, "…and the light is off")
    r.check(rig.t < 10.0, "…without running to the test's own limit")


# ── a fault pauses; the partial data is kept ──────────────────────────────────

def check_pause_keeps_data(r: Report, tmp: Path) -> None:
    """PLAN §6 (4): pause everything that is not capture, and keep the
    frames — and resuming opens a FRESH recording run, never reattaching to
    the one the pause just closed."""
    pattern = tmp / "pause.png"
    pattern.write_bytes(b"x")
    rig = FakeRig()
    routine = Routine(steps=[Step(kind="display", label="lit",
                                  pattern=str(pattern)),
                             Step(kind="wait", label="A", length=1.0,
                                  unit="seconds"),
                             Step(kind="wait", label="B", length=1.0,
                                  unit="seconds")],
                      recordings=[Recording(start=1, end=1),
                                  Recording(start=2, end=2)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: (e.phase == Phase.RUNNING
                                     and e.step is not None
                                     and e.step.kind == "wait" and rig.t > 0.4))
    r.check(eng.phase == Phase.RUNNING and eng.step.kind == "wait" and rig.lit,
            "mid-step, waiting, with the display's light still on")
    r.check(eng.steps_done() == 1, "the earlier display step already counted")

    eng.pause("the stage stopped answering")
    r.check(eng.phase == Phase.PAUSED, "a fault pauses the routine")
    r.check(not rig.lit, "…light off")

    r.check(len(eng.runs) == 1 and eng.runs[0].interrupted,
            "the half-finished recording is KEPT, marked interrupted")
    r.check(eng.runs[0].fault == "the stage stopped answering",
            "…carrying the reason into the file")
    r.check(eng.runs[0].frames and eng.runs[0].frames > 0,
            f"…with the frames it did get ({eng.runs[0].frames})")
    r.check(len(rig.ended) == 1 and rig.ended[0] is eng.runs[0],
            "…and its file boundary was closed, not abandoned")

    # Capture is the operator's: the engine has no way to stop it, and the
    # step it was in the middle of is still the current one.
    r.check(eng.position[0] == 1, "the paused step is still the current step")

    # Resume repeats the step as a fresh attempt, in a FRESH recording run.
    before = len([e for e in rig.log if e[0] == "begin"])
    eng.resume()
    drive(eng, rig)
    r.check(eng.phase == Phase.DONE, f"resume runs to the end ({eng.phase})")
    attempts = [(x.region, x.attempt, x.interrupted) for x in eng.runs]
    r.check(attempts == [(0, 1, True), (0, 2, False), (1, 1, False)],
            f"resume REPEATS the paused step as attempt 2, in a FRESH "
            f"recording run — region 0 twice, never merged into one "
            f"({attempts})")
    r.check(len([e for e in rig.log if e[0] == "begin"]) == before + 2,
            "…and the repeat opens its own file boundary, and so does the "
            "next step")
    r.check(eng.steps_done() == 3, "all three atomic steps eventually completed")


def check_skip(r: Report) -> None:
    """The other way out of a pause: give up on this step, take the next."""
    rig = FakeRig()
    routine = Routine(steps=[Step(kind="wait", label="A", length=1.0,
                                  unit="seconds"),
                             Step(kind="wait", label="B", length=0.2,
                                  unit="seconds")],
                      recordings=[Recording(start=0, end=0),
                                  Recording(start=1, end=1)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.RUNNING and rig.t > 0.3)
    eng.pause("operator")
    eng.skip()
    drive(eng, rig)
    got = [(x.region, x.attempt, x.interrupted) for x in eng.runs]
    r.check(eng.phase == Phase.DONE and got == [(0, 1, True), (1, 1, False)],
            f"skip drops the step and runs the next one, in its own "
            f"recording ({got})")
    r.check(eng.steps_done() == 1,
            "the skipped step does not count as done, only the next one does")


def check_setup_failure(r: Report, tmp: Path) -> None:
    """A step whose own action raises pauses before any light reaches the
    sample. Recording opens BEFORE the step's own action is attempted (the
    camera captures continuously regardless of any one step's setup): a step
    INSIDE a Recording still gets a run — opened, then immediately closed
    interrupted — while one OUTSIDE any bracket opens nothing, same as ever.
    """
    pattern = tmp / "setup.png"
    pattern.write_bytes(b"x")

    # A move that raises, INSIDE a Recording.
    rig = FakeRig()
    rig.fail_move = True
    routine = Routine(steps=[Step(kind="move", x_um=10.0)],
                      recordings=[Recording(start=0, end=0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    r.check(eng.phase == Phase.PAUSED and "setup failed" in eng.fault,
            f"a failing move pauses at setup ({eng.fault!r})")
    r.check(("light", True) not in rig.log,
            "the light was never turned on for a step that never started")
    r.check(len(eng.runs) == 1 and eng.runs[0].interrupted
            and (eng.runs[0].frames or 0) <= 1,
            f"…but the Recording bracket around it still opened, and is "
            f"closed interrupted with ~0 duration ({eng.runs[0]})")
    r.check(len(rig.begun) == 1 and len(rig.ended) == 1,
            "…the file boundary was opened AND closed, not abandoned mid-open")

    # CONTROL: the same failure OUTSIDE any Recording opens nothing at all.
    rig_nc = FakeRig()
    rig_nc.fail_move = True
    routine_nc = Routine(steps=[Step(kind="move", x_um=10.0)])
    eng_nc = RoutineEngine(routine_nc, rig_nc.hooks())
    eng_nc.start()
    r.check(eng_nc.phase == Phase.PAUSED and eng_nc.runs == [],
            "control: the same failure with no Recording over the step "
            "opens no run to interrupt")

    # The projector failing at the top of Display is the same shape.
    rig2 = FakeRig()
    rig2.fail_light = True
    e2 = RoutineEngine(Routine(steps=[Step(kind="display", pattern=str(pattern))],
                               recordings=[Recording(start=0, end=0)]),
                       rig2.hooks())
    e2.start()
    r.check(e2.phase == Phase.PAUSED and "setup failed" in e2.fault,
            f"a projector that will not light pauses the step ({e2.fault!r})")
    r.check(len(e2.runs) == 1 and e2.runs[0].interrupted,
            "…and its Recording bracket is opened, then closed interrupted, "
            "the same as the move above")

    # A recording that cannot open its FILE is a different fault, caught
    # BEFORE the step's own action ever runs.
    rig3 = FakeRig()
    rig3.fail_begin = True
    e3 = RoutineEngine(Routine(steps=[Step(kind="move", x_um=10.0)],
                              recordings=[Recording(start=0, end=0)]),
                       rig3.hooks())
    e3.start()
    r.check(e3.phase == Phase.PAUSED
            and "recording could not start" in e3.fault,
            f"a recording that fails to OPEN pauses with its own message "
            f"({e3.fault!r})")
    r.check(("move", 10.0, None, None) not in rig3.log and e3.runs == [],
            "…and the step's own action never ran on top of that pause")

    # Resuming once the file starts working again must actually resume — a
    # stray phase check here once could not tell "the resume that just
    # failed" from "the resume that is happening now" and stuck forever.
    rig3.fail_begin = False
    e3.resume()
    r.check(e3.phase == Phase.RUNNING and ("move", 10.0, None, None) in rig3.log,
            f"…and resuming once the file opens again actually runs the "
            f"step ({e3.phase})")


def check_frames_vanish(r: Report) -> None:
    """The camera going away mid-step is a fault, not a step that never ends."""
    rig = FakeRig()
    eng = RoutineEngine(Routine(steps=[Step(kind="wait", length=1000,
                                            unit="frames")]), rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.RUNNING and rig.t > 0.2)
    rig.frames_running = False
    rig.advance()
    eng.tick()
    r.check(eng.phase == Phase.PAUSED and "frame count" in eng.fault,
            f"a frames step whose counter vanishes pauses ({eng.fault!r})")


# ── cycles, and the per-repeat file guarantee ─────────────────────────────────

def check_cycles_and_attrs(r: Report) -> None:
    """Repeats run in order, and every recording file can be put back on one
    clock — each step wrapped in its own Recording, `per_repeat`-style. (The
    engine itself never reads `save_mode` — only `adapters/routines.py`
    does — so this exercises the RecordingRun/attrs shape the file-rolling
    feature relies on, not the rolling itself; see check_file_rolling for
    that.)"""
    rig = FakeRig()
    routine = Routine(name="grid", cycles=3, save_mode="per_repeat",
                      steps=[Step(kind="wait", label="A", length=0.10,
                                  unit="seconds"),
                             Step(kind="wait", label="B", length=0.10,
                                  unit="seconds")],
                      recordings=[Recording(start=0, end=0),
                                  Recording(start=1, end=1)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    order = [(x.cycle, x.region) for x in eng.runs]
    r.check(order == [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)],
            f"3 cycles x 2 recordings run in order ({order})")
    r.check(len(rig.begun) == 6 and len(rig.ended) == 6,
            f"one file boundary per recording execution "
            f"({len(rig.begun)} opened, {len(rig.ended)} closed)")
    r.check(routine.total_steps() == 6,
            "the routine says up front how many executions that is")

    # The reassembly guarantee. Per-step files that each restart from zero are
    # not relatable afterwards — so every one carries the same session origin
    # and its own t0 on that clock.
    origin = 1234.5
    attrs = [x.attrs(session_origin=origin) for x in eng.runs]
    r.check(all(a["routine_recording_session_origin"] == origin
               for a in attrs),
            "every recording file names the same session origin")
    t0s = [a["routine_recording_t0"] for a in attrs]
    r.check(all(b > a for a, b in zip(t0s, t0s[1:])),
            f"…and its own t0, strictly increasing across the folder "
            f"({[round(t, 2) for t in t0s]})")
    r.check(t0s[0] < t0s[-1] and t0s[-1] > 0.5,
            "…on the SHARED clock, not restarted per file")
    r.check(all(a["routine_recording_cycle"] == c
               and a["routine_recording_region"] == i
               for a, (c, i) in zip(attrs, order)),
            "…and enough identity to say which recording it was")

    r.check([a["routine_recording_interrupted"] for a in attrs] == [False] * 6,
            "a clean run marks nothing interrupted")


def check_transitions(r: Report) -> None:
    """The control surface refuses what it cannot do, rather than misbehaving."""
    rig = FakeRig()
    eng = RoutineEngine(Routine(steps=[Step(kind="wait", length=0.1,
                                            unit="seconds")]), rig.hooks())
    for name, call in (("resume", eng.resume), ("skip", eng.skip)):
        try:
            call()
            r.check(False, f"{name}() before a start must raise")
        except RoutineError:
            r.check(True, f"{name}() before a start raises RoutineError")

    eng.start()
    try:
        eng.start()
        r.check(False, "a second start() must raise")
    except RoutineError:
        r.check(True, "a second start() raises rather than restarting mid-run")

    r.check(eng.running, "running is True while a routine owns the rig")
    eng.abort()
    r.check(eng.phase == Phase.DONE and not eng.running and not rig.lit,
            "abort finishes the routine with the light off")

    empty = RoutineEngine(Routine(steps=[]), rig.hooks())
    try:
        empty.start()
        r.check(False, "an empty routine must not start")
    except RoutineError:
        r.check(True, "an empty routine raises rather than finishing instantly")


def check_ttl_start_trigger(r: Report) -> None:
    """`start(trigger="ttl")` arms instead of moving right away, and only lets
    step 1 begin once the camera reports a frame it did not have at arm time
    — the fake's `hz` stands in for a real TTL pulse making an externally
    triggered camera emit its first frame.
    """
    rig = FakeRig(hz=0.0)          # frozen: the camera has not been pulsed
    routine = Routine(steps=[Step(kind="move", label="A", x_um=5.0,
                                  settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start(trigger="ttl")
    r.check(eng.phase == Phase.ARMED and eng.running,
            f"start(trigger='ttl') arms rather than moving right away "
            f"({eng.phase})")
    r.check(rig.log == [], "…and nothing has actuated yet")

    for _ in range(20):
        rig.advance()
        eng.tick()
    r.check(eng.phase == Phase.ARMED,
            "with no pulse — the camera's frame count never moves — it "
            "stays armed rather than timing out")

    # The pulse: the camera, in External edge mode, produces its first frame.
    rig.hz = 100.0
    drive(eng, rig, until=lambda e: e.phase != Phase.ARMED)
    r.check(eng.phase == Phase.RUNNING,
            f"the first frame past the baseline starts step 1 ({eng.phase})")
    r.check(rig.log and rig.log[0] == ("light", False),
            "…the move's own blank is the first thing it does, the same way "
            "a manual start begins a step")

    # Abort while armed ends cleanly — nothing was ever opened to unwind.
    rig2 = FakeRig(hz=0.0)
    eng2 = RoutineEngine(routine, rig2.hooks())
    eng2.start(trigger="ttl")
    eng2.abort()
    r.check(eng2.phase == Phase.DONE and eng2.runs == [],
            "aborting while armed ends the routine with no run ever opened")

    # The frame count going away while armed is a fault, the same shape as
    # `check_frames_vanish` mid-run.
    rig3 = FakeRig(hz=0.0)
    eng3 = RoutineEngine(routine, rig3.hooks())
    eng3.start(trigger="ttl")
    rig3.frames_running = False
    rig3.advance()
    eng3.tick()
    r.check(eng3.phase == Phase.PAUSED and "frame count" in eng3.fault,
            f"the camera going away while armed pauses ({eng3.fault!r})")


def check_trigger_step(r: Report) -> None:
    """A `trigger` step: one recording per external edge, repeated.

    The operator's case — "100 recordings at FOV 1, each started by an edge,
    each lasting x seconds" — is a repeat group over [trigger, wait], with the
    Recording bracket on the wait ALONE so the file opens on the edge rather
    than while waiting for it. Here with 3 repeats instead of 100.

    Drain, settle and timeout are all shortened; at their real values
    (1.5 s / 0.75 s / 600 s) this would tick for the better part of an hour on
    the fake clock.
    """
    DRAIN, SETTLE = 0.2, 0.1
    routine = Routine(
        steps=[Step(kind="trigger", label="edge"),
               Step(kind="wait", label="capture", length=0.5, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=3)],
        recordings=[Recording(start=1, end=1)])
    rig = FakeRig(hz=100.0)
    eng = RoutineEngine(routine, rig.hooks(), trigger_drain_s=DRAIN,
                        trigger_settle_s=SETTLE, trigger_timeout_s=5.0)
    eng.start()

    r.check(eng.phase == Phase.WAITING and eng.running,
            f"a trigger step puts the engine in WAITING, which still counts as "
            f"running ({eng.phase})")
    r.check(rig.rearms == 1 and rig.gated,
            f"…having re-armed the camera, so the next edge is detectable "
            f"(rearms={rig.rearms}, gated={rig.gated})")
    r.check(rig.begun == [],
            "…and NOT opened a recording — the bracket is on the next step, so "
            "the file starts on the edge")

    # Frames still being written while the re-arm takes effect must not read as
    # the edge — they raise the baseline and restart the settle window. This is
    # the rig bug: a fixed window expired while these were still arriving, and
    # the next one started a recording with no trigger.
    rig.fire_trigger()                      # residual frames, re-arm not yet in
    for _ in range(int((DRAIN + SETTLE) / DT) + 10):
        rig.advance()
        eng.tick()
    r.check(eng.phase == Phase.WAITING,
            "a camera that never goes quiet never satisfies the step — "
            "in-flight frames raise the baseline instead of ending it")

    # Now the camera really stops. Going quiet is not itself an edge.
    rig.gated = True
    for _ in range(int((DRAIN + SETTLE) / DT) + 10):
        rig.advance()
        eng.tick()
    r.check(eng.phase == Phase.WAITING,
            f"…and the camera merely going quiet is not an edge either "
            f"({eng.phase})")

    # Settled, so the next frame is a genuine edge.
    rig.fire_trigger()
    drive(eng, rig, until=lambda e: e.phase != Phase.WAITING)
    r.check(eng.phase == Phase.RUNNING,
            f"the first frame after it settled IS the edge ({eng.phase})")
    r.check(rig.begun == [],
            "…the recording still has not opened: the trigger step is not in "
            "the bracket, so it opens as the NEXT step is entered")
    rig.advance()
    eng.tick()                       # completes the step, enters the wait
    r.check(len(rig.begun) == 1,
            f"…and it opens there, once the edge has already landed "
            f"({len(rig.begun)})")

    # Each repeat re-arms and waits again. Fire the remaining edges as they
    # come, each once the drain window has passed — the only time an edge can
    # be seen at all. `waiting_since` tracks that from the outside rather than
    # reading the engine's own timer.
    fired, waiting_since = 1, None
    while rig.t < 30.0 and eng.phase not in (Phase.DONE, Phase.PAUSED):
        if eng.phase == Phase.WAITING and rig.gated:
            if waiting_since is None:
                waiting_since = rig.now()
            elif rig.now() - waiting_since >= DRAIN + SETTLE + 0.1:
                rig.fire_trigger()
                fired += 1
                waiting_since = None
        rig.advance()
        eng.tick()

    r.check(eng.phase == Phase.DONE,
            f"three triggered repeats run to completion ({eng.phase}, "
            f"fault={eng.fault!r})")
    r.check(rig.rearms == 3 and fired == 3,
            f"one re-arm per repeat, one edge each — the camera latches, so "
            f"every recording needs its own (rearms={rig.rearms}, "
            f"edges={fired})")
    r.check(len(rig.begun) == 3 and len(rig.ended) == 3,
            f"three recording runs, one per edge ({len(rig.begun)} opened, "
            f"{len(rig.ended)} closed)")
    r.check(all(not run.interrupted for run in eng.runs),
            "…none of them interrupted")

    # THE RIG BUG (2026-09-16), as a test: a camera that keeps streaming
    # through the re-arm must FAULT, naming the reason — never silently treat
    # the next frame as an edge, which is what started a second recording with
    # no trigger. `rig_free`'s `arm_trigger` is overridden to log the call but
    # never actually gate, the way the real camera appeared to.
    rig_free = FakeRig(hz=100.0)
    rig_free.arm_trigger = lambda: rig_free.log.append(("arm_trigger",))
    eng_free = RoutineEngine(routine, rig_free.hooks(), trigger_drain_s=DRAIN,
                             trigger_settle_s=SETTLE, trigger_timeout_s=1.0)
    eng_free.start()
    drive(eng_free, rig_free, limit_s=5.0,
          until=lambda e: e.phase == Phase.PAUSED)
    r.check(eng_free.phase == Phase.PAUSED,
            f"a camera that ignores the re-arm pauses the routine rather than "
            f"inventing an edge ({eng_free.phase})")
    r.check("not re-arming" in eng_free.fault,
            f"…and the fault says what is actually wrong ({eng_free.fault!r})")
    r.check(rig_free.begun == [],
            f"…with no recording ever opened for the trigger that never "
            f"happened ({rig_free.begun})")

    # A step that never sees its edge faults rather than hanging the routine.
    rig2 = FakeRig(hz=100.0)
    eng2 = RoutineEngine(routine, rig2.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE, trigger_timeout_s=1.0)
    eng2.start()
    drive(eng2, rig2, limit_s=5.0, until=lambda e: e.phase == Phase.PAUSED)
    r.check(eng2.phase == Phase.PAUSED and "trigger" in eng2.fault,
            f"an edge that never arrives times out into a pause rather than "
            f"waiting forever ({eng2.fault!r})")

    # The operator can take the rig back mid-wait.
    rig3 = FakeRig(hz=100.0)
    eng3 = RoutineEngine(routine, rig3.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE)
    eng3.start()
    r.check(eng3.phase == Phase.WAITING, "…control: waiting again")
    eng3.pause()
    r.check(eng3.phase == Phase.PAUSED,
            f"Pause works while WAITING — an edge may never come, and the "
            f"operator must not be held by it ({eng3.phase})")

    # A camera that cannot be re-armed is a fault at the step, not a silent
    # wait on an edge nothing can deliver.
    rig4 = FakeRig(hz=100.0)
    rig4.fail_arm = True
    eng4 = RoutineEngine(routine, rig4.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE)
    eng4.start()
    r.check(eng4.phase == Phase.PAUSED and "setup failed" in eng4.fault,
            f"a camera that cannot be re-armed pauses the routine "
            f"({eng4.fault!r})")


def check_arm_camera_trigger(r: Report) -> None:
    """`RoutinesModule._arm_camera_trigger()` — the fix itself: a TTL-start
    routine puts the camera in External edge mode through the host, before
    ever opening its own recording, rather than trusting the operator to
    have set it. Qt-free: this method only calls `self.win`/`self.panel`,
    neither of which needs a real window."""
    from acqApp.adapters.routines import FRAME_STREAM, RoutinesModule

    class FakeHost:
        def __init__(self, result: bool | None) -> None:
            self.result = result
            self.calls: list[tuple] = []
            self.statuses: list[str] = []

        def set_camera_trigger(self, key, on):
            self.calls.append((key, on))
            return self.result

        def status(self, msg):
            self.statuses.append(msg)

    class FakePanel:
        def __init__(self) -> None:
            self.problems: list[list[str]] = []

        def show_problems(self, problems):
            self.problems.append(list(problems))

    # Success: the camera confirms it's in External edge mode.
    host = FakeHost(True)
    adapter = RoutinesModule(host)
    adapter.panel = FakePanel()
    r.check(adapter._arm_camera_trigger() is True,
            "arming succeeds when the host confirms External edge")
    r.check(host.calls == [(FRAME_STREAM, True)],
            f"…asking the host for exactly the voltage camera, on "
            f"({host.calls})")
    r.check(adapter.panel.problems == [], "…and shows no problem")

    # Failure: no camera loaded at all (host returns None for "not loaded"
    # the same way `set_camera_preset` already does).
    host2 = FakeHost(None)
    adapter2 = RoutinesModule(host2)
    adapter2.panel = FakePanel()
    r.check(adapter2._arm_camera_trigger() is False,
            "arming fails when the host can't find the module")
    r.check(adapter2.panel.problems and "no camera loaded" in
           adapter2.panel.problems[0][0],
            f"…and shows a problem naming the reason "
            f"({adapter2.panel.problems})")
    r.check(host2.statuses and "refused" in host2.statuses[0],
            f"…and the status line says the routine was refused "
            f"({host2.statuses})")

    # Failure: a recording is already running and the camera isn't already
    # External edge — VoltageCamModule.set_external_trigger's own contract
    # (adapters/voltage_cam.py) for "would need a restart, can't do it now".
    host3 = FakeHost(False)
    adapter3 = RoutinesModule(host3)
    adapter3.panel = FakePanel()
    r.check(adapter3._arm_camera_trigger() is False,
            "control: a host returning False (not None) is still a failure "
            "— only True means armed")


# ── pre-redesign templates auto-migrate ───────────────────────────────────────

def check_migration(r: Report) -> None:
    """`Routine.from_dict` on a pre-redesign JSON blob (composite steps: x/y,
    pattern, length/unit, settle_s, puff_interval_s, no "kind" key) migrates
    each old step into the atomic steps it implied, each wrapped in its own
    Recording — and old Group ranges (indexing the OLD composite-step list)
    are remapped onto the NEW, expanded step indices."""

    # a) a move+length composite -> a move step, then a wait step.
    old_move = {"steps": [{"x_um": 10.0, "y_um": 20.0, "fov": "f1",
                          "settle_s": 0.4, "length": 50, "unit": "frames"}]}
    rm = Routine.from_dict(old_move)
    r.check(len(rm.steps) == 2 and rm.steps[0].kind == "move"
            and rm.steps[0].x_um == 10.0 and rm.steps[0].y_um == 20.0
            and rm.steps[0].fov == "f1" and rm.steps[0].settle_s == 0.4,
            f"the move half keeps its target, FOV name and settle "
            f"({rm.steps[0]})")
    r.check(rm.steps[1].kind == "wait" and rm.steps[1].length == 50
            and rm.steps[1].unit == "frames",
            f"…followed by a wait step with the old length/unit ({rm.steps[1]})")
    r.check(rm.recordings == [Recording(start=0, end=1)],
            "…both wrapped in one Recording spanning the whole expansion — "
            "an old step always captured")

    # b) a pattern-only composite -> a display step, then a wait step.
    old_pat = {"steps": [{"pattern": "p.png", "length": 10, "unit": "seconds"}]}
    rp = Routine.from_dict(old_pat)
    r.check([s.kind for s in rp.steps] == ["display", "wait"],
            f"a pattern step becomes display then wait ({[s.kind for s in rp.steps]})")
    r.check(rp.steps[0].pattern == "p.png" and rp.steps[1].length == 10,
            "…keeping the pattern path and the wait length")

    # c) a seconds-unit step with puff_interval_s>0 interleaves EXACT
    #    Wait+Puff chunks, summing back to the original length.
    old_puff_s = {"steps": [{"length": 5.0, "unit": "seconds",
                            "puff_interval_s": 2.0, "label": "puffed"}]}
    rs = Routine.from_dict(old_puff_s)
    kinds = [s.kind for s in rs.steps]
    r.check(kinds == ["wait", "puff", "wait", "puff", "wait"],
            f"a seconds step with a puff interval interleaves exact chunks "
            f"({kinds})")
    lens = [s.length for s in rs.steps if s.kind == "wait"]
    r.check(lens == [2.0, 2.0, 1.0],
            f"…summing back to the original 5.0 s ({lens})")
    r.check(rs.steps[0].label == "puffed"
            and all(s.label == "" for s in rs.steps[2::2]),
            "…the label stays on the FIRST chunk only")
    r.check(rs.recordings == [Recording(start=0, end=4)],
            "…all wrapped in one Recording covering the whole expansion")

    # d) a frames-unit step with puff_interval_s>0 falls back to ONE trailing
    #    Puff (lossy — no frame rate here to convert real time against a
    #    frame-gated duration).
    old_puff_f = {"steps": [{"length": 100, "unit": "frames",
                            "puff_interval_s": 1.0}]}
    rf = Routine.from_dict(old_puff_f)
    r.check([s.kind for s in rf.steps] == ["wait", "puff"],
            f"a frames step with a puff interval falls back to one trailing "
            f"puff ({[s.kind for s in rf.steps]})")

    # e) old Group ranges (indexing OLD composite steps) remap onto the NEW
    #    expanded step indices.
    old_grouped = {
        "steps": [
            {"x_um": 1.0, "length": 1, "unit": "seconds"},        # -> move, wait
            {"pattern": "q.png", "length": 1, "unit": "seconds"}, # -> display, wait
            {"length": 1, "unit": "seconds"},                     # -> wait
        ],
        "groups": [{"start": 1, "end": 2, "repeats": 2}],
    }
    rg = Routine.from_dict(old_grouped)
    r.check(len(rg.steps) == 5,
            f"three old steps expand to five new ones ({len(rg.steps)})")
    r.check(len(rg.groups) == 1 and rg.groups[0].start == 2
            and rg.groups[0].end == 4 and rg.groups[0].repeats == 2,
            f"the group's OLD range (steps 1-2) remaps to the NEW steps "
            f"those old positions became (2-4) ({rg.groups[0]})")
    r.check(len(rg.recordings) == 3,
            "each old step still gets its own Recording")

    # A file with no "kind" anywhere must not raise, whatever it contains.
    try:
        Routine.from_dict({"steps": [{"nonsense": True}, "not a dict", 42]})
        r.check(True, "control: a thoroughly malformed old-format blob does "
                     "not raise")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a malformed blob: {e}")


# ── cycles, groups, and the per-step file guarantee are above; below: how ──
# ── long a routine takes, its panel, its table, and the whole app.        ──

def check_estimate(r: Report) -> None:
    """The estimate converts frames to seconds; the RECORDING still never does.

    Frames and seconds are not interconvertible where a step is recorded — a
    rounded conversion sheds frames at every boundary, which is why `unit`
    travels with `length`. An estimate is not a recording, so it may convert;
    what it must not do is convert **silently**, or invent a rate nobody gave it.

    A move step costs only its settle (travel itself is untimed); a display
    or puff step costs nothing (both are instant).
    """
    from acqApp.routines.estimate import clock, estimate, remaining

    r.check(clock(45) == "45 s" and clock(124) == "2:04 min"
            and clock(3725) == "1:02:05 h",
            f"a duration reads as one ({clock(45)}, {clock(124)}, {clock(3725)})")

    rt = Routine(steps=[Step(kind="move", settle_s=0.25),
                        Step(kind="wait", length=100, unit="frames"),
                        Step(kind="wait", length=2.0, unit="seconds"),
                        Step(kind="move", settle_s=0.25)])
    blind = estimate(rt, None)
    r.check(blind.seconds == 2.5 and blind.frames == 100,
            f"with no frame rate the frames stay frames "
            f"({blind.seconds} s, {blind.frames} frames)")
    r.check(not blind.complete and "at least" in blind.text()
            and "100 frames" in blind.text(),
            f"…and it says so rather than guessing ({blind.text()!r})")

    known = estimate(rt, 100.0)
    r.check(known.complete and abs(known.seconds - 3.5) < 1e-9,
            f"at 100 Hz the 100-frame step is 1 s, plus two 0.25 s settles "
            f"and a 2 s wait ({known.seconds} s)")
    r.check(known.text().startswith("about") and "frames" not in known.text(),
            f"…and the whole routine is one duration ({known.text()!r})")
    # CONTROL: the rate is the operator's camera, not a constant in here.
    half = estimate(rt, 50.0)
    r.check(abs(half.seconds - 4.5) < 1e-9,
            f"control: halving the frame rate lengthens only the frames step "
            f"({half.seconds} s)")

    rt.cycles = 3
    r.check(abs(estimate(rt, 100.0).seconds - 10.5) < 1e-9,
            f"cycles multiply the whole list ({estimate(rt, 100.0).seconds} s)")

    # display/puff cost nothing.
    instant = Routine(steps=[Step(kind="display", pattern="p.png"),
                             Step(kind="puff")])
    r.check(estimate(instant, None).seconds == 0.0
            and estimate(instant, None).frames == 0.0,
            "a display/puff-only routine costs nothing to estimate")

    # What is LEFT, part-way through: the readout must not sit still for a
    # whole step and then jump.
    rt2 = Routine(steps=[Step(kind="wait", length=100, unit="frames"),
                         Step(kind="wait", length=2.0, unit="seconds")])
    whole = estimate(rt2, 100.0).seconds
    at_start = remaining(rt2, 100.0, 0, 0, 0.0).seconds
    midway = remaining(rt2, 100.0, 0, 0, 0.5).seconds
    last = remaining(rt2, 100.0, 1, 0, 0.0).seconds
    r.check(abs(at_start - whole) < 1e-9,
            f"before step 1, all of it is left ({at_start} s)")
    r.check(midway < at_start and last < midway,
            f"…and it falls within a step, not only between them "
            f"({at_start} > {midway} > {last})")
    r.check(abs(last - 2.0) < 1e-9,
            f"the last step alone, from its start, is its own length "
            f"({last} s)")

    # The stage is what nothing can time: `RoutineHooks.moving` is a seam
    # nothing fills, so travel is not in any of these totals — and a move
    # with no target does not count as "moving the stage" either.
    moved = Routine(steps=[Step(kind="wait", length=1.0, unit="seconds"),
                          Step(kind="move", x_um=250.0, settle_s=0.0)])
    r.check(estimate(moved, None).moves == 1,
            "an estimate reports how many steps move the stage, since their "
            "travel is not counted")
    r.check(estimate(rt, None).moves == 0,
            "control: a move step with no target does not count as moving "
            "the stage")

    lit = Routine(steps=[Step(kind="display", pattern="p.png"),
                        Step(kind="display", pattern="")])
    r.check(estimate(lit, None).lit == 1,
            "an estimate counts steps that actually display a pattern, not "
            "ones that stop displaying")


def check_progress(r: Report) -> None:
    """The whole-routine progress the tracker draws: monotone, and 1.0 at DONE."""
    rig = FakeRig(hz=100.0)
    rt = Routine(steps=[Step(kind="wait", length=0.2, unit="seconds"),
                        Step(kind="wait", length=0.2, unit="seconds")],
                 cycles=2)
    eng = RoutineEngine(rt, rig.hooks())
    r.check(eng.overall_progress() == 0.0 and eng.total_runs() == 4,
            f"before start it is 0 of {eng.total_runs()} runs")

    eng.start()
    seen: list[float] = []
    while rig.t < 30.0 and eng.phase not in (Phase.DONE, Phase.PAUSED):
        rig.advance()
        eng.tick()
        seen.append(eng.overall_progress())
    r.check(eng.phase == Phase.DONE and eng.overall_progress() == 1.0,
            f"a finished routine is 1.0 ({eng.overall_progress()})")
    r.check(all(b >= a - 1e-12 for a, b in zip(seen, seen[1:])),
            "it never goes backwards while running")
    r.check(any(0.05 < x < 0.95 for x in seen),
            "…and it passes through the middle rather than jumping 0 to 1")
    r.check(eng.elapsed() > 0.0,
            f"and the run reports how long it took ({eng.elapsed():.2f} s)")

    # CONTROL: a repeated attempt is not backwards progress. Resume repeats the
    # step, and a bar that went back would read as a fault.
    rig2 = FakeRig(hz=100.0)
    eng2 = RoutineEngine(Routine(steps=[Step(kind="wait", length=0.2,
                                             unit="seconds"),
                                        Step(kind="wait", length=0.2,
                                             unit="seconds")]),
                         rig2.hooks())
    eng2.start()
    drive(eng2, rig2, until=lambda e: e.position[0] == 1)
    before = eng2.overall_progress()
    eng2.pause()
    eng2.resume()
    r.check(eng2.overall_progress() >= before - 1e-12,
            f"control: resuming a step does not rewind the bar "
            f"({before} -> {eng2.overall_progress()})")


def check_panel_repaint(r: Report, app) -> None:
    """The panel is told its state 30×/s; it must repaint only on a change.

    `setStyleSheet` repolishes the widget against the window's whole cascade —
    26 us a call, and **53 % of the shared display tick** with eight modules
    loaded, spent re-applying the identical string. Counted rather than timed,
    so this is not a flaky benchmark.
    """
    from acqApp.routines.panel import SettingsPanel

    panel = SettingsPanel()
    styled, texted = [], []
    lbl = panel._lbl_state
    real_style, real_text = lbl.setStyleSheet, lbl.setText
    lbl.setStyleSheet = lambda s: (styled.append(s), real_style(s))[1]
    lbl.setText = lambda s: (texted.append(s), real_text(s))[1]

    for i in range(30):
        panel.set_state(Phase.RUNNING, f"step 1/2 — waiting {i} %")
    r.check(len(styled) == 1,
            f"30 ticks in one phase restyle once ({len(styled)})")
    r.check(len(texted) == 30,
            f"…while the moving text still updates every tick ({len(texted)})")

    panel.set_state(Phase.PAUSED, "PAUSED — the stage stopped answering")
    r.check(len(styled) == 2, "a phase change does restyle")
    r.check(not panel._btn_pause.isEnabled() and panel._btn_resume.isEnabled(),
            "…and the buttons follow the new phase")

    # CONTROL: show_problems() writes the label out of band, so the next
    # set_state must repaint even though the phase never moved.
    panel.show_problems(["step 1: nope"])
    panel.set_state(Phase.PAUSED, "PAUSED — the stage stopped answering")
    r.check(len(styled) == 4,
            f"control: an out-of-band write forces the next repaint ({len(styled)})")
    app.processEvents()


def _select_rows(tbl, first: int, last: int) -> None:
    """Select a contiguous row range the way a shift-click/drag would —
    ContiguousSelection mode means this is the only shape a real selection
    can take, so this is the fixture for "select steps N-M" throughout."""
    from PyQt6.QtWidgets import QTableWidgetSelectionRange
    tbl.clearSelection()
    tbl.setRangeSelected(
        QTableWidgetSelectionRange(first, 0, last, tbl.columnCount() - 1), True)


def _menu_texts(tbl, row: int, span: tuple[int, int] | None = None) -> list[str]:
    """The context menu's action labels for `row` — `QMenu.exec` is patched to
    capture the built menu's actions instead of blocking on a real popup, the
    same technique the codebase already uses for `QDialog.exec`."""
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu

    if span is not None:
        _select_rows(tbl, span[0], span[1])
    else:
        tbl.select_row(row)
    captured: list[list[str]] = []
    real_exec = QMenu.exec

    def fake_exec(self, *_a, **_k):
        captured.append([act.text() for act in self.actions()])
        return None

    QMenu.exec = fake_exec
    try:
        pos = tbl.visualRect(tbl.model().index(row, 0)).center()
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos)
        tbl.contextMenuEvent(ev)
    finally:
        QMenu.exec = real_exec
    return captured[0] if captured else []


def check_step_table(r: Report, app, tmp: Path) -> None:
    """The step list edits through widgets, not through typed words — now
    over a Kind column that decides which of the other columns mean anything.

    A step is one atomic action; Details/Length/Unit/Settle render "—" and
    refuse editing on a row whose kind does not use them (Length/Unit are
    Wait-only, Settle is Move-only, Details is Move/Display-only, dialog-set
    like the old Stage/Pattern cells). The value still lives in the cell's
    data — the text is a rendering of it — so a cell can only hold something
    its editor could produce.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import FIELDS, KIND_LABELS, NO_CHANGE, VALUE

    routine = Routine(steps=[Step(kind="wait", label="one", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="two", length=2.0,
                                  unit="seconds")])
    panel = SettingsPanel(routine)
    tbl = panel._tbl
    col = {f: i for i, f in enumerate(FIELDS)}

    edits: list = []
    panel.settings_changed.connect(edits.append)

    from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QLineEdit,
                                 QStyleOptionViewItem)

    def editor(row: int, field: str):
        c = col[field]
        return tbl.itemDelegateForColumn(c).createEditor(
            tbl, QStyleOptionViewItem(), tbl.model().index(row, c))

    ed = editor(0, "kind")
    r.check(isinstance(ed, QComboBox),
            f"the Kind cell opens a drop-down ({type(ed).__name__})")
    r.check(tuple(ed.itemData(i) for i in range(ed.count())) == KINDS,
            f"…offering exactly settings.KINDS ({KINDS}) — so the table "
            f"cannot drift from what validate() dispatches on")
    r.check(tuple(ed.itemText(i) for i in range(ed.count()))
            == tuple(KIND_LABELS[k] for k in KINDS),
            "…labelled with KIND_LABELS, not the raw kind strings")

    ed = editor(0, "unit")
    r.check(isinstance(ed, QComboBox),
            f"the Unit cell opens a drop-down ({type(ed).__name__})")
    r.check(tuple(ed.itemData(i) for i in range(ed.count())) == UNITS,
            f"…offering exactly settings.UNITS ({UNITS})")
    ed = editor(0, "settle_s")
    r.check(isinstance(ed, QDoubleSpinBox) and ed.suffix() == " s",
            f"the Settle delegate opens a spin box in seconds regardless of "
            f"the row's kind — editability is a separate gate "
            f"({type(ed).__name__})")
    idx = tbl.model().index(0, col["label"])
    ed = tbl.itemDelegateForIndex(idx).createEditor(
        tbl, QStyleOptionViewItem(), idx)
    r.check(isinstance(ed, QLineEdit),
            f"control: the Step name is still typed ({type(ed).__name__})")

    # ── kind-gating: Length/Unit are Wait-only, Settle is Move-only ──
    r.check(bool(tbl.item(0, col["length"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "Length is editable on a Wait row")
    r.check(not (tbl.item(0, col["settle_s"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…and Settle is NOT, on the same row")
    r.check(tbl.item(0, col["length"]).text() == "100",
            f"Length renders the value on a Wait row "
            f"({tbl.item(0, col['length']).text()!r})")
    r.check(tbl.item(0, col["settle_s"]).text() == "—",
            "Settle renders as a dash on a row that does not use it")

    tbl.item(0, col["kind"]).setData(VALUE, "move")
    r.check(routine.steps[0].kind == "move", "the Kind cell sets the kind")
    r.check(not (tbl.item(0, col["length"]).flags() & Qt.ItemFlag.ItemIsEditable)
            and tbl.item(0, col["length"]).text() == "—",
            "…and Length stops being editable now the row is a Move")
    r.check(bool(tbl.item(0, col["settle_s"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…while Settle becomes editable")
    r.check(tbl.item(0, col["settle_s"]).text()
            == f"{routine.steps[0].settle_s:g} s",
            "…and renders the settle value")

    # ── Details: Move is (x, y) together, one place not two cells ──
    routine.steps[0].x_um, routine.steps[0].y_um = 250.0, None
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text() == f"(250 um, {NO_CHANGE})",
            f"Move's Details renders both axes as one pair "
            f"({tbl.item(0, col['details']).text()!r})")
    r.check(tbl.item(0, col["details"]).data(VALUE) == (250.0, None, None),
            "…and the triple is the value of record, not just the text")
    r.check(not (tbl.item(0, col["details"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…and is not typed into")

    # A step filled from a saved FOV names it instead of showing two numbers
    # nobody recognises a spot by.
    routine.steps[0].x_um, routine.steps[0].y_um = 111.0, 222.0
    routine.steps[0].fov = "window1"
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text() == "window1 (111 um, 222 um)",
            f"the FOV's name goes in FRONT of the coordinates "
            f"({tbl.item(0, col['details']).text()!r})")

    # Z only joins the text once a step actually has one — most rigs and
    # most steps never do, so the common (x, y) rendering must not gain a
    # silent third number.
    routine.steps[0].z_um = 333.0
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text()
            == "window1 (111 um, 222 um, 333 um)",
            f"a step with a Z target renders all three axes "
            f"({tbl.item(0, col['details']).text()!r})")
    r.check(tbl.item(0, col["details"]).data(VALUE) == (111.0, 222.0, 333.0),
            "…and the value of record carries Z too")
    routine.steps[0].z_um = None
    panel._reload_table()

    # Typing a new position detaches the name.
    routine.steps[0].x_um, routine.steps[0].fov = 999.0, ""
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text() == "(999 um, 222 um)",
            "a typed position drops the FOV name off the whole cell")

    # Double-click on Details asks for the right thing, gated by kind — tested
    # through the signal itself, with the panel's REAL handlers disconnected
    # first: `_pick_pattern_for` would otherwise pop a real, blocking file
    # dialog and `_set_position_for` a real modal, neither of which this
    # offscreen test can dismiss.
    tbl.position_requested.disconnect(panel._set_position)
    tbl.pattern_requested.disconnect(panel._pick_pattern)
    opened: list = []
    tbl.position_requested.connect(lambda: opened.append("position"))
    tbl.pattern_requested.connect(lambda: opened.append("pattern"))
    try:
        tbl.item(0, col["kind"]).setData(VALUE, "move")
        tbl._on_double_click(0, col["details"])
        r.check(opened == ["position"],
                f"double-click on a Move row's Details asks for a position "
                f"({opened})")
        opened.clear()
        tbl.item(1, col["kind"]).setData(VALUE, "display")
        tbl._on_double_click(1, col["details"])
        r.check(opened == ["pattern"],
                f"…and a Display row's Details asks for a pattern ({opened})")
        opened.clear()
        tbl.item(1, col["kind"]).setData(VALUE, "wait")
        tbl._on_double_click(1, col["details"])
        r.check(opened == [],
                "…and a Wait row's Details has no dialog to ask for at all")
    finally:
        tbl.position_requested.connect(panel._set_position)
        tbl.pattern_requested.connect(panel._pick_pattern)
    tbl.item(1, col["kind"]).setData(VALUE, "display")   # restore for below

    # …and the REAL handler, reached the normal way, safely does nothing when
    # its dialog is cancelled. QDialog.exec is patched for exactly this one
    # call: a direct repro shows restoring it afterward (`QDialog.exec =
    # <the original>`) leaves EVERY later `.exec()` on ANY QDialog raising
    # TypeError for the rest of the process — a PyQt6/sip quirk, not
    # something reachable by fixing this test's own code — so this must stay
    # the one and only, LAST real QDialog.exec this file ever touches.
    from PyQt6.QtWidgets import QDialog

    QDialog.exec = lambda self: False
    before = (routine.steps[0].x_um, routine.steps[0].y_um)
    tbl.item(0, col["kind"]).setData(VALUE, "move")
    tbl._on_double_click(0, col["details"])
    r.check((routine.steps[0].x_um, routine.steps[0].y_um) == before,
            "the position dialog, reached for real through the signal, "
            "changes nothing when cancelled")

    # Delete on Details clears back to the kind's "nothing set" state.
    routine.steps[0].x_um, routine.steps[0].y_um = 400.0, 50.0
    panel._reload_table()
    tbl.setCurrentCell(0, col["details"])
    QTest.keyClick(tbl, Qt.Key.Key_Delete)
    r.check(routine.steps[0].x_um is None and routine.steps[0].y_um is None
            and routine.steps[0].z_um is None,
            "Delete on Move's Details clears every axis, not one at a time")
    # CONTROL: Delete is not a general erase — a cell that must hold a number
    # ignores it, and clear_cell("details") on a Wait/Puff row is a no-op.
    tbl.item(1, col["kind"]).setData(VALUE, "wait")
    before_len = routine.steps[1].length
    tbl.setCurrentCell(1, col["length"])
    QTest.keyClick(tbl, Qt.Key.Key_Delete)
    r.check(routine.steps[1].length == before_len,
            f"control: Delete on a cell that must hold a number does "
            f"nothing ({routine.steps[1].length})")
    try:
        tbl.clear_cell(1, "details")
        r.check(True, "control: clearing Details on a Wait row is a silent "
                     "no-op, not a crash")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped clearing a Wait row's "
                      f"Details: {e}")

    # A frames step is a whole number of frames — validate() refuses the
    # alternative, so the panel rounds rather than letting Start refuse it.
    tbl.item(1, col["unit"]).setData(VALUE, "frames")
    tbl.item(1, col["length"]).setData(VALUE, 100.4)
    r.check(routine.steps[1].length == 100,
            f"a fractional length on a frames step is rounded "
            f"({routine.steps[1].length})")
    r.check(not [p for p in validate(routine, RigLimits(has_frames=True,
                                                       has_dmd=True))
                if "whole number" in p],
            "…so the routine validates instead of being refused at Start")
    # CONTROL: seconds are not rounded.
    tbl.item(1, col["unit"]).setData(VALUE, "seconds")
    tbl.item(1, col["length"]).setData(VALUE, 2.5)
    r.check(routine.steps[1].length == 2.5,
            f"control: a seconds step keeps its fraction ({routine.steps[1].length})")
    tbl.item(1, col["kind"]).setData(VALUE, "wait")   # back to a plain wait

    # Reordering. Until this existed the only way to move a step was to
    # delete it and retype it.
    panel._tbl.select_row(0)
    panel._move_down()
    r.check([x.label for x in routine.steps] == ["two", "one"],
            f"a step moves down the list ({[x.label for x in routine.steps]})")
    r.check(panel._tbl.selected_row() == 1,
            "…and the selection follows it, so a second press moves the same step")
    panel._move_up()
    r.check([x.label for x in routine.steps] == ["one", "two"],
            "…and back up again")

    # The pattern is chosen with a file dialog on a Display row.
    tbl.item(0, col["kind"]).setData(VALUE, "display")
    routine.steps[0].pattern = r"C:\patterns\grid.png"
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text() == "grid.png",
            f"the Details cell shows the file's name "
            f"({tbl.item(0, col['details']).text()!r})")
    r.check(not (tbl.item(0, col["details"]).flags()
                & Qt.ItemFlag.ItemIsEditable),
            "…and is not typed into")
    panel._tbl.select_row(0)
    panel._tbl.setCurrentCell(0, col["details"])
    panel._clear_pattern()
    r.check(routine.steps[0].pattern == "" and
            tbl.item(0, col["details"]).text() == "stop displaying",
            "clearing the pattern reads as 'stop displaying', a stated "
            "action now — not a blank cell")
    r.check(tbl.item(0, col["details"]).icon().isNull(),
            "…and the thumbnail clears with it")

    # A real image file gets a thumbnail.
    from PyQt6.QtGui import QPixmap

    real_pattern = tmp / "real_pattern.png"
    QPixmap(8, 8).save(str(real_pattern), "PNG")
    routine.steps[0].pattern = str(real_pattern)
    panel._reload_table()
    r.check(not tbl.item(0, col["details"]).icon().isNull(),
            "a step with a real pattern image shows a thumbnail")

    # An ROI set has no image of its own, but its shapes get one too.
    from acqApp.devices.dmd import roi_store
    from acqApp.devices.dmd.roi import CircleRoi, RoiSet

    roi_set = RoiSet([CircleRoi(x=50.0, y=50.0, r=20.0)])
    roi_path = roi_store.save("some_set", roi_set)
    routine.steps[0].pattern = str(roi_path)
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).text() == "ROI: some_set",
            f"an ROI set still names itself ({tbl.item(0, col['details']).text()!r})")
    r.check(not tbl.item(0, col["details"]).icon().isNull(),
            "…AND now shows a thumbnail of its shapes")

    # CONTROL: an empty/unreadable ROI file has nothing to rasterise — no
    # thumbnail, but no crash either.
    empty_path = tmp / "empty_set.roi.json"
    empty_path.write_text(
        json.dumps({"name": "empty_set", "rois": []}), encoding="utf-8")
    routine.steps[0].pattern = str(empty_path)
    panel._reload_table()
    r.check(tbl.item(0, col["details"]).icon().isNull(),
            "control: an ROI set with nothing in it gets no thumbnail")
    corrupt_path = tmp / "corrupt_set.roi.json"
    corrupt_path.write_text("{not json", encoding="utf-8")
    routine.steps[0].pattern = str(corrupt_path)
    try:
        panel._reload_table()
        r.check(True, "control: a corrupt ROI file does not crash the repaint")
    except Exception as e:                        # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a corrupt ROI file: {e}")

    # The summary totals the protocol and flags anything that emits light.
    routine.steps[0].pattern = r"C:\patterns\grid.png"
    panel._refresh_summary()
    text = panel._lbl_summary.text()
    r.check("2 run(s)" in text and "emit light" in text,
            f"the summary says how much work it is and that it emits light "
            f"({text!r})")
    routine.steps[0].pattern = ""
    panel._refresh_summary()
    r.check("emit light" not in panel._lbl_summary.text(),
            f"control: with nothing projecting it does not warn "
            f"({panel._lbl_summary.text()!r})")

    # Which step is running is shown in the protocol, not only in the label.
    panel.set_state(Phase.RUNNING, "step 2/2", 1)
    r.check(tbl.item(1, 0).font().bold() and not tbl.item(0, 0).font().bold(),
            "the running step is bold in the table")
    panel.set_state(Phase.DONE, "finished", None)
    r.check(not tbl.item(1, 0).font().bold(),
            "…and nothing is bold once it is over")

    # ── the right-click menu is gated by the row's kind ──
    tbl.item(0, col["kind"]).setData(VALUE, "move")
    texts = _menu_texts(tbl, 0)
    r.check(any("Set position" in t for t in texts)
            and any("Fill Stage" in t for t in texts)
            and not any("pattern" in t.lower() for t in texts),
            f"a Move row offers position/FOV actions, not pattern ones "
            f"({texts})")

    tbl.item(0, col["kind"]).setData(VALUE, "display")
    texts = _menu_texts(tbl, 0)
    r.check(any("Set pattern" in t for t in texts)
            and any("Set ROI" in t for t in texts)
            and not any("position" in t.lower() for t in texts)
            and not any("FOV" in t for t in texts),
            f"a Display row offers pattern/ROI actions, not position ones "
            f"({texts})")

    tbl.item(0, col["kind"]).setData(VALUE, "wait")
    texts = _menu_texts(tbl, 0)
    r.check(not any("pattern" in t.lower() or "position" in t.lower()
                   or "fov" in t.lower() for t in texts),
            f"a Wait row offers neither — only Duplicate/Remove ({texts})")
    r.check(any("Duplicate" in t for t in texts)
            and any("Remove" in t for t in texts),
            f"…which every row keeps, regardless of kind ({texts})")

    r.check(not any("Group selected" in t or "Mark steps" in t for t in texts),
            "with only one row selected, neither group nor recording action "
            "is offered")
    texts2 = _menu_texts(tbl, 0, span=(0, 1))
    r.check(any("Group selected steps 1-2" in t for t in texts2),
            f"2+ contiguous rows selected offers Group selected ({texts2})")
    r.check(any("Mark steps 1-2" in t and "recording" in t for t in texts2),
            f"…and Mark as recording, the same way ({texts2})")

    app.processEvents()


def check_group_panel(r: Report, app) -> None:
    """Adding/removing a repeat group, and a recording bracket, by selecting
    rows in the table — the fix for the old flow (typing 1-based row numbers
    into spinboxes, disconnected from the table you were looking at). The
    two controls mirror each other; a Recording has no repeat count."""
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="wait", label="A"),
                             Step(kind="wait", label="B"),
                             Step(kind="wait", label="C")])
    panel = SettingsPanel(routine)

    r.check(not panel._btn_g_add.isEnabled() and not panel._btn_r_add.isEnabled(),
            "control: nothing selected -> neither Group nor Mark as recording "
            "is offered")
    panel._tbl.select_row(0)
    r.check(not panel._btn_g_add.isEnabled(),
            "a single selected row is still not a repeat group — one step "
            "repeated in place is what a Wait's own length already says")
    r.check(panel._btn_r_add.isEnabled(),
            "…but IS a candidate recording: a `trigger` step has to sit "
            "outside the bracket that follows it, so a one-step Recording is "
            "exactly what the per-edge pattern needs")
    r.check("Step 1" in panel._lbl_r_selection.text(),
            f"…named in the singular ({panel._lbl_r_selection.text()!r})")

    _select_rows(panel._tbl, 1, 2)              # steps B, C (0-based 1..2)
    r.check(panel._btn_g_add.isEnabled() and panel._btn_r_add.isEnabled(),
            "2+ contiguous rows selected -> both are offered")
    r.check("2-3" in panel._lbl_g_selection.text()
            and "2-3" in panel._lbl_r_selection.text(),
            f"…and the selection is named in 1-based step numbers in both "
            f"({panel._lbl_g_selection.text()!r}, "
            f"{panel._lbl_r_selection.text()!r})")

    # ── repeat groups ──
    panel._spn_g_repeats.setValue(4)
    panel._group_selected()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 1
            and routine.groups[0].end == 2 and routine.groups[0].repeats == 4,
            f"the table's own (0-based) selection becomes the Group "
            f"({routine.groups})")
    r.check(panel._lst_groups.count() == 1
            and "2-3" in panel._lst_groups.item(0).text()
            and "4" in panel._lst_groups.item(0).text(),
            f"…and the list shows it ({panel._lst_groups.item(0).text()!r})")
    r.check(panel._tbl._group_at(1) is routine.groups[0]
            and panel._tbl._group_at(2) is routine.groups[0]
            and panel._tbl._group_at(0) is None,
            "…and the table itself knows which rows are grouped")

    # ── recordings, the same shape, minus a repeat count ──
    panel._record_selected()
    r.check(len(routine.recordings) == 1 and routine.recordings[0].start == 1
            and routine.recordings[0].end == 2,
            f"Mark as recording becomes a Recording over the same selection "
            f"({routine.recordings})")
    r.check(panel._lst_recordings.count() == 1
            and "2-3" in panel._lst_recordings.item(0).text(),
            f"…shown in its own list ({panel._lst_recordings.item(0).text()!r})")
    r.check(panel._tbl._recording_at(1) is routine.recordings[0]
            and panel._tbl._recording_at(2) is routine.recordings[0]
            and panel._tbl._recording_at(0) is None,
            "…and the table knows which rows are being recorded, "
            "independent of the group above")

    # Round-trips through to_dict/from_dict, the same as steps.
    reloaded = Routine.from_dict(routine.to_dict())
    r.check(len(reloaded.groups) == 1 and reloaded.groups[0].repeats == 4,
            "a saved template keeps its repeat group")
    r.check(len(reloaded.recordings) == 1 and reloaded.recordings[0].start == 1
            and reloaded.recordings[0].end == 2,
            "…and its recording bracket")

    # The repeat count stays editable after the group exists.
    from PyQt6.QtWidgets import QInputDialog

    real_get_int = QInputDialog.getInt
    QInputDialog.getInt = staticmethod(lambda *a, **k: (7, True))
    try:
        panel._edit_group_repeats(panel._lst_groups.item(0))
    finally:
        QInputDialog.getInt = real_get_int
    r.check(routine.groups[0].repeats == 7,
            f"double-clicking a group lets its repeat count be changed "
            f"({routine.groups[0].repeats})")

    # CONTROL: cancelling the dialog must leave it alone.
    QInputDialog.getInt = staticmethod(lambda *a, **k: (99, False))
    try:
        panel._edit_group_repeats(panel._lst_groups.item(0))
    finally:
        QInputDialog.getInt = real_get_int
    r.check(routine.groups[0].repeats == 7,
            "control: cancelling the repeat-count dialog changes nothing")

    panel._lst_groups.setCurrentRow(0)
    panel._del_group()
    r.check(routine.groups == [] and panel._lst_groups.count() == 0,
            "Remove selected clears the group from both the routine and the "
            "list")
    r.check(panel._tbl._group_at(1) is None,
            "…and the table stops tinting/badging those rows")

    panel._lst_recordings.setCurrentRow(0)
    panel._del_recording()
    r.check(routine.recordings == [] and panel._lst_recordings.count() == 0,
            "Remove selected does the same for the recording")
    r.check(panel._tbl._recording_at(1) is None,
            "…independently of the group above")

    # Right-click's actions are the same calls the buttons make.
    _select_rows(panel._tbl, 0, 1)
    panel._tbl.group_requested.emit()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 0
            and routine.groups[0].end == 1,
            f"the context menu's Group action reaches the same handler "
            f"({routine.groups})")
    panel._tbl.record_requested.emit()
    r.check(len(routine.recordings) == 1 and routine.recordings[0].start == 0
            and routine.recordings[0].end == 1,
            f"…and so does Mark as recording ({routine.recordings})")
    panel._lst_groups.setCurrentRow(0)
    panel._del_group()
    panel._lst_recordings.setCurrentRow(0)
    panel._del_recording()

    # set_routine (template load) replaces groups AND recordings, not just steps.
    _select_rows(panel._tbl, 0, 1)
    panel._group_selected()
    panel._record_selected()
    other = Routine(steps=[Step(kind="wait"), Step(kind="wait")])
    panel.set_routine(other)
    r.check(panel.settings.groups == [] and panel.settings.recordings == [],
            "loading a template with neither clears the panel's own")
    app.processEvents()


def check_per_edge_routine_is_buildable(r: Report, app) -> None:
    """The operator's per-edge protocol, built the way the UI actually builds
    it — and the bug that shipped without this: `selected_range()` refused
    fewer than 2 rows, so "Mark as recording" could not be applied to the
    single Wait a `trigger` step must be followed by. Every earlier test
    constructed `Recording(start=1, end=1)` straight into the model, so none
    of them touched the panel path that forbade it.

    "100 recordings at FOV 1, each started by an edge, each x seconds" =
    [move, trigger, wait] with the group over the last two and the bracket on
    the wait ALONE.
    """
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="move", label="FOV 1", x_um=0.0,
                                  y_um=0.0, settle_s=0.0),
                             Step(kind="trigger", label="edge"),
                             Step(kind="wait", label="capture", length=5.0,
                                  unit="seconds")])
    panel = SettingsPanel(routine)

    # The repeat group: trigger + wait, so the move happens once.
    _select_rows(panel._tbl, 1, 2)
    panel._spn_g_repeats.setValue(100)
    panel._group_selected()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 1
            and routine.groups[0].end == 2
            and routine.groups[0].repeats == 100,
            f"the trigger+wait pair repeats 100x, leaving the move outside "
            f"({routine.groups})")

    # The bracket: the wait ALONE — this is what used to be impossible.
    panel._tbl.select_row(2)
    r.check(panel._btn_r_add.isEnabled(),
            "the single Wait row can be marked as a recording")
    panel._record_selected()
    r.check(len(routine.recordings) == 1
            and routine.recordings[0].start == 2
            and routine.recordings[0].end == 2,
            f"…producing a one-step Recording over just it "
            f"({routine.recordings})")
    r.check(panel._lst_recordings.count() == 1
            and panel._lst_recordings.item(0).text() == "step 3",
            f"…listed in the singular ({panel._lst_recordings.item(0).text()!r})")

    # The whole point: the trigger step is NOT inside the bracket, so
    # validate() accepts it. (The reverse case is covered in check_validation.)
    panel._cmb_save.setCurrentIndex(panel._cmb_save.findData("per_repeat"))
    built = panel.settings
    r.check(built.save_mode == "per_repeat",
            f"one file per edge is selectable ({built.save_mode})")
    problems = validate(built, FULL_RIG)
    r.check(problems == [],
            f"the assembled per-edge routine validates clean ({problems})")

    # And it survives a save/reload, like any other routine.
    reloaded = Routine.from_dict(built.to_dict())
    r.check(reloaded.steps[1].kind == "trigger"
            and reloaded.groups[0].repeats == 100
            and reloaded.recordings[0].start == reloaded.recordings[0].end == 2,
            "…and round-trips through a saved template intact")


def check_move_row_repaint(r: Report) -> None:
    """A reorder repaints only the rows between src and dest, not the whole
    table (2026-08-27) — `move_row` used to call `reload()`, which repainted
    every row for what is always a contiguous shift of the rows in between.
    """
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="wait", label=c, length=1, unit="frames")
                             for c in "ABCDE"])
    panel = SettingsPanel(routine)
    tbl = panel._tbl

    touched: list[int] = []
    real_paint_row = tbl._paint_row
    tbl._paint_row = lambda row, s: (touched.append(row), real_paint_row(row, s))[1]

    touched.clear()
    tbl.move_row(0, 1)                  # adjacent — only rows 0,1 shifted
    r.check(sorted(set(touched)) == [0, 1],
            f"an adjacent move repaints only the two rows involved, not all "
            f"5 ({sorted(set(touched))})")
    r.check([s.label for s in routine.steps] == list("BACDE"),
            f"…and the reorder itself is still correct ({[s.label for s in routine.steps]})")

    touched.clear()
    tbl.move_row(4, 0)                  # far move — every row in between shifts
    r.check(sorted(set(touched)) == [0, 1, 2, 3, 4],
            f"a move spanning the whole table repaints every row it actually "
            f"shifted ({sorted(set(touched))})")
    r.check([s.label for s in routine.steps] == list("EBACD"),
            f"…correctly ({[s.label for s in routine.steps]})")

    tbl._paint_row = real_paint_row


# ── the template library ──────────────────────────────────────────────────────

def check_templates(r: Report) -> None:
    """A protocol worth running twice survives as a file, not as a config key."""
    from acqApp.routines import templates

    r.check(templates.names() == [],
            f"an empty library lists nothing ({templates.names()})")

    rt = Routine(name="grid 3x3", cycles=2, save_mode="per_repeat",
                 steps=[Step(kind="move", label="a", x_um=100.0),
                        Step(kind="wait", label="b", length=1.5, unit="seconds")])
    path = templates.save(rt)
    r.check(path.exists() and path.name.endswith(templates.SUFFIX),
            f"saving writes one file ({path.name})")
    r.check(templates.names() == ["grid 3x3"],
            f"…which the library then lists ({templates.names()})")

    back = templates.load("grid 3x3")
    r.check([vars(x) for x in back.steps] == [vars(x) for x in rt.steps],
            "every field of every step comes back")
    r.check(back.cycles == 2 and back.save_mode == "per_repeat"
            and back.name == "grid 3x3",
            f"…and so do the cycles and the save mode "
            f"({back.cycles}, {back.save_mode})")

    # A name is not a path. The routine name is the operator's free text and
    # goes straight at the filesystem.
    evil = "../../evil/name"
    r.check(templates.path_for(evil).parent == templates.DIR
            and templates.path_for("   ").name.startswith("routine"),
            f"a name is not a path — it cannot escape the folder "
            f"({templates.path_for(evil).name!r})")

    templates.save(Routine(name="b"), "b")
    templates.save(Routine(name="A"), "A")
    r.check(templates.names() == ["A", "b", "grid 3x3"],
            f"the library is sorted the way a human reads it "
            f"({templates.names()})")

    # A hand-edited or stale (pre-redesign) template must not stop the app:
    # from_dict migrates what it can, and validate() refuses the rest at
    # Start. Two old-format steps, and one entry that is not a dict at all.
    templates.path_for("stale").write_text(
        '{"name": "stale", "cycles": "lots", "save_mode": "nope",'
        ' "steps": [{"label": "ok", "length": 5, "unit": "seconds"},'
        ' {"gone": 1}, "not a step"]}', encoding="utf-8")
    stale = templates.load("stale")
    r.check(len(stale.steps) == 2 and stale.cycles == 1
            and stale.save_mode == "single",
            f"a stale template migrates both old-format steps and drops "
            f"the one that was not a dict at all "
            f"({len(stale.steps)} step(s), cycles={stale.cycles})")
    r.check(len(stale.recordings) == 2,
            "…each migrated step wrapped in its own auto-generated Recording")

    templates.delete("b")
    r.check("b" not in templates.names() and "A" in templates.names(),
            f"deleting removes one and only one ({templates.names()})")
    templates.delete("b")               # a second delete must not raise


def check_panel_tracker(r: Report, app) -> None:
    """The things the operator asked the panel for, at the widget level.

    Where the routine is, reordering that is not nine button presses, what the
    protocol will cost before it starts, a kind picker on +Step, and a library
    of protocols.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from acqApp.routines import templates
    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import RUNNING

    routine = Routine(name="tracked",
                      steps=[Step(kind="wait", label="one", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="two", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="three", length=100,
                                  unit="frames")])
    panel = SettingsPanel(routine)
    tbl = panel._tbl

    # ── 1. where the routine is ──
    r.check(panel._bar.isHidden(),
            "before a run there is no progress bar — an empty one reads as a "
            "stalled run")
    values: list[int] = []
    real = panel._bar.setValue
    panel._bar.setValue = lambda v: (values.append(v), real(v))[1]
    panel.set_progress(0.25, "1 s elapsed")
    r.check(not panel._bar.isHidden() and values == [250],
            f"…and starting one shows it at where it is ({values})")
    for _ in range(30):
        panel.set_progress(0.25, "1 s elapsed")
    r.check(values == [250],
            f"30 ticks at the same place repaint the bar once ({len(values)})")
    panel.set_progress(0.5, "2 s elapsed")
    r.check(values == [250, 500] and panel._lbl_eta.text() == "2 s elapsed",
            f"…and it follows the routine when it moves ({values})")
    panel.set_progress(None)
    r.check(panel._bar.isHidden() and panel._lbl_eta.isHidden(),
            "a routine that is no longer running puts both away")

    panel.set_state(Phase.RUNNING, "step 2/3", 1)
    heads = [tbl.verticalHeaderItem(i).text() for i in range(tbl.rowCount())]
    r.check(heads == ["1", RUNNING, "3"],
            f"the running step is marked in the row header ({heads})")
    panel.set_state(Phase.DONE, "finished", None)
    heads = [tbl.verticalHeaderItem(i).text() for i in range(tbl.rowCount())]
    r.check(heads == ["1", "2", "3"],
            f"…and the headers are the step order again once it is over "
            f"({heads})")

    # ── 2. reordering ──
    tbl.move_row(0, 2)
    r.check([x.label for x in routine.steps] == ["two", "three", "one"],
            f"a step moves straight to a row, not one press at a time "
            f"({[x.label for x in routine.steps]})")
    r.check(tbl.selected_row() == 2,
            "…and the selection follows the step it moved")
    QTest.keyClick(tbl, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    r.check([x.label for x in routine.steps] == ["two", "one", "three"],
            f"Ctrl+Up moves it from the keyboard "
            f"({[x.label for x in routine.steps]})")
    before = [x.label for x in routine.steps]
    QTest.keyClick(tbl, Qt.Key.Key_Up)
    r.check([x.label for x in routine.steps] == before,
            f"control: the arrow key on its own still just moves the cursor "
            f"({[x.label for x in routine.steps]})")
    r.check(not tbl.move_row(0, -5) and not tbl.move_row(9, 0)
            and [x.label for x in routine.steps] == before,
            "a move off either end of the list does nothing")
    r.check(tbl._steps is panel.settings.steps,
            "the table reorders the routine's own step list, not a copy of it")

    # ── 3. what it will cost ──
    panel.set_frame_rate(None)
    r.check("at least" in panel._lbl_summary.text()
            and "300 frames" in panel._lbl_summary.text(),
            f"with no camera loaded the summary counts frames as frames "
            f"({panel._lbl_summary.text()!r})")
    panel.set_frame_rate(100.0)
    text = panel._lbl_summary.text()
    r.check("about" in text and "100 Hz" in text and "frames" not in text,
            f"…and a loaded camera turns the whole protocol into a duration, "
            f"naming the rate it used ({text!r})")
    r.check(panel.frame_rate == 100.0,
            "the panel keeps the rate, so the run readout does not re-ask the "
            "camera 30 times a second")

    # ── the start trigger ──
    r.check(panel.settings.start_trigger == "manual",
            f"a routine defaults to a manual start trigger "
            f"({panel.settings.start_trigger!r})")
    idx = panel._cmb_trigger.findData("ttl")
    panel._cmb_trigger.setCurrentIndex(idx)
    r.check(panel.settings.start_trigger == "ttl",
            "picking TTL in the combo sets it on the routine")
    reloaded = SettingsPanel(Routine.from_dict(panel.settings.to_dict()))
    r.check(reloaded._cmb_trigger.currentData() == "ttl",
            "…and it survives a save/reload round trip through the panel")
    panel._cmb_trigger.setCurrentIndex(panel._cmb_trigger.findData("manual"))

    # ── the +Step kind picker ──
    r.check(panel._cmb_new_kind.currentData() == "wait",
            "…+Step defaults to appending a Wait step")
    panel._cmb_new_kind.setCurrentIndex(KINDS.index("move"))
    n_before = len(routine.steps)
    panel._add_step()
    r.check(len(routine.steps) == n_before + 1
            and routine.steps[-1].kind == "move",
            f"+Step appends a step of whatever kind is picked ({routine.steps[-1].kind!r})")
    panel._del_step()          # tidy up via the table, so selection stays
                                # sane for what follows — a direct list.pop()
                                # leaves the table's selection dangling on a
                                # row that no longer exists, and _del_step()
                                # (used below) silently no-ops with nothing
                                # selected

    # ── 4. the library ──
    panel.save_template("saved one")
    r.check("saved one" in templates.names(),
            f"Save as… writes the protocol to the library "
            f"({templates.names()})")
    r.check(panel._cmb_tpl.currentText() == "saved one",
            "…and selects it, so Load next means what was just saved")

    panel._del_step()
    panel._del_step()
    r.check(len(routine.steps) == 1, "the protocol is then edited down")
    panel.load_template("saved one")
    r.check([x.label for x in panel.settings.steps] == before,
            f"Load puts the saved protocol back, in its saved order "
            f"({[x.label for x in panel.settings.steps]})")
    r.check(tbl._steps is panel.settings.steps and tbl.rowCount() == 3,
            "…into the same list the table edits — a loaded template is the "
            "routine, not a second one")

    emitted: list = []
    panel.settings_changed.connect(emitted.append)
    panel.load_template("saved one")
    r.check(len(emitted) == 1,
            f"loading persists the routine ONCE, not once per widget it moved "
            f"({len(emitted)})")

    said: list = []
    panel.status_message.connect(said.append)
    panel.load_template("no such template")
    r.check(said and "could not load" in said[-1],
            f"control: loading a missing template says so ({said})")

    panel._cmb_tpl.setCurrentText("saved one")
    panel._on_delete_template()
    r.check("saved one" not in templates.names(),
            f"Delete removes it from the library ({templates.names()})")
    r.check(len(panel.settings.steps) == 3,
            "…and leaves the protocol being edited alone")
    app.processEvents()


# ── the whole app ─────────────────────────────────────────────────────────────

def check_app(r: Report, app, tmp) -> None:
    """The wiring: the panel refuses, the engine drives real adapters, and the
    recording boundaries reach the file on the shared clock.

    Everything above this runs on fakes. This runs the actual window in mock
    mode, because the seam that matters is `ModuleHost.stage_target` /
    `pattern_target` — pooled by the window so the routine never imports the
    stage, which is exactly the kind of link a unit test cannot see.
    """
    import h5py

    out = tmp / "routine_rec"
    win = make_window({"voltage_cam", "stage", "dmd", "routines"})
    mod = {m.key: m for m in win._modules}
    adapter, panel = mod["routines"], mod["routines"].panel

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_mouse_id.setText("routine")
    win._save_panel._ed_template.setText("{mouse_id}_{date}_{time}")
    win._save_panel._on_edited()

    r.check(win.stage_target() is mod["stage"],
            "the window offers the loaded stage as a routine target, even "
            "before Live view starts")
    r.check(win.pattern_target() is mod["dmd"],
            "…and the loaded DMD as a pattern target")
    win._btn_run.setChecked(True)

    ctrl0 = mod["stage"].controller
    real_stop_all = ctrl0.stop_all

    def boom() -> None:
        raise RuntimeError("serial link gone")

    ctrl0.stop_all = boom
    try:
        win.stage_target().stop_motion()
        r.check(True, "stop_motion() survives a controller that raises")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped stop_motion: {e}")
    try:
        ctrl0.stop_all()
        r.check(False, "control: the raw controller call must still raise")
    except RuntimeError:
        r.check(True, "control: the raw controller call does raise")
    ctrl0.stop_all = real_stop_all

    (lo_x, hi_x), (lo_y, hi_y) = win.stage_target().limits_um()
    inside = lo_x + (hi_x - lo_x) * 0.37
    inside_y = lo_y + (hi_y - lo_y) * 0.62

    # ── the atomic protocol: two "old composite step"-sized recordings ──
    app_pattern = tmp / "app.png"
    app_pattern.write_bytes(b"x")
    panel._r.steps = [
        Step(kind="move", label="one", x_um=inside, settle_s=0.0),
        Step(kind="wait", label="frames-wait", length=5, unit="frames"),
        Step(kind="move", label="two", x_um=inside, y_um=inside_y,
             settle_s=0.05),
        Step(kind="display", label="show", pattern=str(app_pattern)),
        Step(kind="wait", label="seconds-wait", length=0.30, unit="seconds"),
    ]
    panel._r.recordings = [Recording(start=0, end=1), Recording(start=2, end=4)]
    panel._reload_table()

    # ── Start opens the recording it needs ──
    r.check(not win._btn_rec.isChecked(), "fixture: not recording yet")
    adapter._start()
    r.check(adapter._engine is not None,
            "Start runs the routine without the operator pressing Record first")
    r.check(win._btn_rec.isChecked(), "…by opening the recording itself")
    r.check(adapter._own_rec, "…and it knows that recording is its own")
    adapter._abort()
    r.check(not win._btn_rec.isChecked(),
            "ending the routine stops the recording it started")

    # CONTROL: a recording the OPERATOR started is not the routine's to stop.
    win._btn_rec.setChecked(True)
    adapter._start()
    r.check(adapter._engine is not None and not adapter._own_rec,
            "control: a recording already running is not adopted as its own")
    adapter._abort()
    r.check(win._btn_rec.isChecked(),
            "control: …so ending the routine leaves that one running")
    win._btn_rec.setChecked(False)              # clean slate for the real run
    pump(app, 0.1)

    panel._r.steps[0].x_um = hi_x + 10_000.0
    r.check(any("soft limits" in p
                for p in validate(panel.settings, adapter._rig())),
            "…and an out-of-limits target is refused before the run, not at step 7")
    panel._r.steps[0].x_um = inside

    # ── the real thing ──
    moved: list[tuple] = []
    lit: list[bool] = []
    ctrl = mod["stage"].controller
    real_move = ctrl.move_to_um
    ctrl.move_to_um = lambda which, um: (moved.append((which, um)),
                                         real_move(which, um))[1]
    real_light = mod["dmd"].set_light
    mod["dmd"].set_light = lambda on: (lit.append(bool(on)), real_light(on))[1]

    win._btn_rec.setChecked(True)
    path = win._rec_path
    if not r.check(path is not None, "recording started"):
        return

    adapter._start()
    if not r.check(adapter._engine is not None,
                   "the routine starts against the operator's own recording"):
        return

    r.check("routine" in adapter.busy_reason().lower(),
            f"the adapter declares itself busy while a routine runs "
            f"({adapter.busy_reason()!r})")
    try:
        win.set_modules({"voltage_cam"})
        r.check(False, "set_modules must be refused while a routine runs")
    except RuntimeError as e:
        r.check(True, f"set_modules is refused while a routine runs ({e})")

    for _ in range(200):
        win._display_tick()
        pump(app, 0.02)
        if adapter._engine.phase == Phase.DONE:
            break

    eng = adapter._engine
    phase, runs, filed, steps_done = (eng.phase, list(eng.runs), adapter._filed,
                                      eng.steps_done())
    win._btn_rec.setChecked(False)
    win._btn_run.setChecked(False)
    win.close()
    pump(app, 0.2)

    r.check(phase == Phase.DONE, f"the routine ran to the end (phase={phase})")
    r.check(len(runs) == 2 and not any(x.interrupted for x in runs),
            f"both recording brackets completed cleanly ({len(runs)} runs)")
    r.check(steps_done == 5,
            f"all five atomic steps completed ({steps_done})")
    r.check(("x", inside) in moved and ("y", inside_y) in moved,
            f"the stage really was commanded through the host ({moved})")
    r.check(sum(1 for a, _ in moved if a == "y") == 1,
            f"…and an axis left blank in a step is left where it is "
            f"({[a for a, _ in moved]})")
    r.check(lit and lit[-1] is False and True in lit,
            f"the light went on for the projecting step and off after ({lit})")

    frames_run = runs[0]
    r.check(frames_run.frames is not None and frames_run.frames >= 5,
            f"the first Recording (move + a 5-frame wait) counted frames "
            f"that reached the FILE ({frames_run.frames})")
    r.check(filed == 4, f"four recording boundaries were filed (got {filed})")

    mod["stage"].busy_reason = lambda: "the stage says no"
    try:
        win.set_modules({"voltage_cam"})
        r.check(False, "control: a busy adapter must block set_modules")
    except RuntimeError as e:
        r.check("stage says no" in str(e),
                f"control: the window really asks every adapter ({e})")
    mod["stage"].busy_reason = lambda: ""

    with h5py.File(path, "r") as f:
        r.check("routine" in f, f"/routine is in the file (has {list(f)})")
        g = f["routine"]
        ts = [float(v) for v in g["timestamps"][:]]
        vals = [float(v) for v in g["values"][:]]
        r.check(all(b >= a for a, b in zip(ts, ts[1:])) and ts[0] >= 0.0,
                f"…stamped on the session clock, in order "
                f"({[round(v, 2) for v in ts]})")
        r.check(len(vals) == 4,
                f"one entry per boundary, opening and closing each Recording "
                f"({vals})")
        r.check(vals[0] > 0 and vals[1] < 0,
                f"…the sign says which edge it is, keyed on `region` "
                f"({vals[:2]})")
        a = dict(f.attrs)
        r.check(a.get("routine_started") in (True, 1, "True"),
                "the file says a routine actually ran")
        r.check(int(a.get("routine_steps_done", 0)) == 5,
                f"…and how many ATOMIC steps finished "
                f"({a.get('routine_steps_done')})")
        r.check(int(a.get("routine_recordings_interrupted", -1)) == 0,
                "…and that no recording bracket was interrupted")
        r.check(int(a.get("routine_n_steps", 0)) == 5
                and "one" in str(a.get("routine_steps", "")),
                "…and carries the protocol itself, which nothing else records")
        run_attrs = json.loads(str(a.get("routine_runs", "[]")))
        r.check(len(run_attrs) == 2
                and [x["routine_recording_region"] for x in run_attrs] == [0, 1],
                f"…and every RECORDING execution, not just the counts "
                f"({len(run_attrs)})")
        r.check(all(x["routine_recording_interrupted"] is False
                   for x in run_attrs)
                and (run_attrs[1]["routine_recording_t0"]
                    > run_attrs[0]["routine_recording_t0"]),
                "…each with its own t0 on the shared clock and its fault flag")


def check_file_rolling(r: Report, app, tmp) -> None:
    """save_mode "per_repeat"/"per_group" actually roll to new files through
    the real MainWindow — `main.py`'s `roll_recording()`, the one path a
    fake rig cannot exercise (adapters/routines.py's `_needs_roll`/
    `_roll_for` are only proven end-to-end here)."""
    out = tmp / "routine_roll"
    win = make_window({"stage", "routines"})
    mod = {m.key: m for m in win._modules}
    adapter, panel = mod["routines"], mod["routines"].panel

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_mouse_id.setText("roll")
    win._save_panel._ed_template.setText("{mouse_id}_{date}_{time}")
    win._save_panel._on_edited()

    def run_to_done(steps, groups, recordings, save_mode) -> int:
        """Run one routine to completion, -> how many NEW .h5 files appeared."""
        before = set(out.rglob("*.h5"))
        panel._r.steps = steps
        panel._r.groups = groups
        panel._r.recordings = recordings
        panel._r.cycles = 1
        # Not `panel._r.save_mode = ...` directly — the `settings` property
        # `_start()` reads overwrites it from this combo box on every read.
        panel._cmb_save.setCurrentIndex(panel._cmb_save.findData(save_mode))
        panel._reload_table()
        adapter._start()
        if not r.check(adapter._engine is not None,
                       f"[{save_mode}] the routine starts "
                       f"({panel._lbl_state.text() if adapter._engine is None else ''})"):
            return 0
        for _ in range(400):
            win._display_tick()
            pump(app, 0.01)
            if adapter._engine.phase == Phase.DONE:
                break
        r.check(adapter._engine.phase == Phase.DONE,
                f"[{save_mode}] the routine ran to the end "
                f"(phase={adapter._engine.phase}, fault={adapter._engine.fault!r})")
        return len(set(out.rglob("*.h5")) - before)

    # One Recording spanning a 3x-repeated 2-step group -> 3 RecordingRuns
    # (routines/engine.py: a Recording across a repeated Group yields one
    # run per repeat) -> per_repeat rolls a file for each.
    n = run_to_done(
        [Step(kind="move", label="m", x_um=10.0, settle_s=0.0),
         Step(kind="wait", label="w", length=0.02, unit="seconds")],
        [Group(start=0, end=1, repeats=3)], [Recording(start=0, end=1)],
        "per_repeat")
    r.check(n == 3, f"per_repeat: one file per repeat of the group ({n})")

    # Two DIFFERENT groups, each repeated twice, each with its OWN Recording
    # -> per_group shares a file across repeats of the SAME group and rolls
    # only when the covering group actually changes -> 2 files (one per
    # group), not 4 (one per repeat) — the whole point of the coarser mode.
    multi_group_steps = [
        Step(kind="move", label="a", x_um=10.0, settle_s=0.0),
        Step(kind="wait", label="wa", length=0.02, unit="seconds"),
        Step(kind="move", label="b", x_um=20.0, settle_s=0.0),
        Step(kind="wait", label="wb", length=0.02, unit="seconds"),
    ]
    multi_group_groups = [Group(start=0, end=1, repeats=2),
                          Group(start=2, end=3, repeats=2)]
    multi_group_recordings = [Recording(start=0, end=1),
                              Recording(start=2, end=3)]
    n2 = run_to_done(list(multi_group_steps), multi_group_groups,
                     multi_group_recordings, "per_group")
    r.check(n2 == 2,
            f"per_group: repeats of the same group share a file, only "
            f"moving to the other one rolls ({n2})")

    # CONTROL: save_mode="single" over the SAME multi-group protocol is the
    # behavior every one of these modes departs from — exactly one file.
    n3 = run_to_done(list(multi_group_steps), multi_group_groups,
                     multi_group_recordings, "single")
    r.check(n3 == 1,
            f"control: single mode never rolls, over the identical protocol "
            f"({n3})")

    win.close()
    pump(app, 0.2)


def main() -> int:
    r = Report("routines")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_routines_"))
    try:
        check_validation(r, tmp)
        check_migration(r)
        check_units(r)
        check_order(r, tmp)
        check_move_timeout(r)
        check_pause_keeps_data(r, tmp)
        check_skip(r)
        check_setup_failure(r, tmp)
        check_frames_vanish(r)
        check_cycles_and_attrs(r)
        check_groups(r)
        check_group_repeat_at(r)
        check_recording_repeats(r)
        check_transitions(r)
        check_ttl_start_trigger(r)
        check_trigger_step(r)
        check_arm_camera_trigger(r)
        check_estimate(r)
        check_progress(r)
        # The window persists as a side effect of ordinary use, so isolate
        # first — an unisolated run overwrites the operator's save folder, and
        # would save into and delete from the operator's template library.
        state = isolate_user_state()
        try:
            sys.argv = ["main.py", "--mock"]
            app = qt_app()
            check_templates(r)
            check_panel_repaint(r, app)
            check_step_table(r, app, tmp)
            check_group_panel(r, app)
            check_per_edge_routine_is_buildable(r, app)
            check_move_row_repaint(r)
            check_panel_tracker(r, app)
            check_app(r, app, state)
            check_file_rolling(r, app, state)
        finally:
            import shutil
            shutil.rmtree(state, ignore_errors=True)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    from acqApp.console import enable_safe_console
    enable_safe_console()
    sys.exit(main())
