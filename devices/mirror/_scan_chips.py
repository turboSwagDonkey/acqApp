"""
Which axes on this rig's MCM6101 actually answer a status request?

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_scan_chips.py [COM54]

`driver.py`'s own `detect_axes()` only checks axes 0-5 and stops after two
consecutive misses -- not enough to find chip 7 (axis 6, by this rig's
chip-N=axis-N-1 convention) if a slot in between it and the stage's axes
0-2 doesn't answer. This scans 0-9 unconditionally, with a longer wait than
the default, and reports each axis instead of guessing at one.

Read-only -- get_status only, no move command.
"""
from __future__ import annotations
import sys

MAX_AXIS = 10
WAIT_S = 1.0


def main() -> int:
    from acqApp.devices.stage.driver import MCM6101, MCM6101Error

    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} "
              f"fw={info.firmware}")
        for a in range(MAX_AXIS):
            try:
                s = dev.get_status(a, wait=WAIT_S)
                flags = [n for n, on in (
                    ("EN", s.enabled), ("HOMED", s.homed),
                    ("MOVING", s.moving), ("FWD_LIM", s.at_fwd_limit),
                    ("REV_LIM", s.at_rev_limit)) if on]
                print(f"axis {a} (chip {a + 1}): pos={s.position:>10}  "
                      f"bits=0x{s.status_bits:08X}  [{' '.join(flags)}]")
            except MCM6101Error as e:
                print(f"axis {a} (chip {a + 1}): no reply ({e})")
        return 0


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    raise SystemExit(main())
