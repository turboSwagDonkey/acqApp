"""Experiment routines: triggers, bursts, and the adapter's files (rolls,
edge log, doomed and *_BAD files, guards).

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_routines_files.py [-q] [--part NAME]
"""
from __future__ import annotations

import csv
import dataclasses
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _harness import Report, run_parts
from _routine_rig import FULL_RIG, DT, FakeRig, drive, ticks

from acqApp.routines.engine import Phase, RoutineEngine
from acqApp.routines.settings import Group, Routine, Step, validate


def check_trigger_step(r: Report) -> None:
    """A `trigger` step: one recording per external edge, repeated — a group
    over [trigger, record], so the file opens on the edge, not before it.
    Drain/settle/timeout are shortened from 1.5 s / 0.75 s / 600 s."""
    DRAIN, SETTLE = 0.2, 0.1
    routine = Routine(
        steps=[Step(kind="trigger", label="edge"),
               Step(kind="record", label="capture", length=0.5, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=3)])
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

    # Frames still arriving while the re-arm takes effect restart the settle
    # window. The rig bug: a fixed window expired while they arrived, and the
    # next one started a recording with no trigger.
    rig.fire_trigger()                      # residual frames, re-arm not yet in
    ticks(eng, rig, int((DRAIN + SETTLE) / DT) + 10)
    r.check(eng.phase == Phase.WAITING,
            "a camera that never goes quiet never satisfies the step — "
            "in-flight frames raise the baseline instead of ending it")

    rig.gated = True
    ticks(eng, rig, int((DRAIN + SETTLE) / DT) + 10)
    r.check(eng.phase == Phase.WAITING,
            f"…and the camera merely going quiet is not an edge either "
            f"({eng.phase})")

    rig.fire_trigger()
    drive(eng, rig, until=lambda e: e.phase != Phase.WAITING)
    r.check(eng.phase == Phase.RUNNING,
            f"the first frame after it settled IS the edge ({eng.phase})")
    r.check(rig.begun == [],
            "…the recording still has not opened: the trigger step is not in "
            "the bracket, so it opens as the NEXT step is entered")
    ticks(eng, rig)                  # completes the step, enters the record
    r.check(len(rig.begun) == 1,
            f"…and it opens there, once the edge has already landed "
            f"({len(rig.begun)})")

    # Fire each remaining edge once the drain window has passed, timed from
    # outside rather than by reading the engine's own timer.
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

    # Rig, 2026-09-16: a camera streaming through the re-arm had its next
    # frame taken as an edge. It must fault instead. This arm never gates.
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
    r.check("isn't re-arming" in eng_free.fault,
            f"…and the fault says what is actually wrong ({eng_free.fault!r})")
    r.check(rig_free.begun == [],
            f"…with no recording ever opened for the trigger that never "
            f"happened ({rig_free.begun})")

    rig2 = FakeRig(hz=100.0)
    eng2 = RoutineEngine(routine, rig2.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE, trigger_timeout_s=1.0)
    eng2.start()
    drive(eng2, rig2, limit_s=5.0, until=lambda e: e.phase == Phase.PAUSED)
    r.check(eng2.phase == Phase.PAUSED and "trigger" in eng2.fault,
            f"an edge that never arrives times out into a pause rather than "
            f"waiting forever ({eng2.fault!r})")

    rig3 = FakeRig(hz=100.0)
    eng3 = RoutineEngine(routine, rig3.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE)
    eng3.start()
    r.check(eng3.phase == Phase.WAITING, "…control: waiting again")
    eng3.pause()
    r.check(eng3.phase == Phase.PAUSED,
            f"Pause works while WAITING — an edge may never come, and the "
            f"operator must not be held by it ({eng3.phase})")

    rig4 = FakeRig(hz=100.0)
    rig4.fail_arm = True
    eng4 = RoutineEngine(routine, rig4.hooks(), trigger_drain_s=DRAIN,
                         trigger_settle_s=SETTLE)
    eng4.start()
    r.check(eng4.phase == Phase.PAUSED and "setup failed" in eng4.fault,
            f"a camera that cannot be re-armed pauses the routine "
            f"({eng4.fault!r})")


def check_trigger_gate(r: Report) -> None:
    """A camera that reports its re-arms (`trigger_gate`): one early or
    missed edge costs that edge, never the rest of the routine.

    The rig bug (2026-09-29): an edge landing just after the re-arm, inside
    the heuristic's settle window, set the camera free-running; the count
    never went still, and every later edge was swallowed until the timeout.
    """
    LATENCY = 0.9                           # a .dcimg swap's measured stop
    routine = Routine(
        steps=[Step(kind="trigger", label="edge"),
               Step(kind="record", label="capture", length=0.5, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=3)])

    def rig_and_engine(report_gate: bool):
        rig = FakeRig(hz=100.0)
        rig.report_gate = report_gate
        # Without the report, the heuristic's own model: an instant re-arm.
        rig.rearm_latency = LATENCY if report_gate else 0.0
        eng = RoutineEngine(routine, rig.hooks(), trigger_timeout_s=5.0)
        eng.start()
        return rig, eng

    def fire_soon_after_gate(rig, eng, delay: float, limit_s: float = 30.0):
        """Fire each edge `delay` after the camera gates."""
        fired, gated_at = 0, None
        while rig.t < limit_s and eng.phase not in (Phase.DONE, Phase.PAUSED):
            if eng.phase == Phase.WAITING and rig.gated:
                gated_at = rig.t if gated_at is None else gated_at
                if rig.t - gated_at >= delay:
                    rig.fire_trigger()
                    fired += 1
                    gated_at = None
            rig.advance()
            eng.tick()
        return fired

    # 1. Edge 0.1 s after the gate closes — inside the heuristic's 1.5 s
    #    drain floor. Control first: the heuristic loses the routine.
    rig, eng = rig_and_engine(report_gate=False)
    fire_soon_after_gate(rig, eng, delay=0.1)
    r.check(eng.phase == Phase.PAUSED and rig.begun == [],
            f"control: without a gate report, an edge 0.1 s after re-arm is "
            f"taken for leftover frames and the routine stalls "
            f"({eng.phase}, {len(rig.begun)} recordings)")

    rig, eng = rig_and_engine(report_gate=True)
    fired = fire_soon_after_gate(rig, eng, delay=0.1)
    r.check(eng.phase == Phase.DONE and fired == 3 and len(rig.begun) == 3,
            f"with the gate reported, the same early edges each start their "
            f"recording ({eng.phase}, fault={eng.fault!r}, edges={fired}, "
            f"recordings={len(rig.begun)})")

    # 2. Frames still streaming before the re-arm lands are not an edge.
    rig, eng = rig_and_engine(report_gate=True)
    ticks(eng, rig, int(LATENCY / DT) - 5)
    r.check(eng.phase == Phase.WAITING and not rig.gated,
            f"leftover frames before the re-arm lands don't end the step "
            f"({eng.phase})")

    # 3. An edge while the camera is stopped is lost — the step waits for
    #    the next one and every later edge still gets its own recording.
    rig, eng = rig_and_engine(report_gate=True)
    rig.fire_trigger()                      # the camera can't see this one
    fired = 1 + fire_soon_after_gate(rig, eng, delay=0.3)
    r.check(eng.phase == Phase.DONE and len(rig.begun) == 3,
            f"a missed edge costs one edge, not the routine "
            f"({eng.phase}, fault={eng.fault!r}, recordings={len(rig.begun)})")
    r.check(fired == 4,
            f"…three recordings from four edges, the lost one included "
            f"({fired})")

    # 4. A re-arm that never lands faults instead of waiting out the long
    #    edge timeout.
    rig, eng = rig_and_engine(report_gate=True)
    rig._gate_at = None                     # the requested re-arm never lands
    drive(eng, rig, limit_s=60.0)
    r.check(eng.phase == Phase.PAUSED and "re-arm" in eng.fault,
            f"a re-arm that never completes pauses, saying so "
            f"({eng.fault!r})")


