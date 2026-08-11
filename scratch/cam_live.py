r"""
Phase 0 hardware de-risk: Hamamatsu Orca Fire live display.

Grabs continuously in a background thread and renders each frame through
matplotlib imshow. The display is downsampled so the GUI stays responsive
even at full 4432x2368 resolution.

Controls:
  - Close the window to stop.
  - Press 'c' in the plot to toggle auto-contrast (percentile clip).

Run:
  ..\..\.venv\Scripts\python.exe cam_live.py
  ..\..\.venv\Scripts\python.exe cam_live.py --exposure 0.005 --downsample 2
  ..\..\.venv\Scripts\python.exe cam_live.py --roi 1000 3432 500 1868
"""

import argparse
import queue
import threading
import time

import matplotlib.pyplot as plt
import matplotlib.animation as animation
import numpy as np


# ---------------------------------------------------------------------------
# Grab thread
# ---------------------------------------------------------------------------

def grab_thread(cam, frame_q: queue.Queue, stop_event: threading.Event):
    """Continuously drain the camera buffer and push latest frame to frame_q."""
    cam.start_acquisition()
    try:
        while not stop_event.is_set():
            cam.wait_for_frame(timeout=1.0)
            imgs = cam.read_multiple_images()
            if imgs:
                # Drop all but the newest — display only cares about latest
                try:
                    frame_q.get_nowait()
                except queue.Empty:
                    pass
                frame_q.put(imgs[-1])
    except Exception:
        pass
    finally:
        cam.stop_acquisition()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Orca Fire live view")
    ap.add_argument("--exposure", type=float, default=0.01,
                    help="exposure in seconds (default 10 ms)")
    ap.add_argument("--downsample", type=int, default=4,
                    help="display every Nth pixel (default 4 → 1108x592)")
    ap.add_argument("--roi", type=int, nargs=4, default=None,
                    metavar=("X0", "X1", "Y0", "Y1"),
                    help="optional ROI x0 x1 y0 y1")
    ap.add_argument("--cmap", type=str, default="gray",
                    help="matplotlib colormap (default gray)")
    ap.add_argument("--plow", type=float, default=1.0,
                    help="auto-contrast low percentile (default 1)")
    ap.add_argument("--phigh", type=float, default=99.0,
                    help="auto-contrast high percentile (default 99)")
    args = ap.parse_args()

    try:
        from pylablib.devices import DCAM
    except Exception as e:
        raise SystemExit(f"Could not import pylablib DCAM: {e}")

    n = DCAM.get_cameras_number()
    if n == 0:
        raise SystemExit("No DCAM camera found.")

    cam = DCAM.DCAMCamera(idx=0)
    try:
        info = cam.get_device_info()
        print(f"Connected: {info}")

        cam.set_exposure(args.exposure)
        if args.roi:
            cam.set_roi(*args.roi)

        roi = cam.get_roi()
        exp_ms = cam.get_exposure() * 1e3
        w = roi[1] - roi[0]
        h = roi[3] - roi[2]
        d = args.downsample
        print(f"ROI: {w}x{h}  exposure: {exp_ms:.3f} ms  display: {w//d}x{h//d} (÷{d})")
        print("Close the window to stop. Press 'c' to toggle auto-contrast.\n")

        frame_q: queue.Queue = queue.Queue(maxsize=1)
        stop_event = threading.Event()

        grabber = threading.Thread(
            target=grab_thread, args=(cam, frame_q, stop_event), daemon=True
        )
        grabber.start()

        # --- wait for first frame so we can size the axes ---
        print("Waiting for first frame...", end="", flush=True)
        first = frame_q.get(timeout=10.0)
        print(" got it.")

        display = first[::d, ::d]
        vmin = int(np.percentile(display, args.plow))
        vmax = int(np.percentile(display, args.phigh))

        fig, ax = plt.subplots(figsize=(10, 6))
        fig.patch.set_facecolor("black")
        ax.set_facecolor("black")
        ax.axis("off")

        im = ax.imshow(display, cmap=args.cmap, vmin=vmin, vmax=vmax,
                       interpolation="nearest", aspect="equal")
        cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.01)
        cbar.ax.yaxis.set_tick_params(color="white")
        plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

        title = ax.set_title("", color="white", fontsize=10, pad=4)
        fig.tight_layout(pad=0.5)

        # state shared between key press and update
        state = {"auto_contrast": True, "fps_times": [], "frame_count": 0}

        def on_key(event):
            if event.key == "c":
                state["auto_contrast"] = not state["auto_contrast"]

        fig.canvas.mpl_connect("key_press_event", on_key)

        def update(_):
            try:
                frame = frame_q.get_nowait()
            except queue.Empty:
                return (im,)

            state["frame_count"] += 1
            now = time.perf_counter()
            state["fps_times"].append(now)
            # keep only last 30 timestamps for rolling FPS
            if len(state["fps_times"]) > 30:
                state["fps_times"].pop(0)

            disp = frame[::d, ::d]

            if state["auto_contrast"]:
                vlo = int(np.percentile(disp, args.plow))
                vhi = int(np.percentile(disp, args.phigh))
            else:
                vlo, vhi = 0, 65535

            im.set_data(disp)
            im.set_clim(vlo, vhi)

            fps_times = state["fps_times"]
            if len(fps_times) >= 2:
                fps = (len(fps_times) - 1) / (fps_times[-1] - fps_times[0])
            else:
                fps = 0.0

            ac_str = "auto" if state["auto_contrast"] else "full range"
            title.set_text(
                f"frame {state['frame_count']}   {fps:.1f} FPS   "
                f"contrast: [{vlo}, {vhi}] ({ac_str})   press 'c' to toggle"
            )
            return (im,)

        ani = animation.FuncAnimation(  # noqa: F841
            fig, update, interval=50, blit=False, cache_frame_data=False
        )

        plt.show()

    finally:
        stop_event.set()
        grabber.join(timeout=3.0)
        cam.close()
        print("Camera closed.")


if __name__ == "__main__":
    # Make the console unable to raise on this script's own output before it
    # prints anything (see acqApp/console.py -- a UnicodeEncodeError from a
    # diagnostic print is otherwise indistinguishable from a device failure).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    main()
