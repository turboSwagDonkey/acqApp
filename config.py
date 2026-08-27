"""
Persistent app config: the loaded modules, the theme, and every panel's saved
parameters.

JSON next to the package (`acqapp_local.json`, gitignored via `*_local.json`).
It is the operator's whole working setup, and it is rewritten **on every
spinbox step**, so `save_config` writes atomically and `load_config` refuses to
throw a damaged one away — see both.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The subsystems the user can load, in display order.
# key → human-readable label shown in the startup picker.
MODULES: dict[str, str] = {
    "voltage_cam": "Voltage camera",
    "pupil_cam":   "Pupil camera",
    "wheel":       "Wheel encoder",
    "puffer":      "Puffer",
    "stage":       "XY stage",
    "dmd":         "DMD",
    # Not an instrument either: it drives the stage and the DMD through a
    # protocol the operator wrote. Before closed_loop so that stays last.
    "routines":    "Experiment routines",
    # Not an instrument: it watches one module's signal and fires another's
    # output. Must come last — its panel asks the window what sources exist,
    # and the adapters are built in this order.
    "closed_loop": "Closed loop",
}

# Modules that are not the operator's to switch off, so they carry no
# checkbox in the picker and `set_modules` puts them back if a caller drops
# them. `routines` is here because it owns no device: it *drives* the
# instruments that are optional, and an operator who unticked it would lose
# the protocol they had written rather than free anything up.
ALWAYS_ON: frozenset[str] = frozenset({"routines"})

_CONFIG_PATH = Path(__file__).with_name("acqapp_local.json")


def load_config() -> dict:
    """The saved config, or {} if there is none.

    A damaged file is moved aside, not discarded: returning {} is right (the app
    has to start), but the next `save_config` would then overwrite the only copy
    of the operator's setup, turning recoverable corruption into a loss.
    """
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        keep = _CONFIG_PATH.with_suffix(".corrupt.json")
        try:
            os.replace(_CONFIG_PATH, keep)
            print(f"[config] {_CONFIG_PATH.name} is unreadable ({e}); kept as "
                  f"{keep.name} and starting from defaults")
        except OSError:
            print(f"[config] {_CONFIG_PATH.name} is unreadable ({e})")
        return {}
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    """Write the config atomically: temp file in the same directory, then rename.

    `open(path, "w")` truncates first, and this file is rewritten on every
    spinbox step — so a native death mid-write (a PyQt6 qFatal from a worker, a
    DCAM segfault) left a truncated file that `load_config` read as "no settings
    at all". `os.replace` is atomic on Windows and POSIX alike.

    No fsync: the threat is a process crash, not power loss, and fsync per
    spinbox step would cost more than the write it protects.
    """
    tmp = _CONFIG_PATH.with_name(f"{_CONFIG_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, _CONFIG_PATH)
    except OSError as e:
        print(f"[config] could not save {_CONFIG_PATH}: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass


def load_enabled_modules() -> list[str]:
    """Last-used module keys (validated + ordered). Defaults to all modules."""
    saved = load_config().get("enabled_modules")
    if not isinstance(saved, list):
        return list(MODULES)
    # keep MODULES order, drop anything unknown/removed, and put ALWAYS_ON back
    # — a config written before a module became always-on would not name it.
    return [k for k in MODULES if k in saved or k in ALWAYS_ON]


def save_enabled_modules(enabled: list[str]) -> None:
    cfg = load_config()
    cfg["enabled_modules"] = [k for k in MODULES
                             if k in enabled or k in ALWAYS_ON]
    save_config(cfg)


# ── App-wide preferences (top-level keys) ─────────────────────────────────────
DEFAULT_THEME = "dark"


def get_theme() -> str:
    """Return the saved UI theme ('dark' or 'light'); defaults to dark."""
    t = load_config().get("theme")
    return t if t in ("dark", "light") else DEFAULT_THEME


def set_theme(theme: str) -> None:
    cfg = load_config()
    cfg["theme"] = "dark" if theme == "dark" else "light"
    save_config(cfg)


# ── Per-module settings (namespaced under "settings") ─────────────────────────
# Persist a panel's *parameters* (exposure, preset, thresholds …) across runs —
# not transient runtime state (LED on/off, recording, scheduled events).
def load_settings(module: str) -> dict:
    """Saved settings dict for `module` (empty if none). Callers should treat
    every key as optional and validate values (a preset may have been removed)."""
    section = load_config().get("settings")
    if isinstance(section, dict) and isinstance(section.get(module), dict):
        return dict(section[module])
    return {}


def save_settings(module: str, values: dict) -> None:
    cfg = load_config()
    section = cfg.get("settings")
    if not isinstance(section, dict):
        section = {}
    section[module] = dict(values)
    cfg["settings"] = section
    save_config(cfg)


def load_dataclass(cls, module: str):
    """Rebuild a settings dataclass from `module`'s saved JSON.

    Unknown or stale keys are dropped rather than raising, so removing a field
    from the dataclass (or hand-editing the JSON) can never stop the app from
    starting — the worst case is falling back to the defaults.
    """
    saved = load_settings(module)
    kwargs = {k: v for k, v in saved.items() if k in cls.__dataclass_fields__}
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()
