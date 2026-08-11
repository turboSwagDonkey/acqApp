r"""
Wheel raw-voltage capture — diagnostic for the distance/speed derivation.

Records the RAW analog voltage on the encoder channel at a high sample rate while
you spin the wheel, so we can see what the signal actually looks like (smooth
ramp? sawtooth wrap? fast pulse train? tiny swing?) and fix the conversion.

Run it, then spin the wheel a few full turns — forward and back — during the
capture. It prints live min/max so you can confirm the number is moving, saves a
CSV, and prints a compact summary you can paste back.

  ..\..\.venv\Scripts\python.exe wheel\capture_raw.py
  ..\..\.venv\Scripts\python.exe wheel\capture_raw.py --seconds 30 --rate 2000
  ..\..\.venv\Scripts\python.exe wheel\capture_raw.py --chan Dev3/ai2 --out spin.csv

Needs NI-DAQmx + nidaqmx. Ctrl+C stops early and still saves what was captured.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture raw wheel-encoder voltage.")
    ap.add_argument("--chan", default="Dev3/ai2", help="analog input channel")
    ap.add_argument("--rate", type=float, default=1000.0,
                    help="sample rate, Hz (hardware clocked)")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="capture duration, s (Ctrl+C stops early)")
    ap.add_argument("--out", default="wheel_raw.csv", help="output CSV path")
    args = ap.parse_args()

    import nidaqmx
    from nidaqmx.constants import (
        TerminalConfiguration, AcquisitionType, READ_ALL_AVAILABLE,
    )

    print(f"Channel {args.chan}  |  {args.rate:.0f} Hz  |  {args.seconds:.0f} s")
    print("Spin the wheel a few full turns (forward AND backward). Ctrl+C to stop.\n")

    chunks: list[np.ndarray] = []
    vmin, vmax = np.inf, -np.inf
    t0 = time.perf_counter()
    last_report = t0

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(
            args.chan,
            terminal_config=TerminalConfiguration.RSE,
            min_val=-10.0, max_val=10.0,
        )
        # Hardware-clocked continuous acquisition — clean, evenly spaced samples
        # (unlike the app's 50 Hz software loop, which can alias a fast signal).
        task.timing.cfg_samp_clk_timing(
            args.rate, sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=int(args.rate))
        task.start()
        try:
            while (time.perf_counter() - t0) < args.seconds:
                data = task.read(number_of_samples_per_channel=READ_ALL_AVAILABLE)
                if data:
                    arr = np.atleast_1d(np.asarray(data, dtype=np.float64))
                    chunks.append(arr)
                    vmin = min(vmin, float(arr.min()))
                    vmax = max(vmax, float(arr.max()))
                now = time.perf_counter()
                if now - last_report >= 0.5:
                    last_report = now
                    cur = float(chunks[-1][-1]) if chunks else float("nan")
                    print(f"  t={now - t0:5.1f}s  now={cur:+7.4f} V   "
                          f"min={vmin:+7.4f}  max={vmax:+7.4f}  "
                          f"span={vmax - vmin:6.4f}")
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\n(stopped early)")
        finally:
            task.stop()

    if not chunks:
        print("No samples captured.")
        return

    v = np.concatenate(chunks)
    n = v.size
    t = np.arange(n) / args.rate
    out = Path(args.out)
    np.savetxt(out, np.column_stack([t, v]), delimiter=",",
               header="t_s,voltage", comments="")

    dv = np.diff(v)
    big = int(np.count_nonzero(np.abs(dv) > 0.5))     # sample-to-sample >0.5 V
    print("\n─── summary ───────────────────────────────────────────────")
    print(f"saved         {out.resolve()}")
    print(f"samples       {n}  ({n / args.rate:.1f} s @ {args.rate:.0f} Hz)")
    print(f"voltage       min {v.min():+.4f}  max {v.max():+.4f}  "
          f"span {v.max() - v.min():.4f} V")
    print(f"mean / std    {v.mean():+.4f} / {v.std():.4f} V")
    print(f"step |Δ|>0.5V  {big} sample(s)   (wraps or glitches show up here)")

    # Coarse trace: mean voltage per 0.5 s bucket, so the shape (ramp? sawtooth?
    # steps? flat + spikes?) is visible in the paste.
    bucket = max(1, int(args.rate * 0.5))
    nb = n // bucket
    if nb:
        means = v[:nb * bucket].reshape(nb, bucket).mean(axis=1)
        print("\nmean V per 0.5 s:")
        print("  " + "  ".join(f"{m:+.3f}" for m in means))
    print("────────────────────────────────────────────────────────────")
    print("Send me the summary above (and the CSV if you can) and I'll fix the "
          "conversion.")


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
