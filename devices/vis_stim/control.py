"""VisStimController — the stimulus state machine, ported from
guiVisStimDAQ.m (startStimFlow, runStimManager, runAnimationLoop).

Advances once per painted frame instead of MATLAB's blocking loop. There
is no MCC DAQ trigger line: each SyncController tick (10 Hz, while live) is
one pulse, so WaitTrigger/TriggersBlank/TriggersStim count ticks (MATLAB
names kept).

Trial types (`VisStimSettings.trial_type`):
  grating     drifting grating, frame-counted
  map         one of 9 regions flips white/black at a time (`regions.py`)
  tuning      circle at a region: 2 white pretrials, then 8 orientations
  contrast    same, sweeping Contrast
  size        same, sweeping the circle's diameter
  visuomotor  grating whose drift follows live wheel speed x gain
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
from .settings import REGION_TRIAL_TYPES as _REGION_TRIAL_TYPES
from .settings import (TRIAL_CONTRAST, TRIAL_MAP, TRIAL_SIZE, TRIAL_TUNING,
                       TRIAL_VISUOMOTOR, StimParams, VisStimSettings)
from .window import StimDisplay

IDLE, PRIMING, RUNNING = "IDLE", "PRIMING", "RUNNING"

# Pretrials-then-sweep trial types, keyed to the prefix of their log keys.
_SWEEPS = {TRIAL_TUNING: "tuning", TRIAL_CONTRAST: "contrast", TRIAL_SIZE: "size"}


class VisStimController(QObject):
    progress_changed = pyqtSignal(str)
    run_state_changed = pyqtSignal(str)       # IDLE | PRIMING | RUNNING
    trial_boundary = pyqtSignal(int, bool, object)   # (index, opening, params)

    def __init__(self, settings: VisStimSettings, parent=None,
                 wheel_speed: Callable[[], tuple[float, float] | None] | None
                 = None) -> None:
        super().__init__(parent)
        self._s = settings
        self._phase = IDLE
        self._window: StimDisplay | None = None
        # (value, acquired_at) or None; without a wheel, visuomotor is static.
        self._wheel_speed = wheel_speed

        # Valid only while PRIMING/RUNNING.
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
        # map
        self._map_region_idx = 0
        self._map_tick_in_region = 0
        self._map_flip_tick = 0
        self._map_white = True
        self._map_pass = 0
        self._map_tick_count = 0
        self._map_total_ticks = 0
        # tuning / contrast / size
        self._sweep: tuple | None = None      # (pre_ticks, level_ticks, n_pre, n, show)
        self._sweep_step = 0
        self._sweep_tick_in_step = 0
        self._sweep_tick_count = 0
        self._sweep_total_steps = 0
        self._sweep_total_ticks = 0
        # visuomotor
        self._visuomotor_tick_count = 0
        # rec.put() carries only floats, so per-trial detail goes to
        # final_metadata as JSON.
        self.trial_log: list[dict[str, Any]] = []
        self.last_run_stats: dict[str, Any] = {
            "trials_total": 0, "trials_completed": 0, "aborted": False}

    # ── settings ──────────────────────────────────────────────────────────
    def apply_settings(self, s: VisStimSettings) -> None:
        self._s = s

    # ── one shared-clock tick = one "trigger" ─────────────────────────────
    def on_tick(self, _elapsed_s: float) -> None:
        if self._phase == PRIMING:
            self._prime_tick()
        elif self._phase == RUNNING:
            self._gate_tick()

    # ── run control ──────────────────────────────────────────────────────
    def run(self) -> bool:
        """Open the display and wait WaitTrigger ticks. False if running."""
        if self._phase != IDLE:
            return False
        base = self._s.params
        # Loop variables are grating/visuomotor only; region types sweep
        # internally.
        self._trials = ([base] if self._s.trial_type in _REGION_TRIAL_TYPES
                        else trials_mod.gen_param_combos(base, self._s.loops))
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

        self._window = StimDisplay()      # opens mid-grey, right for priming
        self._window.painted.connect(self._on_frame)
        self._window.escape_pressed.connect(self._on_escape)
        self._window.skip_pressed.connect(self._on_skip)
        hz = screen.refreshRate() or 60.0
        self._window.open_on(screen, hz)
        self._hz = hz

        self._phase = PRIMING
        self._prime_count = 0
        target = int(base.WaitTrigger)
        self.progress_changed.emit(f"PRIMED: waiting for triggers "
                                   f"(0/{target})...")
        self.run_state_changed.emit(PRIMING)
        return True

    def stop(self) -> None:
        """Same as ESC."""
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
        kind = self._s.trial_type
        if kind == TRIAL_MAP:
            self._begin_map_trial(p)
        elif kind in _SWEEPS:
            self._begin_sweep_trial(p)
        elif kind == TRIAL_VISUOMOTOR:
            self._begin_visuomotor_trial(p)
        else:
            self._begin_grating_trial(p)
        self.trial_boundary.emit(self._trial_idx, True, asdict(p))
        n = len(self._trials)
        self.progress_changed.emit(
            f"Progress: {(self._trial_idx / n) * 100:.1f}%   "
            f"trial {self._trial_idx + 1}/{n}")

    def _rebuild_texture_if_changed(self, p: StimParams, w: int, h: int) -> None:
        mask_key = (p.StimDiameter, p.StimXPosition, p.StimYPosition,
                   p.WaveSpPeriod, p.Contrast, p.Orientation, p.Phase,
                   p.BKGColor)
        if mask_key != self._last_mask_key:
            self._window.set_trial(p, w, h)
            self._last_mask_key = mask_key

    def _reset_drift_gating(self, p: StimParams) -> None:
        self._xoffset = float(p.Phase)
        self._window.set_offset(self._xoffset)
        self._is_stim_phase = False
        self._is_visible = False
        self._window.set_visible(False)
        self._trigger_count = 0
        self._blank_frames = 0
        self._stim_frames = 0
        self._trial_t0 = time.perf_counter()

    def _begin_grating_trial(self, p: StimParams) -> None:
        w, h = self._window.width(), self._window.height()
        self._rebuild_texture_if_changed(p, w, h)
        ifi = 1.0 / self._hz
        self._total_frames = max(
            1, round(1.0 / (ifi * max(p.WaveTempPeriodInHz, 1e-9)))
              * int(p.PeriodsToShow))
        self._shift_per_frame = p.WaveSpPeriod * p.WaveTempPeriodInHz * ifi
        self._frame_i = 0
        self._reset_drift_gating(p)

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

    def _begin_sweep_trial(self, p: StimParams) -> None:
        """A grating in a region's circle: N_PRETRIALS white flashes, then N
        levels x repeats. `show(k)` puts level k on screen."""
        kind = self._s.trial_type
        win = self._window
        w, h = win.width(), win.height()
        region = {TRIAL_TUNING: p.TuningRegion, TRIAL_CONTRAST: p.ContrastRegion,
                  TRIAL_SIZE: p.SizeRegion}[kind]
        cx, cy, diameter = circle_mod.circle_geometry(region, w, h)
        centred = replace(p, StimXPosition=cx - w / 2.0,
                          StimYPosition=cy - h / 2.0)
        if kind == TRIAL_TUNING:
            base = replace(centred, StimDiameter=diameter)
            win.set_trial(replace(base, Orientation=0.0), w, h)
            angles = tuning_mod.orientations()

            def show(k: int) -> None:
                win.set_solid(False)
                win.set_orientation(angles[k])
            timing = (p.TuningTicksPerPretrial, p.TuningTicksPerOrientation,
                      p.TuningRepeats, tuning_mod.N_PRETRIALS,
                      tuning_mod.N_ORIENTATIONS)
        elif kind == TRIAL_CONTRAST:
            # Contrast is baked into the texture: each level is a rebuild.
            base = replace(centred, StimDiameter=diameter)
            levels = contrast_mod.CONTRAST_LEVELS
            win.set_trial(replace(base, Contrast=levels[0]), w, h)

            def show(k: int) -> None:
                win.set_trial(replace(base, Contrast=levels[k]), w, h)
            timing = (p.ContrastTicksPerPretrial, p.ContrastTicksPerLevel,
                      p.ContrastRepeats, contrast_mod.N_PRETRIALS,
                      contrast_mod.N_LEVELS)
        else:
            # The aperture itself is swept; pretrials show the full region.
            win.set_trial(replace(centred, StimDiameter=diameter), w, h)
            fractions = size_mod.SIZE_FRACTIONS

            def show(k: int) -> None:
                win.set_trial(replace(centred,
                                      StimDiameter=diameter * fractions[k]), w, h)
            timing = (p.SizeTicksPerPretrial, p.SizeTicksPerLevel,
                      p.SizeRepeats, size_mod.N_PRETRIALS, size_mod.N_SIZES)

        win.set_visible(True)
        win.set_solid(True)               # pretrial 1 starts white
        pre, lvl, reps, n_pre, n = timing
        pre, lvl, reps = max(1, int(pre)), max(1, int(lvl)), max(1, int(reps))
        self._sweep = (pre, lvl, n_pre, n, show)
        self._sweep_step = self._sweep_tick_in_step = self._sweep_tick_count = 0
        self._sweep_total_steps = n_pre + n * reps
        self._sweep_total_ticks = pre * n_pre + lvl * n * reps
        self._trial_t0 = time.perf_counter()

    def _begin_visuomotor_trial(self, p: StimParams) -> None:
        """A grating whose drift comes from the wheel; length is tick-counted."""
        w, h = self._window.width(), self._window.height()
        self._rebuild_texture_if_changed(p, w, h)
        self._visuomotor_tick_count = 0
        self._reset_drift_gating(p)

    def _gate_tick(self) -> None:
        """One tick while RUNNING — one DAQ edge in the .m code."""
        kind = self._s.trial_type
        if kind == TRIAL_MAP:
            self._map_tick()
        elif kind in _SWEEPS:
            self._sweep_tick()
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
        # Visuomotor has no temporal frequency to count frames by.
        if self._s.trial_type == TRIAL_VISUOMOTOR:
            self._visuomotor_tick_count += 1
            if self._visuomotor_tick_count >= max(
                    1, int(p.VisuomotorDurationTicks)):
                self._end_trial(interrupted=False)
                self._advance_trial()

    def _map_tick(self) -> None:
        """Flip the current region every MapTicksPerFlip; move to the next
        every MapTicksPerRegion."""
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

    def _sweep_tick(self) -> None:
        pre, lvl, n_pre, n, show = self._sweep
        self._sweep_tick_count += 1
        self._sweep_tick_in_step += 1
        if self._sweep_tick_in_step >= (pre if self._sweep_step < n_pre else lvl):
            self._sweep_tick_in_step = 0
            self._sweep_step += 1
            # Past the last step the trial ends this tick anyway.
            if (self._window is not None
                    and self._sweep_step < self._sweep_total_steps):
                if self._sweep_step < n_pre:
                    self._window.set_solid(True)
                else:
                    show((self._sweep_step - n_pre) % n)

        if self._sweep_tick_count >= self._sweep_total_ticks:
            self._end_trial(interrupted=False)
            self._advance_trial()

    def _on_frame(self) -> None:
        """Per painted frame: advance the drift (gating is on ticks). Region
        types finish on ticks, so nothing to do for them."""
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
        self._count_frame()
        self._xoffset = (self._xoffset + self._shift_per_frame) % max(
            p.WaveSpPeriod, 1e-9)
        self._window.set_offset(self._xoffset)
        self._frame_i += 1

    def _visuomotor_frame(self) -> None:
        """Drift by how far the wheel moved, x VisuomotorGain."""
        self._count_frame()
        p = self._trials[self._trial_idx]
        speed = self._read_wheel_speed()
        ifi = 1.0 / self._hz
        self._xoffset = (self._xoffset + speed * p.VisuomotorGain * ifi) % max(
            p.WaveSpPeriod, 1e-9)
        self._window.set_offset(self._xoffset)

    def _count_frame(self) -> None:
        if self._is_visible:
            self._stim_frames += 1
        else:
            self._blank_frames += 1

    def _read_wheel_speed(self) -> float:
        """In the wheel's own units; 0.0 with no wheel or no sample yet."""
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
        kind = self._s.trial_type
        if kind == TRIAL_MAP:
            record["map_ticks"] = self._map_tick_count
            record["map_passes"] = self._map_pass
        elif kind in _SWEEPS:
            record[f"{_SWEEPS[kind]}_ticks"] = self._sweep_tick_count
            record[f"{_SWEEPS[kind]}_steps_completed"] = self._sweep_step
        else:
            record["blank_frames"] = self._blank_frames
            record["stim_frames"] = self._stim_frames
        self.trial_log.append(record)
        self.trial_boundary.emit(self._trial_idx, False, record)
        self.last_run_stats["trials_completed"] += 0 if interrupted else 1

    def _on_skip(self) -> None:
        """'n' — abort the current trial only."""
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
        self._teardown_window()
