"""Experiment routines: the protocol, and the engine that executes it.

This is the first feature in the app whose whole purpose is to **actuate** — it
drives the stage and puts light on the sample without an operator watching each
step. So everything that decides is Qt-free and callable-driven, and this drives
all of it against a fake rig on a fake clock: no window, no device, ~1 s.

What it defends, in the order the decisions were made (PLAN §6):

  * **A step's length is frames OR seconds and the two are never converted.**
    At 106 fps a rounded conversion sheds frames at every step boundary. The
    control is a camera running off its nominal rate: a converting engine ends
    the seconds step at a visibly different frame count.
  * **Validation is up front.** A stage target outside the soft limits is a
    refusal at the Start button, not a fault at step 7 of 12.
  * **A fault pauses, it does not abort** — motion stopped, light off, capture
    untouched — and the interrupted step's data is **kept and marked**, with
    resume repeating that step as a fresh attempt.
  * **Per-step files stay relatable.** Each carries the session origin and its
    own t0 on the shared clock; files that each restarted from zero could not
    be reassembled once the animal is off the rig.
  * **The light is never on while the stage travels.**

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_routines.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from _harness import Report, isolate_user_state, pump, qt_app

from acqApp.routines.engine import Phase, RoutineEngine, RoutineError, RoutineHooks
from acqApp.routines.settings import (UNITS, RigLimits, Routine, Step,
                                      validate)

FULL_RIG = RigLimits(x_um=(-5000.0, 5000.0), y_um=(-5000.0, 5000.0),
                     has_stage=True, has_dmd=True, has_frames=True)
DT = 0.01                      # the worker's tick, near enough


# ── the fake rig ──────────────────────────────────────────────────────────────

class FakeRig:
    """A stage, a projector and a camera, on a clock the test drives.

    Records every actuating call in `log`, which is how the ordering checks
    (light off before a move, light on only during capture) are made at all.
    """

    def __init__(self, fps: float = 106.0, travel_s: float = 0.0) -> None:
        self.t = 0.0
        self.fps = fps
        self.travel_s = travel_s
        self.log: list[tuple] = []
        self.lit = False
        self.frames_running = True
        self._arrive_at: float | None = None
        self.fail_move = False
        self.fail_light = False
        self.begun: list = []
        self.ended: list = []

    # clock + camera
    def now(self) -> float:
        return self.t

    def frames(self) -> int | None:
        return int(self.t * self.fps) if self.frames_running else None

    def advance(self, dt: float = DT) -> None:
        self.t += dt

    # stage
    def move(self, x, y) -> None:
        self.log.append(("move", x, y))
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

    # file boundaries
    def begin_step(self, run) -> None:
        self.log.append(("begin", run.index, run.cycle, run.attempt))
        self.begun.append(run)

    def end_step(self, run) -> None:
        self.log.append(("end", run.index, run.cycle, run.attempt))
        self.ended.append(run)

    def hooks(self) -> RoutineHooks:
        return RoutineHooks(now=self.now, frames=self.frames, move=self.move,
                            moving=self.moving, stop_motion=self.stop_motion,
                            set_pattern=self.set_pattern, light=self.light,
                            begin_step=self.begin_step, end_step=self.end_step,
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

    good = Routine(steps=[Step(x_um=100.0, y_um=-200.0, pattern=str(pattern),
                               project=True, length=100, unit="frames")])
    r.check(validate(good, FULL_RIG) == [],
            "control: a valid routine on a full rig is accepted")

    cases = [
        ("a stage target outside the soft limits",
         Routine(steps=[Step(x_um=9_000.0)]), FULL_RIG, "soft limits"),
        ("a frames step with no camera loaded",
         Routine(steps=[Step(length=100, unit="frames")]),
         RigLimits(has_frames=False), "no camera"),
        ("a step that projects with no DMD loaded",
         Routine(steps=[Step(project=True, length=1, unit="seconds")]),
         RigLimits(), "DMD"),
        ("a step that moves with no stage loaded",
         Routine(steps=[Step(x_um=10.0, length=1, unit="seconds")]),
         RigLimits(), "stage"),
        ("a zero-length step",
         Routine(steps=[Step(length=0, unit="seconds")]), FULL_RIG, "above zero"),
        ("a fractional frame count",
         Routine(steps=[Step(length=10.5, unit="frames")]), FULL_RIG, "whole"),
        ("a negative settle",
         Routine(steps=[Step(settle_s=-1.0, length=1, unit="seconds")]),
         FULL_RIG, "negative"),
        ("a pattern file that is not there",
         Routine(steps=[Step(pattern=str(tmp / "gone.png"), length=1,
                             unit="seconds")]), FULL_RIG, "not a file"),
        ("an empty routine", Routine(steps=[]), FULL_RIG, "no steps"),
    ]
    for label, routine, rig, needle in cases:
        problems = validate(routine, rig)
        r.check(any(needle in p for p in problems),
                f"refused: {label} ({problems[:1] or 'NOTHING SAID'})")

    # The limits are per axis, and y must not be checked against x's.
    tall = RigLimits(x_um=(-100.0, 100.0), y_um=(-9000.0, 9000.0),
                     has_stage=True, has_frames=True)
    r.check(validate(Routine(steps=[Step(y_um=5000.0, length=1,
                                         unit="seconds")]), tall) == [],
            "a target legal on ITS axis is not refused by the other axis")
    r.check(validate(Routine(steps=[Step(x_um=5000.0, length=1,
                                         unit="seconds")]), tall) != [],
            "…and the same number on the narrow axis still is")

    # Persistence round trip: the panel saves this into acqapp_local.json.
    src = Routine(name="grid", cycles=3, save_mode="per_step",
                  steps=[Step(label="a", x_um=1.0, length=5, unit="frames"),
                         Step(label="b", length=2.5, unit="seconds")])
    back = Routine.from_dict(src.to_dict())
    r.check(back == src, "a routine survives the JSON round trip unchanged")
    r.check(Routine.from_dict({"steps": [{"gone": 1, "label": "x"}],
                               "cycles": "nonsense"}).steps[0].label == "x",
            "a stale saved routine drops unknown keys rather than raising")


# ── frames and seconds are different units ────────────────────────────────────

def check_units(r: Report) -> None:
    """The operator's first decision: a step ends on the unit it was GIVEN.

    The control is a camera running below its nominal rate. An engine that
    converted seconds to frames at the nominal rate would end the seconds step
    at a frame count this one demonstrably does not produce.
    """
    nominal, actual = 106.0, 97.0
    rig = FakeRig(fps=actual)
    routine = Routine(steps=[Step(label="A", length=100, unit="frames",
                                  settle_s=0.0),
                             Step(label="B", length=1.5, unit="seconds",
                                  settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    r.check(eng.phase == Phase.DONE and len(eng.runs) == 2,
            f"both steps ran (phase={eng.phase}, {len(eng.runs)} runs)")
    a, b = eng.runs
    r.check(a.frames is not None and 100 <= a.frames <= 102,
            f"the frames step ended on its FRAME count ({a.frames})")
    r.check(abs((a.t_end - a.t0) - 100 / actual) < 3 * DT,
            f"…and took the time that implies at {actual:g} fps "
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
    """Blank, move, pattern, settle, light, capture, blank. In that order."""
    pattern = tmp / "bar.png"
    pattern.write_bytes(b"x")
    rig = FakeRig(travel_s=0.30)
    routine = Routine(steps=[Step(x_um=50.0, y_um=60.0, pattern=str(pattern),
                                  project=True, length=0.20, unit="seconds",
                                  settle_s=0.25)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    kinds = [e[0] for e in rig.log]
    r.check(kinds[:4] == ["light", "move", "pattern", "light"],
            f"blank, move, pattern, then light ({kinds[:4]})")
    r.check(rig.log[0] == ("light", False) and rig.log[3] == ("light", True),
            "the first light call is OFF and the one before capture is ON")

    # The control that matters on a rig with a sample under the objective.
    lit = False
    travelling_lit = False
    for e in rig.log:
        if e[0] == "light":
            lit = e[1]
        if e[0] == "move" and lit:
            travelling_lit = True
    r.check(not travelling_lit, "the light is never on when a move is issued")
    r.check(not rig.lit and rig.log[-1][0] in ("light", "end"),
            "the light is off when the routine finishes")

    run = eng.runs[0]
    r.check(run.t0 >= 0.30 + 0.25 - DT,
            f"capture waited for arrival AND the settle ({run.t0:.3f} s "
            f"after 0.30 s of travel + 0.25 s settle)")

    # Control: with no travel and no settle, capture starts at the first tick.
    quick = FakeRig()
    e2 = RoutineEngine(Routine(steps=[Step(length=0.1, unit="seconds",
                                           settle_s=0.0)]), quick.hooks())
    e2.start()
    quick.advance()
    e2.tick()
    r.check(e2.phase == Phase.CAPTURE,
            "control: no travel and no settle starts capturing immediately")


def check_move_timeout(r: Report) -> None:
    """A stage that never reports arrival must pause, not hang forever."""
    rig = FakeRig(travel_s=1e9)          # never arrives
    eng = RoutineEngine(Routine(steps=[Step(x_um=10.0, length=1,
                                            unit="seconds")]),
                        rig.hooks(), move_timeout_s=2.0)
    eng.start()
    drive(eng, rig, limit_s=10.0)
    r.check(eng.phase == Phase.PAUSED and "did not arrive" in eng.fault,
            f"a stage that never arrives pauses the routine ({eng.fault!r})")
    r.check(("stop_motion",) in rig.log, "…and motion is stopped")
    r.check(not rig.lit, "…and the light is off")
    r.check(rig.t < 10.0, "…without running to the test's own limit")


# ── a fault pauses; the partial data is kept ──────────────────────────────────

def check_pause_keeps_data(r: Report) -> None:
    """PLAN §6 (4): pause everything that is not capture, and keep the frames."""
    rig = FakeRig()
    routine = Routine(steps=[Step(label="A", length=1.0, unit="seconds",
                                  settle_s=0.0, project=True),
                             Step(label="B", length=1.0, unit="seconds",
                                  settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.CAPTURE and rig.t > 0.4)
    r.check(eng.phase == Phase.CAPTURE and rig.lit,
            "mid-step, capturing, light on")

    eng.pause("the stage stopped answering")
    r.check(eng.phase == Phase.PAUSED, "a fault pauses the routine")
    r.check(not rig.lit and ("stop_motion",) in rig.log,
            "…light off and motion stopped")

    r.check(len(eng.runs) == 1 and eng.runs[0].interrupted,
            "the half-finished step is KEPT, marked interrupted")
    r.check(eng.runs[0].fault == "the stage stopped answering",
            "…carrying the reason into the file")
    r.check(eng.runs[0].frames and eng.runs[0].frames > 0,
            f"…with the frames it did get ({eng.runs[0].frames})")
    r.check(len(rig.ended) == 1 and rig.ended[0] is eng.runs[0],
            "…and its file boundary was closed, not abandoned")
    r.check(eng.steps_done() == 0,
            "an interrupted step does not count as done")

    # Capture is the operator's: the engine has no way to stop it, and the
    # step it was in the middle of is still the current one.
    r.check(eng.position[0] == 0, "the paused step is still the current step")

    # Resume repeats the step as a fresh attempt.
    before = len([e for e in rig.log if e[0] == "begin"])
    eng.resume()
    drive(eng, rig)
    r.check(eng.phase == Phase.DONE, f"resume runs to the end ({eng.phase})")
    attempts = [(x.index, x.attempt, x.interrupted) for x in eng.runs]
    r.check(attempts == [(0, 1, True), (0, 2, False), (1, 1, False)],
            f"resume REPEATS the paused step as attempt 2 ({attempts})")
    r.check(len([e for e in rig.log if e[0] == "begin"]) == before + 2,
            "…and the repeat opens its own file boundary")
    r.check(eng.steps_done() == 2, "two steps completed, the abandoned one not")


def check_skip(r: Report) -> None:
    """The other way out of a pause: give up on this step, take the next."""
    rig = FakeRig()
    routine = Routine(steps=[Step(label="A", length=1.0, unit="seconds",
                                  settle_s=0.0),
                             Step(label="B", length=0.2, unit="seconds",
                                  settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.CAPTURE and rig.t > 0.3)
    eng.pause("operator")
    eng.skip()
    drive(eng, rig)
    got = [(x.index, x.attempt, x.interrupted) for x in eng.runs]
    r.check(eng.phase == Phase.DONE and got == [(0, 1, True), (1, 1, False)],
            f"skip drops the step and runs the next one ({got})")


def check_setup_failure(r: Report) -> None:
    """A move that raises must pause BEFORE any light reaches the sample."""
    rig = FakeRig()
    rig.fail_move = True
    eng = RoutineEngine(Routine(steps=[Step(x_um=10.0, project=True,
                                            length=1, unit="seconds")]),
                        rig.hooks())
    eng.start()
    r.check(eng.phase == Phase.PAUSED and "setup failed" in eng.fault,
            f"a failing move pauses at setup ({eng.fault!r})")
    r.check(("light", True) not in rig.log,
            "the light was never turned on for a step that never started")
    r.check(eng.runs == [], "no run is filed for a step that never captured")

    # And the projector failing at the top of capture is the same shape.
    rig2 = FakeRig()
    rig2.fail_light = True
    e2 = RoutineEngine(Routine(steps=[Step(project=True, length=1,
                                           unit="seconds", settle_s=0.0)]),
                       rig2.hooks())
    e2.start()
    rig2.advance()
    e2.tick()
    r.check(e2.phase == Phase.PAUSED and "could not start" in e2.fault,
            f"a projector that will not light pauses the step ({e2.fault!r})")
    r.check(rig2.begun == [], "…and no file boundary was opened for it")


def check_frames_vanish(r: Report) -> None:
    """The camera going away mid-step is a fault, not a step that never ends."""
    rig = FakeRig()
    eng = RoutineEngine(Routine(steps=[Step(length=1000, unit="frames",
                                            settle_s=0.0)]), rig.hooks())
    eng.start()
    drive(eng, rig, until=lambda e: e.phase == Phase.CAPTURE and rig.t > 0.2)
    rig.frames_running = False
    rig.advance()
    eng.tick()
    r.check(eng.phase == Phase.PAUSED and "frame count" in eng.fault,
            f"a frames step whose counter vanishes pauses ({eng.fault!r})")


# ── cycles, and the per-step file guarantee ───────────────────────────────────

def check_cycles_and_attrs(r: Report) -> None:
    """Repeats run in order, and every step file can be put back on one clock."""
    rig = FakeRig()
    routine = Routine(name="grid", cycles=3, save_mode="per_step",
                      steps=[Step(label="A", x_um=0.0, length=0.10,
                                  unit="seconds", settle_s=0.0),
                             Step(label="B", x_um=50.0, length=0.10,
                                  unit="seconds", settle_s=0.0)])
    eng = RoutineEngine(routine, rig.hooks())
    eng.start()
    drive(eng, rig)

    order = [(x.cycle, x.index) for x in eng.runs]
    r.check(order == [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)],
            f"3 cycles x 2 steps run in order ({order})")
    r.check(len(rig.begun) == 6 and len(rig.ended) == 6,
            f"one file boundary per step execution "
            f"({len(rig.begun)} opened, {len(rig.ended)} closed)")
    r.check(routine.total_steps() == 6,
            "the routine says up front how many executions that is")

    # The reassembly guarantee. Per-step files that each restart from zero are
    # not relatable afterwards — so every one carries the same session origin
    # and its own t0 on that clock.
    origin = 1234.5
    attrs = [x.attrs(session_origin=origin) for x in eng.runs]
    r.check(all(a["routine_session_origin"] == origin for a in attrs),
            "every step file names the same session origin")
    t0s = [a["routine_step_t0"] for a in attrs]
    r.check(all(b > a for a, b in zip(t0s, t0s[1:])),
            f"…and its own t0, strictly increasing across the folder "
            f"({[round(t, 2) for t in t0s]})")
    r.check(t0s[0] < t0s[-1] and t0s[-1] > 0.5,
            "…on the SHARED clock, not restarted per file")
    r.check(all(a["routine_step_cycle"] == c and a["routine_step_index"] == i
                for a, (c, i) in zip(attrs, order)),
            "…and enough identity to say which step it was")

    r.check([a["routine_step_interrupted"] for a in attrs] == [False] * 6,
            "a clean run marks nothing interrupted")


def check_transitions(r: Report) -> None:
    """The control surface refuses what it cannot do, rather than misbehaving."""
    rig = FakeRig()
    eng = RoutineEngine(Routine(steps=[Step(length=0.1, unit="seconds",
                                            settle_s=0.0)]), rig.hooks())
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
        panel.set_state(Phase.CAPTURE, f"step 1/2 — capturing {i} %")
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


def check_step_table(r: Report, app) -> None:
    """The step list edits through widgets, not through typed words.

    Every field used to be free text in a table cell — "yes"/"no" for the
    light, "frames"/"seconds" for the unit, a blank cell for "leave this axis".
    That parses, and a typo reads back as the old value with nothing said. The
    contract now is: **the value lives in the cell's data, the text is a
    rendering of it**, so a cell can only hold something the editor could
    produce.
    """
    from PyQt6.QtCore import Qt

    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import FIELDS, VALUE

    routine = Routine(steps=[Step(label="one", length=100, unit="frames"),
                             Step(label="two", length=2.0, unit="seconds")])
    panel = SettingsPanel(routine)
    tbl = panel._tbl
    col = {f: i for i, f in enumerate(FIELDS)}

    edits: list = []
    panel.settings_changed.connect(edits.append)

    # What actually opens when a cell is edited. Asked of the delegate the way
    # the view asks it, because "it is a dropdown" is the whole request here —
    # inspecting the delegate's own list would pass even if it never built one.
    from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QLineEdit,
                                 QStyleOptionViewItem)

    def editor(row: int, field: str):
        c = col[field]
        return tbl.itemDelegateForColumn(c).createEditor(
            tbl, QStyleOptionViewItem(), tbl.model().index(row, c))

    ed = editor(0, "project")
    r.check(isinstance(ed, QComboBox),
            f"the Light cell opens a drop-down ({type(ed).__name__})")
    r.check([ed.itemData(i) for i in range(ed.count())] == [False, True],
            "…offering exactly no/yes")
    ed = editor(0, "unit")
    r.check(isinstance(ed, QComboBox),
            f"the Unit cell opens a drop-down ({type(ed).__name__})")
    r.check(tuple(ed.itemData(i) for i in range(ed.count())) == UNITS,
            f"…offering exactly settings.UNITS ({UNITS}) — so the panel cannot "
            f"drift from what validate() accepts")
    ed = editor(0, "settle_s")
    r.check(isinstance(ed, QDoubleSpinBox) and ed.suffix() == " s",
            f"a duration cell opens a spin box in seconds ({type(ed).__name__})")
    ed = editor(0, "x_um")
    r.check(isinstance(ed, QDoubleSpinBox) and ed.specialValueText() == "leave",
            "an axis cell opens a spin box with a 'leave' state")
    # CONTROL: the free-text field is still free text — the point is typed
    # editors where the value is constrained, not spin boxes everywhere.
    idx = tbl.model().index(0, col["label"])
    # itemDelegateForIndex, not …ForColumn: the name column has no delegate of
    # its own, which is exactly the claim — it falls back to the plain one.
    ed = tbl.itemDelegateForIndex(idx).createEditor(
        tbl, QStyleOptionViewItem(), idx)
    r.check(isinstance(ed, QLineEdit),
            f"control: the Step name is still typed ({type(ed).__name__})")

    # What a delegate does when the operator picks something: it writes the
    # VALUE, and the table renders the text from it.
    tbl.item(0, col["project"]).setData(VALUE, True)
    r.check(routine.steps[0].project is True,
            "choosing 'yes' in the Light cell sets project on the step")
    r.check(tbl.item(0, col["project"]).text() == "yes",
            f"…and the cell reads back as yes "
            f"({tbl.item(0, col['project']).text()!r})")
    r.check(len(edits) == 1, f"…as ONE settings change, not two ({len(edits)})")

    # CONTROL: text alone is not a value. This is the old failure mode — a
    # word typed into the cell deciding what the step does.
    before = routine.steps[1].project
    tbl.item(1, col["project"]).setText("yes")
    r.check(routine.steps[1].project == before,
            "control: text typed into a cell cannot set a value the editor "
            "would not produce")

    tbl.item(0, col["unit"]).setData(VALUE, "seconds")
    r.check(routine.steps[0].unit == "seconds",
            "the Unit cell sets the unit")

    # The optional axes: "leave" is a state, not an empty string.
    tbl.item(0, col["x_um"]).setData(VALUE, 250.0)
    r.check(routine.steps[0].x_um == 250.0 and
            tbl.item(0, col["x_um"]).text() == "250 um",
            f"an axis takes a number ({tbl.item(0, col['x_um']).text()!r})")
    tbl.item(0, col["x_um"]).setData(VALUE, None)
    r.check(routine.steps[0].x_um is None and
            tbl.item(0, col["x_um"]).text() == "leave",
            f"…and clears back to 'leave' ({tbl.item(0, col['x_um']).text()!r})")

    # A frames step is a whole number of frames — validate() refuses the
    # alternative, so the panel rounds rather than letting Start refuse it.
    tbl.item(1, col["unit"]).setData(VALUE, "frames")
    tbl.item(1, col["length"]).setData(VALUE, 100.4)
    r.check(routine.steps[1].length == 100,
            f"a fractional length on a frames step is rounded "
            f"({routine.steps[1].length})")
    r.check(not [p for p in validate(routine, RigLimits(has_frames=True))
                 if "whole number" in p],
            "…so the routine validates instead of being refused at Start")
    # CONTROL: seconds are not rounded — the rounding is about frames, not
    # about tidy numbers.
    tbl.item(1, col["unit"]).setData(VALUE, "seconds")
    tbl.item(1, col["length"]).setData(VALUE, 2.5)
    r.check(routine.steps[1].length == 2.5,
            f"control: a seconds step keeps its fraction ({routine.steps[1].length})")

    # Reordering. Until this existed the only way to move a step was to delete
    # it and retype it.
    panel._tbl.select_row(0)
    panel._move_down()
    r.check([x.label for x in routine.steps] == ["two", "one"],
            f"a step moves down the list ({[x.label for x in routine.steps]})")
    r.check(panel._tbl.selected_row() == 1,
            "…and the selection follows it, so a second press moves the same step")
    panel._move_up()
    r.check([x.label for x in routine.steps] == ["one", "two"],
            "…and back up again")

    # The pattern is chosen with a file dialog, so clearing it needs its own
    # control: cancelling a dialog means "changed my mind", not "no pattern".
    routine.steps[0].pattern = r"C:\patterns\grid.png"
    panel._reload_table()
    r.check(tbl.item(0, col["pattern"]).text() == "grid.png",
            f"the pattern cell shows the file's name "
            f"({tbl.item(0, col['pattern']).text()!r})")
    r.check(not (tbl.item(0, col["pattern"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…and is not typed into")
    panel._tbl.select_row(0)
    panel._clear_pattern()
    r.check(routine.steps[0].pattern == "" and
            tbl.item(0, col["pattern"]).text() == "—",
            "'No pattern' clears it back to whatever the DMD has")

    # The summary is the only place the whole protocol is totalled up.
    routine.steps[0].project = True
    panel._refresh_summary()
    text = panel._lbl_summary.text()
    r.check("2 run(s)" in text and "emit light" in text,
            f"the summary says how much work it is and that it emits light "
            f"({text!r})")
    routine.steps[0].project = False
    routine.steps[1].project = False
    panel._refresh_summary()
    r.check("emit light" not in panel._lbl_summary.text(),
            f"control: with nothing projecting it does not warn "
            f"({panel._lbl_summary.text()!r})")

    # Which step is running is shown in the protocol, not only in the label.
    panel.set_state(Phase.CAPTURE, "step 2/2", 1)
    r.check(tbl.item(1, 0).font().bold() and not tbl.item(0, 0).font().bold(),
            "the running step is bold in the table")
    panel.set_state(Phase.DONE, "finished", None)
    r.check(not tbl.item(1, 0).font().bold(),
            "…and nothing is bold once it is over")
    app.processEvents()


# ── the whole app ─────────────────────────────────────────────────────────────

def check_app(r: Report, app, tmp) -> None:
    """The wiring: the panel refuses, the engine drives real adapters, and the
    step boundaries reach the file on the shared clock.

    Everything above this runs on fakes. This runs the actual window in mock
    mode, because the seam that matters is `ModuleHost.stage_target` /
    `pattern_target` — pooled by the window so the routine never imports the
    stage, which is exactly the kind of link a unit test cannot see.
    """
    import h5py

    import acqApp.main as M

    out = tmp / "routine_rec"
    win = M.MainWindow(cam_info=None, mock=True,
                       enabled={"voltage_cam", "stage", "dmd", "routines"},
                       cam_handle=None)
    mod = {m.key: m for m in win._modules}
    adapter, panel = mod["routines"], mod["routines"].panel

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_subject.setText("routine")
    win._save_panel._ed_template.setText("{subject}_{date}_{time}")
    win._save_panel._on_edited()

    # The stage link is session-scoped, so the targets only exist once a session
    # does — which is also when a routine could possibly run.
    r.check(win.stage_target() is None,
            "no stage target before a session — nothing to drive yet")
    win._btn_run.setChecked(True)

    r.check(win.stage_target() is mod["stage"],
            "the window offers the loaded stage as a routine target")
    r.check(win.pattern_target() is mod["dmd"],
            "…and the loaded DMD as a pattern target")

    # `StageTarget.stop_motion` says it must not raise, and it runs when the
    # stage has ALREADY failed — the engine calls it on every fault. A dead
    # serial link is exactly when it is reached.
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
    # Deliberately off-centre: the midpoint of a symmetric travel is 0.0, and a
    # check against 0.0 passes for a stage that was never commanded at all.
    inside = lo_x + (hi_x - lo_x) * 0.37
    inside_y = lo_y + (hi_y - lo_y) * 0.62

    # ── refusals, before anything is recorded or moved ──
    panel._r.steps = [Step(label="one", x_um=inside, length=5, unit="frames",
                           settle_s=0.0),
                      Step(label="two", x_um=inside, y_um=inside_y,
                           project=True, length=0.30, unit="seconds",
                           settle_s=0.05)]
    panel._reload_table()

    # ── Start opens the recording it needs ──
    # It used to refuse until the operator had found the Record button in
    # another part of the window, which only moved the failure earlier.
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
        # Today the recording refuses first — a routine cannot run without one.
        # `busy_reason` is the mechanism that survives per-step file rolling,
        # where the recorder is closed between steps; it is controlled below.
        r.check(True, f"set_modules is refused while a routine runs ({e})")

    for _ in range(120):
        win._display_tick()
        pump(app, 0.02)
        if adapter._engine.phase == Phase.DONE:
            break

    eng = adapter._engine
    phase, runs, filed = eng.phase, list(eng.runs), adapter._filed
    win._btn_rec.setChecked(False)
    win._btn_run.setChecked(False)
    win.close()
    pump(app, 0.2)

    r.check(phase == Phase.DONE, f"the routine ran to the end (phase={phase})")
    r.check(len(runs) == 2 and not any(x.interrupted for x in runs),
            f"both steps completed cleanly ({len(runs)} runs)")
    r.check(("x", inside) in moved and ("y", inside_y) in moved,
            f"the stage really was commanded through the host ({moved})")
    r.check(sum(1 for a, _ in moved if a == "y") == 1,
            f"…and an axis left blank in a step is left where it is "
            f"({[a for a, _ in moved]})")
    r.check(lit and lit[-1] is False and True in lit,
            f"the light went on for the projecting step and off after ({lit})")

    frames_run = runs[0]
    r.check(frames_run.frames is not None and frames_run.frames >= 5,
            f"the frames step counted frames that reached the FILE "
            f"({frames_run.frames})")
    r.check(filed == 4, f"four step boundaries were filed (got {filed})")

    # Control for the check above: the window must really ask every adapter,
    # not just look at its own recorder.
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
                f"one entry per boundary, opening and closing ({vals})")
        r.check(vals[0] > 0 and vals[1] < 0,
                f"…the sign says which edge it is ({vals[:2]})")
        a = dict(f.attrs)
        r.check(a.get("routine_started") in (True, 1, "True"),
                "the file says a routine actually ran")
        r.check(int(a.get("routine_steps_done", 0)) == 2,
                f"…and how many steps finished ({a.get('routine_steps_done')})")
        r.check(int(a.get("routine_n_steps", 0)) == 2
                and "one" in str(a.get("routine_steps", "")),
                "…and carries the protocol itself, which nothing else records")
        # `/routine` gives the boundaries but only a signed index. Which
        # execution faulted, and when each one started on the session clock,
        # is recoverable from nothing else in the file.
        runs = json.loads(str(a.get("routine_runs", "[]")))
        r.check(len(runs) == 2 and [x["routine_step_index"] for x in runs] == [0, 1],
                f"…and every step EXECUTION, not just the counts ({len(runs)})")
        r.check(all(x["routine_step_interrupted"] is False for x in runs)
                and runs[1]["routine_step_t0"] > runs[0]["routine_step_t0"],
                "…each with its own t0 on the shared clock and its fault flag")


def main() -> int:
    r = Report("routines")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_routines_"))
    try:
        check_validation(r, tmp)
        check_units(r)
        check_order(r, tmp)
        check_move_timeout(r)
        check_pause_keeps_data(r)
        check_skip(r)
        check_setup_failure(r)
        check_frames_vanish(r)
        check_cycles_and_attrs(r)
        check_transitions(r)
        # The window persists as a side effect of ordinary use, so isolate
        # first — an unisolated run overwrites the operator's save folder.
        state = isolate_user_state()
        try:
            sys.argv = ["main.py", "--mock"]
            app = qt_app()
            check_panel_repaint(r, app)
            check_step_table(r, app)
            check_app(r, app, state)
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
