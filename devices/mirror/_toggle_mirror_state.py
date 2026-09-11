"""
Toggle chip 7 (PMT/camera light path) on the MCM6101, then return it to the
CAMERA/epi default -- a one-off manual confirmation that SET_MIRROR_STATE
actually moves this hardware, not just that ThorImage's own switch does.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_toggle_mirror_state.py [COM54]

Only run this with ThorImage CLOSED (it holds COM54). Purely mechanical
light-path redirect -- no laser/light source is switched on by this command
itself. Always leaves the switch on the CAMERA/epi default when done
(`devices/mirror/startup.py`'s target), even if interrupted between the two
SET calls the flip needs.
"""
from __future__ import annotations
import sys
import time

CHIP = 7
AXIS = CHIP - 1


def main() -> int:
    from acqApp.devices.stage.driver import (
        MCM6101, MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA, MIRROR_OUT, MIRROR_IN,
    )
    names = {MIRROR_OUT: "OUT", MIRROR_IN: "IN"}
    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} fw={info.firmware}")

        gr0 = dev.get_mirror_state(AXIS, MIRROR_CHAN_GR)
        cam0 = dev.get_mirror_state(AXIS, MIRROR_CHAN_CAMERA)
        print(f"Before: GR={names.get(gr0, gr0)}  CAMERA={names.get(cam0, cam0)}")

        target = MIRROR_IN if gr0 == MIRROR_OUT else MIRROR_OUT
        try:
            print(f"Flipping GR and CAMERA to {names[target]} -- listen for movement...")
            dev.set_mirror_state(AXIS, MIRROR_CHAN_GR, target)
            dev.set_mirror_state(AXIS, MIRROR_CHAN_CAMERA, target)
            time.sleep(0.5)

            gr1 = dev.get_mirror_state(AXIS, MIRROR_CHAN_GR)
            cam1 = dev.get_mirror_state(AXIS, MIRROR_CHAN_CAMERA)
            print(f"After flip: GR={names.get(gr1, gr1)}  CAMERA={names.get(cam1, cam1)}")
        finally:
            print("Returning to the CAMERA/epi default (MIRROR_OUT on both)...")
            dev.set_mirror_state(AXIS, MIRROR_CHAN_GR, MIRROR_OUT)
            dev.set_mirror_state(AXIS, MIRROR_CHAN_CAMERA, MIRROR_OUT)
            time.sleep(0.5)
            gr2 = dev.get_mirror_state(AXIS, MIRROR_CHAN_GR)
            cam2 = dev.get_mirror_state(AXIS, MIRROR_CHAN_CAMERA)
            print(f"Restored: GR={names.get(gr2, gr2)}  CAMERA={names.get(cam2, cam2)}")
    return 0


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()
    raise SystemExit(main())
