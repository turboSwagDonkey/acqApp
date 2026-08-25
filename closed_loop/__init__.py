"""Closed loop (phase 5) — fire an output from what an instrument is measuring.

`acq/sync.py`'s bus fires at a *time*; this fires on what the animal is doing.
`settings.py` is the decision (Qt-free), `worker.py` its thread at POLL_HZ,
`panel.py` the widgets and the arming switch.

- **Its own thread**, because a rule on the 30 Hz display tick inherits every
  preview stall. It polls a non-consuming snapshot — `get_latest()` hands each
  sample out once, and the display is already that consumer.
- **The actuation is not on this thread**: the worker emits `fired`, the adapter
  re-emits it on the trigger bus, so a rule-driven puff takes the same path as a
  scheduled one.
- **Arming is deliberately not in `LoopSettings`**, so it cannot be persisted —
  as with the LED in audit #4, a restored "armed" fires the puffer at launch.

Re-exported lazily (PEP 562) so `acqApp.closed_loop.settings` stays importable
without Qt; eager re-exports would pull PyQt6 in through the parent package.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "COMPARISONS":      "settings",
    "POLL_HZ":          "settings",
    "TARGETS":          "settings",
    "LoopRule":         "settings",
    "LoopSettings":     "settings",
    "SignalSource":     "settings",
    "ClosedLoopWorker": "worker",
    "SettingsPanel":    "panel",
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
