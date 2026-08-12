"""
Module-subset test.

The startup picker allows any combination of instruments, so every module
adapter has to cope with its neighbours being absent — including the voltage
camera, which owns the central view, and the modules that have no worker at all.
This is the failure mode that a per-module refactor is most likely to introduce
and least likely to show up in a full-stack run.

Builds a window for each subset, runs a short Live-view session, toggles
Emulate (which rebuilds the output controllers), and closes.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_module_subsets.py
"""
from __future__ import annotations

import shutil
import sys
import traceback

from _harness import Report, isolate_user_state, pump, qt_app

SUBSETS = [
    ["voltage_cam"],
    ["wheel"],                          # no central view at all
    ["pupil_cam"],                      # extra dock, no central view
    ["stage"],                          # panel only, no plot
    ["puffer", "dmd"],                  # controllers only, no workers
    ["voltage_cam", "wheel"],
    ["pupil_cam", "wheel", "stage"],
    ["closed_loop"],                    # a rule with no signal and no output
    ["wheel", "closed_loop"],           # a signal but nothing to fire
]


def main() -> int:
    r = Report("subsets")
    tmp = isolate_user_state()

    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from acqApp import config
    import acqApp.main as M

    for subset in SUBSETS + [list(config.MODULES)]:
        label = "+".join(subset)
        try:
            win = M.MainWindow(cam_info=None, mock=True, enabled=set(subset),
                               cam_handle=None)
            # A session: build workers -> start clock -> start -> display -> stop.
            win._btn_run.setChecked(True)
            pump(app, 0.4)
            for _ in range(4):
                win._display_tick()
                pump(app, 0.03)
            win._btn_run.setChecked(False)

            # The Emulate toggle rebuilds the output controllers in place.
            win._btn_emulate.setChecked(False)
            win._btn_emulate.setChecked(True)

            # The Devices monitor collects probe arguments from every adapter.
            assert isinstance(win._probe_kwargs(), dict)

            win.close()
            pump(app, 0.1)
            r.check(True, label)
        except Exception as e:
            traceback.print_exc()
            r.check(False, f"{label}: {type(e).__name__}: {e}")

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
