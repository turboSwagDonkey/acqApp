"""ORCA-Fire acquisition presets and config. Sensor 4432×2368.

Readout is row-by-row at full width, so frame rate follows the ROW count.
Datasheet maxima (Hz, Standard scan):

    Y (rows)   CoaXPress   USB3.1 Gen1 16-bit   USB3.1 Gen1 8-bit
      2368        115            15.7                 31.5
      2304        118            16.2                 32.4
      2048        132            18.2                 36.5
      1024        264            36.4                 72.8
       512        524            72.3                144
       256       1020           143                 286
       128       1980           279                 558
         8      15200          2360                5260
         4      19500          3960                7200

Hz = min(readout max, 1/exposure). ROI sizes/positions are multiples of 4.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Dict, List


SENSOR_W: int = 4432
SENSOR_H: int = 2368


@dataclass(frozen=True)
class ResolutionPreset:
    label: str
    hsize: int
    vsize: int
    hpos: int
    vpos: int

    @property
    def shape(self) -> tuple[int, int]:
        """(rows, cols) before binning."""
        return (self.vsize, self.hsize)

    @property
    def is_full_frame(self) -> bool:
        return (self.hpos == 0 and self.vpos == 0
                and self.hsize == SENSOR_W and self.vsize == SENSOR_H)


def _band(label: str, rows: int) -> ResolutionPreset:
    """A full-width band, centred vertically (vpos a multiple of 4)."""
    vpos = ((SENSOR_H - rows) // 2 // 4) * 4
    return ResolutionPreset(label, SENSOR_W, rows, 0, vpos)


# (rows, USB Hz, CXP Hz). This rig is cabled over CoaXPress; DCAM picks the
# link, not us. Estimates only — the camera's own timing wins at run time.
_ROWS_HZ_BOTH: List[tuple[int, float, float]] = [
    (2368, 15.7,  115.0),
    (2304, 16.2,  118.0),
    (2048, 18.2,  132.0),
    (1024, 36.4,  264.0),
    (512,  72.3,  524.0),
    (256,  143.0, 1020.0),
    (128,  279.0, 1980.0),
    (8,    2360.0, 15200.0),
    (4,    3960.0, 19500.0),
]

USB, CXP = "usb", "cxp"
LINK_LABEL = {USB: "USB3", CXP: "CoaXPress"}
DEFAULT_LINK: str = CXP

_ROWS_HZ_USB: List[tuple[int, float]] = [(r, u) for r, u, _c in _ROWS_HZ_BOTH]
_SORTED = {USB: sorted(_ROWS_HZ_USB),
           CXP: sorted((r, c) for r, _u, c in _ROWS_HZ_BOTH)}


def _label(rows: int, hz_usb: float, hz_cxp: float) -> str:
    dims = f"{SENSOR_W}×{rows}"
    tag = f"Full Frame ({dims})" if rows == SENSOR_H else dims
    return f"{tag} · {hz_usb:g} USB / {hz_cxp:g} CXP Hz"


# 8/4-row bands stay table-only (interpolation). Binning does NOT speed up this
# camera's readout (rig, 2026-09-11) — only fewer rows does.
MIN_PRESET_ROWS: int = 128

PRESETS: Dict[str, ResolutionPreset] = {}
for _rows, _u, _c in _ROWS_HZ_BOTH:
    if _rows < MIN_PRESET_ROWS:
        continue
    _key = f"{SENSOR_W}x{_rows}"
    _lab = _label(_rows, _u, _c)
    if _rows == SENSOR_H:
        PRESETS[_key] = ResolutionPreset(_lab, SENSOR_W, _rows, 0, 0)
    else:
        PRESETS[_key] = _band(_lab, _rows)

PRESET_KEYS: List[str] = list(PRESETS.keys())
DEFAULT_PRESET: str = f"{SENSOR_W}x{SENSOR_H}"    # full frame

# modes.json's spelling of DEFAULT_PRESET.
PRESET_ALIAS_FULL = "full"


def resolve_preset_key(key: str) -> str:
    return DEFAULT_PRESET if key == PRESET_ALIAS_FULL else key


def preset_alias(key: str) -> str:
    return PRESET_ALIAS_FULL if key == DEFAULT_PRESET else key


def readout_hz(rows: int, binning: int = 1, link: str = DEFAULT_LINK) -> float:
    """Datasheet readout ceiling for `rows` rows. Log-log interpolated: below
    ~128 rows fixed overhead bends the curve off const/rows.

    `binning` is ignored: binning doesn't speed readout (512 rows read in
    1.893 ms at bin 1/2/4, 2026-09-28)."""
    eff = max(1.0, float(rows))
    tbl = _SORTED[CXP if link == CXP else USB]
    if eff <= tbl[0][0]:
        return tbl[0][1]
    if eff >= tbl[-1][0]:
        return tbl[-1][1]
    for (r0, f0), (r1, f1) in zip(tbl, tbl[1:]):
        if r0 <= eff <= r1:
            w = (math.log(eff) - math.log(r0)) / (math.log(r1) - math.log(r0))
            return math.exp(math.log(f0) + w * (math.log(f1) - math.log(f0)))
    return tbl[-1][1]

# Sustained end-to-end write rate, MiB/s. The NVMe (D:) measured 1533 without
# the GUI; raise once confirmed in-app.
WRITER_MBPS: float = 1300.0

BINNING_OPTIONS: List[int] = [1, 2, 4]
DEFAULT_BINNING: int = 1

EXTERNAL_EDGE: str = "External edge"
TRIGGER_MODES: List[str] = ["Internal (free-running)", EXTERNAL_EDGE]
DEFAULT_TRIGGER: str = "Internal (free-running)"

# External edge runs off MASTER PULSE. Under TRIGGER ACTIVE=EDGE exposure and
# readout don't overlap, so the shortest interval is readout + exposure; under
# SYNCREADOUT they pipeline and exposure drops out (2026-09-28: same floor at
# 200/500/1500 us). Asking for less than the floor doesn't cap the rate, it
# silently HALVES it. The pad covers a further ~40-57 us the probe found above
# readout (+ exposure) in all 8 configs (2026-09-28); 70 us leaves 13 over the
# worst measured — UNVERIFIED on the rig (2026-09-30).
MP_INTERVAL_PAD_S = 0.00007

# MASTER PULSE MODE=BURST: one edge -> a fixed pulse count, then quiet and
# re-armed with no stop. Under SYNCREADOUT a pulse ENDS the running exposure,
# so the first pulse after a start yields nothing and a later burst's first
# frame is the exposure left open since the previous burst (probe, 2026-09-30:
# 900 pulses -> 899 frames, then 900). One extra pulse keeps N real frames.
BURST_TIMES_MAX = 65535


def burst_pulses(n: int, syncreadout: bool) -> int:
    return n + 1 if syncreadout else n


def burst_stale(index: int, n: int, syncreadout: bool) -> bool:
    """Frame `index` (since capture start) is a burst's leftover exposure."""
    return syncreadout and index >= n and (index - n) % (n + 1) == 0


