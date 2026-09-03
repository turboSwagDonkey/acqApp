"""
Visual stim: trial expansion, grating/aperture geometry, settings round-trip,
the shared-clock-tick-driven priming/gating state machine, and hot-load/
unload — ported from visStimCode's .m files.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_vis_stim.py
"""
from __future__ import annotations

import sys

from _harness import Report, isolate_user_state, pump, qt_app


def check_trials(r: Report) -> None:
    from acqApp.devices.vis_stim.settings import LoopVar, StimParams
    from acqApp.devices.vis_stim.trials import gen_param_combos

    base = StimParams()
    loops = {
        "Orientation": LoopVar("Orientation", (0.0, 90.0)),
        "Contrast":    LoopVar("Contrast", (0.25, 0.5, 1.0)),
    }
    trials = gen_param_combos(base, loops)
    r.check(len(trials) == 6, f"2x3 loop expands to 6 trials ({len(trials)})")
    combos = {(t.Orientation, t.Contrast) for t in trials}
    r.check(combos == {(o, c) for o in (0.0, 90.0) for c in (0.25, 0.5, 1.0)},
            "every combination appears exactly once")
    r.check(gen_param_combos(base, {}) == [base],
            "no loops -> the base params, unchanged")

    unknown = {"NotAField": LoopVar("NotAField", (1.0, 2.0))}
    r.check(gen_param_combos(base, unknown) == [base],
            "a loop naming an unknown field is skipped, not raised")


def check_grating(r: Report) -> None:
    import numpy as np

    from acqApp.devices.vis_stim.grating import aperture_geometry, build_grating
    from acqApp.devices.vis_stim.settings import StimParams

    p = StimParams(StimDiameter=100.0, WaveSpPeriod=10.0, Contrast=1.0)
    row = build_grating(p)
    r.check(row.dtype == np.uint8, f"grating is uint8 ({row.dtype})")
    r.check(row.shape[0] >= p.StimDiameter + 2 * p.WaveSpPeriod,
            f"wide enough to cover any drift offset without wrapping "
            f"({row.shape[0]})")
    r.check(int(row.max()) > int(row.min()),
            "a nonzero-contrast grating actually varies")

    flat = build_grating(StimParams(Contrast=0.0))
    r.check(int(flat.max()) == int(flat.min()),
            "zero contrast -> a uniform (flat) grating")

    cx, cy, rad = aperture_geometry(
        StimParams(StimDiameter=200.0, StimXPosition=10.0, StimYPosition=-5.0),
        screen_w=800, screen_h=600)
    r.check((cx, cy, rad) == (410.0, 295.0, 100.0),
            f"aperture centred on screen centre + offset ({cx}, {cy}, {rad})")


def check_settings_roundtrip(r: Report) -> None:
    from acqApp.devices.vis_stim.settings import (TRIAL_GRATING, TRIAL_MAP,
                                                   TRIAL_TYPES, LoopVar,
                                                   StimParams, VisStimSettings,
                                                   parse_values)

    s = VisStimSettings(
        trial_type=TRIAL_MAP, screen_index=1, stretch_to_screen=True,
        params=StimParams(Orientation=45.0, RegionIgnoredColumn=1.0,
                          MapTicksPerRegion=7.0, TuningRegion=4.0,
                          ContrastRegion=2.0),
        loops={"Orientation": LoopVar("Orientation", (0.0, 45.0, 90.0))})
    back = VisStimSettings.from_dict(s.to_dict())
    r.check(back.trial_type == TRIAL_MAP, "trial type round-trips")
    r.check(back.screen_index == 1, "screen index round-trips")
    r.check(back.stretch_to_screen, "the stretch flag round-trips")
    r.check(back.params.Orientation == 45.0, "nested StimParams round-trips")
    r.check(back.params.RegionIgnoredColumn == 1.0
            and back.params.MapTicksPerRegion == 7.0
            and back.params.TuningRegion == 4.0
            and back.params.ContrastRegion == 2.0,
            "the new Map*/Tuning*/Contrast*/RegionIgnoredColumn fields "
            "round-trip like any other StimParams field")
    r.check(back.loops["Orientation"].values == (0.0, 45.0, 90.0),
            "loop values round-trip as a tuple")
    r.check(VisStimSettings.from_dict({}) == VisStimSettings(),
            "an empty dict (fresh install) falls back to defaults")
    r.check(VisStimSettings.from_dict({"trial_type": "nonsense"}).trial_type
            == TRIAL_GRATING,
            "a stale/unknown trial_type falls back to grating rather than "
            "sticking")

    r.check(parse_values("0, 45, 90") == (0.0, 45.0, 90.0),
            "comma-separated values parse")
    r.check(parse_values("0:45:180") == (0.0, 45.0, 90.0, 135.0, 180.0),
            "start:step:stop range syntax parses (MATLAB colon vectors)")
    r.check(parse_values("not a number") == (),
            "unparseable text yields an empty tuple, not a crash")