def check_burst(r: Report) -> None:
    """Burst mode: the camera captures N frames per edge and re-arms itself,
    so a Record right after a Trigger ends on the burst, not the clock."""
    from acqApp.routines.estimate import burst_frames

    pair = [Step(kind="trigger"), Step(kind="record", length=3.0, unit="seconds")]
    r.check(burst_frames(Routine(steps=pair), 500.0) == (1500, ""),
            f"a 3 s Record at 500 Hz bursts 1500 frames "
            f"({burst_frames(Routine(steps=pair), 500.0)})")
    two = Routine(steps=pair + [Step(kind="trigger"),
                                Step(kind="record", length=1500, unit="frames")])
    r.check(burst_frames(two, 500.0) == (1500, ""),
            "seconds and frames Records that agree give one N")
    odd = Routine(steps=pair + [Step(kind="trigger"),
                                Step(kind="record", length=2.0, unit="seconds")])
    n, why = burst_frames(odd, 500.0)
    r.check(n == 0 and "same length" in why and "1000" in why,
            f"Records of different lengths are refused ({why!r})")
    r.check(burst_frames(Routine(steps=pair), None)[0] == 0
            and burst_frames(Routine(steps=pair), None)[1],
            "a seconds Record with no frame rate is refused, not guessed")
    loose = Routine(steps=[Step(kind="trigger"),
                           Step(kind="wait", length=1.0, unit="seconds")])
    r.check(burst_frames(loose, 500.0) == (0, ""),
            "control: no Trigger->Record pair, no burst")

    N, HZ = 80, 100.0                       # 0.8 s of burst vs a 0.5 s Record
    routine = Routine(
        steps=[Step(kind="trigger"),
               Step(kind="record", length=0.5, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=2)])

    def run(burst: bool, stall_at: int | None = None):
        rig = FakeRig(hz=HZ)
        rig.report_gate = True
        edge = {"t": None}

        def count() -> int:
            if edge["t"] is None:
                return 0
            got = min(N, int((rig.t - edge["t"]) * HZ))
            return got if stall_at is None else min(got, stall_at)

        hooks = dataclasses.replace(rig.hooks(), burst_frames=count)
        eng = RoutineEngine(routine, hooks, trigger_timeout_s=5.0,
                            burst_frames=N if burst else 0)
        eng.start()
        lengths, rec_t0 = [], None
        while rig.t < 30 and eng.phase not in (Phase.DONE, Phase.PAUSED):
            if eng.phase == Phase.WAITING and rig.gated:
                rig.fire_trigger()
                edge["t"] = rig.t
            step = eng.step
            if (eng.phase == Phase.RUNNING and step is not None
                    and step.kind == "record"):
                rec_t0 = rig.t if rec_t0 is None else rec_t0
            elif rec_t0 is not None:
                lengths.append(rig.t - rec_t0)
                rec_t0 = None
            rig.advance()
            eng.tick()
        if rec_t0 is not None:
            lengths.append(rig.t - rec_t0)
        return eng, lengths

    eng, lengths = run(burst=False)
    r.check(eng.phase == Phase.DONE and len(lengths) == 2
            and all(abs(x - 0.5) < 0.05 for x in lengths),
            f"control: without burst the Record runs its 0.5 s ({lengths})")
    eng, lengths = run(burst=True)
    r.check(eng.phase == Phase.DONE and len(lengths) == 2
            and all(abs(x - N / HZ) < 0.05 for x in lengths),
            f"burst: each Record ends when its {N} frames are in "
            f"({[round(x, 2) for x in lengths]} s, {eng.fault!r})")
    eng, _ = run(burst=True, stall_at=40)
    r.check(eng.phase == Phase.PAUSED and "40/80" in eng.fault
            and "retries" in eng.fault,
            f"a burst that keeps stopping short pauses the routine in the "
            f"end ({eng.fault!r})")


