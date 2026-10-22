"""Visual stim (ported from visStimCode's .m files): trial expansion,
grating/aperture geometry, settings round-trip, the clock-tick-driven
priming/gating state machine, and hot-load/unload.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _harness import Report, isolate_user_state, make_window, pump, qt_app


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
                                                   LoopVar, StimParams,
                                                   VisStimSettings,
                                                   parse_values)

    s = VisStimSettings(
        trial_type=TRIAL_MAP, screen_index=1, stretch_to_screen=True,
        params=StimParams(Orientation=45.0, MapWhiteSeconds=0.7,
                          TuneRegion=4.0, ContrastRepeats=3.0),
        loops={"Orientation": LoopVar("Orientation", (0.0, 45.0, 90.0))},
        levels={"contrast": (50.0, 100.0)})
    back = VisStimSettings.from_dict(s.to_dict())
    r.check(back.trial_type == TRIAL_MAP, "trial type round-trips")
    r.check(back.screen_index == 1, "screen index round-trips")
    r.check(back.stretch_to_screen, "the stretch flag round-trips")
    r.check(back.params.Orientation == 45.0, "nested StimParams round-trips")
    r.check(back.params.MapWhiteSeconds == 0.7
            and back.params.TuneRegion == 4.0
            and back.params.ContrastRepeats == 3.0,
            "the Map*/Tune*/repeats fields round-trip like any other "
            "StimParams field")
    r.check(back.levels["contrast"] == (50.0, 100.0)
            and back.levels["size"] == (5.0, 10.0, 35.0, 60.0, 80.0),
            f"saved levels round-trip; missing ones take the paper's "
            f"defaults ({back.levels})")
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
    try:
        bad = parse_values("0:4.5.:90")
    except ValueError as e:
        bad = e
    r.check(bad == (), f"a malformed range yields (), not a ValueError out "
                       f"of the panel's slot ({bad!r})")


def check_regions(r: Report) -> None:
    from acqApp.devices.vis_stim.regions import (N_REGIONS, ignored_rect,
                                                  region_rects)

    regions = region_rects(screen_w=400, screen_h=300)
    r.check(len(regions) == N_REGIONS == 9, f"9 regions returned ({len(regions)})")
    r.check(regions[0] == (0.0, 0.0, 100.0, 100.0),
            f"region 1 is column 0's top row ({regions[0]})")
    r.check(regions[1] == (0.0, 100.0, 100.0, 100.0),
            f"region 2 is column 0's middle row ({regions[1]})")
    r.check(regions[3] == (100.0, 0.0, 100.0, 100.0),
            f"region 4 starts column 1 — column-major order ({regions[3]})")
    r.check(regions[8] == (200.0, 200.0, 100.0, 100.0),
            f"region 9 is column 2's bottom row ({regions[8]})")
    r.check(ignored_rect(400, 300) == (300.0, 0.0, 100.0, 300.0),
            "the ignored column is always the 4th, full height")


def check_tick_driven_map_run(r: Report) -> None:
    """Completes on ticks alone — unlike the grating, no pump()."""
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.regions import N_REGIONS
    from acqApp.devices.vis_stim.settings import (TRIAL_MAP, StimParams,
                                                   VisStimSettings)

    # The operator's spec: 1 s grey, per region 0.5 s white + 0.5 s black,
    # 2 repeats, 1 s grey = 20 s = 200 ticks at 10 Hz.
    params = StimParams(WaitTrigger=50, MapGreySeconds=1.0,
                        MapWhiteSeconds=0.5, MapBlackSeconds=0.5,
                        MapRepeats=2)
    s = VisStimSettings(trial_type=TRIAL_MAP, params=params)
    c = VisStimController(s)

    r.check(c.run() is True, "run() starts priming for a map trial too")
    c.on_tick(0.1)
    r.check(c.phase == RUNNING,
            f"map ignores Prime wait (50 here): its own grey leads in, so "
            f"the trial starts on the first tick ({c.phase})")
    orders = c._map_order
    r.check(len(orders) == 2
            and all(sorted(o) == list(range(N_REGIONS)) for o in orders),
            f"each repeat visits every region once ({orders})")

    shown = []                            # what is on screen for each tick
    while c.phase == RUNNING and len(shown) < 1000:
        shown.append(c.map_state)
        c.on_tick(0.0)
    r.check(len(shown) == 200, f"the trial lasts 20 s = 200 ticks ({len(shown)})")
    grey = (-1, False)
    r.check(shown[:10] == [grey] * 10 and shown[-10:] == [grey] * 10,
            "1 s of all-grey at the start and at the end")
    expect = [st for region in orders[0] + orders[1]
              for st in [(region, True)] * 5 + [(region, False)] * 5]
    r.check(shown[10:-10] == expect,
            "each region: 0.5 s white then straight into 0.5 s black, in "
            "the shuffled order")
    r.check(c.last_run_stats == {"trials_total": 1, "trials_completed": 1,
                                 "aborted": False},
            f"the one trial completed cleanly ({c.last_run_stats})")
    log = c.trial_log[0] if len(c.trial_log) == 1 else {}
    r.check(log.get("map_passes") == 2 and log.get("map_ticks") == 200,
            f"two full passes and 200 ticks logged ({c.trial_log})")
    r.check(log.get("map_order") == [[i + 1 for i in o] for o in orders],
            "the shown order is logged, 1-based like the panel")
    c.close()


def check_mid_run_settings_wait(r: Report) -> None:
    """A panel edit mid-run (here the trial type) waits for the next run:
    stepping the new type's unbuilt timeline raised IndexError in a slot."""
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.settings import (TRIAL_GRATING, TRIAL_MAP,
                                                   TRIAL_TUNING, StimParams,
                                                   VisStimSettings)

    c = VisStimController(VisStimSettings(
        trial_type=TRIAL_GRATING, params=StimParams(WaitTrigger=1)))
    c.run()
    c.on_tick(0.1)
    r.check(c.phase == RUNNING, f"grating running ({c.phase})")
    c.apply_settings(VisStimSettings(trial_type=TRIAL_MAP))
    try:
        for _ in range(3):
            c.on_tick(0.0)
        ok = True
    except Exception as e:                                # noqa: BLE001
        ok = False
        r.check(False, f"a mid-run type switch raised {type(e).__name__}: {e}")
    if ok:
        r.check(c.phase == RUNNING and c._s.trial_type == TRIAL_GRATING,
                "the running grating keeps its own settings")
    c.stop()
    r.check(c.phase == IDLE, "stops")
    c.apply_settings(VisStimSettings(trial_type=TRIAL_TUNING))
    c.run()
    r.check(c._s.trial_type == TRIAL_TUNING,
            "the next run takes up the latest settings")
    states: list[str] = []
    c.run_state_changed.connect(states.append)
    c.close()
    r.check(c.phase == IDLE and states[-1:] == [IDLE],
            f"close() mid-run ends the run and says IDLE, so the panel's "
            f"STOP button resets ({c.phase}, {states})")


