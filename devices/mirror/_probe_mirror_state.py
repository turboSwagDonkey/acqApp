"""
Read chip 7 on the MCM6101 stage controller -- ThorImage's PMT/camera
mirror switch -- using the actual mirror-state protocol, one snapshot at a
time.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_probe_mirror_state.py [COM54]

Replaces `_read_mirror_axis.py` (deleted): that script queried
MOT_REQ_STATUSUPDATE, the stepper-axis status message -- chip 7 is a
Slider_IO_type (LightPath) card, not a stepper, and never answers it (see
PLAN.md S6/S7 2026-09-10 (bn)). The real messages, recovered from
ThorImageLS's own ThorMCM6000 driver source (APT.h/APT.cpp,
MCM6000.cpp: MoveMirror/GetStatusAllBoards): MGMSG_MCM_REQ_MIRROR_STATE
(0x4088) per channel, answered by MGMSG_MCM_GET_MIRROR_STATE (0x4089).

ThorImage holds COM54 exclusively for as long as it's open, so this script
and ThorImage can never have the port at once -- run it only when ThorImage
is closed.

Read-only -- sends only REQ_MIRROR_STATE, never SET_MIRROR_STATE. One
snapshot and exit.

**Why this script needs to be run once before any auto-actuation is wired
up**: ThorImage's own source comments "New MCM6000 cards reverse camera
lightpath positions" and applies a flip to the CAMERA channel (not GR) at
its own API layer. Which raw value (MIRROR_OUT=0 vs MIRROR_IN=1) means
"routed to camera" on *this* card's revision is unconfirmed. To find out:
  1. Open ThorImage, set the light path to Camera (what acqApp calls the
     safe/epi default), close ThorImage.
  2. Run this script -- note the GR and CAMERA channel raw states.
  3. In ThorImage, switch to the PMT/scanning path, close ThorImage.
  4. Run this script again -- compare. Whichever raw values corresponded to
     step 2 is “camera routed” on this card.
"""
from __future__ import annotations
import sys

CHIP = 7
AXIS = CHIP - 1        # chip N = axis N-1 on this rig -- see driver.py


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