def check_burst_retry(r: Report) -> None:
    """A short burst (rig, 2026-09-30: an edge during the camera's restart
    gave 2491/2500) is marked and retried on the next edge, not paused on;
    the LED and ROIs are lit before the edge, so the burst's first frame is."""
    from acqApp.routines.engine import MAX_BURST_RETRIES

    N, HZ = 80, 100.0
    routine = Routine(
        steps=[Step(kind="display", pattern="roi.json"),
               Step(kind="trigger"),
               Step(kind="record", length=0.8, unit="seconds")],
        groups=[Group(start=1, end=2, repeats=2)])

    def run(short: set[int], pause_after: bool = False):
        """`short`: the edges (1-based) whose burst stops at N/2."""
        rig = FakeRig(hz=HZ)
        rig.report_gate = True
        edge = {"t": None, "n": 0}
        lit_at_edge = []

        def count() -> int:
            if edge["t"] is None:
                return 0
            got = min(N, int((rig.t - edge["t"]) * HZ))
            return min(got, N // 2) if edge["n"] in short else got

        hooks = dataclasses.replace(rig.hooks(), burst_frames=count)
        eng = RoutineEngine(routine, hooks, trigger_timeout_s=5.0,
                            burst_frames=N, burst_stall_s=0.3)
        eng.start()
        while rig.t < 30 and eng.phase not in (Phase.DONE, Phase.PAUSED):
            if eng.phase == Phase.WAITING and rig.gated:
                rig.fire_trigger()
                edge["t"], edge["n"] = rig.t, edge["n"] + 1
                lit_at_edge.append((rig.led_on, rig.lit))
            rig.advance()
            eng.tick()
        return eng, rig, lit_at_edge

    eng, rig, lit = run(short={1})
    flags = [(x.interrupted, x.fault) for x in eng.runs]
    r.check(eng.phase == Phase.DONE and len(eng.runs) == 3
            and flags[0] == (True, "burst stopped at 40/80 frames")
            and flags[1:] == [(False, "")] * 2,
            f"a short burst is marked and retried on the next edge; both "
            f"repeats still record ({flags}, {eng.fault!r})")
    r.check(eng.steps_done() == 5,
            f"...and the retried trigger isn't counted twice "
            f"({eng.steps_done()})")
    r.check(lit == [(True, True)] * 3,
            f"the LED and ROIs are already on at every edge, retry and repeat "
            f"included: no dark first frames ({lit})")

    eng, rig, _ = run(short=set(range(1, MAX_BURST_RETRIES + 2)))
    r.check(eng.phase == Phase.PAUSED
            and len(eng.runs) == MAX_BURST_RETRIES + 1
            and all(x.interrupted for x in eng.runs),
            f"{MAX_BURST_RETRIES} retries in a row, then it pauses "
            f"({len(eng.runs)} runs, {eng.fault!r})")
    r.check(not rig.led_on and not rig.lit,
            "the pause puts out the LED and the DMD")
    eng.resume()
    r.check(rig.lit and rig.led_on and eng.phase == Phase.WAITING,
            "resume re-lights what the Display step left on (rig: FOV3 T19-"
            "T50 recorded dark after a resume)")

    eng, rig, _ = run(short=set())
    r.check(eng.phase == Phase.DONE and len(eng.runs) == 2
            and not any(x.interrupted for x in eng.runs),
            "control: full bursts, no retries")


def check_prepare_recording(r: Report) -> None:
    """A `trigger` step followed by a Record step asks for that recording's
    file BEFORE it re-arms (a .dcimg can only be bound while capture is
    stopped, and re-arming stops it). Any other next step asks for nothing."""
    DRAIN, SETTLE = 0.2, 0.1

    def run(routine):
        rig = FakeRig(hz=100.0)
        eng = RoutineEngine(routine, rig.hooks(), trigger_drain_s=DRAIN,
                            trigger_settle_s=SETTLE, trigger_timeout_s=5.0)
        eng.start()
        for _ in range(4000):
            if eng.phase == Phase.DONE:
                break
            if eng.phase == Phase.WAITING and rig.gated:
                ticks(eng, rig, int((DRAIN + SETTLE) / DT) + 10)
                rig.fire_trigger()
            rig.advance()
            eng.tick()
        return eng, rig

    eng, rig = run(Routine(
        steps=[Step(kind="trigger"),
               Step(kind="record", length=0.2, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=2)]))
    kinds = [e[0] for e in rig.log if e[0] in ("prepare", "arm_trigger")]
    r.check(eng.phase == Phase.DONE, f"fixture: ran to the end ({eng.phase})")
    r.check(kinds == ["prepare", "arm_trigger"] * 2,
            f"each trigger step prepares the next file, then re-arms ({kinds})")
    prep = [e for e in rig.log if e[0] == "prepare"]
    r.check(prep == [("prepare", 0, 0, 1)] * 2,
            f"…naming the record step that follows ({prep})")

    eng2, rig2 = run(Routine(steps=[Step(kind="trigger"),
                                    Step(kind="wait", length=0.2,
                                         unit="seconds")]))
    r.check(not any(e[0] == "prepare" for e in rig2.log),
            "control: a trigger followed by a wait prepares nothing")

    eng3, rig3 = run(Routine(steps=[Step(kind="record", length=0.2,
                                         unit="seconds"),
                                    Step(kind="trigger")], cycles=2))
    prep3 = [e for e in rig3.log if e[0] == "prepare"]
    r.check(prep3 == [("prepare", 0, 1, 0)],
            f"a trigger ending a cycle prepares the NEXT cycle's record, and "
            f"the last cycle's trigger prepares nothing ({prep3})")


def check_first_trial_kept(r: Report) -> None:
    """The rig sends one edge per trial. A routine with trigger steps must not
    arm on trial 1's edge and then wait for trial 2's."""
    from acqApp.routines.banner import look_for

    routine = Routine(
        steps=[Step(kind="trigger"),
               Step(kind="record", length=0.5, unit="seconds")],
        groups=[Group(start=0, end=1, repeats=2)])
    rig = FakeRig(hz=100.0)
    rig.report_gate = True
    rig.rearm_latency = 0.5
    eng = RoutineEngine(routine, rig.hooks())
    eng.start(trigger="ttl")
    r.check(eng.phase == Phase.WAITING and rig.rearms == 1,
            f"with trigger steps, Start goes straight to step 1's own wait "
            f"({eng.phase}, re-arms {rig.rearms})")
    r.check(not eng.edge_ready,
            "…reported not ready while the re-arm is still in progress")
    while not rig.gated:
        ticks(eng, rig)
    r.check(eng.edge_ready, "…and ready once the camera is gated")
    rig.fire_trigger()                       # trial 1's edge
    drive(eng, rig, until=lambda e: e.phase != Phase.WAITING)
    ticks(eng, rig)
    r.check(len(rig.begun) == 1,
            f"trial 1's edge opens the first recording ({len(rig.begun)})")

    plain = RoutineEngine(Routine(steps=[Step(kind="wait", length=1,
                                              unit="seconds")]),
                          FakeRig(hz=0.0).hooks())
    plain.start(trigger="ttl")
    r.check(plain.phase == Phase.ARMED,
            "control: a routine without trigger steps still arms")

    r.check(look_for(Phase.WAITING, "RE-ARMING the camera")[0].startswith(
        "RE-ARMING"), "the banner says RE-ARMING while an edge would be lost")
    r.check(look_for(Phase.WAITING, "WAITING for the camera's trigger")[0]
            == "WAITING FOR TRIGGER", "…and WAITING once it's safe")


def check_edge_log(r: Report) -> None:
    """Every edge becomes one CSV row, naming the trial file its recording
    opened: at once with .dcimg (opened before the edge), after the roll
    with TIFF. The log is what bpod_match aligns to the stim rig's."""
    from acqApp.adapters.routines import RoutinesModule
    from acqApp.routines.engine import RecordingRun

    tmp = Path(tempfile.mkdtemp(prefix="acqapp_edgelog_"))
    cur = {"path": tmp / "FOVcustom_T1"}
    win = SimpleNamespace(recording_path=lambda: cur["path"],
                          routine_folder=lambda: tmp)
    a = RoutinesModule.__new__(RoutinesModule)
    a.win = win
    a._routine = Routine(steps=[Step(kind="trigger"),
                                Step(kind="record", length=1, unit="seconds")])
    a._trial_count = {0: 1}
    a._edge_log = None
    a._edge_n = 0
    a._pending_edge = None
    a._rec = SimpleNamespace(put=lambda *_a, **_k: None)
    a._filed = 0
    a._status = lambda m: None

    def run(cycle):
        return RecordingRun(region=0, start_index=1, end_index=1, cycle=cycle,
                            attempt=1, t0=0.0, frame0=None)

    a._on_edge(1.25, run(0))
    a._put(run(0), opening=True)             # T1's file opens
    cur["path"] = tmp / "FOVcustom_T2"
    a._trial_count[0] = 2
    a._on_edge(8.5, run(1))
    a._put(run(1), opening=True)
    a._on_edge(15.0, None)                   # a trigger with no record after
    rows = list(csv.DictReader(open(a._edge_log, encoding="utf-8")))
    r.check([row["edge"] for row in rows] == ["1", "2", "3"],
            f"one row per edge ({[row['edge'] for row in rows]})")
    r.check(rows[0]["trial"] == "1" and rows[0]["path"].endswith("T1")
            and rows[1]["trial"] == "2" and rows[1]["path"].endswith("T2"),
            f"…each naming its trial and file ({rows[:2]})")
    r.check(float(rows[1]["session_s"]) == 8.5 and rows[2]["path"] == "",
            "…with the edge's time, and no file where none opened")

    # A comma in a folder name must not shift the columns, and the rename
    # that marks a bad file must still find its row.
    old = tmp / "Smith, J" / "FOVcustom_T3"
    cur["path"] = old
    a._trial_count[0] = 3
    a._on_edge(22.0, run(2))
    a._put(run(2), opening=True)
    new = tmp / "Smith, J" / "FOVcustom_T3_BAD"
    a._repath_edge_log(old, new)
    rows = list(csv.DictReader(open(a._edge_log, encoding="utf-8")))
    r.check(len(rows) == 4 and rows[3]["path"] == str(new)
            and rows[3]["trial"] == "3" and rows[3]["fov"] == "custom",
            f"a comma in the path keeps its columns, and follows a rename "
            f"({rows[3] if len(rows) > 3 else rows})")
    r.check(rows[0]["path"].endswith("T1"),
            "…without touching the other rows")

    # The engine reports every edge, with the run it starts.
    routine = Routine(steps=[Step(kind="trigger"),
                             Step(kind="record", length=0.2, unit="seconds")],
                      groups=[Group(start=0, end=1, repeats=2)])
    rig = FakeRig(hz=100.0)
    rig.report_gate = True
    seen = []
    eng = RoutineEngine(routine, dataclasses.replace(
        rig.hooks(), edge=lambda t, nxt: seen.append(nxt)))
    eng.start()
    for _ in range(4000):
        if eng.phase == Phase.DONE:
            break
        if eng.phase == Phase.WAITING and rig.gated:
            ticks(eng, rig, int(0.3 / DT) + 10)
            rig.fire_trigger()
        rig.advance()
        eng.tick()
    r.check(len(seen) == 2 and all(n is not None and n.start_index == 1
                                   for n in seen),
            f"the engine reports each edge with the record run it starts "
            f"({len(seen)})")


def check_prepared_adapter(r: Report) -> None:
    """The adapter half: with a .dcimg open it rolls the file and re-arms once,
    inside the swap; the recording's own begin then does NOT roll again."""
    from acqApp.adapters.routines import RoutinesModule
    from acqApp.routines.engine import RecordingRun

    calls: list = []
    win = SimpleNamespace(
        dcimg_frames=lambda key: 0,
        arm_camera_with_next_file=lambda key: (calls.append("arm") or True),
        roll_recording=lambda: (calls.append("roll") or True),
        set_routine_save_context=lambda *a, **k: None)
    a = RoutinesModule.__new__(RoutinesModule)
    a.win = win
    a._routine = Routine(steps=[Step(kind="trigger"),
                                Step(kind="record", length=1, unit="seconds")],
                         save_mode="per_repeat")
    a._engine = SimpleNamespace(runs=[])
    a._trial_count = {}
    a._pending_scope = {}
    a._rolling = False
    a._filed_from = 0
    a._file_group_key = (0, None)
    a._doomed = None
    a._prepared = None
    a._armed_with_file = False
    a._pending_roll_run = None
    a._seal_after = None
    a._sealed = False
    a._rec = None
    a._status = lambda m: None
    run = RecordingRun(region=0, start_index=1, end_index=1, cycle=0,
                       attempt=1, t0=0.0, frame0=None)

    a._prepare_recording(run)
    r.check(calls == ["arm", "roll"],
            f".dcimg open: armed with the next file, then rolled ({calls})")
    r.check(a._prepared == (0, 1) and a._armed_with_file,
            "...and remembers which recording that file is for")

    a._on_recording_begin(run)
    r.check(a._pending_roll_run is None and a._prepared is None,
            "the recording's begin finds its file already open - no 2nd roll")

    # Paused at the trigger, resumed: the same run asks for a file again.
    calls.clear()
    win.recording_path = lambda: "unused.dcimg"
    discarded: list = []
    a._discard = lambda junk, bad: discarded.append(junk)
    a._prepare_recording(run)
    a._prepare_recording(run)
    r.check(discarded == [None, "unused.dcimg"] and a._doomed is None,
            f"resuming at a trigger deletes the empty file its edge never "
            f"filled ({discarded})")

    discarded.clear()
    win.dcimg_frames = lambda key: 12
    a._prepare_recording(run)
    r.check(discarded == [None], f"control: a file that caught frames stays "
                                 f"({discarded})")
    win.dcimg_frames = lambda key: 0

    # ...or aborted there.
    discarded.clear()
    win.set_recording = win.set_live = lambda on: None
    a._bad_file = lambda: None
    a._flush_pending_edge = lambda: None
    a._close_own_recording()
    r.check(discarded == ["unused.dcimg"] and a._prepared is None,
            f"aborting at a trigger deletes its empty file too ({discarded})")

    calls.clear()
    a._prepared = None
    win.dcimg_frames = lambda key: None
    a._armed_with_file = False
    a._prepare_recording(run)
    r.check(calls == [] and a._prepared is None,
            "control: no .dcimg open -> nothing prepared, the old path runs")


def check_burst_limit(r: Report) -> None:
    """Over the camera's limit a routine is refused, not clamped by the
    panel's spin box into shorter bursts than the steps say."""
    from acqApp.adapters.routines import RoutinesModule

    class Past(Exception):
        pass

    def past_the_checks(_on):
        raise Past

    def start(length: int) -> tuple[list, bool]:
        shown: list = []
        a = RoutinesModule.__new__(RoutinesModule)
        a.panel = SimpleNamespace(
            settings=Routine(steps=[Step(kind="trigger"),
                                    Step(kind="record", length=length,
                                         unit="frames")]),
            show_problems=shown.extend)
        a.win = SimpleNamespace(frame_rate_hz=lambda: 1000.0,
                                routine_arming_trigger=past_the_checks)
        a._rig = lambda: FULL_RIG
        a._status = lambda m: None
        try:
            a._start()
        except Past:
            return shown, True
        return shown, False

    shown, went_on = start(70000)
    r.check(not went_on and any("holds at most" in p for p in shown),
            f"a 70000-frame burst is refused ({shown})")
    shown, went_on = start(65534)
    r.check(went_on, f"control: the largest legal burst is accepted ({shown})")


def check_seal_after_burst(r: Report) -> None:
    """Burst mode: the camera re-arms itself, so a finished trial's .dcimg is
    sealed after the tick its Record ended in — unless that tick already
    rolled the file. A sealed file takes no further run (rig, 2026-09-30: a
    Bpod edge during the move to the next FOV added 330 frames to a trial)."""
    from acqApp.adapters.routines import RoutinesModule
    from acqApp.routines.engine import RecordingRun

    calls: list = []
    win = SimpleNamespace(
        path=Path("FOV1_T2"),
        seal_camera_file=lambda key: (calls.append("seal") or True),
        roll_recording=lambda: (calls.append("roll") or True),
        set_routine_save_context=lambda *a, **k: None,
        camera_ready=lambda key: True)
    win.recording_path = lambda: win.path

    def run(i: int, interrupted: bool = False) -> RecordingRun:
        x = RecordingRun(region=0, start_index=i, end_index=i, cycle=0,
                         attempt=1, t0=0.0, frame0=None)
        x.interrupted = interrupted
        return x

    def adapter(burst_n: int = 2500):
        a = RoutinesModule.__new__(RoutinesModule)
        a.win = win
        a._routine = Routine(steps=[
            Step(kind="trigger"),
            Step(kind="record", length=5, unit="seconds"),
            Step(kind="move", x_um=1.0),
            Step(kind="record", length=5, unit="seconds")])
        a._engine = SimpleNamespace(tick=lambda: None, phase=Phase.RUNNING,
                                    runs=[])
        a._burst_n = burst_n
        a._seal_after = None
        a._sealed = False
        a._hold_t0 = None
        a._rec = None
        a._pending_roll_run = None
        a._prepared = None
        a._file_group_key = (0, None)
        a._trial_count = {}
        a._pending_scope = {}
        a._rolling = False
        a._filed_from = 0
        a._doomed = None
        a._timer = None
        a._status = lambda m: None
        return a

    def tick(a, *hooks) -> list:
        """One `_tick` whose engine tick fires `hooks` -> the calls made."""
        calls.clear()
        a._engine.tick = lambda: [h() for h in hooks]
        a._tick()
        return list(calls)

    a = adapter()
    got = tick(a, lambda: a._on_recording_end(run(1)))
    r.check(got == ["seal"] and a._sealed,
            f"a burst Record ending into a move seals its file ({got})")
    a._on_recording_begin(run(3))
    got = tick(a)
    r.check(got == ["roll"] and not a._sealed,
            f"...and the next run rolls rather than land in it, even in "
            f"single mode ({got})")

    def rolled():
        win.path = Path("FOV1_T3")
    a = adapter()
    got = tick(a, lambda: a._on_recording_end(run(1)), rolled)
    win.path = Path("FOV1_T2")
    r.check(got == [], f"a trigger step that rolled in the same tick: the "
                       f"new file is left alone ({got})")

    a = adapter()
    got = tick(a, lambda: a._on_recording_end(run(1)),
               lambda: a._on_recording_begin(run(3)))
    r.check(got == [], f"a run opening in the same file cancels the seal "
                       f"({got})")

    a = adapter()
    got = tick(a, lambda: a._on_recording_end(run(1, interrupted=True)))
    r.check(got == [], f"an interrupted run (a pause) is not sealed ({got})")

    a = adapter()
    a._engine.phase = Phase.DONE
    a._close_own_recording = lambda: None
    a.update_display = lambda: None
    got = tick(a, lambda: a._on_recording_end(run(1)))
    r.check(got == [], f"the routine's end closes the file anyway ({got})")

    a = adapter(burst_n=0)
    got = tick(a, lambda: a._on_recording_end(run(1)))
    r.check(got == [], f"control: outside burst mode nothing is sealed ({got})")

    # A tick that raises must pause the engine (DMD light, LED and motion
    # off), not just stop the timer; one that can't pause stops the timer
    # rather than spin on the same error.
    def raising(a, phase_after_pause):
        stopped, paused = [], []
        a._stop_ticking = lambda: stopped.append(1)

        def boom():
            raise RuntimeError("gate read failed")

        def pause(why):
            paused.append(why)
            if phase_after_pause is not None:
                a._engine.phase = phase_after_pause
        a._engine.tick, a._engine.pause = boom, pause
        a._tick()
        return paused, stopped

    paused, stopped = raising(adapter(), Phase.PAUSED)
    r.check(len(paused) == 1 and "gate read failed" in paused[0] and not stopped,
            f"a raising tick pauses the engine, timer left running "
            f"({paused}, {stopped})")
    paused, stopped = raising(adapter(), None)
    r.check(len(paused) == 1 and stopped == [1],
            f"...and if it could not pause, the timer stops ({paused}, "
            f"{stopped})")


def check_first_file_doomed(r: Report) -> None:
    """The file `_start()` opens is doomed only when a `trigger` step precedes
    the first Recording AND the camera writes .dcimg: that re-arm rolls it
    away before it gets a boundary."""
    from acqApp.adapters.routines import RoutinesModule

    a = RoutinesModule.__new__(RoutinesModule)

    trigger_then_record = Routine(steps=[
        Step(kind="wait", length=1, unit="frames"),
        Step(kind="trigger"),
        Step(kind="record", length=1, unit="seconds")])
    record_first = Routine(steps=[
        Step(kind="record", length=1, unit="seconds"),
        Step(kind="trigger"),          # AFTER the bracket: doesn't count
        Step(kind="record", length=1, unit="seconds")])

    a.win = SimpleNamespace(dcimg_enabled=lambda: True)
    r.check(a._first_file_is_doomed(trigger_then_record),
            "a trigger step before the first bracket, with .dcimg -> doomed")
    r.check(not a._first_file_is_doomed(record_first),
            "no trigger before the bracket -> not doomed, even with .dcimg")

    a.win = SimpleNamespace(dcimg_enabled=lambda: False)
    r.check(not a._first_file_is_doomed(trigger_then_record),
            "same protocol, TIFF/composite (no .dcimg) -> nothing to roll "
            "away, not doomed")


def check_doomed_file_deleted(r: Report, tmp: Path) -> None:
    """The doomed first file (stage travel, no frames) is deleted at the roll
    that replaces it — only its own files, and only if it caught no frames."""
    from acqApp.adapters.routine_files import delete_recording
    from acqApp.adapters.routines import RoutinesModule

    def split_dir(name: str) -> Path:
        d = tmp / name
        d.mkdir()
        for suffix in ("_data.csv", "_settings.json", "_voltage_cam.dcimg"):
            (d / f"{name}{suffix}").write_text("x")
        return d

    d = split_dir("junk_a")
    r.check(delete_recording(d) and not d.exists(),
            "a split recording folder goes, files and folder")
    d = split_dir("junk_b")
    (d / "notes.txt").write_text("the operator's")
    r.check(delete_recording(d) and (d / "notes.txt").exists()
            and len(list(d.iterdir())) == 1,
            "a file not named after the recording is left, and so its folder")
    d = split_dir("junk_c")
    held = open(d / "junk_c_voltage_cam.dcimg", "rb")
    try:
        r.check(not delete_recording(d),
                "a file still held open (DCAM closing it) reports not-yet")
    finally:
        held.close()
    r.check(delete_recording(d) and not d.exists(), "…and goes once released")

    def roll(frames: int):
        a = RoutinesModule.__new__(RoutinesModule)
        path = split_dir(f"doomed_{frames}")
        a._doomed = path
        a._routine = None
        a._engine = SimpleNamespace(runs=[])
        a._filed_from = 0
        a._fov_for = lambda _r, _i: ("fov1", None)
        a._trial_for = lambda _region: 1
        a._scope_for = lambda _run: {}
        a._group_key_for = lambda _run: (0, None)
        a._status = lambda _m: None
        a.win = SimpleNamespace(dcimg_frames=lambda _k: frames,
                                set_routine_save_context=lambda *_a: None,
                                roll_recording=lambda: True)
        a._roll_for(SimpleNamespace(start_index=0, region=0))
        return path, a

    path, a = roll(frames=0)
    r.check(not path.exists() and a._doomed is None,
            "the first roll deletes Start's empty file")
    path, a = roll(frames=12)
    r.check(path.exists() and a._doomed is None,
            "control: a file that caught frames is kept")


def check_bad_trial_marked(r: Report, tmp: Path) -> None:
    """A file whose every run was interrupted is renamed *_BAD once closed,
    the edge log follows, and Bpod renumbering keeps the mark."""
    from acqApp.adapters.routines import RoutinesModule
    from acqApp.routines.engine import RecordingRun
    from acqApp.saving.bpod_match import Match, plan

    def trial(name: str) -> Path:
        d = tmp / "bad" / name
        d.mkdir(parents=True)
        for suffix in ("_data.csv", "_voltage_cam.dcimg"):
            (d / f"{name}{suffix}").write_text("x")
        return d

    def run(interrupted: bool) -> RecordingRun:
        x = RecordingRun(region=0, start_index=1, end_index=1, cycle=0,
                         attempt=1, t0=0.0, frame0=None)
        x.interrupted = interrupted
        return x

    t18, t19 = trial("FOV3_T18"), trial("FOV3_T19")
    log = tmp / "bad" / "routine_edges.csv"
    log.write_text("edge,session_s,wall_time,fov,trial,path\n"
                   f"1,1.0,w,3,18,{t18}\n2,8.0,w,3,19,{t19}\n",
                   encoding="utf-8")
    said: list = []
    a = RoutinesModule.__new__(RoutinesModule)
    a._edge_log = log
    a._status = said.append
    a._filed_from = 0
    a.win = SimpleNamespace(recording_path=lambda: t18)
    a._engine = SimpleNamespace(runs=[run(True)])
    r.check(a._bad_file() == t18, "a file holding only an interrupted run is bad")
    a._engine.runs = [run(True), run(False)]
    r.check(a._bad_file() is None, "...one good run in it and it is not")
    a._engine.runs = []
    r.check(a._bad_file() is None, "...nor is a file with no runs (stage travel)")

    held = open(t18 / "FOV3_T18_voltage_cam.dcimg", "rb")
    try:
        a._mark_bad(t18, 1)
    finally:
        held.close()
    r.check(t18.exists() and "still open" in said[-1],
            f"still open (DCAM closing it): left alone, and said ({said[-1]!r})")
    a._mark_bad(t18, 1)
    bad = tmp / "bad" / "FOV3_T18_BAD"
    r.check(bad.is_dir() and (bad / "FOV3_T18_BAD_voltage_cam.dcimg").exists()
            and not t18.exists(),
            "once closed: folder and files renamed *_BAD")
    text = log.read_text(encoding="utf-8")
    r.check(f",{bad}\n" in text and f",{t19}\n" in text,
            "the edge log follows the rename, other rows untouched")

    rows = list(csv.DictReader(open(log, encoding="utf-8")))
    pl = plan(rows, Match({0: 18, 1: 19}, 0.0, 0.0), 21)
    got = {p.name: stem for p, stem in pl.renames}
    r.check(got == {"FOV3_T18_BAD": "FOV3_T19_BAD", "FOV3_T19": "FOV3_T20"},
            f"Bpod renumbering keeps the mark ({got})")


def check_arm_camera_trigger(r: Report) -> None:
    """`_arm_camera_trigger()` puts the camera in External edge mode through
    the host before the routine opens its recording, rather than trusting
    the operator to have set it."""
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

    host = FakeHost(True)
    adapter = RoutinesModule(host)
    adapter.panel = FakePanel()
    r.check(adapter._arm_camera_trigger() is True,
            "arming succeeds when the host confirms External edge")
    r.check(host.calls == [(FRAME_STREAM, True)],
            f"…asking the host for exactly the voltage camera, on "
            f"({host.calls})")
    r.check(adapter.panel.problems == [], "…and shows no problem")

    # None = no camera loaded: nothing to arm, and every routine arms, so
    # this must not refuse a stage/puffer-only routine.
    host2 = FakeHost(None)
    adapter2 = RoutinesModule(host2)
    adapter2.panel = FakePanel()
    r.check(adapter2._arm_camera_trigger() is True,
            "arming is a no-op, not a refusal, when there's no camera at all")
    r.check(adapter2.panel.problems == [],
            f"…and shows no problem ({adapter2.panel.problems})")
    r.check(host2.statuses == [],
            f"…and nothing is logged as refused ({host2.statuses})")

    # False = would need a restart mid-recording (set_external_trigger).
    host3 = FakeHost(False)
    adapter3 = RoutinesModule(host3)
    adapter3.panel = FakePanel()
    r.check(adapter3._arm_camera_trigger() is False,
            "control: a host returning False (not None) is still a failure "
            "— only True means armed")


def check_hand_edited_load(r: Report) -> None:
    """Wrong-typed values in a hand-edited file fall back to defaults: the
    table formats them at launch, before `validate` could refuse them."""
    from acqApp.routines.estimate import estimate

    rt = Routine.from_dict({
        "cycles": float("inf"),
        "steps": [{"kind": "wait", "length": "lots", "unit": None, "label": 3},
                  {"kind": "move", "x_um": "12", "y_um": [1],
                   "settle_s": float("nan"), "fov": "A_fov"},
                  {"kind": "wait"}],
        "groups": [{"start": [0], "end": 1},
                   {"start": 0, "end": 2, "repeats": "3"}]})
    w, m = rt.steps[0], rt.steps[1]
    r.check(rt.cycles == 1, f"infinite cycles load as 1 ({rt.cycles})")
    r.check(w.length == 100.0 and w.unit == "" and w.label == "",
            f"a non-numeric length and non-text fields take defaults "
            f"({w.length!r}, {w.unit!r}, {w.label!r})")
    r.check(m.x_um == 12.0 and m.y_um is None and m.settle_s == 0.25,
            f"…numeric text parses, junk and NaN drop to the default "
            f"({m.x_um!r}, {m.y_um!r}, {m.settle_s!r})")
    r.check([(g.start, g.end, g.repeats) for g in rt.groups] == [(0, 2, 3)],
            f"an unhashable group index is dropped, text repeats parse "
            f"({[(g.start, g.end, g.repeats) for g in rt.groups]})")
    try:
        validate(rt, FULL_RIG)
        estimate(rt)
        [s.describe() for s in rt.steps]
        r.check(True, "…and validate, estimate and describe run on it")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} on a hand-edited routine: {e}")


