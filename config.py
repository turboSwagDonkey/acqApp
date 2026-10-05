"""
Persistent app config: loaded modules, theme, every panel's saved parameters.

`acqapp_local.json` (gitignored) is rewritten on every spinbox step, so saves
are atomic and a damaged file is moved aside, never discarded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# key -> label in the startup picker, in display (and build) order.
MODULES: dict[str, str] = {
    "voltage_cam": "Voltage camera",
    "pupil_cam":   "Pupil camera",
    "wheel":       "Wheel encoder",
    "puffer":      "Puffer",
    "stage":       "XY stage",
    "dmd":         "DMD",
    "vis_stim":    "Visual stim",
    "mirror":      "PMT/camera mirror",
    "routines":    "Experiment routines",
}

# No picker checkbox; owns no device, so unticking it would only lose the
# operator's protocol.
ALWAYS_ON: frozenset[str] = frozenset({"routines"})

_CONFIG_PATH = Path(__file__).with_name("acqapp_local.json")
# Tracked and hand-edited; the app writes it only from "Save as preset".
_MODES_PATH = Path(__file__).with_name("modes.json")


def _load_json(path: Path) -> dict:
    """The JSON object at `path`, or {}. A damaged file is moved aside, or
    the next save would overwrite the only copy."""
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
    return _load_json(_CONFIG_PATH)


def _atomic_write_json(path: Path, data) -> None:
    """Temp file + os.replace, so a native crash mid-write can't leave a
    truncated file. No fsync: the threat is a crash, not power loss."""
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


# ── Cross-module "Mode" presets (modes.json) ──────────────────────────────────
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# Recipe fields set_mode() iterates, with the value type each map must hold.
_RECIPE_MAPS = {
    "camera_presets":     lambda v: isinstance(v, str),
    "camera_rate_hz":     _is_num,
    "camera_exposure_us": _is_num,      # legacy; set_mode reads it as a rate
    "camera_trigger":     lambda v: isinstance(v, bool),
    "camera_binning":     lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def load_modes() -> dict:
    """Named mode -> recipe. Hand-edited, so wrongly typed fields are dropped
    rather than left to raise in set_mode() (`get(key, {})` doesn't cover a
    present-but-null key)."""
    modes = {}
    for name, recipe in _load_json(_MODES_PATH).items():
        if not isinstance(name, str) or not isinstance(recipe, dict):
            continue
        recipe = dict(recipe)
        for key, ok in _RECIPE_MAPS.items():
            m = recipe.pop(key, None)
            if isinstance(m, dict):
                recipe[key] = {k: v for k, v in m.items()
                               if isinstance(k, str) and ok(v)}
        sub = recipe.pop("dmd_sub_sampling", None)
        if _is_num(sub):
            recipe["dmd_sub_sampling"] = sub
        modes[name] = recipe
    return modes


def save_modes(modes: dict) -> None:
    """Overwrite modes.json in full — the file is the set of modes."""
    _atomic_write_json(_MODES_PATH, modes)


# ── Per-rig hardware profiles (rigs.json) ────────────────────────────────────
# Tracked and hand-edited; which rig THIS machine is lives in
# acqapp_local.json ("rig"). No profile = the original hardcoded wiring.
_RIGS_PATH = Path(__file__).with_name("rigs.json")

DEFAULT_NI_DEVICE = "Dev3"

# module -> (rig channel key, settings field). The LEDs take their channel as
# a constructor argument and call `rig_channel` directly.
RIG_CHANNEL_FIELDS: dict[str, tuple[str, str]] = {
    "puffer": ("puffer", "channel"),
    "wheel":  ("wheel",  "channel"),
}

# Devices that treat a blank channel as "not fitted". Not the wheel: its
# worker raises on "" inside its thread.
RIG_BLANKABLE: frozenset[str] = frozenset({"puffer"})

# A channel starting with one of these is device-relative ("port0/line7");
# anything else names its own device ("Dev4/ai0").
_CHANNEL_SPACES = ("port", "line", "ai", "ao", "ctr", "PFI", "di", "do")


def load_rigs() -> dict:
    """Named rig -> profile, sanitized like modes; every surviving profile
    has all four keys."""
    rigs = {}
    for name, profile in _load_json(_RIGS_PATH).items():
        if not isinstance(name, str) or not isinstance(profile, dict):
            continue
        device = profile.get("ni_device")
        channels = profile.get("channels")
        hardware = profile.get("hardware")
        dmd_cal = profile.get("dmd_calibration")
        model = dmd_cal.get("model") if isinstance(dmd_cal, dict) else None
        cross_frac = dmd_cal.get("cross_frac") if isinstance(dmd_cal, dict) else None
        rigs[name] = {
            "ni_device": device if isinstance(device, str) and device
                         else DEFAULT_NI_DEVICE,
            "channels": {k: v for k, v in channels.items()
                         if isinstance(k, str) and isinstance(v, str) and v}
                        if isinstance(channels, dict) else {},
            # Bools only; anything else falls back to "fitted" in rig_has.
            "hardware": {k: v for k, v in hardware.items()
                         if isinstance(k, str) and isinstance(v, bool)}
                        if isinstance(hardware, dict) else {},
            # None = the module default.
            "dmd_calibration": {
                "model": model if model in ("affine", "homography") else None,
                "cross_frac": float(cross_frac)
                             if isinstance(cross_frac, (int, float))
                             and 0.0 < cross_frac <= 1.0 else None,
            },
        }
    return rigs


def active_rig() -> str:
    name = load_config().get("rig")
    return name if isinstance(name, str) else ""


def rig_profile() -> dict:
    """The active rig's profile, or {} (every accessor then uses defaults)."""
    return load_rigs().get(active_rig(), {})


