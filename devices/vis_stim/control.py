"""VisStimController — the state machine, replacing guiVisStimDAQ.m's
startStimFlow + runStimManager.m + runAnimationLoop.

MATLAB blocks the whole GUI for a run's duration inside one big `while` loop
pumped by `drawnow`/`WaitSecs`. This instead advances one step per painted
frame, so the rest of acqApp (cameras, recording, other modules) keeps
running live during a stimulus run — the one deliberate behavioral departure
from the .m code; everything it actually computes (per-trial blank/stim phase
counting, drift offset, stretch-to-screen) is a direct port.

The .m code gated blank/stim phases on pulses read from an external MCC DAQ
line (hysteresis edge detection). This rig has no such line: `on_tick()` is
called once per `acq/sync.py` SyncController tick (10 Hz by default, only
while a session is live) and stands in for a trigger pulse — the same shared
timing sequence every other module already uses, rather than a second one
invented for this module alone. `WaitTrigger`/`TriggersBlank`/`TriggersStim`
now count ticks, not DAQ edges, but the counting logic itself (and the field
names, for MATLAB config parity) is unchanged.

`VisStimSettings.trial_type` selects the paradigm for the whole run — the
drifting grating above, or:
  `map`    a fixed 3x3 region grid (`regions.py`) where one region at a time
           flips white/black while the rest stay grey, cycling through all
           9 in one continuous trial.
  `tuning` a circle at one of those same 9 regions (diameter = the region's
           width; `circle.py`), showing 2 white "pretrial" flashes and then
           the plain grating's own rendering swept through 8 orientations
           (`tuning.py`) — again one continuous trial.
  `contrast` the same circle, sweeping the grating's Contrast through fixed
           levels (`contrast.py`) instead of Orientation — otherwise the
           same 2-pretrial-then-sweep shape as tuning.
  `size`   the same circle, sweeping the circle's own diameter through
           fixed fractions of the region's width (`size.py`) instead of
           Orientation/Contrast — otherwise the same 2-pretrial-then-sweep
           shape as tuning/contrast.
  `visuomotor` a normal drifting grating (full StimParams geometry, same as
           plain Grating) except the drift offset each painted frame is
           driven by the wheel's live speed (`_visuomotor_frame`) instead of
           a fixed WaveTempPeriodInHz, scaled by VisuomotorGain — the
           classic locomotion/optic-flow closed-loop coupling. Blank/stim
           gating is still tick-counted exactly like Grating
           (`_grating_gate_tick`); trial length is VisuomotorDurationTicks
           instead of PeriodsToShow, since there is no fixed temporal
           frequency to count cycles of.
Priming (`WaitTrigger`) is shared by every trial type; `_begin_trial`/
`_gate_tick`/`_on_frame` each branch to a trial-type-specific half.
"""
from __future__ import annotations

import time
from dataclasses import asdict, replace
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QGuiApplication

from . import circle as circle_mod
from . import contrast as contrast_mod
from . import regions as regions_mod
from . import size as size_mod
from . import trials as trials_mod
from . import tuning as tuning_mod
from .settings import (TRIAL_CONTRAST, TRIAL_MAP, TRIAL_SIZE, TRIAL_TUNING,
                       TRIAL_VISUOMOTOR, StimParams, VisStimSettings)
from .window import StimDisplay

# A region/tick-based trial type builds its own geometry and finishes on
# ticks, not painted frames or a fixed temporal frequency — Grating and
# Visuomotor both fall through to the plain-grating code path instead
# (_begin_grating_trial/_begin_visuomotor_trial), so they're excluded here.
_REGION_TRIAL_TYPES = (TRIAL_MAP, TRIAL_TUNING, TRIAL_CONTRAST, TRIAL_SIZE)

IDLE, PRIMING, RUNNING = "IDLE", "PRIMING", "RUNNING"