def check_adapter_guards(r: Report) -> None:
    """The adapter's slots never raise: with no sys.excepthook, an exception
    out of a Qt slot aborts the process with the light as it was."""
    from acqApp.adapters.routines import RoutinesModule

    a = RoutinesModule.__new__(RoutinesModule)
    said: list[str] = []
    a._status = said.append
    a._timer = None
    a._hold_t0 = None
    a._pending_roll_run = None
    a._seal_after = None
    rig = FakeRig()
    a._engine = RoutineEngine(Routine(steps=[Step(kind="wait", length=1,
                                                  unit="seconds")]),
                              rig.hooks())
    a._engine.start()
    try:
        a._resume()                 # a second click: no longer paused
        a._skip()
        r.check(any("refused" in m for m in said),
                f"Resume/Skip on a running routine say so ({said})")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a button slot: {e}")

    def no_disk(_run):
        raise RuntimeError("disk gone")

    a._roll_for = no_disk
    a._pending_roll_run = SimpleNamespace()
    try:
        a._tick()
        r.check(a._engine.phase == Phase.PAUSED
                and "disk gone" in a._engine.fault,
                f"a raise after eng.tick() (the deferred roll) pauses the "
                f"routine ({a._engine.phase}, {a._engine.fault!r})")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped _tick: {e}")


