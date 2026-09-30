"""Vialux ALP-4.2 (1024x768) device layer, via ALP4lib and the vendor's
high-speed API. No Qt, so `build_frame` is testable without the device.

`build_frame` ports `dmdGUI_project`'s `dmdCommandLine.buildFrame`, which the
optics are aligned with; the two must stay identical.

Only one process can hold the ALP (over USB); `open()` raises if the
standalone app has it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

# Vendor cap between pictures (AlpSeqTiming); clamped here, not truncated.
MAX_PICTURE_US = 10_000_000

_SIBLING_API = Path("ALP-4.2") / "ALP-4.2 high-speed API"
# The standalone app's libDir and aligned scale/rotation.
_SIBLING_CONFIG = Path("dmdGUI_project") / "dmd_config.json"

_ROOT = Path(__file__).resolve().parents[3]        # …/python


def sibling_config() -> dict[str, Any]:
    """The standalone DMD app's `dmd_config.json`, or {}."""
    p = _ROOT / _SIBLING_CONFIG
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_lib_dir(explicit: str = "") -> tuple[str | None, str]:
    """-> (API path or None, where it came from). None = ALP4lib's registry
    lookup, right for a normal install."""
    if explicit:
        return explicit, "panel setting"
    env = os.environ.get("ACQAPP_ALP_DIR", "")
    if env:
        return env, "ACQAPP_ALP_DIR"
    from_cfg = sibling_config().get("libDir")
    if from_cfg and Path(from_cfg).is_dir():
        return str(from_cfg), "dmdGUI_project/dmd_config.json"
    sib = _ROOT / _SIBLING_API
    if sib.is_dir():
        return str(sib), "sibling ALP-4.2 folder"
    return None, "ALP4lib registry lookup"


# ══════════════════════════════════════════════════════════════════════════════
#  Image → binary frame
# ══════════════════════════════════════════════════════════════════════════════

def build_frame(image: Path | np.ndarray, width: int, height: int, *,
                scale_pct: float = 100.0, rotation_deg: float = 0.0,
                offset_x: float = 0.0, offset_y: float = 0.0,
                invert: bool = False, fit: bool = False) -> np.ndarray:
    """A pattern as a (height, width) uint8 {0,255} frame: binarize, invert,
    scale, rotate, place — the standalone app's order and conventions:

      * threshold >127 before AND after interpolation (mirrors have no grey);
      * rotation clockwise-positive (Qt's; PIL's is the opposite);
      * offsets move the pattern centre off the DMD's, in device px;
      * `fit` overrides scale, rotation and offset — never for calibration.
    """
    from PIL import Image

    if isinstance(image, np.ndarray):
        arr = image
    else:
        arr = np.asarray(Image.open(image).convert("L"))
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D pattern, got shape {arr.shape}")

    arr = np.where(arr > 127, 255, 0).astype(np.uint8)
    if invert:
        arr = (~arr).astype(np.uint8)

    proc = Image.fromarray(arr, mode="L")
    src_w, src_h = proc.size
    if src_w == 0 or src_h == 0:
        raise ValueError("pattern has a zero dimension")

    if fit:
        scale_pct = 100.0 * min(width / src_w, height / src_h)
        rotation_deg = 0.0
        offset_x = offset_y = 0.0

    s = scale_pct / 100.0
    new_w, new_h = max(1, int(round(src_w * s))), max(1, int(round(src_h * s)))
    if (new_w, new_h) != (src_w, src_h):
        proc = proc.resize((new_w, new_h), Image.BILINEAR)

    if rotation_deg % 360.0 != 0.0:
        proc = proc.rotate(-rotation_deg, resample=Image.BILINEAR,
                           expand=True, fillcolor=0)

    pw, ph = proc.size
    canvas = Image.new("L", (width, height), color=0)
    canvas.paste(proc, (int(round(width / 2.0 + offset_x - pw / 2.0)),
                        int(round(height / 2.0 + offset_y - ph / 2.0))))
    out = np.asarray(canvas)
    return np.ascontiguousarray(np.where(out > 127, 255, 0).astype(np.uint8))


# ══════════════════════════════════════════════════════════════════════════════
#  The device
# ══════════════════════════════════════════════════════════════════════════════

class AlpDevice:
    """Per projection: SeqAlloc(1, 1 bit) -> SeqPut -> BIN_UNINTERRUPTED (so a
    held pattern holds between pictures) -> SetTiming -> Run. `halt()` is
    Halt + FreeSeq, so the next project starts clean."""

    def __init__(self, lib_dir: str | None = None, version: str = "4.2") -> None:
        self._lib_dir = lib_dir
        self._version = version
        self._dev = None
        self._seq = False               # a sequence is allocated
        self.width = 0
        self.height = 0

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def open(self) -> tuple[int, int]:
        """-> (width, height). Raises if the ALP isn't free."""
        from ALP4 import ALP4
        dev = ALP4(version=self._version, libDir=self._lib_dir)
        dev.Initialize()
        self._dev = dev
        self.width, self.height = int(dev.nSizeX), int(dev.nSizeY)
        return self.width, self.height

    def project(self, frame: np.ndarray, *, illumination_us: int | None = None,
                loop: bool = True, repeats: int = 0) -> None:
        """Upload one frame and display it. None timing = device default (a
        held pattern); `repeats` > 0 shows it that many times."""
        from ALP4 import ALP_BIN_MODE, ALP_BIN_UNINTERRUPTED, ALP_SEQ_REPEAT
        if self._dev is None:
            raise RuntimeError("ALP not open")
        if frame.shape != (self.height, self.width):
            raise ValueError(f"frame is {frame.shape}, device is "
                             f"{(self.height, self.width)}")

        self.halt()                     # never upload into a running sequence
        dev = self._dev
        dev.SeqAlloc(nbImg=1, bitDepth=1)
        self._seq = True
        dev.SeqPut(imgData=np.ascontiguousarray(frame, dtype=np.uint8))
        dev.SeqControl(ALP_BIN_MODE, ALP_BIN_UNINTERRUPTED)
        if illumination_us is None:
            dev.SetTiming()
        else:
            dev.SetTiming(illuminationTime=int(illumination_us))
        if repeats > 0:
            dev.SeqControl(ALP_SEQ_REPEAT, int(repeats))
        dev.Run(loop=loop)

    def halt(self) -> None:
        """Safe at any time. Steps guarded separately: an unplugged device
        fails Halt but must still get FreeSeq."""
        if self._dev is None or not self._seq:
            return
        for step in ("Halt", "FreeSeq"):
            try:
                getattr(self._dev, step)()
            except Exception as e:                    # noqa: BLE001
                print(f"[DMD] {step}: {e}")
        self._seq = False

    def close(self) -> None:
        self.halt()
        if self._dev is not None:
            try:
                self._dev.Free()
            except Exception as e:                    # noqa: BLE001
                print(f"[DMD] Free: {e}")
            self._dev = None
