"""Experiment routines: the protocol (validation, groups, units) and the
engine (order, pauses, cycles, transitions, estimates).

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_routines.py [-q] [--part NAME]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from dataclasses import replace

from _harness import Report, run_parts
from _routine_rig import FULL_RIG, Z_RIG, DT, FakeRig, drive, ticks

from acqApp.routines.engine import Phase, RoutineEngine, RoutineError
from acqApp.routines.settings import (
    Group, Recording, RigLimits, Routine, Step, expand, play_order,
    recording_run_ids, validate,
)


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

    r.check(validate(Routine(steps=[Step(kind="wait", length=1,
                                        unit="seconds")]), FULL_RIG) == [],
            "control: every routine arms on Start, and is accepted once a "
            "camera is loaded to receive that arm")

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
         FULL_RIG, "isn't a file"),
        ("an empty routine", Routine(steps=[]), FULL_RIG, "no steps"),
        ("an unrecognised kind",
         Routine(steps=[Step(kind="teleport")]), FULL_RIG, "unknown kind"),
        # A trigger step is only observable as frames appearing, so with no
        # camera nothing could ever end it.
        ("a trigger step with no camera loaded",
         Routine(steps=[Step(kind="trigger")]), RigLimits(), "no camera"),
        ("a record step of zero length",
         Routine(steps=[Step(kind="record", length=0, unit="seconds")]),
         FULL_RIG, "above zero"),
        ("a record step measured in frames with no camera",
         Routine(steps=[Step(kind="record", length=5, unit="frames")]),
         RigLimits(), "no camera"),
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

    # The control for the two Z refusal cases above.
    r.check(validate(Routine(steps=[Step(kind="move", x_um=10.0, y_um=10.0,
                                        z_um=50.0)]), Z_RIG) == [],
            "control: an (x, y, z) move within every axis's own limits, on "
            "a rig with a Z stage, is accepted")

    # Persistence round trip: the panel saves this into acqapp_local.json.
    src = Routine(name="grid", cycles=3, save_mode="per_repeat",
                  steps=[Step(kind="move", label="a", x_um=1.0, settle_s=0.1),
                         Step(kind="record", label="b", length=5, unit="frames"),
                         Step(kind="display", label="c", pattern="p.png"),
                         Step(kind="puff", label="d")],
                  groups=[Group(start=1, end=2, repeats=2)])
    back = Routine.from_dict(src.to_dict())
    r.check(back == src, "a routine survives the JSON round trip unchanged")
    r.check(not hasattr(Routine.from_dict({"start_trigger": "ttl"}),
                        "start_trigger"),
            "an old file's start_trigger key is dropped — every routine "
            "arms on Start")
    r.check(Routine.from_dict({"save_mode": "per_step"}).save_mode
            == "per_repeat",
            "an old 'per_step' save mode migrates to per_repeat, not "
            "silently to single")
    r.check(Routine.from_dict(
        {"steps": [{"kind": "wait", "gone": 1, "label": "x"}],
         "cycles": "nonsense"}).steps[0].label == "x",
            "a stale saved step drops unknown keys rather than raising")


def check_groups(r: Report) -> None:
    """A repeat group re-runs a contiguous range in place, inside `cycles`:
    [A, B, C] with B..C x3 plays A, B, C, B, C, B, C."""
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

    # ── recordings are derived: one per record step ──
    two = Routine(steps=[Step(kind="record"), Step(kind="wait"),
                         Step(kind="record")])
    r.check(two.recordings == [Recording(start=0, end=0),
                               Recording(start=2, end=2)],
            "each record step is its own one-step recording; a wait is not")
    r.check(validate(two, FULL_RIG) == [],
            "control: a routine with record steps is accepted")

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
    """`group_repeat_at` numbers each pass of a group (rig, 2026-09-17: the
    panel showed "step 1/2" identically on every repeat)."""
    from acqApp.routines.settings import group_repeat_at

    routine = Routine(steps=[Step(kind="trigger"),
                             Step(kind="record", length=8.0, unit="seconds")],
                      groups=[Group(start=0, end=1, repeats=3)])
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

    # The operator's Move-then-repeat shape.
    with_move = Routine(
        steps=[Step(kind="move", label="FOV"), Step(kind="trigger"),
              Step(kind="wait", length=1.0, unit="seconds")],
        groups=[Group(start=1, end=2, repeats=2)])
    rep2 = group_repeat_at(with_move, play_order(with_move))
    r.check(rep2 == [None, (1, 2), (1, 2), (2, 2), (2, 2)],
            f"the ungrouped Move stays None; only the repeated pair is "
            f"numbered ({rep2})")


def check_recording_repeats(r: Report) -> None:
    """A Record step in a repeated Group, or across cycles, opens one
    RecordingRun per pass, never one merged run (the engine keys on
    `(cycle, serial)`)."""
    # ── across a repeated Group ──
    routine = Routine(steps=[Step(kind="wait", label="A", length=0.05,
                                  unit="seconds"),
                             Step(kind="record", label="B", length=0.05,
                                  unit="seconds")],
                      groups=[Group(start=1, end=1, repeats=3)])
    order = play_order(routine)
    r.check(order == [0, 1, 1, 1], f"fixture: A once, B x3 ({order})")
    ids = recording_run_ids(routine, order)
    r.check(ids == [None, 0, 1, 2],
            f"a NEW serial every time the order doubles back onto the "
            f"record step, not one for the whole repeat ({ids})")

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
    routine2 = Routine(steps=[Step(kind="record", label="A", length=0.05,
                                   unit="seconds")],
                       cycles=2)
    order2 = play_order(routine2)
    ids2 = recording_run_ids(routine2, order2)
    r.check(ids2 == [0],
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
    """A step ends on the unit it was GIVEN. The camera runs below nominal
    rate, so an engine converting seconds at the nominal rate would show."""
    nominal, actual = 106.0, 97.0
    rig = FakeRig(hz=actual)
    routine = Routine(steps=[Step(kind="record", label="A", length=100,
                                  unit="frames"),
                             Step(kind="record", label="B", length=1.5,
                                  unit="seconds")])
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
    """Move: blank, move, settle, restore what Display last lit. Display:
    pattern, then light. Wait and Puff: neither."""
    pattern = tmp / "bar.png"
    pattern.write_bytes(b"x")

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

    # A Move step with no target is a pure settle.
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
    r.check(eng.phase == Phase.PAUSED and "didn't arrive" in eng.fault,
            f"a stage that never arrives pauses the routine ({eng.fault!r})")
    r.check(("stop_motion",) in rig.log, "…and motion is stopped")
    r.check(not rig.lit, "…and the light is off")
    r.check(rig.t < 10.0, "…without running to the test's own limit")


# ── a fault pauses; the partial data is kept ──────────────────────────────────

def check_pause_keeps_data(r: Report, tmp: Path) -> None:
    """A pause stops everything but capture and keeps the frames; resume
    opens a FRESH recording run, never the one the pause closed."""
    pattern = tmp / "pause.png"
    pattern.write_bytes(b"x")
    rig = FakeRig()
    routine = Routine(steps=[Step(kind="display", label="lit",
                                  pattern=str(pattern)),
                             Step(kind="record", label="A", length=1.0,
                                  unit="seconds"),
                             Step(kind="record", label="B", length=1.0,
                                  unit="seconds")])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: (e.phase == Phase.RUNNING
                                     and e.step is not None
                                     and e.step.kind == "record" and rig.t > 0.4))
    r.check(eng.phase == Phase.RUNNING and eng.step.kind == "record" and rig.lit,
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
    routine = Routine(steps=[Step(kind="record", label="A", length=1.0,
                                  unit="seconds"),
                             Step(kind="record", label="B", length=0.2,
                                  unit="seconds")])
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
    sample; a failing Move/Display opens no run (only Record steps record).
    """
    pattern = tmp / "setup.png"
    pattern.write_bytes(b"x")

    rig = FakeRig()
    rig.fail_move = True
    routine = Routine(steps=[Step(kind="move", x_um=10.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    r.check(eng.phase == Phase.PAUSED and "setup failed" in eng.fault,
            f"a failing move pauses at setup ({eng.fault!r})")
    r.check(("light", True) not in rig.log,
            "the light was never turned on for a step that never started")
    r.check(eng.runs == [] and rig.begun == [],
            "…and no recording opened, since a move is never inside one")

    # The projector failing at the top of Display is the same shape.
    rig2 = FakeRig()
    rig2.fail_light = True
    e2 = RoutineEngine(Routine(steps=[Step(kind="display", pattern=str(pattern))]),
                       rig2.hooks())
    e2.start()
    r.check(e2.phase == Phase.PAUSED and "setup failed" in e2.fault,
            f"a projector that will not light pauses the step ({e2.fault!r})")
    r.check(e2.runs == [], "…and opens no recording either")

    # A recording that cannot open its FILE pauses before the step runs.
    rig3 = FakeRig()
    rig3.fail_begin = True
    e3 = RoutineEngine(Routine(steps=[Step(kind="record", length=0.05,
                                           unit="seconds")]), rig3.hooks())
    e3.start()
    r.check(e3.phase == Phase.PAUSED
            and "recording couldn't start" in e3.fault,
            f"a recording that fails to OPEN pauses with its own message "
            f"({e3.fault!r})")
    r.check(e3.runs == [], "…and no run was recorded")

    # A phase check here once confused the failed resume with this one and
    # stuck forever.
    rig3.fail_begin = False
    e3.resume()
    r.check(e3.phase == Phase.RUNNING and len(rig3.begun) == 1,
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
    ticks(eng, rig)
    r.check(eng.phase == Phase.PAUSED and "frame count" in eng.fault,
            f"a frames step whose counter vanishes pauses ({eng.fault!r})")


# ── cycles, and the per-repeat file guarantee ─────────────────────────────────

def check_cycles_and_attrs(r: Report) -> None:
    """Cycles run in order, and every recording's attrs put it back on one
    clock. The engine never reads `save_mode`; rolling itself is
    check_file_rolling's."""
    rig = FakeRig()
    routine = Routine(name="grid", cycles=3, save_mode="per_repeat",
                      steps=[Step(kind="record", label="A", length=0.10,
                                  unit="seconds"),
                             Step(kind="record", label="B", length=0.10,
                                  unit="seconds")])
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

    # Files that each restart from zero could not be related afterwards.
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
    """`start(trigger="ttl")` arms, and step 1 begins only on a frame the
    camera did not have at arm time (the fake's `hz` going 0 -> 100)."""
    rig = FakeRig(hz=0.0)          # frozen: the camera has not been pulsed
    routine = Routine(steps=[Step(kind="move", label="A", x_um=5.0,
                                  settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start(trigger="ttl")
    r.check(eng.phase == Phase.ARMED and eng.running,
            f"start(trigger='ttl') arms rather than moving right away "
            f"({eng.phase})")
    r.check(rig.log == [], "…and nothing has actuated yet")

    ticks(eng, rig, 20)
    r.check(eng.phase == Phase.ARMED,
            "with no pulse — the camera's frame count never moves — it "
            "stays armed rather than timing out")

    rig.hz = 100.0                  # the pulse
    drive(eng, rig, until=lambda e: e.phase != Phase.ARMED)
    r.check(eng.phase == Phase.RUNNING,
            f"the first frame past the baseline starts step 1 ({eng.phase})")
    r.check(rig.log and rig.log[0] == ("light", False),
            "…the move's own blank is the first thing it does, the same way "
            "a manual start begins a step")

    rig2 = FakeRig(hz=0.0)
    eng2 = RoutineEngine(routine, rig2.hooks())
    eng2.start(trigger="ttl")
    eng2.abort()
    r.check(eng2.phase == Phase.DONE and eng2.runs == [],
            "aborting while armed ends the routine with no run ever opened")

    rig3 = FakeRig(hz=0.0)
    eng3 = RoutineEngine(routine, rig3.hooks())
    eng3.start(trigger="ttl")
    rig3.frames_running = False
    ticks(eng3, rig3)
    r.check(eng3.phase == Phase.PAUSED and "frame count" in eng3.fault,
            f"the camera going away while armed pauses ({eng3.fault!r})")


# ── pre-redesign templates auto-migrate ───────────────────────────────────────

def check_migration(r: Report) -> None:
    """A pre-redesign blob (composite steps, no "kind" key) migrates each old
    step into the atomic steps it implied, and remaps old Group ranges onto
    the expanded indices."""

    # a) a move+length composite -> move, record -> one FOV step.
    old_move = {"steps": [{"x_um": 10.0, "y_um": 20.0, "fov": "f1",
                          "settle_s": 0.4, "length": 50, "unit": "frames"}]}
    rm = Routine.from_dict(old_move)
    r.check(len(rm.steps) == 1 and rm.steps[0].kind == "fov"
            and rm.steps[0].x_um == 10.0 and rm.steps[0].y_um == 20.0
            and rm.steps[0].fov == "f1" and rm.steps[0].settle_s == 0.4,
            f"the move keeps its target, FOV name and settle, as an FOV step "
            f"({rm.steps})")
    r.check(rm.steps[0].length == 50 and rm.steps[0].unit == "frames"
            and not rm.steps[0].pattern,
            f"…capturing the old length/unit, unlit ({rm.steps[0]})")
    eng, _rows = expand(rm)
    r.check(eng.recordings == [Recording(start=1, end=1)],
            "…which still runs as a Record — an old step always captured")

    # b) a pattern-only composite -> a display step, then a record step.
    old_pat = {"steps": [{"pattern": "p.png", "length": 10, "unit": "seconds"}]}
    rp = Routine.from_dict(old_pat)
    r.check([s.kind for s in rp.steps] == ["display", "record"],
            f"a pattern step becomes display then record ({[s.kind for s in rp.steps]})")
    r.check(rp.steps[0].pattern == "p.png" and rp.steps[1].length == 10,
            "…keeping the pattern path and the record length")

    # c) seconds + puff_interval_s: exact Record+Puff chunks.
    old_puff_s = {"steps": [{"length": 5.0, "unit": "seconds",
                            "puff_interval_s": 2.0, "label": "puffed"}]}
    rs = Routine.from_dict(old_puff_s)
    kinds = [s.kind for s in rs.steps]
    r.check(kinds == ["record", "puff", "record", "puff", "record"],
            f"a seconds step with a puff interval interleaves exact chunks "
            f"({kinds})")
    lens = [s.length for s in rs.steps if s.kind == "record"]
    r.check(lens == [2.0, 2.0, 1.0],
            f"…summing back to the original 5.0 s ({lens})")
    r.check(rs.steps[0].label == "puffed"
            and all(s.label == "" for s in rs.steps[2::2]),
            "…the label stays on the FIRST chunk only")
    r.check(len(rs.recordings) == 3,
            "…each record chunk is its own recording")

    # d) frames + puff_interval_s: one trailing Puff (lossy; no frame rate).
    old_puff_f = {"steps": [{"length": 100, "unit": "frames",
                            "puff_interval_s": 1.0}]}
    rf = Routine.from_dict(old_puff_f)
    r.check([s.kind for s in rf.steps] == ["record", "puff"],
            f"a frames step with a puff interval falls back to one trailing "
            f"puff ({[s.kind for s in rf.steps]})")

    # e) old Group ranges remap onto the expanded indices.
    old_grouped = {
        "steps": [
            {"x_um": 1.0, "length": 1, "unit": "seconds"},        # move, rec
            {"pattern": "q.png", "length": 1, "unit": "seconds"}, # disp, rec
            {"length": 1, "unit": "seconds"},                     # rec
        ],
        "groups": [{"start": 1, "end": 2, "repeats": 2}],
    }
    rg = Routine.from_dict(old_grouped)
    r.check([s.kind for s in rg.steps] == ["fov", "display", "record",
                                            "record"],
            f"three old steps -> move+record folded to an FOV step, then "
            f"display, record, record ({[s.kind for s in rg.steps]})")
    r.check(len(rg.groups) == 1 and rg.groups[0].start == 1
            and rg.groups[0].end == 3 and rg.groups[0].repeats == 2,
            f"the group's OLD range (steps 1-2) remaps to the steps those old "
            f"positions became (1-3) ({rg.groups[0]})")
    r.check(len(expand(rg)[0].recordings) == 3,
            "each old step's wait still runs as its own Record step")

    # A file with no "kind" anywhere must not raise, whatever it contains.
    try:
        Routine.from_dict({"steps": [{"nonsense": True}, "not a dict", 42]})
        r.check(True, "control: a thoroughly malformed old-format blob does "
                     "not raise")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a malformed blob: {e}")


def check_fov_step(r: Report, tmp: Path) -> None:
    """An FOV step runs as move, show its ROI set, capture, light off, wait;
    a Trigger before one waits after its move; rows map back for the table."""
    roi = tmp / "a.roi.json"
    roi.write_text("{}")
    fov = Step(kind="fov", fov="a_fov", x_um=1.0, y_um=2.0, settle_s=0.3,
               pattern=str(roi), length=200, unit="frames", wait_s=5.0)
    eng, rows = expand(Routine(steps=[fov]))
    r.check([(s.kind, s.pattern) for s in eng.steps]
            == [("move", ""), ("display", str(roi)), ("record", ""),
                ("display", ""), ("wait", "")],
            f"FOV step -> move, show ROI, record, light off, wait "
            f"({[s.kind for s in eng.steps]})")
    r.check(eng.steps[0].settle_s == 0.3 and eng.steps[2].length == 200
            and eng.steps[4].length == 5.0 and eng.steps[4].unit == "seconds",
            "…with its settle, capture and wait (in seconds)")
    r.check(rows == [0] * 5, f"…every part maps to its row ({rows})")

    unlit = replace(fov, pattern="", wait_s=0.0)
    r.check([s.kind for s in expand(Routine(steps=[unlit]))[0].steps]
            == ["move", "record"],
            "control: no ROI set and no wait -> no light and no wait steps")

    trig = Routine(steps=[Step(kind="trigger"), fov, Step(kind="puff")],
                   groups=[Group(start=0, end=1, repeats=3)])
    eng, rows = expand(trig)
    r.check([s.kind for s in eng.steps]
            == ["move", "display", "trigger", "record", "display", "wait",
                "puff"],
            f"a Trigger before an FOV step waits after the move, right before "
            f"the capture ({[s.kind for s in eng.steps]})")
    r.check(rows == [1, 1, 0, 1, 1, 1, 2],
            f"…its wait maps to the Trigger's row ({rows})")
    r.check([(g.start, g.end, g.repeats) for g in eng.groups] == [(0, 5, 3)],
            f"…and the group covers the whole folded block ({eng.groups})")
    split = replace(trig, groups=[Group(start=1, end=1, repeats=2)])
    r.check([s.kind for s in expand(split)[0].steps][:2] == ["trigger", "move"],
            "control: a group starting at the FOV step keeps the Trigger "
            "outside it")

    rig = RigLimits(x_um=(-10, 10), y_um=(-10, 10), has_stage=True,
                    has_dmd=True, has_frames=True)
    far = replace(fov, x_um=99.0)
    probs = validate(Routine(steps=[Step(kind="puff"), far]),
                     replace(rig, has_puffer=True))
    r.check(any(p.startswith("step 2: x = 99") for p in probs),
            f"a problem in an FOV step's move names its table row ({probs})")
    r.check(any("FOV step has no position" in p for p in
                validate(Routine(steps=[replace(fov, x_um=None, y_um=None)]),
                         rig)),
            "an FOV step with no position is refused")

    legacy = Routine.from_dict({"steps": [
        {"kind": "move", "x_um": 1.0, "fov": "a_fov"},
        {"kind": "display", "pattern": str(roi)},
        {"kind": "record", "length": 3, "unit": "seconds"},
        {"kind": "display", "pattern": ""},
        {"kind": "wait", "length": 4, "unit": "seconds"},
        {"kind": "puff"}]})
    r.check([s.kind for s in legacy.steps] == ["fov", "puff"]
            and legacy.steps[0].pattern == str(roi)
            and legacy.steps[0].wait_s == 4,
            f"an older move/show/record/off/wait run loads as one FOV step "
            f"({[s.kind for s in legacy.steps]})")
    kept = Routine.from_dict({
        "steps": [{"kind": "move", "x_um": 1.0},
                  {"kind": "record", "length": 3}],
        "groups": [{"start": 1, "end": 1, "repeats": 2}]})
    r.check([s.kind for s in kept.steps] == ["move", "record"],
            "control: a group edge inside the run keeps the steps apart")
    back = Routine.from_dict(Routine(steps=[fov], capture_length=7,
                                     capture_unit="seconds",
                                     wait_s=2.5).to_dict())
    r.check(back.steps == [fov] and (back.capture_length, back.capture_unit,
                                     back.wait_s) == (7, "seconds", 2.5),
            "FOV steps and the general capture/wait survive save and load")


# ── estimate, panel, table, and the whole app ─────────────────────────────────

def check_estimate(r: Report) -> None:
    """The estimate may convert frames to seconds (a recording never does),
    but not silently, and never at a rate nobody gave it. A move costs its
    settle (travel is untimed); display and puff cost nothing."""
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

    instant = Routine(steps=[Step(kind="display", pattern="p.png"),
                             Step(kind="puff")])
    r.check(estimate(instant, None).seconds == 0.0
            and estimate(instant, None).frames == 0.0,
            "a display/puff-only routine costs nothing to estimate")

    # What is left must fall within a step, not sit still and then jump.
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

    # Travel is in no total, so the estimate counts the moves instead.
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

    # Resume repeats the step; a bar that went back would read as a fault.
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


def _part_routines() -> int:
    r = Report("routines")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_routines_"))
    try:
        check_validation(r, tmp)
        check_migration(r)
        check_fov_step(r, tmp)
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
        check_estimate(r)
        check_progress(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


PARTS = {"routines": _part_routines}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