def check_abort_is_not_finish(r: Report) -> None:
    """An aborted routine says so on the panel, the banner and in the file,
    rather than reading as finished; a restart clears it."""
    from acqApp.adapters.routines import RoutinesModule
    from acqApp.routines.banner import look_for

    rig = FakeRig()
    one = Routine(steps=[Step(kind="wait", length=0.05, unit="seconds")])
    eng = RoutineEngine(one, rig.hooks())
    eng.start()
    drive(eng, rig)
    r.check(eng.phase == Phase.DONE and not eng.aborted,
            "control: a routine run to its end is not aborted")
    eng.start()
    eng.abort()
    r.check(eng.phase == Phase.DONE and eng.aborted, "abort() marks it aborted")

    a = RoutinesModule.__new__(RoutinesModule)
    shown: list[tuple[str, str]] = []
    a.panel = SimpleNamespace(
        set_state=lambda phase, text, _row: shown.append((phase, text)),
        set_progress=lambda *_a: None)
    a._rate_tick = 0
    a._engine = eng
    a._routine, a._routine_origin = one, 0.0
    a._filed = a._filed_from = 0
    a._edge_log = None
    a.update_display()
    phase, text = shown[-1]
    r.check(text.startswith("aborted") and look_for(phase, text)[0] == "ABORTED",
            f"the panel and banner say aborted, not finished ({text!r}, "
            f"{look_for(phase, text)})")
    fault = a.final_metadata()["routine_fault"]
    r.check(fault == "aborted", f"…and so does the file's fault ({fault!r})")
    r.check(a._mini_text()[0] == "aborted",
            f"…and the sidebar ({a._mini_text()})")
    eng.start()
    r.check(not eng.aborted, "a restart clears the abort")
    eng.abort()