def check_map_shuffle(r: Report) -> None:
    import random

    from acqApp.devices.vis_stim.regions import N_REGIONS
    from acqApp.devices.vis_stim.trials import shuffled_blocks

    def shuffled_passes(n, rng=None):
        return shuffled_blocks(N_REGIONS, n, rng)

    passes = shuffled_passes(200, random.Random(1))
    r.check(len(passes) == 200
            and all(sorted(p) == list(range(N_REGIONS)) for p in passes),
            "each repeat is a permutation of the 9 regions")
    r.check(len({tuple(p) for p in passes}) > 150,
            "repeats get different orders")
    r.check(all(a[-1] != b[0] for a, b in zip(passes, passes[1:])),
            "no region flashes twice in a row across a repeat boundary")
    r.check(len(shuffled_passes(0)) == 1, "Repeats < 1 still runs one pass")


def check_panel_settings_file(r: Report, tmp: Path) -> None:
    from unittest import mock

    from acqApp.devices.vis_stim import panel as panel_mod
    from acqApp.devices.vis_stim.settings import (TRIAL_MAP, LoopVar,
                                                   StimParams, VisStimSettings)

    s = VisStimSettings(
        trial_type=TRIAL_MAP, stretch_to_screen=True,
        params=StimParams(MapWhiteSeconds=0.3, MapRepeats=4, Contrast=0.25),
        loops={"Orientation": LoopVar("Orientation", (0.0, 90.0))})
    src = panel_mod.SettingsPanel(s)
    path = tmp / "stim.json"
    dlg = panel_mod.QFileDialog
    with mock.patch.object(dlg, "getSaveFileName", return_value=(str(path), "")):
        src._save_file()
    r.check(path.is_file(), "Save… writes the chosen .json")

    dst = panel_mod.SettingsPanel(VisStimSettings())
    seen = []
    dst.settings_changed.connect(seen.append)
    with mock.patch.object(dlg, "getOpenFileName", return_value=(str(path), "")):
        dst._load_file()
    got = dst.settings
    r.check(got == src.settings,
            f"Load… restores every setting ({got} != {src.settings})")
    r.check(len(seen) == 1, f"a load emits settings_changed once ({len(seen)})")
    r.check(dst._grp_map.isVisibleTo(dst),
            "the loaded trial type's group is shown")
    r.check(not dst._grp_trigger.isVisibleTo(dst),
            "map hides the Trial timing (Prime wait) box")

    path.write_text("not json", encoding="utf-8")
    with mock.patch.object(dlg, "getOpenFileName", return_value=(str(path), "")), \
            mock.patch.object(panel_mod.QMessageBox, "warning") as warn:
        dst._load_file()
    r.check(warn.called and dst.settings == got,
            "a bad file warns and leaves the settings alone")

    dst.set_settings(VisStimSettings(params=StimParams(MapRepeats=2)))
    r.check(dst._lbl_map_total.text() == "Total: 20 s",
            f"defaults x 2 repeats read 20 s ({dst._lbl_map_total.text()})")
    r.check(all(dst._spins[name].toolTip() for name, *_ in panel_mod._MAP_FIELDS),
            "every map field explains itself in a tooltip")

    from acqApp.widgets import collapsible
    dst.set_settings(VisStimSettings(trial_type="contrast"))
    dst.show()
    box = collapsible(dst._grp_sweep)
    box.setChecked(False)
    box.setChecked(True)
    r.check(not dst._rows["TuneContrastPct"].isVisible()
            and dst._rows["TuneCyclesPerDeg"].isVisible(),
            "folding a box open again keeps the type-hidden field hidden")
    dst._spn_repeats.setValue(7)
    dst._region.changed.emit(6)
    got = dst.settings.params
    r.check(got.ContrastRepeats == 7 and got.TuningRepeats == 5
            and got.TuneRegion == 6,
            "Repeats and Region edit the current tuning type's own values")
    dst.hide()
    src.deleteLater()
    dst.deleteLater()


