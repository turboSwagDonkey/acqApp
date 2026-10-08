"""Re-shoot the README screenshots offscreen, from a mock session.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\shots.py [--out docs\\images\\readme]

Writes picker.png, main_live.png, routines.png and pupil_settings.png. Mock
window, isolated user state: nothing on the rig is touched or rewritten. The
frames are synthetic noise, which is what a mock session shows. Run it after
any change that alters how those windows look. Not part of run_all.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Before the first Qt import: windowless, with real fonts to measure text by.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from _harness import isolate_user_state, make_window, pump, qt_app  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "images" / "readme"


def _save(widget, out: Path, name: str) -> None:
    path = out / name
    if not widget.grab().save(str(path)):
        sys.exit(f"could not write {path}")
    print(f"{path}: {widget.width()}x{widget.height()}")


def _sample_routine():
    from acqApp.routines.settings import Routine, Step
    return Routine(name="two fields, baseline then stimulus", steps=[
        Step(kind="move", label="field A", x_um=0.0, y_um=0.0, settle_s=0.5),
        Step(kind="record", label="baseline", length=300, unit="frames"),
        Step(kind="move", label="field B", x_um=250.0, y_um=0.0, settle_s=0.5),
        Step(kind="display", label="grid on", pattern="grid.png"),
        Step(kind="record", label="stimulus", length=5, unit="seconds"),
    ])


def _shoot_picker(app, out: Path) -> None:
    from acqApp import config
    from acqApp.dialogs import ModuleSelectDialog
    dlg = ModuleSelectDialog(list(config.MODULES))
    dlg.show()
    app.processEvents()
    _save(dlg, out, "picker.png")
    dlg.close()


def _shoot_main(app, out: Path) -> None:
    from acqApp import config
    win = make_window(set(config.MODULES))
    win.resize(1600, 900)
    win.show()
    pump(app, 0.6)                      # let the docks and axes lay out first
    win._btn_run.setChecked(True)       # Live: the mock devices start streaming
    pump(app, 3.0)
    _save(win, out, "main_live.png")
    win._btn_run.setChecked(False)
    win.close()


def _shoot_routines(app, out: Path) -> None:
    win = make_window({"routines"})
    panel = win._module("routines").panel
    panel.set_routine(_sample_routine())
    panel.setParent(None)
    panel.resize(700, 860)
    panel.show()
    pump(app, 0.3)
    _save(panel, out, "routines.png")
    panel.close()
    win.close()


def _shoot_pupil_settings(app, out: Path) -> None:
    win = make_window({"pupil_cam"})
    panel = win._module("pupil_cam").panel
    panel.setParent(None)
    panel.resize(420, 100)
    panel.show()
    app.processEvents()
    panel.resize(420, max(panel.sizeHint().height(), panel.minimumSizeHint().height()))
    app.processEvents()
    _save(panel, out, "pupil_settings.png")
    panel.close()
    win.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    isolate_user_state()
    app = qt_app()      # held: a collected QApplication takes the process down
    for shoot in (_shoot_picker, _shoot_main, _shoot_routines, _shoot_pupil_settings):
        shoot(app, out)
    app.processEvents()
    return 0


if __name__ == "__main__":
    sys.exit(main())
