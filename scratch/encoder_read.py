r"""
Phase 0 hardware de-risk: rotary wheel encoder on NI PCIe-6363 (Dev3).

Reads the encoder as an analog voltage from ai2 (single-ended), matching
the MATLAB script exactly. Polls in a tight loop, timestamps every sample,
shows a live plot of position and speed, and saves to a .csv on exit.

Wiring: encoder signal -> Dev3/ai2 (single-ended / referenced to AIGND)

Run:
  ..\..\.venv\Scripts\python.exe encoder_read.py
  ..\..\.venv\Scripts\python.exe encoder_read.py --chan Dev3/ai2 --rate 50
  ..\..\.venv\Scripts\python.exe encoder_read.py --volts-per-rev 5.0 --wheel-diameter 15.0
"""

import argparse
import csv
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.animation as animation


# ---------------------------------------------------------------------------
# DAQ reader -- runs in a background thread
# ---------------------------------------------------------------------------

def daq_thread(chan, period, volts_per_rev, wheel_circumference_m, stop_event,
               time_buf, voltage_buf, dist_buf,
               time_data, abs_time_data, encoder_data, dist_data):
    """Poll the analog input and push results into shared deques."""
    try:
        import nidaqmx
        from nidaqmx.constants import TerminalConfiguration
    except Exception as e:
        raise SystemExit(
            f"Could not import nidaqmx: {e}\n"
            "Install the NI-DAQmx runtime driver (the one NI MAX uses)."
        )

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(
            chan,
            terminal_config=TerminalConfiguration.RSE,
            min_val=-10.0,
            max_val=10.0,
        )

        trial_start = time.perf_counter()
        last_v = None
        volt_offset = 0.0  # accumulates +volts_per_rev on each wrap

        while not stop_event.is_set():
            voltage: float = task.read()  # type: ignore[assignment]
            now = time.perf_counter()
            elapsed = now - trial_start
            abs_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            if last_v is not None:
                delta = voltage - last_v
                # wrap threshold: half a revolution worth of volts
                if delta < -(volts_per_rev / 2):
                    volt_offset += volts_per_rev   # forward wrap
                elif delta > (volts_per_rev / 2):
                    volt_offset -= volts_per_rev   # reverse wrap

            unwrapped = voltage + volt_offset
            dist_m = (unwrapped / volts_per_rev) * wheel_circumference_m

            # Rolling plot buffers
            time_buf.append(elapsed)
            voltage_buf.append(voltage)
            dist_buf.append(dist_m)

            # Full-session save buffers
            time_data.append(elapsed)
            abs_time_data.append(abs_now)
            encoder_data.append(voltage)
            dist_data.append(dist_m)

            last_v = voltage
            time.sleep(period)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="PCIe-6363 analog encoder live read")
    ap.add_argument("--chan", type=str, default="Dev3/ai2",
                    help="analog input channel (default Dev3/ai2)")
    ap.add_argument("--rate", type=float, default=50.0,
                    help="polling rate in Hz (default 50)")
    ap.add_argument("--window", type=float, default=10.0,
                    help="live plot time window in seconds (default 10)")
    ap.add_argument("--volts-per-rev", type=float, default=4.0,
                    help="voltage span for one full revolution (default 4.0)")
    ap.add_argument("--wheel-diameter", type=float, default=15.0,
                    help="wheel diameter in cm (default 15.0)")
    ap.add_argument("--no-save", action="store_true",
                    help="skip saving .csv on exit")
    args = ap.parse_args()

    wheel_circumference_m = math.pi * (args.wheel_diameter / 100.0)

    period = 1.0 / args.rate
    maxlen = int(args.window * args.rate * 2)  # keep 2x the window in the deque

    # Shared buffers (deque is thread-safe for append + read from two threads)
    time_buf    = deque(maxlen=maxlen)
    voltage_buf = deque(maxlen=maxlen)
    dist_buf    = deque(maxlen=maxlen)

    # Full-session storage (grows unbounded, saved on exit)
    time_data     = []
    abs_time_data = []
    encoder_data  = []
    dist_data     = []

    stop_event = threading.Event()

    reader = threading.Thread(
        target=daq_thread,
        args=(args.chan, period, args.volts_per_rev, wheel_circumference_m, stop_event,
              time_buf, voltage_buf, dist_buf,
              time_data, abs_time_data, encoder_data, dist_data),
        daemon=True,
    )

    print(f"Opening encoder on {args.chan}  rate={args.rate} Hz  window={args.window} s")
    if args.volts_per_rev:
        print(f"  volts-per-rev={args.volts_per_rev}  wheel-diameter={args.wheel_diameter} cm")
        print(f"  circumference={wheel_circumference_m*100:.2f} cm -> position in metres, speed in m/s")
    else:
        print("  No --volts-per-rev given -> position axis shows raw voltage")
    print("Close the plot window to stop.\n")

    reader.start()

    # --- Build the figure ---
    fig, ax_pos = plt.subplots(figsize=(9, 4))
    fig.suptitle("Encoder — live", fontsize=13)

    ax_pos.set_ylabel("Distance (m)")
    ax_pos.set_xlabel("Time (s)")
    ax_pos.grid(True, alpha=0.3)

    (line_pos,) = ax_pos.plot([], [], color="royalblue", linewidth=1.2)

    def update(_):
        if len(time_buf) < 2:
            return (line_pos,)

        t   = list(time_buf)
        pos = list(dist_buf)

        # Trim to rolling window
        t_end = t[-1]
        t_start = t_end - args.window
        idx = next((i for i, v in enumerate(t) if v >= t_start), 0)
        t, pos = t[idx:], pos[idx:]

        line_pos.set_data(t, pos)

        ax_pos.set_xlim(t[0], t[-1] + 0.1)
        ax_pos.relim(); ax_pos.autoscale_view(scalex=False)

        fig.suptitle(f"t={t[-1]:.1f}s   dist={pos[-1]:.3f} m", fontsize=13)
        return (line_pos,)

    ani = animation.FuncAnimation(  # noqa: F841  # must stay referenced to prevent GC
        fig, update,
        interval=max(50, int(1000 / args.rate)),  # ms between frames
        blit=False,
        cache_frame_data=False,
    )

    plt.tight_layout()
    plt.show()  # blocks until window is closed

    # --- Shutdown ---
    stop_event.set()
    reader.join(timeout=2.0)
    print(f"\nStopped. Captured {len(time_data)} samples.")

    if not args.no_save and time_data:
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = Path(__file__).parent / f"encoder_{ts}.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "absolute_time", "voltage_V", "distance_m"])
            writer.writerows(zip(time_data, abs_time_data, encoder_data, dist_data))
        print(f"Data saved to:\n  {out_path}")


if __name__ == "__main__":
    main()
