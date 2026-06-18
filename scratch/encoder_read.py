r"""
Phase 0 hardware de-risk: rotary wheel encoder on NI PCIe-6363 (Dev3).

Goal of this throwaway script:
  1. Prove the quadrature encoder reads through nidaqmx on a counter input.
  2. Print live angular position / revolutions / linear distance as you spin
    the wheel by hand, so we can confirm wiring, PPR, and decoding direction
    before any of it goes into the real app.

This is software-timed (on-demand polling) on purpose -- it mirrors the v1
SoftwareClock plan. The v2 upgrade to a buffered, hardware-clocked counter
task reuses this exact channel setup plus cfg_samp_clk_timing.

This is NOT app code. It lives in scratch/ and gets deleted after Phase 0.

Requires:
  - nidaqmx (pip, installed)
  - NI-DAQmx runtime driver installed (the one NI MAX uses).

Wiring note (X-series default terminals for ctr0):
  - Encoder A  -> PFI8   (counter 0 source)
  - Encoder B  -> PFI10  (counter 0 aux/up-down)
  - Encoder Z  -> PFI9   (only if --use-z)
  These are routable; if your wiring differs we pass --chan and adjust.

Run:
  ..\..\.venv\Scripts\python.exe encoder_read.py
  ..\..\.venv\Scripts\python.exe encoder_read.py --ppr 1024 --wheel-dia 150
"""

import argparse
import math
import time


def main():
    ap = argparse.ArgumentParser(description="PCIe-6363 quadrature encoder live read")
    ap.add_argument("--chan", type=str, default="Dev3/ctr0",
                    help="counter input channel (default Dev3/ctr0)")
    ap.add_argument("--ppr", type=int, default=1024,
                    help="encoder pulses per revolution (PRE-decoding count)")
    ap.add_argument("--decoding", type=str, default="X4",
                    choices=["X1", "X2", "X4"],
                    help="quadrature decoding (X4 = 4x resolution, default)")
    ap.add_argument("--wheel-dia", type=float, default=None,
                    help="wheel diameter in mm; if given, prints linear distance")
    ap.add_argument("--use-z", action="store_true",
                    help="enable the Z/index channel for per-rev zeroing")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="console refresh rate in Hz (polling cadence)")
    args = ap.parse_args()

    try:
        import nidaqmx
        from nidaqmx.constants import EncoderType, AngleUnits, EncoderZIndexPhase
    except Exception as e:
        raise SystemExit(
            f"Could not import nidaqmx: {e}\n"
            "Install the NI-DAQmx runtime driver (the one NI MAX uses)."
        )

    decode_map = {
        "X1": EncoderType.X_1,
        "X2": EncoderType.X_2,
        "X4": EncoderType.X_4,
    }

    print(f"Opening encoder task on {args.chan}")
    print(f"  PPR={args.ppr}  decoding={args.decoding}  "
          f"({'Z enabled' if args.use_z else 'no Z'})")

    with nidaqmx.Task() as task:
        # pulses_per_rev is the encoder's native count; DAQmx applies the
        # decoding multiplier internally and reports angle in the chosen units.
        chan = task.ci_channels.add_ci_ang_encoder_chan(
            args.chan,
            decoding_type=decode_map[args.decoding],
            zidx_enable=args.use_z,
            zidx_val=0.0,
            zidx_phase=EncoderZIndexPhase.AHIGH_BHIGH,
            units=AngleUnits.RADIANS,
            pulses_per_rev=args.ppr,
            initial_angle=0.0,
        )

        task.start()
        print("\nSpin the wheel. Ctrl+C to stop.\n")

        radius_mm = (args.wheel_dia / 2.0) if args.wheel_dia else None
        period = 1.0 / args.rate
        last_angle = 0.0
        last_t = time.perf_counter()

        try:
            while True:
                angle = task.read()            # radians, cumulative
                now = time.perf_counter()
                dt = now - last_t

                revs = angle / (2.0 * math.pi)
                ang_vel = (angle - last_angle) / dt if dt > 0 else 0.0  # rad/s

                line = (f"\rangle={angle:9.3f} rad | "
                        f"revs={revs:8.3f} | "
                        f"omega={ang_vel:8.3f} rad/s")
                if radius_mm is not None:
                    distance_mm = angle * radius_mm
                    speed_mm_s = ang_vel * radius_mm
                    line += (f" | dist={distance_mm/1000.0:8.3f} m"
                             f" | speed={speed_mm_s/1000.0:7.3f} m/s")

                print(line + "   ", end="", flush=True)

                last_angle = angle
                last_t = now
                time.sleep(period)
        except KeyboardInterrupt:
            print("\n\nStopped.")


if __name__ == "__main__":
    main()