def check_circle_geometry(r: Report) -> None:
    """circle.py, shared by the region trial types."""
    from acqApp.devices.vis_stim.circle import circle_geometry
    from acqApp.devices.vis_stim.sweep import (conditions, deg_to_px,
                                               orientations, period_px)

    r.check(orientations() == (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0,
                               315.0),
            f"8 orientations, 0..315 step 45 ({orientations()})")
    r.check(len(conditions(())) == 8 and len(conditions((1.0, 2.0))) == 16,
            "orientation alone is 8 conditions; x 2 levels is 16")
    r.check(abs(deg_to_px(90.0, 10.0, 1.0) - 20.0) < 1e-9,
            "90° at 10 cm spans 2 x 10 x tan(45°) = 20 cm")
    r.check(abs(period_px(0.04, 10.0, 1.0) - deg_to_px(25.0, 10.0, 1.0))
            < 1e-9, "0.04 cyc/° is one cycle per 25°")
    r.check(deg_to_px(200.0, 10.0, 1.0) > deg_to_px(170.0, 10.0, 1.0) > 0
            and period_px(0.001, 10.0, 1.0) > 0,
            "a span of 180° or more (or a tiny cyc/°) stays wide and positive, "
            "never the negative period that aliased the grating")

    cx, cy, d = circle_geometry(1, screen_w=400, screen_h=300)
    r.check((cx, cy, d) == (50.0, 50.0, 100.0),
            f"region 1's circle centred on it, diameter = region WIDTH "
            f"({cx}, {cy}, {d})")
    cx, cy, d = circle_geometry(5, screen_w=400, screen_h=300)
    r.check((cx, cy, d) == (150.0, 150.0, 100.0),
            f"region 5 (2nd visible column, middle row) ({cx}, {cy}, {d})")

    lo = circle_geometry(0, 400, 300)
    hi = circle_geometry(99, 400, 300)
    r.check(lo == circle_geometry(1, 400, 300), "region 0 clamps to region 1")
    r.check(hi == circle_geometry(9, 400, 300),
            "region 99 clamps to region 9, the last")


