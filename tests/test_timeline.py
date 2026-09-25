"""
The routine timeline (`routines/timeline.py`) — one cycle drawn to scale.

The one thing worth pinning down with a test, not just eyeballing a dialog:
**a Record step inside a repeated Group must draw as
SEPARATE bars, one per repeat** — the same correctness point
`test_routines.py` checks for the engine's actual file boundaries. A drawing
that silently merged them would be worse than no drawing at all: it would
show the operator something that isn't what the engine will do.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_timeline.py
"""
from __future__ import annotations

import sys

from _harness import Report, isolate_user_state, qt_app

from acqApp.routines.settings import Group, Routine, Step, play_order
from acqApp.routines.timeline import _group_spans, _layout, _recording_runs


def check_layout(r: Report) -> None:
    routine = Routine(steps=[
        Step(kind="move", x_um=1, y_um=2),
        Step(kind="display", pattern="p.png"),
        Step(kind="wait", length=5, unit="seconds"),
        Step(kind="puff"),
        Step(kind="wait", length=100, unit="frames"),
    ])
    segs, total_w = _layout(routine, hz=30.0)
    r.check(len(segs) == 5, "one segment per step, no camera/repeats involved")
    r.check(total_w > 0, "a non-empty routine has a non-zero width")
    order = [s.kind for s in segs]
    r.check(order == ["move", "display", "wait", "puff", "wait"],
           "segments follow the step order")

    wait5s = segs[2]
    wait100f_known = segs[4]
    r.check(wait5s.duration_s == 5.0, "a seconds-unit Wait's duration is exact")
    r.check(abs(wait100f_known.duration_s - 100 / 30.0) < 1e-9,
           "a frames-unit Wait converts at the given rate, like estimate.py")
    r.check(segs[2].width > segs[3].width,
           "a longer Wait draws wider than an instant Puff")

    segs_no_hz, _ = _layout(routine, hz=None)
    r.check(segs_no_hz[4].duration_s is None,
           "a frames-unit Wait with no frame rate is UNKNOWN, not guessed")
    r.check(segs_no_hz[2].duration_s == 5.0,
           "…but a seconds-unit Wait next to it is still exact — unknown is "
           "per-segment, not all-or-nothing")


def check_group_span(r: Report) -> None:
    routine = Routine(
        steps=[Step(kind="wait", length=1, unit="seconds") for _ in range(4)],
        groups=[Group(start=1, end=2, repeats=3)],
    )
    order = play_order(routine)
    r.check(order == [0, 1, 2, 1, 2, 1, 2, 3],
           "control: the group's range repeats in place in play_order")
    spans = _group_spans(routine, order)
    r.check(spans == [(1, 6, 3)],
           "one bracket for the WHOLE repeated run (positions 1..6), "
           "labelled with the true repeat count — not three separate "
           "brackets, since the segments underneath already show the repeats")


def check_recording_runs_separate(r: Report) -> None:
    """The point of the feature: repeats never draw as one merged bar."""
    routine = Routine(
        steps=[Step(kind="wait", length=1, unit="seconds"),
               Step(kind="record", length=1, unit="seconds"),
               Step(kind="wait", length=1, unit="seconds")],
        groups=[Group(start=1, end=1, repeats=3)],
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(len(runs) == 3,
           "a Record step in a repeated group draws as "
           "3 separate bars, one per repeat — never one merged bar")
    r.check(runs == [(1, 1, 0), (2, 2, 0), (3, 3, 0)],
           "…each bar covering exactly one repeat's position, same region "
           "(there is only one Record step, region 0)")
    starts = [a for a, _b, _c in runs]
    ends = [b for _a, b, _c in runs]
    r.check(all(a <= b for a, b in zip(starts, ends)),
           "every run is a valid (start <= end) range")
    gaps = all(runs[i][1] < runs[i + 1][0] for i in range(len(runs) - 1))
    r.check(gaps, "consecutive runs don't touch — a real gap separates them, "
                  "matching the bars drawn with a gap either side")


def check_recording_runs_not_grouped(r: Report) -> None:
    """No Group at all — adjacent Record steps are each their own run."""
    routine = Routine(
        steps=[Step(kind="record", length=1, unit="seconds") for _ in range(3)],
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(runs == [(0, 0, 0), (1, 1, 1), (2, 2, 2)],
           "adjacent Record steps draw as three bars, one region each")


def check_recording_runs_across_cycle(r: Report) -> None:
    """cycles > 1 isn't drawn (one cycle only, per the module's own
    docstring) — `_recording_runs` operates on a single `play_order` pass,
    so this just confirms it stays a single run within that one pass even
    though the routine as a whole will run it twice."""
    routine = Routine(
        steps=[Step(kind="record", length=1, unit="seconds")],
        cycles=2,
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(runs == [(0, 0, 0)],
           "one pass drawn = one run, regardless of routine.cycles (the "
           "engine's own per-cycle splitting is a separate, tested "
           "correctness point in test_routines.py — this file only pins "
           "down what gets DRAWN)")


def check_widget_builds(r: Report) -> None:
    """The dialog and its canvas construct without error, empty routine
    included — this is what `panel.py`'s Timeline… button actually calls."""
    isolate_user_state()
    app = qt_app()                      # assign it: an unreferenced one is GC'd
    from acqApp.routines.timeline import TimelineDialog

    empty = TimelineDialog(Routine(), None)
    r.check(empty is not None, "an empty routine builds a dialog, not a crash")

    routine = Routine(
        steps=[Step(kind="move", x_um=1, y_um=2),
              Step(kind="display", pattern="p.png"),
              Step(kind="record", length=5, unit="seconds"),
              Step(kind="puff")],
        groups=[Group(start=1, end=2, repeats=2)],
        cycles=3,
    )
    dlg = TimelineDialog(routine, hz=30.0)
    dlg.show()
    app.processEvents()
    r.check(dlg.isVisible(), "a populated routine's timeline shows")
    dlg.close()

    no_hz = TimelineDialog(routine, hz=None)
    no_hz.show()
    app.processEvents()
    r.check(no_hz.isVisible(),
           "…and still shows with no frame rate set (unknown-duration path)")
    no_hz.close()


def main() -> int:
    r = Report("timeline")
    check_layout(r)
    check_group_span(r)
    check_recording_runs_separate(r)
    check_recording_runs_not_grouped(r)
    check_recording_runs_across_cycle(r)
    check_widget_builds(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