def check_regions(r: Report) -> None:
    from acqApp.devices.vis_stim.regions import (N_REGIONS, ignored_rect,
                                                  region_rects)

    regions = region_rects(ignored_column=3, screen_w=400, screen_h=300)
    r.check(len(regions) == N_REGIONS == 9, f"9 regions returned ({len(regions)})")
    r.check(regions[0] == (0.0, 0.0, 100.0, 100.0),
            f"region 1 is column 0's top row ({regions[0]})")
    r.check(regions[1] == (0.0, 100.0, 100.0, 100.0),
            f"region 2 is column 0's middle row ({regions[1]})")
    r.check(regions[3] == (100.0, 0.0, 100.0, 100.0),
            f"region 4 starts column 1 — column-major order ({regions[3]})")
    r.check(regions[8] == (200.0, 200.0, 100.0, 100.0),
            f"region 9 is column 2's bottom row ({regions[8]})")
    r.check(ignored_rect(3, 400, 300) == (300.0, 0.0, 100.0, 300.0),
            "the ignored column is the 4th, full height")

    # A non-default ignored column: the 9 regions still sit at their real
    # screen position, not at a "visible-index" position.
    mid = region_rects(ignored_column=1, screen_w=400, screen_h=300)
    xs = sorted({x for x, _y, _w, _h in mid})
    r.check(xs == [0.0, 200.0, 300.0],
            f"columns 0, 2, 3 remain, at their real screen x-positions ({xs})")
    r.check(ignored_rect(1, 400, 300) == (100.0, 0.0, 100.0, 300.0),
            "ignoring column 1 blacks out the second column")


def check_tick_driven_map_run(r: Report) -> None:
    """A map trial completes on ticks alone — no painted frames needed,
    unlike the grating (check_tick_driven_run), so this needs no pump()."""
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.regions import N_REGIONS
    from acqApp.devices.vis_stim.settings import (TRIAL_MAP, StimParams,
                                                   VisStimSettings)

    params = StimParams(WaitTrigger=1, MapTicksPerRegion=2, MapTicksPerFlip=1,
                        MapRepeats=1)
    s = VisStimSettings(trial_type=TRIAL_MAP, params=params)
    c = VisStimController(s)

    r.check(c.run() is True, "run() starts priming for a map trial too")
    c.on_tick(0.1)                        # WaitTrigger=1 -> primed
    r.check(c.phase == RUNNING, f"primed straight into the trial ({c.phase})")
    r.check(c._map_region_idx == 0 and c._map_white,
            "region 1 starts active and white")

    c.on_tick(0.2)                        # 1st running tick: a flip
    r.check(not c._map_white, "one tick flips white -> black")
    c.on_tick(0.3)                        # 2nd: another flip AND region advance
    r.check(c._map_region_idx == 1 and c._map_white,
            f"after MapTicksPerRegion=2 ticks, region 2 is active, reset to "
            f"white ({c._map_region_idx}, {c._map_white})")

    total_ticks = (int(params.MapTicksPerRegion) * N_REGIONS
                  * int(params.MapRepeats))
    for _ in range(total_ticks - 2):      # 2 already delivered above
        c.on_tick(0.0)
    r.check(c.phase == IDLE,
            f"the trial completes once every region has had its ticks "
            f"({c.phase})")
    r.check(c.last_run_stats == {"trials_total": 1, "trials_completed": 1,
                                 "aborted": False},
            f"the one trial completed cleanly ({c.last_run_stats})")
    r.check(len(c.trial_log) == 1 and c.trial_log[0]["map_passes"] == 1,
            f"one full pass through all 9 regions logged ({c.trial_log})")
    c.close()


