"""MASTER PULSE MODE=BURST probe. Camera only: opens, configures, reads frames.

Questions:
  A. No edge -> zero frames?
  B. One edge -> exactly N frames, then the camera goes quiet?
  C. Later edges with no re-arm -> another N each (burst re-arms itself)?
  D. If not: does the usual mode-cycle re-arm (CONTINUOUS -> BURST) work?
  E. Rate inside a burst = 1/INTERVAL, same as START mode?

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\voltage_cam\\_probe_burst.py
        [N=900] [part1_s=90] [part2_s=60] [target_hz=500]

target_hz > the EDGE floor (~452 Hz here) needs SYNCREADOUT, which is asked for.

Close acqApp first (it holds the camera). At "PART 1 ... SEND EDGES NOW", click
triggerApp's RUN PROBE SEQUENCE once (same part1_s/part2_s) - it covers both parts.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from acqApp.console import enable_safe_console              # noqa: E402

enable_safe_console()

from acqApp.devices.voltage_cam.acquisition import (             # noqa: E402
    OrcaFireWorker, open_camera)
from acqApp.devices.voltage_cam.presets import (                  # noqa: E402
    PRESETS, master_pulse_interval)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 900
PART1_S = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
PART2_S = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
TARGET_HZ = float(sys.argv[4]) if len(sys.argv) > 4 else 500.0
EXP_US = 250.0
BASELINE_S = 5.0
QUIET_S = 0.3           # no frames this long = the burst is over

MODE, SRC, INTERVAL, BURST = ("MASTER PULSE MODE", "MASTER PULSE TRIGGER SOURCE",
                              "MASTER PULSE INTERVAL", "MASTER PULSE BURST TIMES")
CONTINUOUS, START_, BURST_MODE = 1, 2, 3


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def readback(cam):
    out = []
    for p in ("TRIGGER SOURCE", "TRIGGER ACTIVE", "TRIGGER POLARITY", SRC, MODE, INTERVAL, BURST):
        try:
            out.append(f"{p}={cam.get_attribute_value(p, enum_as_str=True)}")
        except Exception as e:                  # noqa: BLE001
            out.append(f"{p}=? ({type(e).__name__}: {e})")
    return ", ".join(out)


def watch(cam, seconds, label, interval):
    """Collect frames for `seconds`; report each burst as it ends."""
    bursts, cur, last_arrival = [], None, None
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        try:
            cam.wait_for_frame(timeout=0.1)
        except Exception:                       # noqa: BLE001 - timeout
            pass
        res = cam.read_multiple_images(return_info=True)
        imgs, infos = res if res else (None, None)
        now = time.perf_counter()
        for info in (infos or []):
            if cur is None:
                cur = {"t0": now, "stamps": [], "fs": []}
                say(f"{label}: burst {len(bursts) + 1} started")
            cur["stamps"].append(info.timestamp_us * 1e-6)
            cur["fs"].append(info.framestamp)
            last_arrival = now
        if cur is not None and now - last_arrival > QUIET_S:
            bursts.append(cur)
            report(label, len(bursts), cur, bursts, interval)
            cur = None
    if cur is not None:
        bursts.append(cur)
        report(label, len(bursts), cur, bursts, interval, still_running=True)
    return bursts


def report(label, k, b, bursts, interval, still_running=False):
    import numpy as np
    ts = np.array(b["stamps"])
    dt = np.diff(ts)
    gap = ""
    if k > 1:
        gap = f", {b['t0'] - bursts[-2]['t0']:.2f} s after the previous start"
    rate = f"{1 / np.median(dt):.1f} Hz (median dt {np.median(dt) * 1e3:.4f} ms, " \
           f"max {dt.max() * 1e3:.3f})" if len(dt) else "-"
    say(f"{label}: burst {k}: {len(ts)} frames"
        f"{' (STILL RUNNING at window end)' if still_running else ''}, "
        f"framestamps {b['fs'][0]}..{b['fs'][-1]}, {rate}{gap}; "
        f"INTERVAL asks {1 / interval:.1f} Hz")


def main():
    say(f"opening camera (N={N}, exposure {EXP_US:g} us, "
        f"target {TARGET_HZ:g} Hz)")
    cam = open_camera(0)
    try:
        p = PRESETS["4432x512"]
        cam.set_roi(hstart=p.hpos, hend=p.hpos + p.hsize,
                    vstart=p.vpos, vend=p.vpos + p.vsize, hbin=4, vbin=4)
        cam.set_exposure(EXP_US * 1e-6)
        try:
            cam.set_readout_speed("fast")
        except Exception:                       # noqa: BLE001
            pass
        cam.set_trigger_mode("master_pulse")
        cam.setup_ext_trigger(invert=True)
        sync = OrcaFireWorker._enable_syncreadout(cam)
        say(f"SYNCREADOUT {'on' if sync else 'REFUSED - EDGE floor'}")
        cam.set_attribute_value(SRC, 1)
        cam.set_attribute_value(MODE, BURST_MODE)
        cam.set_attribute_value(BURST, N)
        period = cam.get_frame_period()
        interval = master_pulse_interval(period, EXP_US, syncreadout=sync,
                                         target_hz=TARGET_HZ)
        say(f"frame period {period * 1e3:.4f} ms -> INTERVAL "
            f"{interval * 1e3:.4f} ms ({1 / interval:.1f} Hz"
            f"{'' if interval <= 1 / TARGET_HZ + 1e-9 else ', FLOOR ABOVE TARGET'})")
        cam.set_attribute_value(INTERVAL, interval)
        try:
            a = cam.get_attribute(BURST)
            say(f"BURST TIMES range {a.min}..{a.max}")
        except Exception as e:                  # noqa: BLE001
            say(f"BURST TIMES range unreadable ({e})")
        say(f"readback: {readback(cam)}")

        cam.start_acquisition(nframes=max(2 * N, 2000))
        say(f"A. baseline {BASELINE_S:g} s - DO NOT send edges")
        base = watch(cam, BASELINE_S, "baseline", interval)
        say(f"A. baseline: {sum(len(b['stamps']) for b in base)} frames "
            f"(want 0)")

        say(f"B/C. PART 1 ({PART1_S:g} s): SEND EDGES NOW "
            f"(click triggerApp RUN PROBE SEQUENCE once)")
        p1 = watch(cam, PART1_S, "part1", interval)
        say(f"part 1: {len(p1)} burst(s), sizes {[len(b['stamps']) for b in p1]}")

        say("D. re-arm by mode cycle (stop, CONTINUOUS -> BURST, start)")
        cam.stop_acquisition()
        cam.set_attribute_value(MODE, CONTINUOUS)
        cam.set_attribute_value(MODE, BURST_MODE)
        cam.start_acquisition(nframes=max(2 * N, 2000))
        say(f"readback: {readback(cam)}")
        say(f"PART 2 ({PART2_S:g} s): SEND EDGES AGAIN "
            f"(triggerApp sequence covers this)")
        p2 = watch(cam, PART2_S, "part2", interval)
        say(f"part 2: {len(p2)} burst(s), sizes {[len(b['stamps']) for b in p2]}")
    finally:
        try:
            cam.stop_acquisition()
        except Exception:                       # noqa: BLE001
            pass
        # Leave the camera as the app expects to find it.
        try:
            cam.set_attribute_value(MODE, START_)
        except Exception:                       # noqa: BLE001
            pass
        cam.close()
        say("camera closed (DCAM releases slowly - wait before reopening)")


if __name__ == "__main__":
    main()
