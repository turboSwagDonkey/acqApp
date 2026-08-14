"""Closed loop — fire an output from what an instrument is measuring.

Phase 5. `acq/sync.py`'s bus fires named events at a *time*; this is the other kind,
depending on what the animal is doing.

    settings.py   `SignalSource`, `LoopSettings`, `LoopRule` — the decision,
                  pure and Qt-free
    worker.py     `ClosedLoopWorker`, its own thread at POLL_HZ
    panel.py      the rule's widgets and the arming switch

**Why a thread.** A rule on the 30 Hz display tick inherits every stall the
camera previews have. It *polls* rather than consumes: `get_latest()` hands each
sample out once and the display is already that consumer, so the loop reads a
non-consuming snapshot.

**The decision is on the loop thread; the actuation is not.** The worker emits
`fired` and the adapter re-emits it on the trigger bus, so a rule-driven puff
takes the identical path to a scheduled one.

**Arming is deliberately absent from `LoopSettings`** so it cannot be persisted
— as with the LED in audit #4, restoring "armed" means a rule that fires the
puffer at next launch, before anyone checked the threshold.

Re-exported lazily (PEP 562) so `from acqApp.closed_loop import LoopRule` keeps
working, while `acqApp.closed_loop.settings` stays importable without Qt —
eager re-exports here would run `panel.py` and pull PyQt6 in through the parent
package, which is exactly what splitting the rule out was for.
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
