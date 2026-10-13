"""Visual stim settings, from visStimCode's getDefaultParams. No Qt.

The MATLAB wave/flash/LUT fields were dropped; `from_dict` ignores stale
saved values. Nested, so not config.load_dataclass.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

TRIAL_GRATING    = "grating"
TRIAL_MAP        = "map"
TRIAL_TUNING     = "tuning"
TRIAL_CONTRAST   = "contrast"
TRIAL_SPATIAL_FREQ = "spatial_freq"
TRIAL_SIZE       = "size"
TRIAL_VISUOMOTOR = "visuomotor"
TRIAL_TYPES = (TRIAL_GRATING, TRIAL_MAP, TRIAL_TUNING, TRIAL_CONTRAST,
              TRIAL_SPATIAL_FREQ, TRIAL_SIZE, TRIAL_VISUOMOTOR)
# Lets a reserved-but-unbuilt type show in the panel, disabled.
IMPLEMENTED_TRIAL_TYPES = TRIAL_TYPES

# Orientation x level gratings in a region (`sweep.py`); tuning has no level.
SWEEP_TRIAL_TYPES = (TRIAL_TUNING, TRIAL_CONTRAST, TRIAL_SPATIAL_FREQ,
                     TRIAL_SIZE)
# Types that sweep a region internally (no Loop variables, no stretch).
REGION_TRIAL_TYPES = (TRIAL_MAP,) + SWEEP_TRIAL_TYPES

# The swept levels, editable on the panel: contrast in %, spatial frequency
# in cycles/deg, size in deg.
DEFAULT_LEVELS: dict[str, tuple[float, ...]] = {
    TRIAL_CONTRAST:     (10.0, 25.0, 40.0, 65.0, 100.0),
    TRIAL_SPATIAL_FREQ: (0.01, 0.04, 0.10, 0.15, 0.30),
    TRIAL_SIZE:         (5.0, 10.0, 35.0, 60.0, 80.0),
}


@dataclass
class StimParams:
    StimDiameter: float = 1000.0
    WaveSpPeriod: float = 12.0
    Orientation: float = 90.0
    Mean: float = 0.5
    Phase: float = 0.0
    WaveTempPeriodInHz: float = 2.0
    Contrast: float = 0.5
    StimXPosition: float = 0.0
    StimYPosition: float = 0.0
    PeriodsToShow: float = 1000.0
    BKGColor: float = 0.5
    TriggersBlank: float = 10.0       # counted in shared-clock ticks
    TriggersStim: float = 5.0
    WaitTrigger: float = 5.0
    # map, in seconds (rounded to clock ticks): grey, then per region white
    # then black, each repeat in a new order, then grey as long again
    MapGreySeconds: float = 1.0
    MapWhiteSeconds: float = 0.5
    MapBlackSeconds: float = 0.5
    MapRepeats: float = 1.0
    # tuning / contrast / spatial freq / size, shared: region 1-9, seconds
    # (rounded to clock ticks), the grating the swept level is applied to
    TuneRegion: float = 1.0
    TuneOnSeconds: float = 0.2
    TuneGapSeconds: float = 0.3
    TuneGreySeconds: float = 1.0
    TuneCyclesPerDeg: float = 0.04
    TuneDriftHz: float = 2.0
    TuneContrastPct: float = 100.0
    TuningRepeats: float = 5.0
    ContrastRepeats: float = 2.0
    SpatialFreqRepeats: float = 2.0
    SizeRepeats: float = 2.0
    # degrees -> px; width 0 = as the display reports it (EDID)
    ScreenWidthCm: float = 0.0
    ViewDistanceCm: float = 20.0
    # visuomotor: px of drift per wheel unit (mm, or rev without a diameter);
    # 0 = static, an open-loop control. Length in ticks.
    VisuomotorGain: float = 1.0
    VisuomotorDurationTicks: float = 100.0


@dataclass
class LoopVar:
    name: str
    values: tuple[float, ...] = ()


@dataclass
class VisStimSettings:
    trial_type: str = TRIAL_GRATING
    screen_index: int = 0
    stretch_to_screen: bool = False
    params: StimParams = field(default_factory=StimParams)
    loops: dict[str, LoopVar] = field(default_factory=dict)
    levels: dict[str, tuple[float, ...]] = field(
        default_factory=lambda: dict(DEFAULT_LEVELS))

    def to_dict(self) -> dict:
        return {
            "trial_type": self.trial_type,
            "screen_index": self.screen_index,
            "stretch_to_screen": self.stretch_to_screen,
            "params": asdict(self.params),
            "loops": {name: list(lv.values) for name, lv in self.loops.items()},
            "levels": {t: list(v) for t, v in self.levels.items()},
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "VisStimSettings":
        d = d or {}
        praw = d.get("params") or {}
        pkw = {k: v for k, v in praw.items() if k in StimParams.__dataclass_fields__}
        try:
            params = StimParams(**pkw)
        except TypeError:
            params = StimParams()
        loops: dict[str, LoopVar] = {}
        for name, vals in (d.get("loops") or {}).items():
            try:
                loops[str(name)] = LoopVar(str(name),
                                           tuple(float(v) for v in vals))
            except (TypeError, ValueError):
                continue
        levels = dict(DEFAULT_LEVELS)
        for t, vals in (d.get("levels") or {}).items():
            try:
                parsed = tuple(float(v) for v in vals)
            except (TypeError, ValueError):
                continue
            if t in levels and parsed:
                levels[t] = parsed
        trial_type = d.get("trial_type", TRIAL_GRATING)
        if trial_type not in TRIAL_TYPES:
            trial_type = TRIAL_GRATING
        return cls(
            trial_type=trial_type,
            screen_index=int(d.get("screen_index", 0) or 0),
            stretch_to_screen=bool(d.get("stretch_to_screen", False)),
            params=params,
            loops=loops,
            levels=levels,
        )


_RANGE_RE = re.compile(r"^\s*([+-]?[\d.]+)\s*:\s*([+-]?[\d.]+)\s*:\s*([+-]?[\d.]+)\s*$")


def parse_values(text: str) -> tuple[float, ...]:
    """"1,2,3", "1 2 3" or MATLAB "start:step:stop"; () if it doesn't parse."""
    text = (text or "").strip()
    if not text:
        return ()
    m = _RANGE_RE.match(text)
    if m:
        start, step, stop = (float(g) for g in m.groups())
        if step == 0:
            return ()
        n = int(round((stop - start) / step)) + 1
        if n <= 0:
            return ()
        return tuple(round(start + i * step, 10) for i in range(n))
    out = []
    for tok in re.split(r"[,\s]+", text):
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            return ()
    return tuple(out)
