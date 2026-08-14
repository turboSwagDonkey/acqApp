"""Vialux ALP device layer — no Qt, no app state.

The rig's DMD is a **1024x768 Vialux ALP-4.2** driven through `ALP4lib` over the
vendor's high-speed API (a separate non-pip install, like NI-DAQmx and DCAM).
All the hardware knowledge lives here: where that API is, image → binary frame,
and the open/project/halt/close lifecycle.

Separate from `control.py` so the geometry is testable without Qt or the device
— `build_frame` is pure numpy/PIL, and is where a misplaced stimulus comes from.

The maths is a port of `dmdGUI_project`'s `dmdCommandLine.buildFrame`, the path
the optics are aligned with; keeping the two identical is the point, so a
pattern positioned there lands in the same place here.

**One process at a time** — whoever opened the ALP holds it over USB, so acqApp
and `dmdGUI_project` cannot both connect. `open()` raises rather than waiting.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

# The vendor caps the time between pictures at 10 s (ALP-4 doc, AlpSeqTiming).
# The panel allows longer, so on-times are clamped here and reported rather than
# silently truncated by the driver.
MAX_PICTURE_US = 10_000_000

# A hint only — see resolve_lib_dir().
_SIBLING_API = Path("ALP-4.2") / "ALP-4.2 high-speed API"
# The standalone app's config already carries the operator's libDir and aligned
# scale/rotation — the arrangement the stage has with stage_control/.
_SIBLING_CONFIG = Path("dmdGUI_project") / "dmd_config.json"

_ROOT = Path(__file__).resolve().parents[2]        # …/python


def sibling_config() -> dict[str, Any]:
    """The standalone DMD app's `dmd_config.json`, or {} if it isn't there."""
    p = _ROOT / _SIBLING_CONFIG
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_lib_dir(explicit: str = "") -> tuple[str | None, str]:
    """Find the ALP-4.2 high-speed API → (path or None, where it came from).

    `None` means "let ALP4lib look in the registry", right for a normal vendor
    install. The earlier sources exist because this rig's copy sits beside the
    repo, and the standalone app already records where.
    """
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
    """Render a pattern into the (height, width) uint8 {0,255} frame to project.

    Order — binarize, invert, scale, rotate, place — is the standalone app's,
    and so is every convention in it:

      * the threshold is >127, applied to the source AND again after the
        interpolating scale/rotate, so the frame really is binary (the mirrors
        have no grey);
      * rotation is **clockwise-positive**, matching Qt (and so the standalone
        GUI's dial), which is the opposite of PIL's;
      * `offset_x/offset_y` move the pattern's centre off the DMD's centre in
        device pixels, so 0,0 is centred whatever the image size;
      * `fit` scales to the largest size that fits and re-centres, overriding
        scale, rotation and offset.

    Anything outside the panel is cropped: the DMD cannot project it.
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
        # PIL rotates counter-clockwise; Qt (and the standalone GUI) clockwise.
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
    """Open / upload / project / halt / close, and nothing else.

    Sequence lifecycle per projection, as in the standalone app:
        SeqAlloc(1 image, 1 bit) -> SeqPut(frame) -> SeqControl(BIN_UNINTERRUPTED)
        -> SetTiming(...) -> Run(loop)
    and `halt()` is Halt + FreeSeq, so the next `project()` starts from a clean
    sequence. ALP_BIN_UNINTERRUPTED is what makes a held pattern actually hold:
    the mirrors keep their state between pictures instead of blanking.
    """

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
        """Connect and return (width, height). Raises if the ALP is not free."""
        from ALP4 import ALP4
        dev = ALP4(version=self._version, libDir=self._lib_dir)
        dev.Initialize()
        self._dev = dev
        self.width, self.height = int(dev.nSizeX), int(dev.nSizeY)
        return self.width, self.height

    def project(self, frame: np.ndarray, *, illumination_us: int | None = None,
                loop: bool = True, repeats: int = 0) -> None:
        """Upload one frame and start displaying it.

        `illumination_us=None` leaves the timing at the device default, which is
        what a held pattern wants. `repeats` > 0 shows the picture that many
        times and stops; `loop` displays until `halt()`.
        """
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
        """Stop display and release the sequence. Safe to call at any time.

        Each step is guarded separately: a device unplugged mid-run fails the
        Halt but must still get the FreeSeq attempt, and neither failure should
        stop the caller from closing.
        """
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
