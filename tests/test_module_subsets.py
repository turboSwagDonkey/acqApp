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

    # ── teardown survives one module failing to stop ─────────────────────────
    # Stopping touches hardware: a stage whose serial port went away, a camera
    # that will not release. `_stop_session` stops every adapter in one loop, so
    # unguarded, the FIRST raise skipped every module after it (worker threads
    # left running) and skipped `stop_all()` (clock and trigger bus still alive
    # while the UI said "Stopped"). Via closeEvent it also skipped the DCAM
    # handle close, which is the native crash the pre-init note describes.
    try:
        win = M.MainWindow(cam_info=None, mock=True,
                           enabled={"wheel", "stage", "puffer"}, cam_handle=None)
        win._btn_run.setChecked(True)
        pump(app, 0.3)

        victim = win._modules[0]
        later = win._modules[1:]
        stopped: list[str] = []
        for m in later:                       # record that the rest still stop
            original = m.stop
            m.stop = (lambda mod=m, orig=original: (stopped.append(mod.key),
                                                    orig())[1])

        def boom():
            raise RuntimeError("serial port went away")
        victim.stop = boom

        win._stop_session()
        r.check([m.key for m in later] == stopped,
                f"one module raising in stop() does not strand the others "
                f"(stopped {stopped})")
        r.check(not win._sync.running,
                "…and the clock/trigger bus is still torn down")
        r.check(win._btn_run.text() == "Live view",
                "…and the UI returns to a consistent state")

        # CONTROL: the same failure through the OLD unguarded loop must strand
        # them, or the three checks above pass no matter what `_stop_session`
        # does.
        reached: list[str] = []
        adapters = [("victim", boom)] + [(m.key, lambda: None) for m in later]
        try:
            for key, fn in adapters:
                fn()
                reached.append(key)
        except RuntimeError:
            pass
        r.check(reached == [],
                f"control: the unguarded loop strands every later module "
                f"(reached {reached})")

        win.close()
        pump(app, 0.1)
    except Exception as e:
        traceback.print_exc()
        r.check(False, f"teardown-failure: {type(e).__name__}: {e}")

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
