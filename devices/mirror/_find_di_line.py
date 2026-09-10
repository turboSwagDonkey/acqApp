"""
Which DAQ digital line reacts to ThorImage's PMT/camera mirror switch?

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\mirror\\_find_di_line.py

Read-only — opens only digital INPUT channels, never actuates anything, so
this is safe to run mid-session. Polls the static digital-input ports
(port1, port2; port0 is already taken by the puffer/LED digital outputs, see
devices/puffer/control.py) and prints only when a line's live state changes.

Run this, then flip the mirror switch in ThorImage: whichever line prints is
the one the PMT/camera Mirror tab's DAQ auto-detect should watch
(PLAN.md §6 "PMT/camera mirror" — the DI line is unknown; this is how to
find it). Ctrl+C to stop.
"""
from __future__ import annotations
import time

DEVICE = "Dev3"
PORTS = ("port1", "port2")
POLL_HZ = 20.0


def main() -> int:
    import nidaqmx

    chans = [f"{DEVICE}/{p}/line{n}" for p in PORTS for n in range(8)]
    with nidaqmx.Task() as task:
        for c in chans:
            task.di_channels.add_di_chan(c)
        try:
            task.start()
        except Exception as e:
            print(f"[mirror] couldn't open {DEVICE} {PORTS}: {e}")
            return 1

        print(f"[mirror] watching {len(chans)} lines on {DEVICE} {PORTS} "
              f"at {POLL_HZ:g} Hz — flip the ThorImage mirror switch now. "
              f"Ctrl+C to stop.")
        prev: list[bool] | None = None
        t0 = time.perf_counter()
        period = 1.0 / POLL_HZ
        try:
            while True:
                vals = task.read()
                if vals != prev:
                    elapsed = time.perf_counter() - t0
                    if prev is None:
                        print(f"t={elapsed:7.2f}s  initial: "
                              f"{dict(zip(chans, vals))}")
                    else:
                        changed = [chans[i] for i in range(len(chans))
                                   if vals[i] != prev[i]]
                        print(f"t={elapsed:7.2f}s  CHANGED: {changed}  -> "
                              f"{dict(zip(chans, vals))}")
                    prev = vals
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
