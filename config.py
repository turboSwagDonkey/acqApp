"""
Persistent app config — currently just which modules the user last loaded.

Stored as JSON next to the package (`acqapp_local.json`, gitignored via the
`*_local.json` rule) so the startup module picker can default to the most
recently used selection.
"""
from __future__ import annotations

import json
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
}

_CONFIG_PATH = Path(__file__).with_name("acqapp_local.json")


def load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError as e:
        print(f"[config] could not save {_CONFIG_PATH}: {e}")


def load_enabled_modules() -> list[str]:
    """Last-used module keys (validated + ordered). Defaults to all modules."""
    saved = load_config().get("enabled_modules")
    if not isinstance(saved, list):
        return list(MODULES)
    # keep MODULES order and drop anything unknown/removed
    return [k for k in MODULES if k in saved]


def save_enabled_modules(enabled: list[str]) -> None:
    cfg = load_config()
    cfg["enabled_modules"] = [k for k in MODULES if k in enabled]
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
