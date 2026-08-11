r"""
Analyse a wheel_raw.csv capture — turn the raw position voltage into revolutions
and distance, so we can confirm the conversion before trusting it in the app.

The encoder is a single-turn sawtooth: voltage ramps 0..Vfs over one revolution,
then wraps. This unwraps that sawtooth (np.unwrap), integrates it, and reports
revolutions + distance — at the captured rate and again decimated to the app's
50 Hz, so aliasing (if any) is visible.

  ..\..\.venv\Scripts\python.exe wheel\analyze_raw.py wheel_raw.csv
  ..\..\.venv\Scripts\python.exe wheel\analyze_raw.py wheel_raw.csv --dia 150 --vfs 5.0

--vfs (volts per revolution / full-scale) defaults to the measured signal range.
"""
from __future__ import annotations
import argparse
import sys

import numpy as np


def revolutions(v: np.ndarray, vfs: float) -> np.ndarray:
    """Cumulative signed revolutions from a sawtooth position voltage."""
    phase = v / vfs * 2 * np.pi          # volts → radians (one turn = 2π)
    return np.unwrap(phase) / (2 * np.pi)


def report(name: str, v: np.ndarray, rate: float, vfs: float, dia: float,
           deadband_vel: float, is_app: bool = False) -> None:
    rev = revolutions(v, vfs)
    net = rev[-1] - rev[0]                # net rotation (forward - backward)
    step = np.diff(rev)
    # Mirror the app worker: drop >0.5 rev/sample glitches, and steps whose speed
    # is below the velocity deadband. So this predicts what the app will report.
    speed = np.abs(step) * rate          # rev/s per sample
    good = step[(np.abs(step) <= 0.5) & (speed > deadband_vel)]
    path_db = float(np.sum(np.abs(good)))
    circ = np.pi * dia / 1000.0          # m per revolution
    tag = "  <- what the app records (net_forward)" if is_app else ""
    print(f"[{name}]  {v.size} samples @ {rate:.0f} Hz")
    print(f"    net rotation    {net:+.2f} rev   ({net * circ:+.2f} m){tag}")
    print(f"    path (both dirs){path_db:.2f} rev   ({path_db * circ:.2f} m)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyse a raw wheel capture.")
    ap.add_argument("csv", help="wheel_raw.csv from capture_raw.py")
    ap.add_argument("--dia", type=float, default=150.0, help="wheel diameter, mm")
    ap.add_argument("--vfs", type=float, default=None,
                    help="volts per revolution (default: measured range)")
    ap.add_argument("--app-rate", type=float, default=120.0,
                    help="app sample rate to simulate by decimation")
    args = ap.parse_args()

    data = np.loadtxt(args.csv, delimiter=",", skiprows=1)
    if data.ndim != 2 or data.shape[0] < 3:
        print("CSV has too few rows.")
        return
    t, v = data[:, 0], data[:, 1]
    dt = float(np.median(np.diff(t)))
    rate = 1.0 / dt if dt > 0 else 1000.0

    lo, hi = np.percentile(v, 1), np.percentile(v, 99)
    # Full-scale volts per revolution = the sawtooth's peak-to-peak span.
    vfs = args.vfs if args.vfs is not None else float(v.max() - v.min())
    deadband_vel = 0.05                  # rev/s; matches _EncoderBase._DEADBAND_REV_S

    print("=== wheel capture analysis ==================================")
    print(f"file            {args.csv}")
    print(f"rate (measured) {rate:.0f} Hz")
    print(f"voltage range   {v.min():+.3f} .. {v.max():+.3f} V  "
          f"(p1..p99 {lo:+.3f}..{hi:+.3f})")
    print(f"-> volts_per_rev {vfs:.3f} V   (set the app's 'V / rev' to this)")
    print(f"wheel diameter  {args.dia:.0f} mm  ->  {np.pi * args.dia / 1000:.3f} m/rev")
    print(f"noise deadband  {deadband_vel:.3f} rev/s")
    print()
    report("full rate (reference)", v, rate, vfs, args.dia, deadband_vel)

    step = max(1, int(round(rate / args.app_rate)))
    report(f"decimated ~{args.app_rate:.0f}Hz (app)", v[::step], rate / step,
           vfs, args.dia, deadband_vel, is_app=True)
    print("=============================================================")
    print("Set the app's V/rev to the value above. 'net rotation' is what the app "
          "records (forward-back); it should match how far you *net* turned the "
          "wheel. Big net differences between full-rate and ~50Hz mean the spin "
          "aliased (>0.5 rev/sample) - the app samples at 120Hz to avoid that.")


if __name__ == "__main__":
    # Make the console unable to raise on this script's own output before it
    # prints anything (see acqApp/console.py -- a UnicodeEncodeError from a
    # diagnostic print is otherwise indistinguishable from a device failure).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    sys.exit(main())
