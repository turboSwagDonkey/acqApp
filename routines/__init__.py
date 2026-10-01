"""Experiment routines — run a protocol step by step, unattended.

`settings.py` is the protocol and its validation, `engine.py` the executor
over callables; both are Qt-free and driven against fakes by
`tests/test_routines.py`. `panel.py`/`table.py` are the widgets, and only
`adapters/routines.py` (which owns the ticking QTimer) touches real devices.
The split matters because this feature's whole purpose is to actuate.

Re-exported lazily (PEP 562), as in `closed_loop/`: an eager re-export would
pull PyQt6 in through the parent package.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "MAX_SETTLE_S":   "settings",
    "SAVE_MODES":     "settings",
    "UNITS":          "settings",
    "KINDS":          "settings",
    "RigLimits":      "settings",
    "Routine":        "settings",
    "Step":           "settings",
    "Group":          "settings",
    "Recording":      "settings",
    "validate":       "settings",
    "MOVE_TIMEOUT_S": "engine",
    "Phase":          "engine",
    "RoutineEngine":  "engine",
    "RoutineError":   "engine",
    "RoutineHooks":   "engine",
    "RecordingRun":   "engine",
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
