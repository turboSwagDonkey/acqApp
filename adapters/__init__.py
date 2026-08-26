"""Per-subsystem wiring for the main window.

One `ModuleAdapter` subclass per instrument, owning its whole lifecycle, and
`MainWindow` just iterates — replacing six near-identical `if "foo" in enabled:`
branches in each of four methods. Adapters share only `base.py` and never
import each other.

They reach the window only through `devices.ModuleHost`, and the surface is
written down *there* because this docstring once listed it and was wrong.
`test_device_contracts` checks every `self.win.X` here against it.

Lifecycle, in call order:

    build_panel()      once -> settings tab (or None)
    build_plot()       once -> Signals tab (or None)
    build_views()      once -> central image / extra docks
    build_controller() at startup and whenever Emulate is toggled
    build_session()    per session: create the worker, but DO NOT start it
    start()            per session: only after the shared clock reached t=0
    update_display()   ~30 Hz while running
    attach_sink()      at record start;  detach_sink() at stop
    metadata()         at record start
    stop()             per session teardown
"""
from __future__ import annotations

from typing import Any, Callable

from acqApp import config
from acqApp.adapters.base import ModuleAdapter
from acqApp.adapters.closed_loop import ClosedLoopModule
from acqApp.adapters.dmd import DmdModule
from acqApp.adapters.puffer import PufferModule
from acqApp.adapters.pupil_cam import PupilCamModule
from acqApp.adapters.routines import RoutinesModule
from acqApp.adapters.stage import StageModule
from acqApp.adapters.voltage_cam import VoltageCamModule
from acqApp.adapters.wheel import WheelModule


# A new module needs a line here, one in config.MODULES, AND an accent colour in
# style.HEX — the third is easy to forget and shows up as a KeyError.
ADAPTERS: dict[str, Callable[[Any], ModuleAdapter]] = {
    "voltage_cam": VoltageCamModule,
    "pupil_cam":   PupilCamModule,
    "wheel":       WheelModule,
    "puffer":      PufferModule,
    "stage":       StageModule,
    "dmd":         DmdModule,
    "routines":    RoutinesModule,
    "closed_loop": ClosedLoopModule,
}


def build_adapters(win, enabled) -> list[ModuleAdapter]:
    """Adapters for the enabled modules, in config.MODULES display order."""
    return [ADAPTERS[k](win) for k in config.MODULES
            if k in enabled and k in ADAPTERS]


__all__ = ["ADAPTERS", "ModuleAdapter", "build_adapters"] + [
    c.__name__ for c in ADAPTERS.values()]