def check_restart_forgets_edge(r: Report) -> None:
    """Aborted between a trigger's edge and its burst, a restarted engine
    must not run its first Record as a burst with no edge."""
    rig = FakeRig(hz=100.0)
    rig.report_gate = True
    routine = Routine(steps=[Step(kind="record", length=0.2, unit="seconds"),
                             Step(kind="trigger"),
                             Step(kind="record", length=0.5, unit="seconds")])
    eng = RoutineEngine(routine, rig.hooks(), burst_frames=50)
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.WAITING and rig.gated)
    rig.fire_trigger()
    drive(eng, rig, limit_s=rig.t + 1.0,
          until=lambda e: e.phase == Phase.RUNNING)
    r.check(eng.phase == Phase.RUNNING and eng.position[0] == 1,
            f"fixture: the edge is seen, the burst not entered yet "
            f"({eng.phase}, {eng.position})")
    eng.abort()
    eng.start()
    drive(eng, rig, limit_s=rig.t + 1.0,
          until=lambda e: e.phase == Phase.WAITING)
    r.check(eng.phase == Phase.WAITING and eng.position[0] == 1,
            f"the restart's first Record runs its 0.2 s, not a burst "
            f"({eng.phase}, {eng.fault!r})")
    eng.abort()


def check_roll_rebases_frames(r: Report) -> None:
    """A deferred file roll restarts the file's frame count: a frames Record
    and its run re-baseline on the new file, or it runs the old count long."""
    from acqApp.adapters.routines import RoutinesModule

    rig = FakeRig(hz=100.0)
    rig.advance(1.0)                    # the old file already holds 100 frames
    rec = Routine(steps=[Step(kind="record", length=10, unit="frames")])
    eng = RoutineEngine(rec, rig.hooks())
    eng.start()
    a = RoutinesModule.__new__(RoutinesModule)
    a._status = lambda _m: None
    a._timer = a._hold_t0 = a._seal_after = a._rec = None
    a._routine = rec
    a.win = SimpleNamespace(camera_ready=lambda _k: True)

    def roll(_run) -> bool:
        rig._gated_total = rig.t        # the new file counts from 0
        return True

    a._roll_for = roll
    a._pending_roll_run = SimpleNamespace()
    a._tick_once(eng)
    drive(eng, rig, limit_s=rig.t + 0.5)
    run = eng.runs[-1] if eng.runs else None
    r.check(eng.phase == Phase.DONE and run is not None and run.frame0 == 0
            and abs(run.frames - 10) <= 1,
            f"the Record takes its 10 frames of the new file "
            f"({eng.phase}, frame0={run and run.frame0}, "
            f"frames={run and run.frames})")


def _part_files() -> int:
    r = Report("routine-files")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_routine_files_"))
    try:
        check_trigger_step(r)
        check_trigger_gate(r)
        check_burst(r)
        check_burst_retry(r)
        check_prepare_recording(r)
        check_prepared_adapter(r)
        check_burst_limit(r)
        check_seal_after_burst(r)
        check_first_trial_kept(r)
        check_edge_log(r)
        check_first_file_doomed(r)
        check_doomed_file_deleted(r, tmp)
        check_bad_trial_marked(r, tmp)
        check_arm_camera_trigger(r)
        check_hand_edited_load(r)
        check_adapter_guards(r)
        check_abort_is_not_finish(r)
        check_restart_forgets_edge(r)
        check_roll_rebases_frames(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


PARTS = {"routine-files": _part_files}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
