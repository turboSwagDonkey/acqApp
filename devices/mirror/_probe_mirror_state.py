"""
One read-only snapshot of chip 7 on the MCM6101 (ThorImage's PMT/camera
mirror switch): sends only REQ_MIRROR_STATE, never SET.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_probe_mirror_state.py [COM54]

Chip 7 is a Slider_IO_type (LightPath) card, not a stepper, so it never
answers MOT_REQ_STATUSUPDATE (PLAN.md S6/S7 2026-09-10 (bn)). It answers
MGMSG_MCM_REQ_MIRROR_STATE (0x4088) per channel with
MGMSG_MCM_GET_MIRROR_STATE (0x4089), per ThorImageLS's ThorMCM6000 source
(APT.h/APT.cpp, MCM6000.cpp: MoveMirror/GetStatusAllBoards).

ThorImage holds COM54 exclusively while open: run this only with it closed.

ThorImage's source flips the CAMERA channel for "New MCM6000 cards", so
which raw value means "camera" depends on the card. To check (done
2026-09-11, see startup.py):
  1. In ThorImage set the light path to Camera, then close it.
  2. Run this; note the GR and CAMERA raw states.
  3. In ThorImage switch to PMT/scanning, then close it.
  4. Run this again. Step 2's raw values are "camera routed".
"""
from __future__ import annotations
import sys

CHIP = 7
AXIS = CHIP - 1        # chips are 1-indexed, axes 0-indexed (driver.py)


def main() -> int:
    from acqApp.devices.stage.driver import (
        MCM6101, MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA,
        MIRROR_OUT, MIRROR_IN, MIRROR_UNKNOWN,
    )

    names = {MIRROR_OUT: "OUT", MIRROR_IN: "IN", MIRROR_UNKNOWN: "UNKNOWN"}
    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} "
              f"fw={info.firmware}")
        for label, chan in (("GR", MIRROR_CHAN_GR), ("CAMERA", MIRROR_CHAN_CAMERA)):
            state = dev.get_mirror_state(AXIS, chan)
            print(f"chip {CHIP} (axis {AXIS}) channel {label} ({chan}): "
                  f"{names.get(state, state)}")
        print("Set the light path in ThorImage, close it, and run this "
              "again to compare -- see this script's module docstring.")
        return 0


if __name__ == "__main__":
    # Before the first print: a UnicodeEncodeError from a diagnostic print
    # reads as a device failure (acqApp/console.py).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    raise SystemExit(main())
