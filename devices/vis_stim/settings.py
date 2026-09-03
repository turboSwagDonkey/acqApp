"""Visual stim — the settings model. No Qt (the widgets are in `panel.py`).

Port of visStimCode's logicLibHelpers.getDefaultParams: a drifting sinusoidal
grating shown through a circular aperture, gated on/off by "trigger" pulses.
The .m code read those pulses from an external MCC DAQ line; this rig has no
such line, so acqApp counts pulses off the shared session clock's own
periodic tick instead (`acq/sync.py`'s SyncController, 10 Hz by default) —
the same timing sequence every other module already shares, rather than
inventing a second one. See control.py's `on_tick`.

BarWidth, RotationPeriodInHz, FlashPeriodInHz, LUTStart/End, DoubleStim,
FlashType, ModulationType and WaveType are carried over from the MATLAB
defaults for config parity — but, as in the current .m code, nothing renders
them yet; only a drifting sinusoid (the MATLAB default WaveType) is
implemented (`grating.py`).

`VisStimSettings` nests `StimParams`/`LoopVar`, so it does not go through
config.load_dataclass (that helper only handles flat dataclasses — see
adapters/dmd.py's DmdSettings for the flat convention). It gets its own
to_dict/from_dict instead, the same shape as routines/settings.py's Routine.

Beyond the grating, `VisStimSettings.trial_type` selects among the
paradigms the operator's rig runs — string constants, the same convention
`devices/dmd/control.py` uses for MODE_ALL_ON/MODE_PATTERN/MODE_ROI. Only
`TRIAL_GRATING` and `TRIAL_MAP` are implemented; the rest are reserved names
the panel shows but disables, so the roadmap is visible.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

TRIAL_GRATING    = "grating"
TRIAL_MAP        = "map"
TRIAL_TUNING     = "tuning"
TRIAL_CONTRAST   = "contrast"
TRIAL_SIZE       = "size"
TRIAL_VISUOMOTOR = "visuomotor"
TRIAL_TYPES = (TRIAL_GRATING, TRIAL_MAP, TRIAL_TUNING, TRIAL_CONTRAST,
              TRIAL_SIZE, TRIAL_VISUOMOTOR)
IMPLEMENTED_TRIAL_TYPES = (TRIAL_GRATING, TRIAL_MAP, TRIAL_TUNING, TRIAL_CONTRAST)


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
    BarWidth: float = 6.0
    RotationPeriodInHz: float = 0.0
    BKGColor: float = 0.5
    FlashPeriodInHz: float = 0.0
    LUTStart: float = 1.0
    LUTEnd: float = 10000.0
    DoubleStim: float = 0.0
    FlashType: float = 3.0
    ModulationType: float = 1.0
    WaveType: float = 3.0
    TriggersBlank: float = 10.0
    TriggersStim: float = 5.0
    WaitTrigger: float = 5.0
    # shared by every region-grid trial type (map/tuning/contrast/size,
    # regions.py) — which of the 4 columns is blacked out.
    RegionIgnoredColumn: float = 3.0
    # map trial only — the shared-clock-tick counts that pace region
    # advance/flip.
    MapTicksPerRegion: float = 10.0
    MapTicksPerFlip: float = 2.0
    MapRepeats: float = 1.0
    # tuning trial only (tuning.py) — which of the 9 regions (1-9) the
    # circle sits at, and the tick counts that pace the 2 white pretrials
    # and the 8-orientation sweep (repeated MapRepeats-style).
    TuningRegion: float = 1.0
    TuningTicksPerPretrial: float = 10.0
    TuningTicksPerOrientation: float = 10.0
    TuningRepeats: float = 1.0
    # contrast trial only (contrast.py) — which of the 9 regions (1-9) the
    # circle sits at, and the tick counts that pace the 2 white pretrials
    # and the contrast-level sweep (repeated ContrastRepeats-style).
    ContrastRegion: float = 1.0
    ContrastTicksPerPretrial: float = 10.0
    ContrastTicksPerLevel: float = 10.0
    ContrastRepeats: float = 1.0


@dataclass
class LoopVar:
    name: str
    values: tuple[float, ...] = ()


@dataclass
class VisStimSettings:
    trial_type: str = TRIAL_GRATING
    screen_index: int = 0             # which QScreen the stimulus opens on
    stretch_to_screen: bool = False
    params: StimParams = field(default_factory=StimParams)
    loops: dict[str, LoopVar] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "trial_type": self.trial_type,
            "screen_index": self.screen_index,
            "stretch_to_screen": self.stretch_to_screen,
            "params": asdict(self.params),
            "loops": {name: list(lv.values) for name, lv in self.loops.items()},
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
        trial_type = d.get("trial_type", TRIAL_GRATING)
        if trial_type not in TRIAL_TYPES:      # a stale/hand-edited value
            trial_type = TRIAL_GRATING
        return cls(
            trial_type=trial_type,
            screen_index=int(d.get("screen_index", 0) or 0),
            stretch_to_screen=bool(d.get("stretch_to_screen", False)),
            params=params,
            loops=loops,
        )


_RANGE_RE = re.compile(r"^\s*([+-]?[\d.]+)\s*:\s*([+-]?[\d.]+)\s*:\s*([+-]?[\d.]+)\s*$")


def parse_values(text: str) -> tuple[float, ...]:
    """Parse a loop-values field: "1,2,3", "1 2 3", or a "start:step:stop"
    range (MATLAB colon syntax) — replaces `str2num` on a numeric vector
    literal. Returns () if nothing parses, same as MATLAB's empty result."""
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
