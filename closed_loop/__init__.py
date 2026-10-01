"""Closed loop (phase 5) — fire an output from what an instrument is measuring.

`acq/sync.py`'s bus fires at a *time*; this fires on what the animal is doing.
`settings.py` is the decision (Qt-free), `worker.py` its thread at POLL_HZ,
`panel.py` the widgets and the arming switch.

- Its own thread: on the 30 Hz display tick a rule inherits every preview
  stall. It polls a non-consuming snapshot; `get_latest()` hands each sample
  out once, and the display is already that consumer.
- Actuation happens elsewhere: `fired` goes onto the trigger bus, the same
  path as a scheduled puff.
- Arming is not in `LoopSettings`, so it can't be persisted: a restored
  "armed" would fire the puffer at launch (as with the LED, audit #4).

Lazy re-exports (PEP 562) keep `acqApp.closed_loop.settings` importable
without Qt.
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
    globals()[name] = value          # once per name
    return value


def __dir__() -> list[str]:
    return __all__
