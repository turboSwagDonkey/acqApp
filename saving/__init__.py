"""Session save destination: the path model, and the Save tab.

    config.py   `SaveConfig`, `sanitize`, `resolve` — no Qt
    panel.py    `SavePanel`

Lazy (PEP 562), so `acqApp.saving.config` imports without Qt while
`from acqApp.saving import SaveConfig` keeps working.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "SaveConfig":     "config",
    "TOKENS":         "config",
    "sanitize":       "config",
    "list_drives":    "config",
    "free_bytes":     "config",
    "default_folder": "config",
    "SavePanel":      "panel",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    where = _LAZY.get(name)
    if where is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{where}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
