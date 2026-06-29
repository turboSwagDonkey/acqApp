"""
Hamamatsu ORCA-Fire acquisition presets and configuration.
Sensor: 4432 × 2368 pixels.

All subarray positions/sizes are multiples of 4 as required by DCAM-API.
Square center crops are included for ROI-based imaging.
"""
from __future__ import annotations
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


def _centered(label: str, w: int, h: int) -> ResolutionPreset:
    """Center a w×h ROI on the sensor. w and h must be multiples of 4."""
    hpos = ((SENSOR_W - w) // 2 // 4) * 4
    vpos = ((SENSOR_H - h) // 2 // 4) * 4
    return ResolutionPreset(label, w, h, hpos, vpos)


PRESETS: Dict[str, ResolutionPreset] = {
    "Full Frame": ResolutionPreset(
        "Full Frame (4432×2368)", SENSOR_W, SENSOR_H, 0, 0,
    ),
    "4096×2048":  _centered("4096×2048 (center wide)",   4096, 2048),
    "2048×2048":  _centered("2048×2048 (center square)", 2048, 2048),
    "1024×1024":  _centered("1024×1024 (center square)", 1024, 1024),
    "512×512":    _centered("512×512 (center square)",   512,  512),
}

PRESET_KEYS: List[str] = list(PRESETS.keys())
DEFAULT_PRESET: str = "Full Frame"

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

    @property
    def preset(self) -> ResolutionPreset:
        return PRESETS[self.preset_key]

    @property
    def frame_shape(self) -> tuple[int, int]:
        """Output shape (rows, cols) after binning."""
        rows, cols = self.preset.shape
        return (rows // self.binning, cols // self.binning)