def check_circle_geometry(r: Report) -> None:
    """circle.py is shared by tuning/contrast/(eventually size) — tested
    once here rather than duplicated per trial type."""
    from acqApp.devices.vis_stim.circle import circle_geometry
    from acqApp.devices.vis_stim.tuning import N_ORIENTATIONS, N_PRETRIALS, \
        orientations

    r.check(orientations() == (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0,
                               315.0),
            f"8 orientations, 0..315 step 45 ({orientations()})")
    r.check(N_ORIENTATIONS == 8 and N_PRETRIALS == 2,
            "8 orientations, 2 pretrials")

    cx, cy, d = circle_geometry(1, ignored_column=3, screen_w=400, screen_h=300)
    r.check((cx, cy, d) == (50.0, 50.0, 100.0),
            f"region 1's circle centred on it, diameter = region WIDTH "
            f"({cx}, {cy}, {d})")
    cx, cy, d = circle_geometry(5, ignored_column=3, screen_w=400, screen_h=300)
    r.check((cx, cy, d) == (150.0, 150.0, 100.0),
            f"region 5 (2nd visible column, middle row) ({cx}, {cy}, {d})")

    lo = circle_geometry(0, 3, 400, 300)
    hi = circle_geometry(99, 3, 400, 300)
    r.check(lo == circle_geometry(1, 3, 400, 300), "region 0 clamps to region 1")
    r.check(hi == circle_geometry(9, 3, 400, 300),
            "region 99 clamps to region 9, the last")


def check_tick_driven_tuning_run(r: Report) -> None:
    """A tuning trial is also entirely tick-driven — 2 pretrials then the
    8-step orientation sweep — no pump() needed, same as map."""
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.settings import (TRIAL_TUNING, StimParams,
                                                   VisStimSettings)
    from acqApp.devices.vis_stim.tuning import N_ORIENTATIONS, N_PRETRIALS

    params = StimParams(WaitTrigger=1, TuningTicksPerPretrial=1,
                        TuningTicksPerOrientation=1, TuningRepeats=1,
                        TuningRegion=1)
    s = VisStimSettings(trial_type=TRIAL_TUNING, params=params)
    c = VisStimController(s)

    r.check(c.run() is True, "run() starts priming for a tuning trial too")
    c.on_tick(0.1)                     # WaitTrigger=1 -> primed
    r.check(c.phase == RUNNING, f"primed straight into the trial ({c.phase})")
    r.check(c._window._solid is True,
            "the trial opens on pretrial 1 (solid white)")

    c.on_tick(0.2)                     # pretrial 1's tick elapses -> pretrial 2
    r.check(c._tuning_step_idx == 1 and c._window._solid,
            f"pretrial 2 is still solid white ({c._tuning_step_idx})")
    c.on_tick(0.3)                     # pretrial 2 elapses -> orientation 0
    r.check(c._tuning_step_idx == 2 and not c._window._solid
            and c._window._orientation == 0.0,
            f"the sweep starts at orientation 0 ({c._tuning_step_idx}, "
            f"{c._window._orientation})")
    c.on_tick(0.4)                     # orientation 0 elapses -> orientation 45
    r.check(c._window._orientation == 45.0,
            f"the next tick advances to 45 degrees ({c._window._orientation})")

    delivered = 3                       # RUNNING ticks so far (0.2, 0.3, 0.4)
    total_ticks = (int(params.TuningTicksPerPretrial) * N_PRETRIALS
                  + int(params.TuningTicksPerOrientation) * N_ORIENTATIONS
                  * int(params.TuningRepeats))
    for _ in range(total_ticks - delivered):
        c.on_tick(0.0)
    r.check(c.phase == IDLE,
            f"the trial completes once the whole sweep has run ({c.phase})")
    r.check(c.last_run_stats == {"trials_total": 1, "trials_completed": 1,
                                 "aborted": False},
            f"the one trial completed cleanly ({c.last_run_stats})")
    r.check(c.trial_log[0]["tuning_steps_completed"] == N_PRETRIALS + N_ORIENTATIONS,
            f"all pretrial + orientation steps were reached ({c.trial_log[0]})")
    c.close()


