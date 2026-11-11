"""
In vivo acquisition suite — entry point.

Owns what is session-wide: the clock, the sync/trigger bus, the recorder, the
save destination and docks. Each instrument is a `ModuleAdapter` in
`adapters/`; this window only iterates over them.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\main.py
  python acqApp\\main.py                  (re-execs into the venv)
  python -m acqApp.main --mock

ACQAPP_NO_REEXEC=1 skips the venv re-exec; ACQAPP_NO_INSTALL=1 skips
installing requirements.
"""

from __future__ import annotations
import argparse
import faulthandler
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# A segfault in the DCAM SDK can't be caught; this at least dumps the stacks.
faulthandler.enable()


# ── Environment bootstrap (before any third-party import) ─────────────────────
def _bootstrap() -> None:
    """Run from anywhere, but only ever inside `acqApp/.venv`: create it and
    re-exec if needed, and pip-install only from inside it."""
    here = Path(__file__).resolve().parent
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    venv_dir = here / ".venv"
    venv_py = venv_dir / scripts / exe

    parent = str(here.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    # Before any print (console.py imports only sys).
    from acqApp.console import enable_safe_console
    enable_safe_console()

    try:
        in_venv = Path(sys.executable).resolve() == venv_py.resolve()
    except OSError:
        in_venv = False

    if not in_venv and not os.environ.get("ACQAPP_NO_REEXEC"):
        if not venv_py.exists():
            print(f"[bootstrap] creating project venv at {venv_dir} …")
            try:
                subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            except (subprocess.CalledProcessError, OSError) as e:
                sys.exit(f"[bootstrap] couldn't create venv ({e}); create it "
                         f"manually:\n    python -m venv {venv_dir}")
        os.environ["ACQAPP_NO_REEXEC"] = "1"          # no re-exec loops
        print(f"[bootstrap] launching under {venv_py}")
        os.execv(str(venv_py), [str(venv_py), str(here / "main.py"), *sys.argv[1:]])

    try:
        import PyQt6  # noqa: F401
    except ImportError:
        if not in_venv:
            sys.exit("[bootstrap] dependencies are missing and we're not in the "
                     "project venv. Remove ACQAPP_NO_REEXEC so it can re-exec "
                     "into acqApp/.venv, or install deps into your environment "
                     "yourself — refusing to pip-install into an unknown Python.")
        if os.environ.get("ACQAPP_NO_INSTALL"):
            raise
        req = here / "requirements.txt"
        print(f"[bootstrap] installing dependencies into the venv from {req} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])


_bootstrap()

from acqApp import config

# ── Hardware pre-init ─────────────────────────────────────────────────────────
# The camera is opened ONCE and the handle reused: re-opening a just-closed
# DCAM device crashes natively. Closed in MainWindow.closeEvent
# (window/main_window.py); the window itself lives in window/.
_cam_info = None
_cam_handle = None
_cam_thread = None
_mock = "--mock" in sys.argv


def _open_camera() -> None:
    """The startup open, on a worker thread; never raises."""
    global _cam_handle, _cam_info
    t0 = time.perf_counter()
    dcam = None
    try:
        from pylablib.devices import DCAM as dcam
        from acqApp.devices.voltage_cam.acquisition import open_camera
        # Open first, count only on failure: get_cameras_number()
        # re-enumerates every call (~5 s).
        handle = open_camera(0)
        # Kept even if the info read fails: a second open would crash natively.
        _cam_handle = handle
        try:
            _cam_info = handle.get_device_info()
        except Exception as e:                    # noqa: BLE001
            print(f"Voltage cam: device info unreadable ({e})")
        print(f"Voltage cam: {_cam_info or 'opened'} "
              f"(in {time.perf_counter() - t0:.1f} s)")
    except Exception as e:                        # noqa: BLE001
        _cam_handle = None
        n = -1
        if dcam is not None:
            try:
                n = dcam.get_cameras_number()
            except Exception:
                pass
        if n == 0:
            print("No DCAM camera detected — use Emulate to run without hardware")
        else:
            print(f"Camera unavailable ({type(e).__name__}: {e}) — if HCImage "
                  f"or another app has it open, close that first. Use Emulate "
                  f"to run without it.")


if not _mock and "voltage_cam" in config.load_enabled_modules():
    # Overlaps the ~8 s open with the Qt import and module picker. Keyed on
    # the LAST selection; if voltage_cam is enabled only now, the worker opens
    # its own handle on first Start.
    _cam_thread = threading.Thread(target=_open_camera, name="cam-open")
    _cam_thread.start()


def _await_camera() -> None:
    if _cam_thread is not None:
        if _cam_thread.is_alive():
            print("Waiting for the camera to finish opening…")
        _cam_thread.join()


# ─────────────────────────────────────────────────────────────────────────────

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtWidgets import QApplication, QDialog
import pyqtgraph as pg

from acqApp import style
from acqApp.dialogs import ModuleSelectDialog

pg.setConfigOptions(imageAxisOrder="row-major")

from acqApp.window.main_window import MainWindow, close_camera


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true",
                    help="start in Emulate mode (simulated signals, no hardware)")
    ap.parse_args()

    app = QApplication(sys.argv)
    style.apply_theme(app)

    dlg = ModuleSelectDialog(config.load_enabled_modules())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        # Release the handle anyway, or the next launch double-opens and crashes.
        _await_camera()
        close_camera(_cam_handle)
        return
    enabled = dlg.selected()
    config.save_enabled_modules(enabled)

    _await_camera()
    win = MainWindow(cam_info=_cam_info, mock=_mock, enabled=set(enabled),
                     cam_handle=_cam_handle)
    win.show()

    from acqApp.devices.mirror.startup import ensure_camera_default
    mirror_result = ensure_camera_default()
    if not mirror_result.ok:
        win.status(f"Mirror check skipped: {mirror_result.error}")
    elif mirror_result.corrected:
        win.status("Mirror wasn't on CAMERA/epi — corrected at launch.")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