def burst_next_boundary(acquired: int, n: int, syncreadout: bool) -> int:
    """Frame count (since start) at the end of the burst `acquired` is in, or
    `acquired` itself if it sits between bursts."""
    if acquired <= 0:
        return 0
    if acquired <= n:
        return n
    p = burst_pulses(n, syncreadout)
    return n + -(-(acquired - n) // p) * p


def burst_stale_indices(total: int, n: int, syncreadout: bool) -> list[int]:
    """Every leftover-exposure frame among the first `total` since a start."""
    return list(range(n, total, n + 1)) if syncreadout and n > 0 else []


# Slack on the datasheet ceiling before a requested rate counts as unreachable
# (512 rows: table 524 Hz, measured 528.2).
RATE_ESTIMATE_TOLERANCE: float = 1.05

# Exposure floor for the EDGE fallback at its ceiling, where the frame period
# leaves no room for more.
MIN_EXPOSURE_US: float = 10.0


def master_pulse_interval(period_s: float, exposure_us: float,
                          syncreadout: bool = False,
                          target_hz: float = 0.0) -> float:
    """MASTER PULSE INTERVAL for Internal's frame period `period_s`: the floor,
    or 1/`target_hz` when that's slower. EDGE (the default) is the safe floor
    for a camera that refused SYNCREADOUT."""
    floor = period_s + MP_INTERVAL_PAD_S
    if not syncreadout:
        floor += exposure_us * 1e-6
    return max(floor, 1.0 / target_hz) if target_hz > 0 else floor


def fit_exposure(readout_s: float, target_hz: float, master_pulse: bool,
                 syncreadout: bool = True) -> tuple[float, float]:
    """(exposure_s, frame period_s): the longest exposure that still holds
    `target_hz` (0 = as fast as possible), clamped to what the camera can do.

    Internal: the period is max(readout, exposure), so exposure = the period.
    SYNCREADOUT: exposure overlaps readout, so it gets the whole interval.
    EDGE: they're back to back, so exposure gets what readout + pad leave."""
    want = 1.0 / target_hz if target_hz > 0 else 0.0
    if not master_pulse:
        t = max(readout_s, want)
        return t, t
    floor = readout_s + MP_INTERVAL_PAD_S
    if syncreadout:
        t = max(floor, want)
        return t, t
    exp = max(want - floor, MIN_EXPOSURE_US * 1e-6)
    return exp, floor + exp


@dataclass
class AcqConfig:
    preset_key:   str   = DEFAULT_PRESET
    binning:      int   = DEFAULT_BINNING
    # Derived from target_hz by fit_exposure() — never set by hand.
    exposure_us:  float = 10_000.0
    trigger_mode: str   = DEFAULT_TRIGGER
    link:         str   = DEFAULT_LINK      # for estimates only
    # Capture rate, both trigger modes; 0 = as fast as this preset allows.
    target_hz:    float = 0.0
    # External edge only: frames per edge (BURST); 0 = until re-armed (START).
    burst_frames: int   = 0

    # ── preview (display only; persisted as preferences) ──
    show_lut:     bool = True
    auto_levels:  bool = True
    preview_avg:  int  = 1                  # 1 = off
    led_follow_live: bool = True

    @property
    def preset(self) -> ResolutionPreset:
        return PRESETS[self.preset_key]

    @property
    def frame_shape(self) -> tuple[int, int]:
        """(rows, cols) after binning."""
        rows, cols = self.preset.shape
        return (rows // self.binning, cols // self.binning)

    @property
    def frame_bytes(self) -> int:
        h, w = self.frame_shape
        return max(int(h) * int(w) * 2, 1)

    @property
    def readout_hz(self) -> float:
        return readout_hz(self.preset.vsize, link=self.link)

    @property
    def exposure_hz(self) -> float:
        return 1e6 / max(self.exposure_us, 1e-6)

    @property
    def expected_hz(self) -> float:
        """Internal (free-running) rate at this exposure."""
        return min(self.readout_hz, self.exposure_hz)

    @property
    def master_pulse(self) -> bool:
        return self.trigger_mode == EXTERNAL_EDGE

    @property
    def burst(self) -> bool:
        return self.master_pulse and self.burst_frames > 0

    @property
    def trigger_hz(self) -> float:
        """External edge ceiling assuming SYNCREADOUT, so exposure doesn't
        enter it. A camera that refuses it runs slower; the worker says so."""
        return 1.0 / master_pulse_interval(
            1.0 / max(self.readout_hz, 1e-9), self.exposure_us, syncreadout=True)

    @property
    def ceiling_hz(self) -> float:
        return self.trigger_hz if self.master_pulse else self.readout_hz

    @property
    def rate_unreachable(self) -> bool:
        """`target_hz` beyond the ceiling (with estimate slack): clamped."""
        return (self.target_hz > 0.0
                and self.target_hz > self.ceiling_hz * RATE_ESTIMATE_TOLERANCE)

    @property
    def rate_hz(self) -> float:
        """What this config runs at (estimate)."""
        if self.target_hz > 0.0 and not self.rate_unreachable:
            return self.target_hz
        return self.ceiling_hz

    def fit_exposure(self) -> "AcqConfig":
        """Set `exposure_us` to the longest the capture rate allows (datasheet
        estimate; the worker refits from the camera's own readout)."""
        exp_s, _ = fit_exposure(1.0 / max(self.readout_hz, 1e-9),
                                self.target_hz, self.master_pulse)
        self.exposure_us = exp_s * 1e6
        return self