def check_tick_driven_contrast_run(r: Report) -> None:
    """A contrast trial is also entirely tick-driven — 2 pretrials then the
    contrast-level sweep — no pump() needed, same shape as tuning."""
    from acqApp.devices.vis_stim.contrast import (CONTRAST_LEVELS, N_LEVELS,
                                                   N_PRETRIALS)
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.settings import (TRIAL_CONTRAST, StimParams,
                                                   VisStimSettings)

    params = StimParams(WaitTrigger=1, ContrastTicksPerPretrial=1,
                        ContrastTicksPerLevel=1, ContrastRepeats=1,
                        ContrastRegion=1)
    s = VisStimSettings(trial_type=TRIAL_CONTRAST, params=params)
    c = VisStimController(s)

    r.check(c.run() is True, "run() starts priming for a contrast trial too")
    c.on_tick(0.1)                     # WaitTrigger=1 -> primed
    r.check(c.phase == RUNNING, f"primed straight into the trial ({c.phase})")
    r.check(c._window._solid is True,
            "the trial opens on pretrial 1 (solid white)")

    c.on_tick(0.2)                     # pretrial 1 elapses -> pretrial 2
    r.check(c._contrast_step_idx == 1 and c._window._solid,
            f"pretrial 2 is still solid white ({c._contrast_step_idx})")
    c.on_tick(0.3)                     # pretrial 2 elapses -> level 0
    r.check(c._contrast_step_idx == 2 and not c._window._solid,
            f"the sweep starts at the first contrast level "
            f"({c._contrast_step_idx})")
    img_at_level0 = c._window._img
    c.on_tick(0.4)                     # level 0 elapses -> level 1
    r.check(c._window._img is not img_at_level0,
            "advancing a level rebuilds the texture (Contrast bakes into it, "
            "unlike Orientation)")

    delivered = 3                       # RUNNING ticks so far (0.2, 0.3, 0.4)
    total_ticks = (int(params.ContrastTicksPerPretrial) * N_PRETRIALS
                  + int(params.ContrastTicksPerLevel) * N_LEVELS
                  * int(params.ContrastRepeats))
    r.check(N_LEVELS == 6 and CONTRAST_LEVELS[0] == 0.0
            and CONTRAST_LEVELS[-1] == 1.0,
            f"the recommended 6-level 0..1 spread ({CONTRAST_LEVELS})")
    for _ in range(total_ticks - delivered):
        c.on_tick(0.0)
    r.check(c.phase == IDLE,
            f"the trial completes once the whole sweep has run ({c.phase})")
    r.check(c.last_run_stats == {"trials_total": 1, "trials_completed": 1,
                                 "aborted": False},
            f"the one trial completed cleanly ({c.last_run_stats})")
    r.check(c.trial_log[0]["contrast_steps_completed"] == N_PRETRIALS + N_LEVELS,
            f"all pretrial + level steps were reached ({c.trial_log[0]})")
    c.close()


