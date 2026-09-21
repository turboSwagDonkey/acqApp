"""Hamamatsu ORCA-Fire acquisition presets and configuration. Sensor 4432×2368.

Presets follow the datasheet (Standard scan, Area Readout). Readout is
row-by-row at full width, so frame rate follows the number of ROWS almost
alone. Datasheet maxima (Hz):

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

Actual Hz = min(readout max, 1/exposure); vertical binning multiplies it
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


# (rows, USB3.1 Gen1 16-bit Hz, CoaXPress Hz), datasheet, slowest → fastest.
#
# WHICH LINK IS LIVE MATTERS: the ORCA-Fire has both, and this rig is cabled
# over **CoaXPress** (Active Silicon FireBird 4xCXP6-2PE8) — full frame ~8.7 ms,
# 115 Hz. The 2026-07-29 figure of 15.8 Hz was USB3 and doesn't apply.
# Software can't pick the link; DCAM enumerates it, and DEFAULT_LINK only
# chooses the column the label shows.
#
# ESTIMATES, for UI and buffer sizing. get_frame_timings() is authoritative at
# run time, and the panel shows that measured rate once capture starts.
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

# Which link the app assumes for its estimates. Only affects labels and the
# initial buffer sizing; the measured value from the camera overrides it. This
# rig is CoaXPress-cabled, so default to CXP.
DEFAULT_LINK: str = CXP

_ROWS_HZ_USB: List[tuple[int, float]] = [(r, u) for r, u, _c in _ROWS_HZ_BOTH]
_ROWS_HZ_CXP: List[tuple[int, float]] = [(r, c) for r, _u, c in _ROWS_HZ_BOTH]

# Sorted once: readout_hz() is called on every panel update.
_SORTED = {USB: sorted(_ROWS_HZ_USB), CXP: sorted(_ROWS_HZ_CXP)}


def _label(rows: int, hz_usb: float, hz_cxp: float) -> str:
    dims = f"{SENSOR_W}×{rows}"
    tag = "Full Frame " + f"({dims})" if rows == SENSOR_H else dims
    # Show both, so the label never silently lies about which link is live.
    return f"{tag} · {hz_usb:g} USB / {hz_cxp:g} CXP Hz"


# Smallest band the UI offers as a real preset; 8/4 rows stay table-only
# (too thin an FOV to pick, kept for readout_hz()'s binned-ROI interpolation).
# 2026-09-11 rig measurement: binning does NOT speed up THIS camera's own
# readout (512 rows @ bin 2x2 still reads out at the 512-row rate) — only
# row count does. So 256/128 are real presets, not just a smaller-frame
# knob; a genuinely faster capture needs fewer physical rows, not binning.
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

# modes.json's friendly spelling of DEFAULT_PRESET (main.py's Mode dropdown,
# both applying and capturing a mode) — readable in a hand-edited file, and
# immune to a sensor swap changing what the literal key spells out. The two
# directions live together so nothing else has to know which preset key
# happens to be "full" right now.
PRESET_ALIAS_FULL = "full"


def resolve_preset_key(key: str) -> str:
    """A modes.json preset value -> an actual PRESET_KEYS entry."""
    return DEFAULT_PRESET if key == PRESET_ALIAS_FULL else key


def preset_alias(key: str) -> str:
    """The inverse of `resolve_preset_key` — what to write into modes.json
    for a captured preset key, preferring the friendly alias when it applies."""
    return PRESET_ALIAS_FULL if key == DEFAULT_PRESET else key


def readout_hz(rows: int, binning: int = 1, link: str = DEFAULT_LINK) -> float:
    """Datasheet 16-bit readout ceiling for an ROI of `rows` rows on `link`.

    Hz ≈ const / rows over most of the range (2368·15.7 ≈ 1024·36.4 ≈ 37 000),
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
# The ~510 figure measured 2026-08-27 was the SAVE DRIVE, not the writer or
# the GIL: acqapp_local.json's saving.folder was E: (SATA MX500, ~550 MB/s
# cap). A zero-Qt harness (worker+Recorder+writer, no QApplication, no
# preview) got 472 MB/s / 29% kept on E: — reproducing the historical number
# with no GUI in the loop at all — and 1533 MB/s / 100% kept on D: (NVMe),
# the drive the save folder now points at. Set conservatively BELOW that
# measured 1533 pending one confirmation run through the real app (GUI, live
# preview, D:) — re-measure and raise this once that run is clean.
WRITER_MBPS: float = 1300.0

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

    # ── preview ── cosmetic only, no effect on acquisition. Carried here
    # anyway because AcqConfig is what this panel already persists and
    # restores — a display preference nobody wants to redo every launch, the
    # same call made for PupilSettings.cr_show_mask.
    show_lut:     bool = True     # the histogram/contrast bar beside the image
    # True (the long-standing behaviour) recomputes levels from a 1st/99th
    # percentile of each frame every LEVELS_EVERY ticks; False leaves them
    # exactly where the operator dragged the LUT's handles.
    auto_levels:  bool = True
    # How many of the most recent PREVIEW frames to average before display.
    # 1 = off. Cosmetic only: the recorded file still gets every raw frame,
    # since this only touches the downsampled copy update_display() draws.
    preview_avg:  int  = 1
    # On by default (new device — nothing already in use changes for the
    # pupil-cam LED, whose own flag defaults off): fire the primary LED from
    # the same start()/stop() Live/Record already call, rather than the
    # operator remembering a separate switch every time.
    led_follow_live: bool = True

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
    # Hz = min(readout ceiling, 1/exposure). Both are surfaced so the UI can
    # say *which* binds: an over-long exposure silently throws away the preset.

    @property
    def readout_hz(self) -> float:
        """Sensor/link ceiling for this ROI + binning (datasheet estimate)."""
        return readout_hz(self.preset.vsize, self.binning, self.link)

    @property
    def exposure_hz(self) -> float:
        """Ceiling imposed by the exposure time alone."""
        return 1e6 / max(self.exposure_us, 1e-6)

    @property
    def expected_hz(self) -> float:
        return min(self.readout_hz, self.exposure_hz)

    @property
    def exposure_limited(self) -> bool:
        """True when exposure — not readout — is what caps the frame rate."""
        return self.exposure_hz < self.readout_hz

    @property
    def max_exposure_us(self) -> float:
        """Longest exposure that still reaches the readout ceiling."""
        return 1e6 / max(self.readout_hz, 1e-9)
