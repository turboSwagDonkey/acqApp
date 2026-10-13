"""VisStimController — the stimulus state machine, ported from
guiVisStimDAQ.m (startStimFlow, runStimManager, runAnimationLoop).

Advances once per painted frame instead of MATLAB's blocking loop. There
is no MCC DAQ trigger line: each SyncController tick (10 Hz, while live) is
one pulse, so WaitTrigger/TriggersBlank/TriggersStim count ticks (MATLAB
names kept).

Trial types (`VisStimSettings.trial_type`):
  grating     drifting grating, frame-counted
  map         grey, then each of 9 regions white then black, in a fresh random
              order each repeat (`regions.py`), then grey
  tuning      drifting gratings in a region's circle, 8 orientations in a
              fresh random order each repeat, grey between (`sweep.py`)
  contrast    same, orientation x contrast level
  spatial_freq  same, orientation x cycles/deg
  size        same, orientation x circle diameter (deg)
  visuomotor  grating whose drift follows live wheel speed x gain
"""
from __future__ import annotations

import time
from dataclasses import asdict, replace
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QGuiApplication

from acqApp.acq.sync import DEFAULT_TICK_MS
from . import circle as circle_mod
from . import regions as regions_mod
from . import sweep as sweep_mod
from . import trials as trials_mod
from .settings import REGION_TRIAL_TYPES as _REGION_TRIAL_TYPES
from .settings import (TRIAL_CONTRAST, TRIAL_MAP, TRIAL_SIZE,
                       TRIAL_SPATIAL_FREQ, TRIAL_TUNING, TRIAL_VISUOMOTOR,
                       StimParams, VisStimSettings)
from .window import StimDisplay, reported_width_cm

IDLE, PRIMING, RUNNING = "IDLE", "PRIMING", "RUNNING"

# Screen width when it's neither typed in nor reported by the display.
_FALLBACK_SCREEN_CM = 52.0

# The tuning types, keyed to the prefix of their log keys.
_SWEEPS = {TRIAL_TUNING: "tuning", TRIAL_CONTRAST: "contrast",
           TRIAL_SPATIAL_FREQ: "spatial_freq", TRIAL_SIZE: "size"}


def _ticks(seconds: float) -> int:
    """Seconds as whole shared-clock ticks (assumes DEFAULT_TICK_MS)."""
    return max(0, round(seconds * 1000.0 / DEFAULT_TICK_MS))