def check_tick_driven_run(r: Report, app) -> None:
    """The whole priming -> per-trial gating -> finish flow, driven entirely
    by direct on_tick() calls — deterministic, no real DAQ or sleep-based
    simulation needed now that a "trigger" is just the shared clock's tick."""
    from acqApp.devices.vis_stim.control import IDLE, PRIMING, RUNNING, \
        VisStimController
    from acqApp.devices.vis_stim.settings import LoopVar, StimParams, \
        VisStimSettings

    events: list = []
    params = StimParams(WaitTrigger=2, TriggersBlank=1, TriggersStim=1,
                        PeriodsToShow=2, WaveTempPeriodInHz=4.0,
                        StimDiameter=50)
    s = VisStimSettings(
        params=params, loops={"Orientation": LoopVar("Orientation", (0.0, 90.0))})
    c = VisStimController(s)
    c.run_state_changed.connect(lambda st: events.append(("state", st)))
    c.trial_boundary.connect(lambda i, o, p: events.append(("trial", i, o)))

    r.check(c.phase == IDLE, "starts idle")
    r.check(c.run() is True, "run() opens the display and starts priming")
    r.check(c.phase == PRIMING, f"…and is priming ({c.phase})")

    c.on_tick(0.1)
    r.check(c.phase == PRIMING, "one tick short of WaitTrigger=2 -> still priming")
    c.on_tick(0.2)
    r.check(c.phase == RUNNING,
            f"the WaitTrigger'th tick starts the first trial ({c.phase})")
    r.check(("state", RUNNING) in events, "run_state_changed fired for RUNNING")
    r.check(("trial", 0, True) in events, "trial 0's opening boundary fired")

    # TriggersBlank=1, TriggersStim=1: each further tick flips the phase.
    c.on_tick(0.3)
    r.check(c._is_visible, "one more tick (TriggersBlank=1) enters the stim phase")
    c.on_tick(0.4)
    r.check(not c._is_visible, "a further tick (TriggersStim=1) returns to blank")

    # Pump the real Qt event loop so the display's own paint timer actually
    # advances frames and the two trials (PeriodsToShow=2 each) complete.
    pump(app, 5.0)
    r.check(c.phase == IDLE, f"back to idle once both trials finish ({c.phase})")
    r.check(c.last_run_stats == {"trials_total": 2, "trials_completed": 2,
                                 "aborted": False},
            f"both trials completed, nothing aborted ({c.last_run_stats})")
    r.check(len(c.trial_log) == 2, f"one log entry per trial ({len(c.trial_log)})")
    c.close()


def check_hotload(r: Report, win) -> None:
    keys = [m.key for m in win._modules]
    r.check("vis_stim" not in keys, f"not loaded by default here ({keys})")

    added, removed = win.set_modules(["voltage_cam", "vis_stim"])
    r.check(added == ["vis_stim"] and removed == [],
            f"vis_stim hot-loads like any other module ({added}, {removed})")
    r.check("vis_stim" in [m.key for m in win._modules],
            "…and is in the live module list")

    m = next(x for x in win._modules if x.key == "vis_stim")
    r.check(m.own_window, "asks for its own window, like routines")
    r.check(m.controller is not None and m.controller.phase == "IDLE",
            "its controller is built and idle immediately (always-on, "
            "not per-session)")

    added, removed = win.set_modules(["voltage_cam"])
    r.check(removed == ["vis_stim"],
            f"…and hot-unloads cleanly ({added}, {removed})")


def main() -> int:
    r = Report("vis_stim")
    check_trials(r)
    check_grating(r)
    check_regions(r)
    check_circle_geometry(r)
    check_settings_roundtrip(r)

    isolate_user_state()
    app = qt_app()          # keep the reference: a GC'd QApplication aborts
    check_tick_driven_run(r, app)
    check_tick_driven_map_run(r)
    check_tick_driven_tuning_run(r)
    check_tick_driven_contrast_run(r)

    from acqApp.main import MainWindow

    win = MainWindow(mock=True, enabled={"voltage_cam"})
    try:
        check_hotload(r, win)
    finally:
        win.close()
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
