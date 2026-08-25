"""Hamamatsu ORCA-Fire acquisition presets and configuration. Sensor 4432×2368.

Presets follow the datasheet (Standard scan, Area Readout). Readout is
row-by-row at full width, so frame rate follows the number of ROWS almost
alone. Datasheet maxima (fps):

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

Actual fps = min(readout max, 1/exposure); vertical binning multiplies it
further. ROI sizes and positions are multiples of 4, as DCAM-API requires.

The table is the sensor's physics and stays complete; only rows ≥
MIN_PRESET_ROWS are OFFERED. Separate questions — read the note there before
deleting a row.
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
    hpos: int   # left edge of ROI on sensor
    vpos: int   # top edge of ROI on sensor

    @property
    def shape(self) -> tuple[int, int]:
        """(rows, cols) of the output frame before binning."""
        return (self.vsize, self.hsize)

    @property
    def is_full_frame(self) -> bool:
        return (self.hpos == 0 and self.vpos == 0
                and self.hsize == SENSOR_W and self.vsize == SENSOR_H)


def _band(label: str, rows: int) -> ResolutionPreset:
    """Full-width (X=4432) band of `rows` rows, centred vertically on the sensor
    (vpos snapped to a multiple of 4)."""
    vpos = ((SENSOR_H - rows) // 2 // 4) * 4
    return ResolutionPreset(label, SENSOR_W, rows, 0, vpos)


# (rows, USB3.1 Gen1 16-bit fps, CoaXPress fps), datasheet, slowest → fastest.
#
# WHICH LINK IS LIVE MATTERS: the ORCA-Fire has both, and this rig is cabled
# over **CoaXPress** (Active Silicon FireBird 4xCXP6-2PE8) — full frame ~8.7 ms,
# 115 fps. The 2026-07-29 figure of 15.8 fps was USB3 and does not apply.
# Software cannot pick the link; DCAM enumerates it, and DEFAULT_LINK only
# chooses the column the label shows.
#
# ESTIMATES, for UI and buffer sizing. get_frame_timings() is authoritative at
# run time, and the panel shows that measured rate once capture starts.
_ROWS_FPS_BOTH: List[tuple[int, float, float]] = [
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

# Which link the app assumes for its estimates. Only affects labels and the
# initial buffer sizing; the measured value from the camera overrides it. This
# rig is CoaXPress-cabled, so default to CXP.
DEFAULT_LINK: str = CXP

_ROWS_FPS: List[tuple[int, float]] = [(r, u) for r, u, _c in _ROWS_FPS_BOTH]
_ROWS_FPS_CXP: List[tuple[int, float]] = [(r, c) for r, _u, c in _ROWS_FPS_BOTH]

# Sorted once: readout_fps() is called on every panel update.
_SORTED = {USB: sorted(_ROWS_FPS), CXP: sorted(_ROWS_FPS_CXP)}


def _label(rows: int, fps_usb: float, fps_cxp: float) -> str:
    dims = f"{SENSOR_W}×{rows}"
    tag = "Full Frame " + f"({dims})" if rows == SENSOR_H else dims
    # Show both, so the label never silently lies about which link is live.
    return f"{tag} · {fps_usb:g} USB / {fps_cxp:g} CXP fps"


# Smallest band the UI offers: below 512 rows the sensor outruns the writer so
# far that the preset can only make an unrecordable session. The table keeps its
# smaller entries on purpose — readout_fps() interpolates them for BINNED ROIs
# (512 rows at bin 4 reads out like 128).
MIN_PRESET_ROWS: int = 512

PRESETS: Dict[str, ResolutionPreset] = {}
for _rows, _u, _c in _ROWS_FPS_BOTH:
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


def readout_fps(rows: int, binning: int = 1, link: str = DEFAULT_LINK) -> float:
    """Datasheet 16-bit readout ceiling for an ROI of `rows` rows on `link`.

    fps ≈ const / rows over most of the range (2368·15.7 ≈ 1024·36.4 ≈ 37 000),
    but below ~128 rows fixed per-frame overhead dominates and the datasheet
    falls off that line — hence interpolating the table, not extrapolating 1/rows.
    Vertical binning reads out `rows/binning` lines, so it scales the same way.
    """
    eff = max(1.0, rows / max(binning, 1))
    tbl = _SORTED[CXP if link == CXP else USB]    # ascending rows
    if eff <= tbl[0][0]:
        return tbl[0][1]
    if eff >= tbl[-1][0]:
        return tbl[-1][1]
    for (r0, f0), (r1, f1) in zip(tbl, tbl[1:]):  # log-log interpolation
        if r0 <= eff <= r1:
            w = (math.log(eff) - math.log(r0)) / (math.log(r1) - math.log(r0))
            return math.exp(math.log(f0) + w * (math.log(f1) - math.log(f0)))
    return tbl[-1][1]

# Sustained end-to-end write rate, MiB/s (worker → Recorder → HDF5Writer →
# NVMe). Here because the worker and the panel must agree what can be recorded.
#
# THE ONE FIGURE HERE THAT IS NOT A RIG MEASUREMENT. It was 1000, measured
# 2026-08-17 with the camera; the direct-chunk writer then took the same bench
# to 2464 MB/s saturated (2350 MiB/s), derated by the 0.77 that run showed
# against its own bench (1004/1305) for ORCA grab-thread contention.
# Deliberately low — too high and frames drop unwarned. Re-measure and replace.
WRITER_MBPS: float = 1800.0

BINNING_OPTIONS: List[int] = [1, 2, 4]
DEFAULT_BINNING: int = 1

TRIGGER_MODES: List[str] = ["Internal (free-running)", "External edge"]
DEFAULT_TRIGGER: str = "Internal (free-running)"


@dataclass
class AcqConfig:
    """All acquisition parameters for a single capture session."""
    preset_key:   str   = DEFAULT_PRESET
    binning:      int   = DEFAULT_BINNING
    exposure_us:  float = 10_000.0      # µs; 10 ms default for voltage imaging
    trigger_mode: str   = DEFAULT_TRIGGER
    # Datasheet ESTIMATE only; the worker replaces it with the camera's own
    # get_frame_timings() at start.
    link:         str   = DEFAULT_LINK

    @property
    def preset(self) -> ResolutionPreset:
        return PRESETS[self.preset_key]

    @property
    def frame_shape(self) -> tuple[int, int]:
        """Output shape (rows, cols) after binning."""
        rows, cols = self.preset.shape
        return (rows // self.binning, cols // self.binning)

    @property
    def frame_bytes(self) -> int:
        """Bytes on the wire per frame (16-bit)."""
        h, w = self.frame_shape
        return max(int(h) * int(w) * 2, 1)

    # ── Frame-rate budget ────────────────────────────────────────────────────
    # fps = min(readout ceiling, 1/exposure). Both are surfaced so the UI can
    # say *which* binds: an over-long exposure silently throws away the preset.

    @property
    def readout_fps(self) -> float:
        """Sensor/link ceiling for this ROI + binning (datasheet estimate)."""
        return readout_fps(self.preset.vsize, self.binning, self.link)

    @property
    def exposure_fps(self) -> float:
        """Ceiling imposed by the exposure time alone."""
        return 1e6 / max(self.exposure_us, 1e-6)

    @property
    def expected_fps(self) -> float:
        return min(self.readout_fps, self.exposure_fps)

    @property
    def exposure_limited(self) -> bool:
        """True when exposure — not readout — is what caps the frame rate."""
        return self.exposure_fps < self.readout_fps

    @property
    def max_exposure_us(self) -> float:
        """Longest exposure that still reaches the readout ceiling."""
        return 1e6 / max(self.readout_fps, 1e-9)
