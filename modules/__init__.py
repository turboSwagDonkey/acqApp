"""
Per-subsystem wiring for the main window.

Every instrument needs the same five things done to it, in the same order, every
session: build a settings panel, build a worker, update a display each tick,
attach a recording sink, and contribute metadata to the session file. Spelling
that out inline meant `MainWindow` carried six near-identical `if "foo" in
enabled:` branches in each of four methods — adding an instrument meant four
edits in four places, and the branches had already drifted apart.

Here each subsystem is one `ModuleAdapter` subclass that owns its whole
lifecycle, and `MainWindow` just iterates. Adding an instrument is a new class
plus one line in `ADAPTERS`.

The adapter talks to the window through a narrow surface — it never reaches
into the window's widgets, so the two stay independently readable. That surface
is `devices.ModuleHost`, and it is written down there rather than listed here
because this sentence used to list it and was already wrong: it named seven
members while the code used nine. `test_device_contracts` now checks every
`self.win.X` in this package against that protocol, so widening the surface is
a deliberate line in `devices.py` rather than something that just happens.

Layout
------
This was one 1200-line file until the closed loop pushed it 28% past the size
that prompted the question. It is now one file per instrument, because that is
the unit people actually work in: a session at the rig is spent on *the wheel*
or *the DMD*, and every adapter is independent of its neighbours by
construction — they share only `base.py` and never import each other.

    base.py         `ModuleAdapter`, the two shared widget builders, and the
                    plot/preview constants
    voltage_cam.py  pupil_cam.py  wheel.py  puffer.py  stage.py  dmd.py
    closed_loop.py  the rule's adapter (the rule itself is acqApp/closed_loop.py)

Callers see no difference: `modules.build_adapters`, `modules.ADAPTERS` and
`modules.ModuleAdapter` are still exactly where they were.

Lifecycle, in call order
------------------------
    build_panel()      once, at startup -> the settings tab (or None)
    build_plot()       once, at startup -> the Signals tab (or None)
    build_views()      once, at startup -> central image / extra docks
    build_controller() at startup and whenever Emulate is toggled
    build_session()    per session: create the worker, but DO NOT start it
    start()            per session: start the worker — only after the shared
                       clock has reached t=0, so no sample is ever stamped
                       against an unstarted clock
    update_display()   ~30 Hz while running
    attach_sink()      when recording starts;  detach_sink() when it stops
    metadata()         when recording starts
    stop()             per session teardown
"""
from __future__ import annotations

from typing import Any, Callable

from acqApp import config
from acqApp.modules.base import ModuleAdapter
from acqApp.modules.closed_loop import ClosedLoopModule
from acqApp.modules.dmd import DmdModule
from acqApp.modules.puffer import PufferModule
from acqApp.modules.pupil_cam import PupilCamModule
from acqApp.modules.stage import StageModule
from acqApp.modules.voltage_cam import VoltageCamModule
from acqApp.modules.wheel import WheelModule


# ── registry ──────────────────────────────────────────────────────────────────
# Keys must match config.MODULES; the window builds adapters in MODULES order.
# A new module needs a line here, one in config.MODULES, AND an accent colour
# in style.HEX — the third is easy to forget and shows up as a KeyError.

ADAPTERS: dict[str, Callable[[Any], ModuleAdapter]] = {
    "voltage_cam": VoltageCamModule,
    "pupil_cam":   PupilCamModule,
    "wheel":       WheelModule,
    "puffer":      PufferModule,
    "stage":       StageModule,
    "dmd":         DmdModule,
    "closed_loop": ClosedLoopModule,
}


def build_adapters(win, enabled) -> list[ModuleAdapter]:
    """Adapters for the enabled modules, in config.MODULES display order."""
    return [ADAPTERS[k](win) for k in config.MODULES
            if k in enabled and k in ADAPTERS]


__all__ = ["ADAPTERS", "ModuleAdapter", "build_adapters"] + [
    c.__name__ for c in ADAPTERS.values()]
