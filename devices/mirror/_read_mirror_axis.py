"""
Read chip 7 on the MCM6101 stage controller -- ThorImage's PMT/camera
mirror switch, one snapshot at a time.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_read_mirror_axis.py [COM54]

Operator-confirmed (PLAN.md S0/S6, 2026-09-10): on this rig's MCM6101, the
mirror switch answers as chip 7 (= axis 6 -- see AXIS below) on the same
MCM6101 the XY stage driver already talks to, but it is NOT something this
script can watch live: ThorImage holds COM54 for as long as it is open (to
command the switch at all), so this script and ThorImage can never have the
port at once. `driver.py`'s own `detect_axes()` never sees it either way --
it only probes axes 0-5 by default, one short of axis 6.

What DOES work: the switch is a physical position, so it holds whatever
ThorImage last left it in even after ThorImage closes. To find out what
chip 7 reports for each state:

    1. Close ThorImage.
    2. Run this script -- note the position/status bits (whatever state it
       was last left in).
    3. Open ThorImage, flip the switch to the OTHER state, close ThorImage.
    4. Run this script again -- compare against step 2.

Read-only -- opens a status read, never a move command. One snapshot and
exit, not a loop: there is nothing to watch change while connected.
"""
from __future__ import annotations
import sys

CHIP = 7
AXIS = CHIP - 1        # chip N = axis N-1 on this rig -- see module docstring


def main() -> int:
    from acqApp.devices.stage.driver import MCM6101

    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} "
              f"fw={info.firmware}")
        s = dev.get_status(AXIS)
        flags = [n for n, on in (
            ("EN", s.enabled), ("HOMED", s.homed),
            ("MOVING", s.moving), ("FWD_LIM", s.at_fwd_limit),
            ("REV_LIM", s.at_rev_limit)) if on]
        print(f"chip {CHIP} (axis {AXIS}):  pos={s.position:>10}  "
              f"bits=0x{s.status_bits:08X}  [{' '.join(flags)}]")
        print("Flip the switch in ThorImage, close it, and run this again "
              "to compare.")
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
