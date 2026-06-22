r"""
Phase 0 hardware de-risk: rotary wheel encoder on NI PCIe-6363 (Dev3).

Reads the encoder as an analog voltage from ai2 (single-ended), matching
the MATLAB script exactly. Polls in a tight loop, timestamps every sample,
shows a live plot of position and speed, and saves to a .csv on exit.

Wiring: encoder signal -> Dev3/ai2 (single-ended / referenced to AIGND)

Run:
  ..\..\.venv\Scripts\python.exe encoder_read.py
  ..\..\.venv\Scripts\python.exe encoder_read.py --chan Dev3/ai2 --rate 50
  ..\..\.venv\Scripts\python.exe encoder_read.py --volts-per-rev 5.0
"""

import argparse
import csv
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

def daq_thread(chan, period, volts_per_rev, stop_event,
               time_buf, voltage_buf, rev_buf, speed_buf,
               time_data, abs_time_data, encoder_data):
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
        last_t = trial_start
        last_v = None

        while not stop_event.is_set():
            voltage: float = task.read()  # type: ignore[assignment]  # stubs over-widen AI voltage return
            now = time.perf_counter()
            elapsed = now - trial_start
            abs_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            dt = now - last_t
            if last_v is not None and dt > 0 and volts_per_rev is not None:
                speed_rev_s = (voltage - last_v) / dt / volts_per_rev
            else:
                speed_rev_s = 0.0

            revs = (voltage / volts_per_rev) if volts_per_rev is not None else voltage

            # Rolling plot buffers
            time_buf.append(elapsed)
            voltage_buf.append(voltage)
            rev_buf.append(revs)
            speed_buf.append(speed_rev_s)

            # Full-session save buffers
            time_data.append(elapsed)
            abs_time_data.append(abs_now)
            encoder_data.append(voltage)

            last_t = now
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
    ap.add_argument("--volts-per-rev", type=float, default=None,
                    help="voltage span for one full revolution; enables rev/speed axes")
    ap.add_argument("--no-save", action="store_true",
                    help="skip saving .csv on exit")
    args = ap.parse_args()

    period = 1.0 / args.rate
    maxlen = int(args.window * args.rate * 2)  # keep 2x the window in the deque

    # Shared buffers (deque is thread-safe for append + read from two threads)
    time_buf    = deque(maxlen=maxlen)
    voltage_buf = deque(maxlen=maxlen)
    rev_buf     = deque(maxlen=maxlen)
    speed_buf   = deque(maxlen=maxlen)

    # Full-session storage (grows unbounded, saved on exit)
    time_data     = []
    abs_time_data = []
    encoder_data  = []

    stop_event = threading.Event()

    reader = threading.Thread(
        target=daq_thread,
        args=(args.chan, period, args.volts_per_rev, stop_event,
              time_buf, voltage_buf, rev_buf, speed_buf,
              time_data, abs_time_data, encoder_data),
        daemon=True,
    )

    print(f"Opening encoder on {args.chan}  rate={args.rate} Hz  window={args.window} s")
    if args.volts_per_rev:
        print(f"  volts-per-rev={args.volts_per_rev} -> position in revolutions, speed in rev/s")
    else:
        print("  No --volts-per-rev given -> position axis shows raw voltage")
    print("Close the plot window to stop.\n")

    reader.start()

    # --- Build the figure ---
    fig, (ax_pos, ax_spd) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    fig.suptitle("Encoder — live", fontsize=13)

    pos_label = "Position (rev)" if args.volts_per_rev else "Voltage (V)"
    ax_pos.set_ylabel(pos_label)
    ax_pos.grid(True, alpha=0.3)

    ax_spd.set_ylabel("Speed (rev/s)" if args.volts_per_rev else "dV/dt (V/s)")
    ax_spd.set_xlabel("Time (s)")
    ax_spd.grid(True, alpha=0.3)

    (line_pos,) = ax_pos.plot([], [], color="royalblue", linewidth=1.2)
    (line_spd,) = ax_spd.plot([], [], color="tomato",    linewidth=1.2)

    title_tpl = "t={t:.1f}s   pos={pos:.3f}   speed={spd:.3f}"

    def update(_):
        if len(time_buf) < 2:
            return line_pos, line_spd

        t   = list(time_buf)
        pos = list(rev_buf)
        spd = list(speed_buf)

        # Trim to rolling window
        t_end = t[-1]
        t_start = t_end - args.window
        idx = next((i for i, v in enumerate(t) if v >= t_start), 0)
        t, pos, spd = t[idx:], pos[idx:], spd[idx:]

        line_pos.set_data(t, pos)
        line_spd.set_data(t, spd)

        ax_pos.set_xlim(t[0], t[-1] + 0.1)
        ax_pos.relim(); ax_pos.autoscale_view(scalex=False)
        ax_spd.relim(); ax_spd.autoscale_view(scalex=False)

        fig.suptitle(title_tpl.format(t=t[-1], pos=pos[-1], spd=spd[-1]), fontsize=13)
        return line_pos, line_spd

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
            writer.writerow(["time_s", "absolute_time", "voltage_V"])
            writer.writerows(zip(time_data, abs_time_data, encoder_data))
        print(f"Data saved to:\n  {out_path}")


if __name__ == "__main__":
    main()