class VisStimController(QObject):
    progress_changed = pyqtSignal(str)
    progress_fraction = pyqtSignal(float)     # 0-1 through the run
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
        # map: _map_order[pass] lists 0-based regions; _map_segments is the
        # timeline, (ticks, region or -1 for all grey, white, ends_pass)
        self._map_order: list[list[int]] = []
        self._map_segments: list[tuple[int, int, bool, bool]] = []
        self._map_seg = 0
        self._map_tick_in_seg = 0
        self._map_pass = 0
        self._map_tick_count = 0
        # tuning types: _sweep_segments is the timeline, (ticks, condition or
        # None for grey); the trial opens at segment _sweep_open_at (after
        # the pretrial). _sweep_order[repeat] lists the shown conditions.
        self._sweep_segments: list[tuple[int, tuple | None]] = []
        self._sweep_order: list[list[tuple]] = []
        self._sweep_stim: Callable[[tuple], StimParams] | None = None
        self._sweep_seg = 0
        self._sweep_open_at = 0
        self._sweep_opened = False
        self._sweep_tick_in_seg = 0
        self._sweep_tick_count = 0
        self._sweep_shown = 0
        self._screen_cm = _FALLBACK_SCREEN_CM   # set per run
        self._px_per_cm = 1.0
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
        self._screen_cm = (self._s.params.ScreenWidthCm
                           or reported_width_cm(screen) or _FALLBACK_SCREEN_CM)

        self._phase = PRIMING
        self._prime_count = 0
        self.progress_changed.emit(f"PRIMED: waiting for triggers "
                                   f"(0/{self._prime_target()})...")
        self.run_state_changed.emit(PRIMING)
        return True

    def stop(self) -> None:
        """Same as ESC."""
        self._on_escape()

    def _prime_target(self) -> int:
        """Ticks to wait before trial 1; region types lead in with their own
        grey or pretrial, so they start on the first tick."""
        if self._s.trial_type in _REGION_TRIAL_TYPES:
            return 0
        return int(self._s.params.WaitTrigger)

    def _prime_tick(self) -> None:
        target = self._prime_target()
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
        # A tuning type opens after its pretrial (`_show_sweep_segment`).
        if kind not in _SWEEPS:
            self.trial_boundary.emit(self._trial_idx, True, asdict(p))
        if kind in _REGION_TRIAL_TYPES:
            self._region_progress()       # then per tick
            return
        n = len(self._trials)
        self.progress_fraction.emit(self._trial_idx / n)
        self.progress_changed.emit(
            f"Progress: {(self._trial_idx / n) * 100:.1f}%   "
            f"trial {self._trial_idx + 1}/{n}")

    def _region_progress(self) -> None:
        """Map/tuning run as one trial: report seconds through it."""
        if self._s.trial_type == TRIAL_MAP:
            segs, done, pre = self._map_segments, self._map_tick_count, 0
        else:
            segs, done = self._sweep_segments, self._sweep_tick_count
            pre = sum(t for t, _ in segs[:self._sweep_open_at])
        total = sum(seg[0] for seg in segs)
        self.progress_fraction.emit(done / max(total, 1))
        hz = 1000.0 / DEFAULT_TICK_MS
        if done < pre:
            text = f"Pretrial  {done / hz:.1f} / {pre / hz:.1f} s"
        else:
            text = f"Trial  {(done - pre) / hz:.1f} / {(total - pre) / hz:.1f} s"
        self.progress_changed.emit(text)

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
        self._map_order = trials_mod.shuffled_blocks(regions_mod.N_REGIONS,
                                                     int(p.MapRepeats))
        white, black = _ticks(p.MapWhiteSeconds), _ticks(p.MapBlackSeconds)
        grey = (_ticks(p.MapGreySeconds), -1, False, False)
        segs = [grey]
        for order in self._map_order:
            for step, region in enumerate(order):
                last = step == len(order) - 1
                segs += [(white, region, True, last and not black),
                         (black, region, False, last)]
        segs.append(grey)
        self._map_segments = ([s for s in segs if s[0] > 0]
                              or [(1, -1, False, False)])
        self._map_seg = 0
        self._map_tick_in_seg = 0
        self._map_pass = 0
        self._map_tick_count = 0
        self._show_map_segment()
        self._trial_t0 = time.perf_counter()

    def _show_map_segment(self) -> None:
        if self._window is not None and self._map_seg < len(self._map_segments):
            _, region, white, _ = self._map_segments[self._map_seg]
            self._window.set_map_state(region, white)

    def _begin_sweep_trial(self, p: StimParams) -> None:
        """Build the timeline (`sweep.py`): pretrial, grey, the shuffled
        conditions each as on + gap, grey."""
        kind = self._s.trial_type
        w, h = self._window.width(), self._window.height()
        cx, cy, diameter = circle_mod.circle_geometry(p.TuneRegion, w, h)
        px_per_cm = self._px_per_cm = w / self._screen_cm
        dist = p.ViewDistanceCm
        base = replace(p, StimXPosition=cx - w / 2.0,
                       StimYPosition=cy - h / 2.0, StimDiameter=diameter,
                       Contrast=p.TuneContrastPct / 100.0, Phase=0.0,
                       WaveSpPeriod=sweep_mod.period_px(p.TuneCyclesPerDeg,
                                                        dist, px_per_cm))

        def stim(cond: tuple) -> StimParams:
            orientation, level = cond
            q = replace(base, Orientation=orientation)
            if level is None:
                return q
            if kind == TRIAL_CONTRAST:
                return replace(q, Contrast=level / 100.0)
            if kind == TRIAL_SPATIAL_FREQ:
                return replace(q, WaveSpPeriod=sweep_mod.period_px(
                    level, dist, px_per_cm))
            return replace(q, StimDiameter=sweep_mod.deg_to_px(
                level, dist, px_per_cm))
        self._sweep_stim = stim

        levels = () if kind == TRIAL_TUNING else self._s.levels.get(kind, ())
        conds = sweep_mod.conditions(levels)
        repeats = {TRIAL_TUNING: p.TuningRepeats,
                   TRIAL_CONTRAST: p.ContrastRepeats,
                   TRIAL_SPATIAL_FREQ: p.SpatialFreqRepeats,
                   TRIAL_SIZE: p.SizeRepeats}[kind]
        self._sweep_order = [[conds[i] for i in block] for block in
                             trials_mod.shuffled_blocks(len(conds), int(repeats))]
        on, gap = _ticks(p.TuneOnSeconds), _ticks(p.TuneGapSeconds)
        grey = (_ticks(p.TuneGreySeconds), None)
        pretrial = [seg for o in sweep_mod.orientations()
                    for seg in ((on, (o, None)), (gap, None))]
        trial = [grey] + [seg for block in self._sweep_order for c in block
                          for seg in ((on, c), (gap, None))] + [grey]
        pretrial = [s for s in pretrial if s[0] > 0]
        trial = [s for s in trial if s[0] > 0] or [(1, None)]
        self._sweep_segments = pretrial + trial
        self._sweep_open_at = len(pretrial)
        self._sweep_opened = False
        self._sweep_seg = self._sweep_tick_in_seg = 0
        self._sweep_tick_count = self._sweep_shown = 0
        self._trial_t0 = time.perf_counter()
        self._show_sweep_segment()

    def _show_sweep_segment(self) -> None:
        """Put the current segment on screen; open the trial on reaching it."""
        if self._sweep_seg == self._sweep_open_at and not self._sweep_opened:
            self._sweep_opened = True
            self._trial_t0 = time.perf_counter()
            self.trial_boundary.emit(self._trial_idx, True,
                                     asdict(self._trials[self._trial_idx]))
        win = self._window
        if win is None:
            return
        _, cond = self._sweep_segments[self._sweep_seg]
        self._is_visible = cond is not None
        if cond is not None:
            q = self._sweep_stim(cond)
            win.set_trial(q, win.width(), win.height())
            self._xoffset = 0.0
            self._shift_per_frame = q.WaveSpPeriod * q.TuneDriftHz / self._hz
        win.set_visible(self._is_visible)

    def _begin_visuomotor_trial(self, p: StimParams) -> None:
        """A grating whose drift comes from the wheel; length is tick-counted."""
        w, h = self._window.width(), self._window.height()
        self._rebuild_texture_if_changed(p, w, h)
        self._visuomotor_tick_count = 0
        self._reset_drift_gating(p)

    def _gate_tick(self) -> None:
        """One tick while RUNNING — one DAQ edge in the .m code."""
        kind = self._s.trial_type
        if kind in _REGION_TRIAL_TYPES:
            if kind == TRIAL_MAP:
                self._map_tick()
            else:
                self._sweep_tick()
            if self._phase == RUNNING:
                self._region_progress()
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
        """Step through the timeline built by `_begin_map_trial`."""
        self._map_tick_count += 1
        self._map_tick_in_seg += 1
        ticks, _, _, ends_pass = self._map_segments[self._map_seg]
        if self._map_tick_in_seg < ticks:
            return
        self._map_tick_in_seg = 0
        self._map_pass += ends_pass
        self._map_seg += 1
        if self._map_seg >= len(self._map_segments):
            self._end_trial(interrupted=False)
            self._advance_trial()
        else:
            self._show_map_segment()

    def _sweep_tick(self) -> None:
        """Step through the timeline built by `_begin_sweep_trial`."""
        self._sweep_tick_count += 1
        self._sweep_tick_in_seg += 1
        ticks, cond = self._sweep_segments[self._sweep_seg]
        if self._sweep_tick_in_seg < ticks:
            return
        self._sweep_tick_in_seg = 0
        if cond is not None and self._sweep_opened:
            self._sweep_shown += 1
        self._sweep_seg += 1
        if self._sweep_seg >= len(self._sweep_segments):
            self._end_trial(interrupted=False)
            self._advance_trial()
        else:
            self._show_sweep_segment()

    def _on_frame(self) -> None:
        """Per painted frame: advance the drift (gating is on ticks). Map
        finishes on ticks, so nothing to do for it."""
        if self._phase != RUNNING:
            return
        if self._s.trial_type in _SWEEPS:
            if self._is_visible and self._window is not None:
                self._xoffset += self._shift_per_frame
                self._window.set_offset(self._xoffset)
            return
        if self._s.trial_type in _REGION_TRIAL_TYPES:
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
            # 1-based, as on the panel: the only record of which region was lit.
            record["map_order"] = [[i + 1 for i in order]
                                   for order in self._map_order]
        elif kind in _SWEEPS:
            prefix = _SWEEPS[kind]
            record[f"{prefix}_ticks"] = self._sweep_tick_count   # with pretrial
            record[f"{prefix}_shown"] = self._sweep_shown
            record["screen_width_cm"] = self._screen_cm      # as used for deg
            # Per repeat, in shown order: orientation, or [orientation, level].
            record[f"{prefix}_order"] = [
                [o if level is None else [o, level] for o, level in block]
                for block in self._sweep_order]
        else:
            record["blank_frames"] = self._blank_frames
            record["stim_frames"] = self._stim_frames
        self.trial_log.append(record)
        # Stopped during a tuning pretrial: the trial never opened.
        if kind not in _SWEEPS or self._sweep_opened:
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
            self.progress_fraction.emit(0.0)
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
            self.progress_fraction.emit(1.0)
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

    @property
    def map_state(self) -> tuple[int, bool]:
        """(lit region 0-based or -1 for all grey, white); map trials only."""
        _, region, white, _ = self._map_segments[self._map_seg]
        return region, white

    def close(self) -> None:
        self._teardown_window()