def rig_device() -> str:
    return rig_profile().get("ni_device") or DEFAULT_NI_DEVICE


def rig_channel(key: str) -> str | None:
    """Fully-qualified DAQ channel for `key`, or None to keep the default."""
    profile = rig_profile()
    chan = profile.get("channels", {}).get(key)
    if not chan:
        return None
    if not chan.split("/", 1)[0].startswith(_CHANNEL_SPACES):
        return chan
    return f"{profile.get('ni_device') or DEFAULT_NI_DEVICE}/{chan}"


def rig_has(key: str) -> bool:
    """Unset means fitted. Only an explicit False skips the DAQ open."""
    fitted = rig_profile().get("hardware", {}).get(key)
    return fitted if isinstance(fitted, bool) else True


def rig_dmd_calibration() -> dict:
    """{"model", "cross_frac"} seeding the calibration dialog; cross_frac
    None = calibration.py's default."""
    d = rig_profile().get("dmd_calibration", {})
    return {"model": d.get("model") or "affine", "cross_frac": d.get("cross_frac")}


def order_modules(keys) -> list[str]:
    """Known modules only, in MODULES order, ALWAYS_ON included."""
    keys = set(keys)
    return [k for k in MODULES if k in keys or k in ALWAYS_ON]


def load_enabled_modules() -> list[str]:
    saved = load_config().get("enabled_modules")
    if not isinstance(saved, list):
        return list(MODULES)
    return order_modules(saved)


def save_enabled_modules(enabled: list[str]) -> None:
    cfg = load_config()
    cfg["enabled_modules"] = order_modules(enabled)
    save_config(cfg)


# ── App-wide preferences ──────────────────────────────────────────────────────
DEFAULT_THEME = "dark"


def get_theme() -> str:
    t = load_config().get("theme")
    return t if t in ("dark", "light") else DEFAULT_THEME


def set_theme(theme: str) -> None:
    cfg = load_config()
    cfg["theme"] = "dark" if theme == "dark" else "light"
    save_config(cfg)


# ── Per-module settings (under "settings") ────────────────────────────────────
def load_settings(module: str) -> dict:
    """Every key optional; callers validate values."""
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
    """Rebuild a settings dataclass; unknown keys are dropped, and a bad
    value falls back to defaults rather than stop startup."""
    saved = load_settings(module)
    kwargs = {k: v for k, v in saved.items() if k in cls.__dataclass_fields__}
    # The rig profile beats a saved channel, which may be left over from
    # another rig.
    entry = RIG_CHANNEL_FIELDS.get(module)
    if entry is not None:
        rig_key, field = entry
        if field in cls.__dataclass_fields__:
            if not rig_has(rig_key) and rig_key in RIG_BLANKABLE:
                kwargs[field] = ""          # "" = no hardware, DAQ open skipped
            elif (chan := rig_channel(rig_key)):
                kwargs[field] = chan
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()