def check_tuning_type_runs(r: Report) -> None:
    """The paper's protocols on the defaults: pretrial (8 orientations,
    unrecorded), 1 s grey, conditions x repeats each 0.2 s on + 0.3 s grey,
    1 s grey. Orientation 8 x 5 = 22 s; contrast/SF/size 8 x 5 x 2 = 42 s."""
    from acqApp.devices.vis_stim import sweep
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.settings import (TRIAL_CONTRAST, TRIAL_SIZE,
                                                   TRIAL_SPATIAL_FREQ,
                                                   TRIAL_TUNING, StimParams,
                                                   VisStimSettings)

    expected_s = {TRIAL_TUNING: 22, TRIAL_CONTRAST: 42,
                  TRIAL_SPATIAL_FREQ: 42, TRIAL_SIZE: 42}
    for kind, seconds in expected_s.items():
        s = VisStimSettings(trial_type=kind)
        p = s.params
        c = VisStimController(s)
        marks = []                       # (tick, opening)
        ticks = [0]
        c.trial_boundary.connect(lambda _i, opening, _x: marks.append(
            (ticks[0], opening)))
        r.check(c.run() is True, f"{kind}: run() starts")
        c.on_tick(0.0)                   # no Prime wait: straight in
        r.check(c.phase == RUNNING, f"{kind}: runs on the first tick")

        shown = []                       # per tick: orientation, or None
        while c.phase == RUNNING and ticks[0] < 2000:
            win = c._window
            shown.append(win._orientation if win._visible else None)
            if ticks[0] == 0:            # pretrial's first grating: drifts
                before = c._xoffset
                c._on_frame()
                r.check(win._visible and c._xoffset > before,
                        f"{kind}: the grating drifts while on")
            ticks[0] += 1
            c.on_tick(0.0)

        pre = [x for o in sweep.orientations() for x in [o] * 2 + [None] * 3]
        order = c._sweep_order
        conds = [cond for block in order for cond in block]
        rec = ([None] * 10
               + [x for o, _lv in conds for x in [o] * 2 + [None] * 3]
               + [None] * 10)
        r.check(shown == pre + rec,
                f"{kind}: pretrial, then 1 s grey, each condition 0.2 s on "
                f"+ 0.3 s grey in the shuffled order, 1 s grey")
        r.check(len(rec) == seconds * 10,
                f"{kind}: the recorded trial is {seconds} s ({len(rec) / 10} s)")
        r.check(marks == [(40, True), (len(shown), False)],
                f"{kind}: the trial marks bracket the recorded part only, "
                f"not the 4 s pretrial ({marks})")
        levels = s.levels.get(kind, ()) if kind != TRIAL_TUNING else ()
        want = sorted(sweep.conditions(levels), key=str)
        r.check(all(sorted(b, key=str) == want for b in order),
                f"{kind}: each repeat shows every condition once")
        r.check(c.last_run_stats["trials_completed"] == 1 and c.phase == IDLE,
                f"{kind}: completes cleanly")
        log = c.trial_log[0]
        prefix = {TRIAL_TUNING: "tuning", TRIAL_CONTRAST: "contrast",
                  TRIAL_SPATIAL_FREQ: "spatial_freq", TRIAL_SIZE: "size"}[kind]
        r.check(log[f"{prefix}_shown"] == len(conds)
                and len(log[f"{prefix}_order"]) == len(order),
                f"{kind}: shown count and order logged")
        c.close()

    # The swept level reaches the screen, in degrees via the screen geometry.
    from acqApp.devices.vis_stim.window import reported_width_cm
    from PyQt6.QtGui import QGuiApplication

    s = VisStimSettings(trial_type=TRIAL_SIZE)
    c = VisStimController(s)
    c.run()
    c.on_tick(0.0)
    w = c._window.width()
    reported = reported_width_cm(QGuiApplication.screens()[0])
    r.check(c._screen_cm == (reported or 52.0),
            f"Screen width 0 uses the display's reported width "
            f"({c._screen_cm} vs {reported})")
    ppc = w / c._screen_cm
    q = c._sweep_stim((0.0, 35.0))
    r.check(abs(q.StimDiameter - sweep.deg_to_px(35.0, s.params.ViewDistanceCm,
                                                 ppc)) < 1e-6,
            "size: a 35° level is 35° of visual angle at the eye distance")
    c.close()
    s = VisStimSettings(trial_type=TRIAL_SPATIAL_FREQ,
                        params=StimParams(ScreenWidthCm=40.0))
    c = VisStimController(s)
    c.run()
    c.on_tick(0.0)
    r.check(c._screen_cm == 40.0, "a typed Screen width overrides the display")
    q = c._sweep_stim((90.0, 0.1))
    r.check(abs(q.WaveSpPeriod - sweep.period_px(
                0.1, s.params.ViewDistanceCm, w / 40.0))
            < 1e-6 and q.Orientation == 90.0,
            "spatial freq: 0.1 cyc/° sets the grating period")
    c.close()
    s = VisStimSettings(trial_type=TRIAL_CONTRAST)
    c = VisStimController(s)
    c.run()
    c.on_tick(0.0)
    r.check(c._sweep_stim((0.0, 25.0)).Contrast == 0.25,
            "contrast: levels are percent")
    c._on_escape()                       # stopped inside the pretrial
    r.check(c.phase == IDLE and c.last_run_stats["aborted"],
            "Esc during the pretrial stops cleanly")
    c.close()


