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
    sanitizes each recipe's `camera_presets`/`camera_exposure_us`/
    `camera_trigger`/`camera_binning`, the four fields `set_mode()` iterates
    (`.items()`) rather than merely truth-tests: a natural hand-edit mistake
    — `null` for "no camera presets", or a list by typo — would otherwise
    reach `set_mode()` as `None`/a list and raise there, since
    `dict.get(key, {})` only substitutes the default when the key is
    *absent*, not when it's present with a non-dict value. `dmd_sub_sampling`
    is sanitized too, even though `set_mode()` only reads it (no `.items()`)
    — it flows into `int(sub_sampling)`, which raises on a str/list where
    `dmd_all_on`'s plain truth-test never would.
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
        triggers = recipe.get("camera_trigger")
        if triggers is None:
            recipe.pop("camera_trigger", None)
        elif isinstance(triggers, dict):
            recipe["camera_trigger"] = {
                k: v for k, v in triggers.items()
                if isinstance(k, str) and isinstance(v, bool)}
        else:
            recipe.pop("camera_trigger")   # wrong type entirely -> drop it
        binnings = recipe.get("camera_binning")
        if binnings is None:
            recipe.pop("camera_binning", None)
        elif isinstance(binnings, dict):
            recipe["camera_binning"] = {
                k: v for k, v in binnings.items()
                if isinstance(k, str) and isinstance(v, int)
                and not isinstance(v, bool)}
        else:
            recipe.pop("camera_binning")   # wrong type entirely -> drop it
        sub_sampling = recipe.get("dmd_sub_sampling")
        if sub_sampling is None:
            recipe.pop("dmd_sub_sampling", None)
        elif (isinstance(sub_sampling, (int, float))
              and not isinstance(sub_sampling, bool)):
            recipe["dmd_sub_sampling"] = sub_sampling
        else:
            recipe.pop("dmd_sub_sampling")   # wrong type entirely -> drop it
        modes[name] = recipe
    return modes


def save_modes(modes: dict) -> None:
    """Overwrite modes.json with `modes` in full (not merged) — the whole
    file IS the set of modes, unlike acqapp_local.json's namespaced sections.
    Called by the sidebar's "Save as preset" action; still a plain text file
    afterwards, so a further hand-edit remains just as valid the next time
    the app loads it."""
    _atomic_write_json(_MODES_PATH, modes)


# ── Per-rig hardware profiles (rigs.json) ────────────────────────────────────
# Which DAQ this machine has and what is wired to it. Tracked and hand-edited
# like modes.json (never written by the app), because the profiles describe
# rigs the lab owns and should travel with the repo; which of them THIS
# machine is lives in acqapp_local.json ("rig"), which is gitignored and so
# stays put. Defaults below are the pre-rigs.json hardcoded wiring, so a
# machine with no rigs.json and no "rig" key behaves exactly as before.
_RIGS_PATH = Path(__file__).with_name("rigs.json")

DEFAULT_NI_DEVICE = "Dev3"

# module key -> (channels key in the rig profile, dataclass field to set).
# Only DAQ-addressed modules whose channel is a *settings field* appear here;
# the two LED channels are constructor arguments, so their adapters call
# `rig_channel` directly.
RIG_CHANNEL_FIELDS: dict[str, tuple[str, str]] = {
    "puffer": ("puffer", "channel"),
    "wheel":  ("wheel",  "channel"),
}

# Of those, the ones whose device treats a blank channel as "not fitted" and
# skips the DAQ open (PufferController._open). `hardware: false` only blanks
# the channel for these: EncoderWorker._add_channel would raise on "" from
# inside its worker thread, so for the wheel the flag stays advisory and the
# channel keeps pointing at real hardware. Widening this set means giving the
# device a blank-channel path first.
RIG_BLANKABLE: frozenset[str] = frozenset({"puffer"})

# NI channel-space prefixes. A profile channel is normally device-relative
# ("port0/line7") so the device name lives in exactly one place per rig, but
# a value naming its own device ("Dev4/ai0" — a second DAQ) is passed through
# untouched. Distinguishing them by prefix beats guessing at device names,
# which NI MAX lets the operator rename to anything.
_CHANNEL_SPACES = ("port", "line", "ai", "ao", "ctr", "PFI", "di", "do")


