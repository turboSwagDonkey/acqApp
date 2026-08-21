"""Open the DMD ROI editor on its own, with no rig and no light.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\dmd\\_roi_editor.py
    ...\\_roi_editor.py --image snapshot.npy      # a real ORCA frame
    ...\\_roi_editor.py --calib dmd_calib.json    # a measured registration

`RoiEditor` is built and tested (`tests/test_dmd_roi.py`) but is **not wired
into the DMD tab** — see PLAN.md §6 item 2. This is how to look at it until it
is. Nothing here opens a device or projects anything.

Without `--calib` there is no measured DMD↔camera transform, so a plausible
stand-in is invented to draw the reachable field with: **the outline is then
made up, and an ROI inside it proves nothing about where light would land.**
The window title says so.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

CAM_W, CAM_H = 2216, 1184           # ORCA at 2x2 binning
DMD_W, DMD_H = 1024, 768


def demo_frame(w: int = CAM_W, h: int = CAM_H) -> np.ndarray:
    """Something to draw on: soft blobs on a noisy field, so ROIs are visible."""
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = rng.normal(40, 6, (h, w)).astype(np.float32)
    for cx, cy, r, amp in ((0.42, 0.45, 0.10, 120), (0.58, 0.38, 0.06, 90),
                           (0.50, 0.62, 0.08, 70), (0.33, 0.58, 0.05, 60)):
        img += amp * np.exp(-(((xx - cx * w) ** 2 + (yy - cy * h) ** 2)
                              / (2.0 * (r * min(w, h)) ** 2)))
    return np.clip(img, 0, 255).astype(np.uint8)


def stand_in_calibration(cam_size, dmd_size):
    """A *made-up* registration: the DMD covering the middle ~60 % of the frame.

    Only so the reachable-field outline has something to draw. Not a
    measurement — `run_calibration()` in `calibration.py` is.
    """
    from acqApp.devices.dmd.calibration import DmdCalibration
    cw, ch = cam_size
    dw, dh = dmd_size
    scale = 0.6 * cw / dw                       # DMD mirrors → camera px
    # camera px → DMD mirrors, centred
    cam_to_dmd = np.array([[1.0 / scale, 0.0, -(cw / 2 - dw * scale / 2) / scale],
                           [0.0, 1.0 / scale, -(ch / 2 - dh * scale / 2) / scale],
                           [0.0, 0.0, 1.0]])
    return DmdCalibration(cam_to_dmd=cam_to_dmd, dmd_size=dmd_size,
                          cam_size=cam_size, model="affine", notes="STAND-IN")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", help=".npy or .csv camera snapshot to draw on")
    ap.add_argument("--calib", help="DmdCalibration JSON from run_calibration()")
    args = ap.parse_args()

    if args.image:
        p = Path(args.image)
        frame = (np.load(p) if p.suffix == ".npy"
                 else np.loadtxt(p, delimiter=","))
        frame = np.asarray(frame)
        if frame.ndim != 2:
            print(f"expected a 2-D image, got shape {frame.shape}")
            return 2
        print(f"snapshot: {p.name}  {frame.shape[1]}x{frame.shape[0]}")
    else:
        frame = demo_frame()
        print(f"snapshot: synthetic {frame.shape[1]}x{frame.shape[0]} "
              f"(pass --image for a real ORCA frame)")

    cam_size = (int(frame.shape[1]), int(frame.shape[0]))
    if args.calib:
        from acqApp.devices.dmd.calibration import DmdCalibration
        calib = DmdCalibration.load(args.calib)
        note = f"calibration {Path(args.calib).name} — {calib.describe()}"
        real = True
    else:
        calib = stand_in_calibration(cam_size, (DMD_W, DMD_H))
        note = "STAND-IN registration — the field outline is invented"
        real = False
    print(note)

    from PyQt6.QtWidgets import QApplication

    from acqApp.devices.dmd.roi_panel import RoiEditor
    from acqApp.style import apply_theme

    app = QApplication([])          # assign it: an unreferenced one is GC'd
    apply_theme(app, "dark")
    ed = RoiEditor(calib)
    ed.set_image(frame)
    ed.resize(1100, 800)
    ed.setWindowTitle("DMD ROI editor — "
                      + ("real calibration" if real
                         else "STAND-IN calibration, positions are not real"))
    ed.rois_changed.connect(
        lambda s: print(f"{len(s)} ROI(s): "
                        + ", ".join(r.name for r in s)))
    ed.show()
    print("\nAdd / Delete / Clear are under the image; drag and resize on it.\n"
          "The dashed outline is the DMD's reachable field — an ROI outside it\n"
          "is a stimulus that never arrives.")
    return app.exec()


if __name__ == "__main__":
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()       # before the first print (see acqApp/console.py)
    raise SystemExit(main())