def check_visuomotor_run(r: Report, app) -> None:
    """Drift is wheel speed x Gain per painted frame, and the trial ends on
    VisuomotorDurationTicks; otherwise the plain grating path."""
    from acqApp.devices.vis_stim.control import IDLE, RUNNING, VisStimController
    from acqApp.devices.vis_stim.settings import (TRIAL_VISUOMOTOR, StimParams,
                                                   VisStimSettings)

    # No wheel_speed source (wheel not loaded): a static grating, no crash.
    params = StimParams(WaitTrigger=1, TriggersBlank=1, TriggersStim=1,
                        VisuomotorDurationTicks=100000, VisuomotorGain=2.0,
                        WaveSpPeriod=1000.0)
    s = VisStimSettings(trial_type=TRIAL_VISUOMOTOR, params=params)
    c = VisStimController(s)
    r.check(c.run() is True, "run() starts priming for a visuomotor trial too")
    c.on_tick(0.1)                      # WaitTrigger=1 -> primed
    r.check(c.phase == RUNNING, f"primed straight into the trial ({c.phase})")
    pump(app, 0.3)
    r.check(c._xoffset == 0.0,
            "no wheel_speed source -> the grating never drifts (stays static)")
    c.close()

    params2 = StimParams(WaitTrigger=1, TriggersBlank=1, TriggersStim=1,
                         VisuomotorDurationTicks=100000, VisuomotorGain=1.0,
                         WaveSpPeriod=1_000_000.0)
    s2 = VisStimSettings(trial_type=TRIAL_VISUOMOTOR, params=params2)
    c2 = VisStimController(s2, wheel_speed=lambda: (50.0, 0.0))
    c2.run()
    c2.on_tick(0.1)
    pump(app, 0.3)
    r.check(c2._xoffset > 0.0,
            f"a live wheel_speed source drives the drift forward "
            f"({c2._xoffset})")
    c2.close()

    params3 = StimParams(WaitTrigger=1, TriggersBlank=100000,
                         TriggersStim=100000, VisuomotorDurationTicks=2)
    s3 = VisStimSettings(trial_type=TRIAL_VISUOMOTOR, params=params3)
    c3 = VisStimController(s3)
    c3.run()
    c3.on_tick(0.1)                     # WaitTrigger=1 -> primed, RUNNING begins
    c3.on_tick(0.2)                     # 1st RUNNING tick: count 1/2
    r.check(c3.phase == RUNNING, f"tick 1/2: still running ({c3.phase})")
    c3.on_tick(0.3)                     # 2nd RUNNING tick: count 2/2 -> ends
    r.check(c3.phase == IDLE,
            f"ends once VisuomotorDurationTicks is reached ({c3.phase})")
    c3.close()


def check_tick_driven_run(r: Report, app) -> None:
    """Priming -> per-trial gating -> finish, driven by on_tick() calls."""
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

    # The display's paint timer advances the frames that finish the trials.
    pump(app, 5.0)
    r.check(c.phase == IDLE, f"back to idle once both trials finish ({c.phase})")
    r.check(c.last_run_stats == {"trials_total": 2, "trials_completed": 2,
                                 "aborted": False},
            f"both trials completed, nothing aborted ({c.last_run_stats})")
    r.check(len(c.trial_log) == 2, f"one log entry per trial ({len(c.trial_log)})")
    c.close()


