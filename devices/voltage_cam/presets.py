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
_ROWS_HZ_CXP: List[tuple[int, float]] = [(r, c) for r, _u, c in _ROWS_HZ_BOTH]
_SORTED = {USB: sorted(_ROWS_HZ_USB), CXP: sorted(_ROWS_HZ_CXP)}


def _label(rows: int, hz_usb: float, hz_cxp: float) -> str:
    dims = f"{SENSOR_W}×{rows}"
    tag = "Full Frame " + f"({dims})" if rows == SENSOR_H else dims
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
    ~128 rows fixed overhead bends the curve off const/rows."""
    eff = max(1.0, rows / max(binning, 1))
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

# Sustained end-to-end write rate, MiB/s. The old ~510 was the SATA save drive;
# the NVMe (D:) measured 1533 without the GUI. Raise once confirmed in-app.
WRITER_MBPS: float = 1300.0

BINNING_OPTIONS: List[int] = [1, 2, 4]
DEFAULT_BINNING: int = 1

TRIGGER_MODES: List[str] = ["Internal (free-running)", "External edge"]
DEFAULT_TRIGGER: str = "Internal (free-running)"


@dataclass
class AcqConfig:
    preset_key:   str   = DEFAULT_PRESET
    binning:      int   = DEFAULT_BINNING
    exposure_us:  float = 10_000.0
    trigger_mode: str   = DEFAULT_TRIGGER
    link:         str   = DEFAULT_LINK      # for estimates only

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

    # Hz = min(readout, 1/exposure); both surfaced so the UI can say which binds.
    @property
    def readout_hz(self) -> float:
        return readout_hz(self.preset.vsize, self.binning, self.link)

    @property
    def exposure_hz(self) -> float:
        return 1e6 / max(self.exposure_us, 1e-6)

    @property
    def expected_hz(self) -> float:
        return min(self.readout_hz, self.exposure_hz)

    @property
    def exposure_limited(self) -> bool:
        return self.exposure_hz < self.readout_hz

    @property
    def max_exposure_us(self) -> float:
        """Longest exposure that still reaches the readout ceiling."""
        return 1e6 / max(self.readout_hz, 1e-9)
