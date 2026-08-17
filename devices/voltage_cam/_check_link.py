"""
Which physical link is DCAM actually using?

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\voltage_cam\\_check_link.py

This camera (C16240-20UP) has both a USB3 and a CoaXPress interface, and this
machine has BOTH cabled (Active Silicon FireBird 4xCXP6-2PE8 grabber + USB).
Nothing in the app selects the link — DCAM enumerates whichever it finds, and
on 2026-07-29 that was USB3, costing a factor of ~7.3 in frame rate.

The frame period at full frame is decided by the link, so it is a reliable
fingerprint:

    USB3.1 Gen1 16-bit   ~63.3 ms   (15.8 fps,   316 MB/s)
    CoaXPress             ~8.7 ms   ( 115 fps,  2300 MB/s)

Run this after any cabling change. Requires the camera to be free (close the
toy and the main app first — DCAM will not hand out the device twice).
"""
from pylablib.devices import DCAM

W, H = 4432, 2368


def main() -> int:
    n = DCAM.get_cameras_number()
    print(f"DCAM cameras: {n}")
    if not n:
        print("no camera found — check cabling and power")
        return 1

    cam = DCAM.DCAMCamera(idx=0)
    try:
        print(f"device: {cam.get_device_info()}")
        cam.set_roi(hbin=1, vbin=1)
        cam.set_exposure(0.001)          # short enough not to be the limit
        _exp, period = cam.get_frame_timings()
        fps = 1.0 / period
        print(f"\nfull frame: {period * 1e3:.2f} ms  ->  {fps:.1f} fps  "
              f"({W * H * 2 * fps / (1 << 20):.0f} MB/s)")
        if fps > 40:
            print("LINK: CoaXPress")
            print("NOTE: recording full frame needs ~2300 MB/s; the write path "
                  "sustains ~1004 MB/s end to end (measured), so recording caps "
                  "near 48 fps. 2x2 binning records all of it.")
        else:
            print("LINK: USB3  — CoaXPress is NOT in use")
            print("To switch: unplug USB, power-cycle the camera, re-run this.")
        return 0
    finally:
        cam.close()


if __name__ == "__main__":
    # Make the console unable to raise on this script's own output before it
    # prints anything (see acqApp/console.py -- a UnicodeEncodeError from a
    # diagnostic print is otherwise indistinguishable from a device failure).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    raise SystemExit(main())
