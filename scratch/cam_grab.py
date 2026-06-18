r"""
Phase 0 hardware de-risk: Hamamatsu Orca Fire grab + throughput measurement.

Goal of this throwaway script:
  1. Prove the Orca talks to Python through pylablib's DCAM wrapper.
  2. Measure the REAL achieved frame rate and data rate (MB/s) at a given
     exposure / ROI / binning. That number sizes the ring buffer and decides
     the streaming-to-disk strategy for the real app.

This is NOT app code. It lives in scratch/ and gets deleted after Phase 0.

Requires:
  - pylablib  (pip, installed)
  - Hamamatsu DCAM-API runtime installed on the machine (comes with the camera
    driver / HCImage). If HCImage sees the camera, this should too.

Run:
  ..\..\.venv\Scripts\python.exe cam_grab.py
  ..\..\.venv\Scripts\python.exe cam_grab.py --frames 200 --exposure 0.005 --save
"""

import argparse
import time
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser(description="Orca Fire grab + throughput test")
    ap.add_argument("--frames", type=int, default=100,
                    help="number of frames to acquire for the timing run")
    ap.add_argument("--exposure", type=float, default=0.01,
                    help="exposure time in seconds (default 10 ms)")
    ap.add_argument("--binning", type=int, default=1,
                    help="NxN binning (1 = full res)")
    ap.add_argument("--roi", type=int, nargs=4, default=None,
                    metavar=("X0", "X1", "Y0", "Y1"),
                    help="optional ROI as x0 x1 y0 y1 (pixels)")
    ap.add_argument("--save", action="store_true",
                    help="save the captured stack to a TIFF for inspection")
    ap.add_argument("--out", type=str, default="cam_grab_out.tif",
                    help="output TIFF path when --save is used")
    args = ap.parse_args()

    # Import here so a missing DCAM runtime gives a clear message, not an
    # import-time crash before we can explain it.
    try:
        from pylablib.devices import DCAM
    except Exception as e:
        raise SystemExit(f"Could not import pylablib DCAM module: {e}")

    n = DCAM.get_cameras_number()
    print(f"DCAM cameras detected: {n}")
    if n == 0:
        raise SystemExit(
            "No DCAM camera found. Check: camera powered on, USB/CoaXPress "
            "connected, and the Hamamatsu DCAM-API runtime installed "
            "(does HCImage see it?)."
        )

    cam = DCAM.DCAMCamera(idx=0)
    try:
        info = cam.get_device_info()
        print(f"Connected: {info}")

        # --- configure ---
        cam.set_exposure(args.exposure)
        try:
            cam.set_attribute_value("BINNING", args.binning)
        except Exception:
            # not all models expose BINNING by this name; non-fatal for the test
            print("(binning attribute not set — model may name it differently)")

        if args.roi:
            x0, x1, y0, y1 = args.roi
            cam.set_roi(x0, x1, y0, y1)
        roi = cam.get_roi()
        print(f"ROI (x0,x1,y0,y1,xbin,ybin): {roi}")
        print(f"Exposure set to: {cam.get_exposure() * 1e3:.3f} ms")

        det = cam.get_detector_size()
        print(f"Detector size: {det}")

        # --- timed acquisition ---
        print(f"\nAcquiring {args.frames} frames...")
        frames = []
        t0 = time.perf_counter()
        cam.start_acquisition()
        try:
            collected = 0
            while collected < args.frames:
                cam.wait_for_frame()
                # read_multiple_images drains everything buffered since last read
                imgs = cam.read_multiple_images()
                if imgs:
                    frames.extend(imgs)
                    collected = len(frames)
        finally:
            cam.stop_acquisition()
        t1 = time.perf_counter()

        frames = frames[:args.frames]
        stack = np.asarray(frames)
        elapsed = t1 - t0

        # --- report ---
        nframes = stack.shape[0]
        bytes_per_frame = stack[0].nbytes
        total_bytes = stack.nbytes
        fps = nframes / elapsed if elapsed > 0 else float("nan")
        mbps = (total_bytes / 1e6) / elapsed if elapsed > 0 else float("nan")

        print("\n--- RESULTS ---")
        print(f"frames captured : {nframes}")
        print(f"frame shape     : {stack.shape[1:]}  dtype={stack.dtype}")
        print(f"bytes / frame   : {bytes_per_frame/1e6:.3f} MB")
        print(f"elapsed         : {elapsed:.3f} s")
        print(f"achieved FPS    : {fps:.2f}")
        print(f"data rate       : {mbps:.1f} MB/s")
        print("\nUse the data rate to size the ring buffer and confirm the disk")
        print("can keep up (compare against your SSD's sustained write speed).")

        if args.save:
            import tifffile
            out = Path(args.out).resolve()
            tifffile.imwrite(str(out), stack)
            print(f"\nSaved stack -> {out}")

    finally:
        cam.close()
        print("Camera closed.")


if __name__ == "__main__":
    main()