def check_loops_dont_multiply_region_trials(r: Report) -> None:
    """Loop variables persist across trial types, but only Grating and
    Visuomotor use them; a region trial runs its own sweep once."""
    from acqApp.devices.vis_stim.control import VisStimController
    from acqApp.devices.vis_stim.settings import (LoopVar, StimParams,
                                                   TRIAL_TUNING,
                                                   VisStimSettings)

    params = StimParams(TuningRepeats=1)
    s = VisStimSettings(
        trial_type=TRIAL_TUNING, params=params,
        loops={"Orientation": LoopVar("Orientation", (0.0, 45.0, 90.0))})
    c = VisStimController(s)
    r.check(c.run() is True, "run() still starts with loop variables set")
    r.check(len(c._trials) == 1,
            f"a region trial type ignores Loop variables — one trial, not "
            f"three ({len(c._trials)})")
    r.check(c.last_run_stats["trials_total"] == 1,
            f"…and trials_total agrees, not overcounting the run "
            f"({c.last_run_stats})")
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
    r.check("vis_stim" in win._panel_windows,
            "its panel gets a window of its own, like every module's")
    r.check(m.controller is not None and m.controller.phase == "IDLE",
            "its controller is built and idle immediately (always-on, "
            "not per-session)")

    added, removed = win.set_modules(["voltage_cam"])
    r.check(removed == ["vis_stim"],
            f"…and hot-unloads cleanly ({added}, {removed})")


def check_session_metadata(r: Report, win) -> None:
    """final_metadata reports the runs of THIS recording: not a run from
    before it (the controller is always-on), and every run within it."""
    import json
    from acqApp.devices.vis_stim.settings import StimParams, VisStimSettings

    win.set_modules(["voltage_cam", "vis_stim"])
    try:
        m = next(x for x in win._modules if x.key == "vis_stim")
        c = m.controller
        c.apply_settings(VisStimSettings(params=StimParams(WaitTrigger=1)))

        def one_run() -> None:          # prime, start trial 1, Esc
            c.run()
            c.on_tick(0.1)
            c.stop()

        class Rec:
            def put(self, *_a, **_kw) -> None:
                pass

        one_run()                       # before the recording
        m.attach_sink(Rec())
        md = m.final_metadata()
        r.check(md["vis_stim_started"] is False
                and json.loads(md["vis_stim_trials"]) == [],
                f"a run before Record isn't this session's "
                f"({md['vis_stim_started']}, {md['vis_stim_trials'][:60]})")
        one_run()
        one_run()
        m.detach_sink()
        md = m.final_metadata()
        trials = json.loads(md["vis_stim_trials"])
        r.check(md.get("vis_stim_runs") == 2
                and md["vis_stim_trials_total"] == 2
                and [t.get("run") for t in trials] == [1, 2],
                f"both runs in the recording are kept, not just the last "
                f"({md.get('vis_stim_runs')}, {[t.get('run') for t in trials]})")

        c.run()
        c.on_tick(0.1)
        c.trial_log.extend({"index": i, "interrupted": False} for i in (90, 91))
        m.attach_sink(Rec())            # Record pressed mid-run
        c.stop()
        m.detach_sink()
        md = m.final_metadata()
        trials = json.loads(md["vis_stim_trials"])
        idx = [t["index"] for t in trials]
        r.check(trials and not {90, 91} & set(idx)
                and md["vis_stim_trials_completed"]
                == sum(not t["interrupted"] for t in trials),
                f"Record mid-run: only the trials after it are this file's "
                f"({idx}, completed={md['vis_stim_trials_completed']})")
    finally:
        win.set_modules(["voltage_cam"])


def main() -> int:
    r = Report("vis_stim")
    check_trials(r)
    check_grating(r)
    check_regions(r)
    check_circle_geometry(r)
    check_settings_roundtrip(r)
    check_map_shuffle(r)

    isolate_user_state()
    app = qt_app()          # held: a collected QApplication aborts
    check_tick_driven_run(r, app)
    check_tick_driven_map_run(r)
    check_mid_run_settings_wait(r)
    with tempfile.TemporaryDirectory() as tmp:
        check_panel_settings_file(r, Path(tmp))
    check_tuning_type_runs(r)
    check_loops_dont_multiply_region_trials(r)
    check_visuomotor_run(r, app)

    win = make_window({"voltage_cam"})
    try:
        check_hotload(r, win)
        check_session_metadata(r, win)
    finally:
        win.close()
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
