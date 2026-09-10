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
    "vis_stim":    "Visual stim",
    "mirror":      "PMT/camera mirror",
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
# Unlike acqapp_local.json (gitignored, the operator's own live working
# state), this one is meant to be hand-edited and carried between sessions
# or machines — so it is tracked, not gitignored. The sidebar's "Save as
# preset" button does write it (`save_modes`, from main.py's
# `_save_mode_as` — which re-reads the file first, so a hand-edit made
# since startup isn't clobbered) — everywhere else treats it as read-only,
# hand-curated input.
_MODES_PATH = Path(__file__).with_name("modes.json")


def _load_json(path: Path) -> dict:
    """The JSON object at `path`, or {} if missing/unreadable.

    A damaged file is moved aside, not discarded: returning {} is right (the
    app has to start), but the next save over `path` would then overwrite
    the only copy of whatever was in it, turning recoverable corruption into
    a loss. Shared by `load_config`/`load_modes` so both get this for free.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        keep = path.with_suffix(".corrupt.json")
        try:
            os.replace(path, keep)
            print(f"[config] {path.name} is unreadable ({e}); kept as "
                  f"{keep.name} and starting from defaults")
        except OSError:
            print(f"[config] {path.name} is unreadable ({e})")
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    """The saved config, or {} if there is none. See `_load_json`."""
    return _load_json(_CONFIG_PATH)


def _atomic_write_json(path: Path, data) -> None:
    """Write `data` to `path` atomically: temp file in the same directory,
    then rename. `open(path, "w")` truncates first, so a native death
    mid-write (a PyQt6 qFatal from a worker, a DCAM segfault) would otherwise
    leave a truncated file that the matching loader reads as empty.
    `os.replace` is atomic on Windows and POSIX alike.

    No fsync: the threat is a process crash, not power loss, and fsync on
    every write (acqapp_local.json rewrites on every spinbox step) would cost
    more than the write it protects.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[config] could not save {path}: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass


def save_config(cfg: dict) -> None:
    _atomic_write_json(_CONFIG_PATH, cfg)


# ── Cross-module "Mode" presets (the sidebar's Mode dropdown) ─────────────────
# modes.json, hand-edited by the operator and portable between sessions/rigs —
# see _MODES_PATH above. Each entry is {name: recipe}; MainWindow.set_mode()
# interprets the recipe, not this module — load_modes() only validates shape.
def load_modes() -> dict:
    """Named mode -> recipe dict, from modes.json ({} if missing/unreadable).

    A malformed file must not stop the app from starting — worst case is an
    empty Mode dropdown (just "None"), the same failure mode as a removed
    preset in load_dataclass. Non-dict entries are dropped rather than
    raising, so one bad hand-edit doesn't take out every mode. Also
    sanitizes each recipe's `camera_presets`/`camera_exposure_us`, the two
    fields `set_mode()` iterates (`.items()`) rather than merely truth-tests:
    a natural hand-edit mistake — `null` for "no camera presets", or a list
    by typo — would otherwise reach `set_mode()` as `None`/a list and raise
    there, since `dict.get(key, {})` only substitutes the default when the
    key is *absent*, not when it's present with a non-dict value.
    """
    data = _load_json(_MODES_PATH)
    modes = {}
    for name, recipe in data.items():
        if not isinstance(name, str) or not isinstance(recipe, dict):
            continue
        recipe = dict(recipe)          # don't mutate the parsed JSON in place
        presets = recipe.get("camera_presets")
        if presets is None:
            recipe.pop("camera_presets", None)
        elif isinstance(presets, dict):
            recipe["camera_presets"] = {
                k: v for k, v in presets.items()
                if isinstance(k, str) and isinstance(v, str)}
        else:
            recipe.pop("camera_presets")   # wrong type entirely -> drop it
        exposures = recipe.get("camera_exposure_us")
        if exposures is None:
            recipe.pop("camera_exposure_us", None)
        elif isinstance(exposures, dict):
            recipe["camera_exposure_us"] = {
                k: v for k, v in exposures.items()
                if isinstance(k, str) and isinstance(v, (int, float))
                and not isinstance(v, bool)}
        else:
            recipe.pop("camera_exposure_us")   # wrong type entirely -> drop it
        modes[name] = recipe
    return modes


def save_modes(modes: dict) -> None:
    """Overwrite modes.json with `modes` in full (not merged) — the whole
    file IS the set of modes, unlike acqapp_local.json's namespaced sections.
    Called by the sidebar's "Save as preset" action; still a plain text file
    afterwards, so a further hand-edit remains just as valid the next time
    the app loads it."""
    _atomic_write_json(_MODES_PATH, modes)


def order_modules(keys) -> list[str]:
    """`keys` restricted to known modules, in MODULES order, dropping anything
    unknown/removed, and with ALWAYS_ON modules always included — a config
    written before a module became always-on would not name it."""
    keys = set(keys)
    return [k for k in MODULES if k in keys or k in ALWAYS_ON]


def load_enabled_modules() -> list[str]:
    """Last-used module keys (validated + ordered). Defaults to all modules."""
    saved = load_config().get("enabled_modules")
    if not isinstance(saved, list):
        return list(MODULES)
    return order_modules(saved)


def save_enabled_modules(enabled: list[str]) -> None:
    cfg = load_config()
    cfg["enabled_modules"] = order_modules(enabled)
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