class VisStimController(QObject):
    progress_changed = pyqtSignal(str)
    run_state_changed = pyqtSignal(str)       # IDLE | PRIMING | RUNNING
    # (trial_index, opening, params_dict) — the adapter routes this to the
    # Recorder, the same shape adapters/routines.py uses for step boundaries.
    trial_boundary = pyqtSignal(int, bool, object)

    def __init__(self, settings: VisStimSettings, parent=None,
                 wheel_speed: Callable[[], tuple[float, float] | None] | None
                 = None) -> None:
        super().__init__(parent)
        self._s = settings
        self._phase = IDLE
        self._window: StimDisplay | None = None
        # visuomotor trial only — a live-speed reader the adapter injects
        # (mirrors closed_loop's SignalSource.read: (value, acquired_at) or
        # None while unavailable). None if no wheel module is loaded; the
        # trial then just degrades to a static grating rather than crashing.
        self._wheel_speed = wheel_speed

        # Run-time state, valid only while PRIMING/RUNNING.
        self._prime_count = 0
        self._trials: list[StimParams] = []
        self._trial_idx = 0
        self._is_stim_phase = False
        self._is_visible = False
        self._trigger_count = 0
        self._frame_i = 0
        self._total_frames = 0
        self._shift_per_frame = 0.0
        self._xoffset = 0.0
        self._blank_frames = 0
        self._stim_frames = 0
        self._trial_t0 = 0.0
        self._last_mask_key: tuple | None = None
        # map trial only
        self._map_region_idx = 0
        self._map_tick_in_region = 0
        self._map_flip_tick = 0
        self._map_white = True
        self._map_pass = 0
        self._map_tick_count = 0
        self._map_total_ticks = 0
        # tuning trial only
        self._tuning_step_idx = 0
        self._tuning_tick_in_step = 0
        self._tuning_tick_count = 0
        self._tuning_total_steps = 0
        self._tuning_total_ticks = 0
        # contrast trial only
        self._contrast_base: StimParams | None = None
        self._contrast_step_idx = 0
        self._contrast_tick_in_step = 0
        self._contrast_tick_count = 0
        self._contrast_total_steps = 0
        self._contrast_total_ticks = 0
        # size trial only
        self._size_base: StimParams | None = None
        self._size_region_diameter = 0.0
        self._size_step_idx = 0
        self._size_tick_in_step = 0
        self._size_tick_count = 0
        self._size_total_steps = 0
        self._size_total_ticks = 0
        # visuomotor trial only
        self._visuomotor_tick_count = 0
        # Per-trial records (params + outcome) for final_metadata — rec.put()
        # only ever carries a float (Writer.write coerces via float(data)), so
        # the detail travels as one JSON file attribute at the end, the same
        # way adapters/routines.py's `routine_runs` does.
        self.trial_log: list[dict[str, Any]] = []
        self.last_run_stats: dict[str, Any] = {
            "trials_total": 0, "trials_completed": 0, "aborted": False}

    # ── settings ──────────────────────────────────────────────────────────
    def apply_settings(self, s: VisStimSettings) -> None:
        self._s = s

    # ── the shared clock's tick — one call = one "trigger" pulse ────────
    def on_tick(self, _elapsed_s: float) -> None:
        if self._phase == PRIMING:
            self._prime_tick()
        elif self._phase == RUNNING:
            self._gate_tick()

    # ── run control ──────────────────────────────────────────────────────
    def run(self) -> bool:
        """Port of startStimFlow: open the display, then wait for triggers
        before the first trial. False if already running."""
        if self._phase != IDLE:
            return False
        base = self._s.params
        self._trials = trials_mod.gen_param_combos(base, self._s.loops)
        if not self._trials:
            return False
        self.trial_log = []
        self.last_run_stats = {"trials_total": len(self._trials),
                               "trials_completed": 0, "aborted": False}

        screens = QGuiApplication.screens()
        idx = min(max(self._s.screen_index, 0), len(screens) - 1)
        screen = screens[idx] if screens else QGuiApplication.primaryScreen()
        if screen is None:
            return False

        self._window = StimDisplay()
        # No set_trial/set_map_trial call here: a fresh StimDisplay already
        # paints a blank mid-grey screen (its own __init__ defaults), which
        # is the right thing to show while priming regardless of trial
        # type — the first _begin_trial() builds whatever it actually needs.
        self._window.painted.connect(self._on_frame)
        self._window.escape_pressed.connect(self._on_escape)
        self._window.skip_pressed.connect(self._on_skip)
        fps = screen.refreshRate() or 60.0
        self._window.open_on(screen, fps)
        self._fps = fps

        self._phase = PRIMING
        self._prime_count = 0
        target = int(base.WaitTrigger)
        self.progress_changed.emit(f"PRIMED: waiting for triggers "
                                   f"(0/{target})...")
        self.run_state_changed.emit(PRIMING)
        return True

    def stop(self) -> None:
        """Operator-requested Stop, from any phase — same effect as ESC."""
        self._on_escape()

    def _prime_tick(self) -> None:
        target = int(self._s.params.WaitTrigger)
        self._prime_count += 1
        self.progress_changed.emit(
            f"PRIMED: waiting for triggers ({self._prime_count}/{target})...")
        if self._prime_count >= target:
            self._start_running()

    def _start_running(self) -> None:
        w, h = self._window.width(), self._window.height()
        # Regions/tuning circles already have their own explicit geometry;
        # stretch-to-screen is a plain-grating concept and has nothing to
        # act on here.
        if (self._s.trial_type not in _REGION_TRIAL_TYPES
                and self._s.stretch_to_screen):
            diag = int((w * w + h * h) ** 0.5) + 1
            self._trials = [replace(t, StimDiameter=diag, StimXPosition=0.0,
                                    StimYPosition=0.0) for t in self._trials]
        self._trial_idx = 0
        self._phase = RUNNING
        self._last_mask_key = None
        self.run_state_changed.emit(RUNNING)
        self._begin_trial()

    def _begin_trial(self) -> None:
        p = self._trials[self._trial_idx]
        if self._s.trial_type == TRIAL_MAP:
            self._begin_map_trial(p)
        elif self._s.trial_type == TRIAL_TUNING:
            self._begin_tuning_trial(p)
        elif self._s.trial_type == TRIAL_CONTRAST:
            self._begin_contrast_trial(p)
        elif self._s.trial_type == TRIAL_SIZE:
            self._begin_size_trial(p)
        elif self._s.trial_type == TRIAL_VISUOMOTOR:
            self._begin_visuomotor_trial(p)
        else:
            self._begin_grating_trial(p)
        self.trial_boundary.emit(self._trial_idx, True, asdict(p))
        n = len(self._trials)
        self.progress_changed.emit(
            f"Progress: {(self._trial_idx / n) * 100:.1f}%   "
            f"trial {self._trial_idx + 1}/{n}")

    def _begin_grating_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        mask_key = (p.StimDiameter, p.StimXPosition, p.StimYPosition,
                   p.WaveSpPeriod, p.Contrast, p.Orientation, p.Phase,
                   p.BKGColor)
        if mask_key != self._last_mask_key:
            self._window.set_trial(p, w, h)
            self._last_mask_key = mask_key
        ifi = 1.0 / self._fps
        self._total_frames = max(
            1, round(1.0 / (ifi * max(p.WaveTempPeriodInHz, 1e-9)))
              * int(p.PeriodsToShow))
        self._shift_per_frame = p.WaveSpPeriod * p.WaveTempPeriodInHz * ifi
        self._xoffset = float(p.Phase)
        self._window.set_offset(self._xoffset)
        self._is_stim_phase = False
        self._is_visible = False
        self._window.set_visible(False)
        self._trigger_count = 0
        self._frame_i = 0
        self._blank_frames = 0
        self._stim_frames = 0
        self._trial_t0 = time.perf_counter()

    def _begin_map_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        ignored = regions_mod.ignored_rect(w, h)
        regions = regions_mod.region_rects(w, h)
        self._window.set_map_trial(ignored, regions, p.BKGColor)
        self._map_region_idx = 0
        self._map_tick_in_region = 0
        self._map_flip_tick = 0
        self._map_white = True
        self._map_pass = 0
        self._map_tick_count = 0
        self._map_total_ticks = (max(1, int(p.MapTicksPerRegion))
                                 * regions_mod.N_REGIONS
                                 * max(1, int(p.MapRepeats)))
        self._window.set_map_state(0, True)
        self._trial_t0 = time.perf_counter()

    def _begin_tuning_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        cx, cy, diameter = circle_mod.circle_geometry(p.TuningRegion, w, h)
        # The plain grating's own render path, just repositioned/resized to
        # the region — "a normal grating within that circle" needs no new
        # paint code, only these three fields swapped from the operator's.
        base = replace(p, StimDiameter=diameter,
                       StimXPosition=cx - w / 2.0, StimYPosition=cy - h / 2.0)
        self._window.set_trial(replace(base, Orientation=0.0), w, h)
        self._window.set_visible(True)
        self._window.set_solid(True)      # pretrial 1 starts white
        self._tuning_step_idx = 0
        self._tuning_tick_in_step = 0
        self._tuning_tick_count = 0
        repeats = max(1, int(p.TuningRepeats))
        self._tuning_total_steps = (tuning_mod.N_PRETRIALS
                                    + tuning_mod.N_ORIENTATIONS * repeats)
        self._tuning_total_ticks = (
            max(1, int(p.TuningTicksPerPretrial)) * tuning_mod.N_PRETRIALS
            + max(1, int(p.TuningTicksPerOrientation))
              * tuning_mod.N_ORIENTATIONS * repeats)
        self._trial_t0 = time.perf_counter()

    def _begin_contrast_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        cx, cy, diameter = circle_mod.circle_geometry(p.ContrastRegion, w, h)
        # Unlike orientation, Contrast bakes into the grating texture itself
        # (grating.build_grating), so each level needs a real set_trial()
        # rebuild rather than a lightweight setter — _contrast_base is kept
        # around for exactly that (_apply_contrast_step).
        self._contrast_base = replace(
            p, StimDiameter=diameter,
            StimXPosition=cx - w / 2.0, StimYPosition=cy - h / 2.0)
        self._window.set_trial(
            replace(self._contrast_base, Contrast=contrast_mod.CONTRAST_LEVELS[0]),
            w, h)
        self._window.set_visible(True)
        self._window.set_solid(True)      # pretrial 1 starts white
        self._contrast_step_idx = 0
        self._contrast_tick_in_step = 0
        self._contrast_tick_count = 0
        repeats = max(1, int(p.ContrastRepeats))
        self._contrast_total_steps = (contrast_mod.N_PRETRIALS
                                      + contrast_mod.N_LEVELS * repeats)
        self._contrast_total_ticks = (
            max(1, int(p.ContrastTicksPerPretrial)) * contrast_mod.N_PRETRIALS
            + max(1, int(p.ContrastTicksPerLevel))
              * contrast_mod.N_LEVELS * repeats)
        self._trial_t0 = time.perf_counter()

    def _begin_size_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        cx, cy, region_diameter = circle_mod.circle_geometry(p.SizeRegion, w, h)
        # Unlike contrast/tuning, the SWEPT quantity here is the aperture's
        # own diameter — center stays fixed at the region's center, but each
        # step needs a real set_trial() rebuild (geometry, not just texture).
        # The pretrial flash is shown at the full region size (fraction 1.0)
        # regardless of the sweep, same "fixed reference size" role
        # tuning/contrast's constant-size pretrial plays.
        self._size_base = replace(p, StimXPosition=cx - w / 2.0,
                                  StimYPosition=cy - h / 2.0)
        self._size_region_diameter = region_diameter
        self._window.set_trial(
            replace(self._size_base, StimDiameter=region_diameter), w, h)
        self._window.set_visible(True)
        self._window.set_solid(True)      # pretrial 1 starts white
        self._size_step_idx = 0
        self._size_tick_in_step = 0
        self._size_tick_count = 0
        repeats = max(1, int(p.SizeRepeats))
        self._size_total_steps = (size_mod.N_PRETRIALS
                                  + size_mod.N_SIZES * repeats)
        self._size_total_ticks = (
            max(1, int(p.SizeTicksPerPretrial)) * size_mod.N_PRETRIALS
            + max(1, int(p.SizeTicksPerLevel)) * size_mod.N_SIZES * repeats)
        self._trial_t0 = time.perf_counter()

    def _begin_visuomotor_trial(self, p: StimParams) -> None:
        """Full grating geometry/appearance, same as plain Grating — only the
        per-frame drift source and the trial-length condition differ (see
        `_visuomotor_frame`/`_grating_gate_tick`), so this is
        `_begin_grating_trial` minus the WaveTempPeriodInHz/PeriodsToShow
        bookkeeping that drift source replaces."""
        w, h = self._window.width(), self._window.height()
        mask_key = (p.StimDiameter, p.StimXPosition, p.StimYPosition,
                   p.WaveSpPeriod, p.Contrast, p.Orientation, p.Phase,
                   p.BKGColor)
        if mask_key != self._last_mask_key:
            self._window.set_trial(p, w, h)
            self._last_mask_key = mask_key
        self._xoffset = float(p.Phase)
        self._window.set_offset(self._xoffset)
        self._is_stim_phase = False
        self._is_visible = False
        self._window.set_visible(False)
        self._trigger_count = 0
        self._visuomotor_tick_count = 0
        self._blank_frames = 0
        self._stim_frames = 0
        self._trial_t0 = time.perf_counter()

    def _gate_tick(self) -> None:
        """One shared-clock tick arrived while RUNNING — the direct analog of
        one DAQ edge in runAnimationLoop's per-sample loop."""
        if self._s.trial_type == TRIAL_MAP:
            self._map_tick()
        elif self._s.trial_type == TRIAL_TUNING:
            self._tuning_tick()
        elif self._s.trial_type == TRIAL_CONTRAST:
            self._contrast_tick()
        elif self._s.trial_type == TRIAL_SIZE:
            self._size_tick()
        else:
            self._grating_gate_tick()

    def _grating_gate_tick(self) -> None:
        p = self._trials[self._trial_idx]
        self._trigger_count += 1
        if not self._is_stim_phase and self._trigger_count >= p.TriggersBlank:
            self._is_stim_phase, self._is_visible = True, True
            self._trigger_count = 0
        elif self._is_stim_phase and self._trigger_count >= p.TriggersStim:
            self._is_stim_phase, self._is_visible = False, False
            self._trigger_count = 0
        if self._window is not None:
            self._window.set_visible(self._is_visible)
        # Grating's own trial length is frame-counted in `_on_frame`
        # (PeriodsToShow/WaveTempPeriodInHz); Visuomotor has no fixed
        # temporal frequency to count cycles of, so it's tick-counted here
        # instead, same mechanism as WaitTrigger/TriggersBlank/TriggersStim.
        if self._s.trial_type == TRIAL_VISUOMOTOR:
            self._visuomotor_tick_count += 1
            if self._visuomotor_tick_count >= max(
                    1, int(p.VisuomotorDurationTicks)):
                self._end_trial(interrupted=False)
                self._advance_trial()

    def _map_tick(self) -> None:
        """One tick: advance the white/black flip and, every
        MapTicksPerRegion ticks, move on to the next region — the direct
        implementation of "region 1, then 2, ... flipping white/black while
        the others stay grey" the operator described."""
        p = self._trials[self._trial_idx]
        self._map_tick_count += 1

        self._map_flip_tick += 1
        if self._map_flip_tick >= max(1, int(p.MapTicksPerFlip)):
            self._map_flip_tick = 0
            self._map_white = not self._map_white

        self._map_tick_in_region += 1
        if self._map_tick_in_region >= max(1, int(p.MapTicksPerRegion)):
            self._map_tick_in_region = 0
            self._map_flip_tick = 0
            self._map_white = True
            self._map_region_idx += 1
            if self._map_region_idx >= regions_mod.N_REGIONS:
                self._map_region_idx = 0
                self._map_pass += 1

        if self._window is not None:
            self._window.set_map_state(self._map_region_idx, self._map_white)

        if self._map_tick_count >= self._map_total_ticks:
            self._end_trial(interrupted=False)
            self._advance_trial()

    def _tuning_tick(self) -> None:
        """One tick: the current step (a pretrial or an orientation) counts
        down; when it runs out, move to the next step in the fixed sequence
        [pretrial, pretrial, orientation 0, 45, ..., 315 (x TuningRepeats)]."""
        p = self._trials[self._trial_idx]
        self._tuning_tick_count += 1
        is_pretrial = self._tuning_step_idx < tuning_mod.N_PRETRIALS
        duration = (p.TuningTicksPerPretrial if is_pretrial
                   else p.TuningTicksPerOrientation)

        self._tuning_tick_in_step += 1
        if self._tuning_tick_in_step >= max(1, int(duration)):
            self._tuning_tick_in_step = 0
            self._tuning_step_idx += 1
            self._apply_tuning_step()

        if self._tuning_tick_count >= self._tuning_total_ticks:
            self._end_trial(interrupted=False)
            self._advance_trial()

    def _apply_tuning_step(self) -> None:
        """Push whatever the new `_tuning_step_idx` calls for onto the
        window — white for the 2 pretrials, else the next orientation."""
        if self._window is None or self._tuning_step_idx >= self._tuning_total_steps:
            return                        # the trial is ending this tick anyway
        if self._tuning_step_idx < tuning_mod.N_PRETRIALS:
            self._window.set_solid(True)
        else:
            k = self._tuning_step_idx - tuning_mod.N_PRETRIALS
            orientation = tuning_mod.orientations()[k % tuning_mod.N_ORIENTATIONS]
            self._window.set_solid(False)
            self._window.set_orientation(orientation)

    def _contrast_tick(self) -> None:
        """One tick: the current step (a pretrial or a contrast level)
        counts down; when it runs out, move to the next step in the fixed
        sequence [pretrial, pretrial, level 0, 1, ..., N-1 (x ContrastRepeats)]."""
        p = self._trials[self._trial_idx]
        self._contrast_tick_count += 1
        is_pretrial = self._contrast_step_idx < contrast_mod.N_PRETRIALS
        duration = (p.ContrastTicksPerPretrial if is_pretrial
                   else p.ContrastTicksPerLevel)

        self._contrast_tick_in_step += 1
        if self._contrast_tick_in_step >= max(1, int(duration)):
            self._contrast_tick_in_step = 0
            self._contrast_step_idx += 1
            self._apply_contrast_step()

        if self._contrast_tick_count >= self._contrast_total_ticks:
            self._end_trial(interrupted=False)
            self._advance_trial()

    def _apply_contrast_step(self) -> None:
        """Push whatever the new `_contrast_step_idx` calls for onto the
        window — white for the 2 pretrials, else the next contrast level (a
        full set_trial() rebuild, since Contrast is baked into the texture)."""
        if (self._window is None
                or self._contrast_step_idx >= self._contrast_total_steps):
            return                        # the trial is ending this tick anyway
        if self._contrast_step_idx < contrast_mod.N_PRETRIALS:
            self._window.set_solid(True)
        else:
            k = self._contrast_step_idx - contrast_mod.N_PRETRIALS
            level = contrast_mod.CONTRAST_LEVELS[k % contrast_mod.N_LEVELS]
            w, h = self._window.width(), self._window.height()
            self._window.set_trial(replace(self._contrast_base, Contrast=level),
                                   w, h)

    def _size_tick(self) -> None:
        """One tick: the current step (a pretrial or a size fraction) counts
        down; when it runs out, move to the next step in the fixed sequence
        [pretrial, pretrial, size 0, 1, ..., N-1 (x SizeRepeats)]."""
        p = self._trials[self._trial_idx]
        self._size_tick_count += 1
        is_pretrial = self._size_step_idx < size_mod.N_PRETRIALS
        duration = (p.SizeTicksPerPretrial if is_pretrial
                   else p.SizeTicksPerLevel)

        self._size_tick_in_step += 1
        if self._size_tick_in_step >= max(1, int(duration)):
            self._size_tick_in_step = 0
            self._size_step_idx += 1
            self._apply_size_step()

        if self._size_tick_count >= self._size_total_ticks:
            self._end_trial(interrupted=False)
            self._advance_trial()

    def _apply_size_step(self) -> None:
        """Push whatever the new `_size_step_idx` calls for onto the window
        — white at full region size for the 2 pretrials, else the next size
        fraction (a full set_trial() rebuild, since the aperture's own
        diameter is what's swept, not just the texture)."""
        if (self._window is None
                or self._size_step_idx >= self._size_total_steps):
            return                        # the trial is ending this tick anyway
        if self._size_step_idx < size_mod.N_PRETRIALS:
            self._window.set_solid(True)
        else:
            k = self._size_step_idx - size_mod.N_PRETRIALS
            fraction = size_mod.SIZE_FRACTIONS[k % size_mod.N_SIZES]
            w, h = self._window.width(), self._window.height()
            self._window.set_trial(
                replace(self._size_base,
                       StimDiameter=self._size_region_diameter * fraction),
                w, h)

    def _on_frame(self) -> None:
        """One painted frame — advances the drift phase and frame counters.
        Phase gating itself happens in `_gate_tick`, on its own (slower,
        shared) cadence; this only reads whatever `_is_visible` currently is.
        `map`/`tuning`/`contrast`/`size` trials have no per-frame drift or
        frame-count completion — they finish on ticks, so this is a no-op
        for them. `visuomotor` has its own per-frame drift source
        (`_visuomotor_frame`) instead of the fixed-frequency one below."""
        if self._phase != RUNNING or self._s.trial_type in _REGION_TRIAL_TYPES:
            return
        if self._s.trial_type == TRIAL_VISUOMOTOR:
            self._visuomotor_frame()
            return
        if self._frame_i >= self._total_frames:
            self._end_trial(interrupted=False)
            self._advance_trial()
            return

        p = self._trials[self._trial_idx]
        if self._is_visible:
            self._stim_frames += 1
        else:
            self._blank_frames += 1

        self._xoffset = (self._xoffset + self._shift_per_frame) % max(
            p.WaveSpPeriod, 1e-9)
        self._window.set_offset(self._xoffset)
        self._frame_i += 1

    def _visuomotor_frame(self) -> None:
        """Per painted frame: advance the drift phase by however far the
        wheel actually moved since the last frame, scaled by
        VisuomotorGain — a stationary wheel means a stationary grating,
        closing the loop between locomotion and optic flow. Trial-end and
        blank/stim gating are tick-driven, in `_grating_gate_tick`, same as
        the plain grating; this only ever updates the offset."""
        if self._is_visible:
            self._stim_frames += 1
        else:
            self._blank_frames += 1
        p = self._trials[self._trial_idx]
        speed = self._read_wheel_speed()
        ifi = 1.0 / self._fps
        self._xoffset = (self._xoffset + speed * p.VisuomotorGain * ifi) % max(
            p.WaveSpPeriod, 1e-9)
        self._window.set_offset(self._xoffset)

    def _read_wheel_speed(self) -> float:
        """Live wheel speed, in whatever unit the wheel module itself
        reports (mm/s with a diameter configured there, else rev/s) — 0.0 if
        no wheel is loaded or it hasn't produced a sample yet, which is what
        makes Visuomotor degrade to a static grating instead of crashing."""
        if self._wheel_speed is None:
            return 0.0
        sample = self._wheel_speed()
        return 0.0 if sample is None else float(sample[0])

    def _advance_trial(self) -> None:
        if self._trial_idx + 1 < len(self._trials):
            self._trial_idx += 1
            self._begin_trial()
        else:
            self._finish_run(aborted=False)

    def _end_trial(self, *, interrupted: bool) -> None:
        elapsed = time.perf_counter() - self._trial_t0
        p = self._trials[self._trial_idx]
        record = {"index": self._trial_idx, "params": asdict(p),
                 "elapsed_s": elapsed, "interrupted": interrupted}
        if self._s.trial_type == TRIAL_MAP:
            record["map_ticks"] = self._map_tick_count
            record["map_passes"] = self._map_pass
        elif self._s.trial_type == TRIAL_TUNING:
            record["tuning_ticks"] = self._tuning_tick_count
            record["tuning_steps_completed"] = self._tuning_step_idx
        elif self._s.trial_type == TRIAL_CONTRAST:
            record["contrast_ticks"] = self._contrast_tick_count
            record["contrast_steps_completed"] = self._contrast_step_idx
        elif self._s.trial_type == TRIAL_SIZE:
            record["size_ticks"] = self._size_tick_count
            record["size_steps_completed"] = self._size_step_idx
        else:
            record["blank_frames"] = self._blank_frames
            record["stim_frames"] = self._stim_frames
        self.trial_log.append(record)
        self.trial_boundary.emit(self._trial_idx, False, record)
        self.last_run_stats["trials_completed"] += 0 if interrupted else 1

    def _on_skip(self) -> None:
        """'n' — abort just the current trial (abortState=1 in the .m code)."""
        if self._phase != RUNNING:
            return
        self._end_trial(interrupted=True)
        self._advance_trial()

    def _on_escape(self) -> None:
        if self._phase == PRIMING:
            self.last_run_stats["aborted"] = True
            self._teardown_window()
            self._phase = IDLE
            self.progress_changed.emit("Progress: 0%")
            self.run_state_changed.emit(IDLE)
        elif self._phase == RUNNING:
            self._end_trial(interrupted=True)
            self._finish_run(aborted=True)

    def _finish_run(self, *, aborted: bool) -> None:
        self.last_run_stats["aborted"] = aborted
        n = len(self._trials)
        done = self.last_run_stats["trials_completed"]
        self._teardown_window()
        self._phase = IDLE
        # _begin_trial only ever reports "trials completed so far", so a run
        # that finishes normally never actually showed 100% — it just
        # dropped straight back to "Progress: 0%", indistinguishable from a
        # run that was stopped early. Report the real outcome instead.
        if aborted:
            self.progress_changed.emit(
                f"Progress: stopped ({done}/{n} trials completed)")
        else:
            self.progress_changed.emit(
                f"Progress: 100%   {done}/{n} trials complete")
        self.run_state_changed.emit(IDLE)

    def _teardown_window(self) -> None:
        if self._window is not None:
            self._window.close_display()
            self._window.deleteLater()
            self._window = None

    @property
    def phase(self) -> str:
        return self._phase

    def close(self) -> None:
        """Adapter teardown (module unload, app close)."""
        self._teardown_window()
