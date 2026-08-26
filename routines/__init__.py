"""Experiment routines — run a protocol step by step, unattended.

"100 frames at this stage position with this DMD pattern, move, another 100
with a different pattern, repeat." `settings.py` is the protocol and its
validation (Qt-free), `engine.py` the executor over callables (Qt-free),
`worker.py` the thread that ticks it, `panel.py` the widgets.

**It is the first feature whose whole purpose is to actuate**, so the split is
the point: everything that decides is in the two Qt-free halves and is driven
against fakes by `tests/test_routines.py`, and the only code that touches a
real stage or projector is the adapter's hooks.

Re-exported lazily (PEP 562), as in `closed_loop/`: an eager re-export would
pull PyQt6 in through the parent package and cost the Qt-free halves their
whole point.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "MAX_SETTLE_S":   "settings",
    "SAVE_MODES":     "settings",
    "UNITS":          "settings",
    "RigLimits":      "settings",
    "Routine":        "settings",
    "Step":           "settings",
    "validate":       "settings",
    "MOVE_TIMEOUT_S": "engine",
    "Phase":          "engine",
    "RoutineEngine":  "engine",
    "RoutineError":   "engine",
    "RoutineHooks":   "engine",
    "StepRun":        "engine",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    where = _LAZY.get(name)
    if where is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{where}"), name)
    globals()[name] = value          # cache, so this runs once per name
    return value


def __dir__() -> list[str]:
    return __all__