def load_rigs() -> dict:
    """Named rig -> profile dict, from rigs.json ({} if missing/unreadable).

    Sanitized the same way and for the same reason as `load_modes`: this file
    is hand-edited, a malformed one must not stop the app from starting, and
    the fields below are read with `.get(...)` + `.items()` downstream, where
    a present-but-wrong-typed value (`"hardware": null` for "nothing fitted",
    a list by typo) would raise rather than fall back. A profile that survives
    validation always has all four keys, so callers never guard for absence.
    """
    data = _load_json(_RIGS_PATH)
    rigs = {}
    for name, profile in data.items():
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
            # `is True`/`is False` rather than truthiness: a non-bool here is a
            # hand-edit mistake, and `rig_has` treats anything it doesn't
            # recognise as "fitted" — the pre-rigs.json behaviour.
            "hardware": {k: v for k, v in hardware.items()
                         if isinstance(k, str) and isinstance(v, bool)}
                        if isinstance(hardware, dict) else {},
            # A stable physical fact about the rig's camera mount, not a
            # per-session setting — a camera tilted enough that the DMD
            # relay stops being close to straight-on needs the full
            # projective fit every time, so it belongs here rather than
            # being re-picked in the calibration dialog each run. Both
            # default to None ("use the module default") rather than a
            # concrete value, so a rig that never mentions this is byte-for-
            # byte the pre-dmd_calibration behaviour.
            "dmd_calibration": {
                "model": model if model in ("affine", "homography") else None,
                "cross_frac": float(cross_frac)
                             if isinstance(cross_frac, (int, float))
                             and 0.0 < cross_frac <= 1.0 else None,
            },
        }
    return rigs


def active_rig() -> str:
    """This machine's rig name (from acqapp_local.json), "" if unset."""
    name = load_config().get("rig")
    return name if isinstance(name, str) else ""


def set_active_rig(name: str) -> None:
    cfg = load_config()
    cfg["rig"] = name
    save_config(cfg)


def rig_profile() -> dict:
    """The active rig's validated profile, or {} when unset or not in
    rigs.json. {} means "no profile", and every accessor below then returns
    the pre-rigs.json default rather than something half-applied."""
    return load_rigs().get(active_rig(), {})


def rig_device() -> str:
    """The NI device name for this rig (e.g. "Dev2")."""
    return rig_profile().get("ni_device") or DEFAULT_NI_DEVICE


def rig_channel(key: str) -> str | None:
    """Fully-qualified DAQ channel for logical `key` ("puffer", "wheel",
    "pupil_led", "primary_led"), or None when this rig doesn't name it —
    in which case the caller keeps whatever default it already had.
    """
    chan = rig_profile().get("channels", {}).get(key)
    if not chan:
        return None
    head = chan.split("/", 1)[0]
    return (f"{rig_device()}/{chan}" if head.startswith(_CHANNEL_SPACES)
            else chan)


def rig_has(key: str) -> bool:
    """Whether this rig has hardware `key` fitted.

    Unset/unknown is True, so a profile that lists no flags — or no profile at
    all — behaves exactly as the app did before rigs.json existed. Only False
    is a claim, and it is the operator's claim, not a probe: it suppresses the
    DAQ attempt entirely, which is the point (an absent device otherwise
    surfaces as a multi-line nidaqmx traceback at every startup).
    """
    fitted = rig_profile().get("hardware", {}).get(key)
    return fitted if isinstance(fitted, bool) else True


def rig_dmd_calibration() -> dict:
    """{"model": "affine"|"homography", "cross_frac": float|None} for this
    rig's DMD stripe-sweep calibration — seeds the Calibration dialog's
    controls, which the operator can still override for one run. `cross_frac`
    of None means "use calibration.py's own default (25% of the panel)";
    `model` always comes back a valid name, defaulting to "affine" (every
    rig before this key existed, and any rig that never sets it)."""
    d = rig_profile().get("dmd_calibration", {})
    return {"model": d.get("model") or "affine", "cross_frac": d.get("cross_frac")}


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
    # The rig profile wins over anything saved. acqapp_local.json is this
    # machine's live working state and can hold a channel left over from
    # whatever rig the config was last used on, which would silently shadow
    # rigs.json — exactly the confusion the profiles exist to prevent. An
    # in-panel edit still applies for the session; it just doesn't outlive it.
    entry = RIG_CHANNEL_FIELDS.get(module)
    if entry is not None:
        rig_key, field = entry
        if field in cls.__dataclass_fields__:
            if not rig_has(rig_key) and rig_key in RIG_BLANKABLE:
                # Not fitted on this rig: blank the line rather than point it
                # at a plausible-looking one. Callers treat "" as "no hardware"
                # and skip the DAQ open (see PufferController._open).
                kwargs[field] = ""
            elif (chan := rig_channel(rig_key)):
                kwargs[field] = chan
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()
