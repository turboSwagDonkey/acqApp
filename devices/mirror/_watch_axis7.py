"""
Watch chip 7 on the MCM6101 stage controller -- ThorImage's PMT/camera
mirror switch.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_watch_axis7.py [COM54]

Operator-confirmed (PLAN.md S6, 2026-09-10): the mirror switch is not a NI
DAQ line -- it answers as axis (chip) 7 on the same MCM6101 stage controller
devices/stage/driver.py already talks to. Normal stage startup never sees it:
devices/stage/driver.py's own detect_axes() only probes axes 0-5 by default,
and the XY(-Z) stage itself only occupies a couple of those.

Read-only -- opens a status poll, never a move command, so this is safe to
run mid-session even if the stage is in use elsewhere (though not the same
serial port at the same time). Prints only when axis 7's position or status
bits change; flip the switch in ThorImage to see which one moves and what
value it settles on, so the real Mirror-tab poll knows what to watch for.
Ctrl+C to stop.
"""
from __future__ import annotations
import sys
import time

AXIS = 7
POLL_HZ = 10.0


def main() -> int:
    from acqApp.devices.stage.driver import MCM6101

    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} "
              f"fw={info.firmware}")
        print(f"[mirror] watching axis {AXIS} at {POLL_HZ:g} Hz -- flip the "
              f"ThorImage mirror switch now. Ctrl+C to stop.")

        prev: tuple[int, int] | None = None
        t0 = time.perf_counter()
        period = 1.0 / POLL_HZ
        try:
            while True:
                s = dev.get_status(AXIS)
                cur = (s.position, s.status_bits)
                if cur != prev:
                    elapsed = time.perf_counter() - t0
                    flags = [n for n, on in (
                        ("EN", s.enabled), ("HOMED", s.homed),
                        ("MOVING", s.moving), ("FWD_LIM", s.at_fwd_limit),
                        ("REV_LIM", s.at_rev_limit)) if on]
                    tag = "initial" if prev is None else "CHANGED"
                    print(f"t={elapsed:7.2f}s  {tag}: pos={s.position:>10}  "
                          f"bits=0x{s.status_bits:08X}  [{' '.join(flags)}]")
                    prev = cur
                time.sleep(period)
        except KeyboardInterrupt:
            print("\n[mirror] stopped.")
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
