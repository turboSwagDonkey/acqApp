"""
Per-rig hardware profiles (rigs.json).

The scenario this exists to prevent: the DAQ wiring was hardcoded to one rig
(`Dev3/port0/line7` and friends), so the app on any other rig opened lines
that aren't there — a multi-line nidaqmx traceback at every startup, then a
silent mock fallback. Worse, the channel is also a *persisted* setting, so a
config carried between rigs could keep shadowing the profile with a stale
value from whichever rig it was last used on.

Two properties, both checked here:
  1. rigs.json is hand-edited, so a malformed one degrades to defaults rather
     than stopping the app (the `load_modes` policy, applied to profiles).
  2. The profile WINS over anything saved in acqapp_local.json — the whole
     point of the file, and the failure mode above.

Cheap and hardware-free: no QApplication for the config half, ~1 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_rigs.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report

from acqApp import config
from acqApp.devices.puffer.control import PufferSettings
from acqApp.devices.wheel.settings import EncoderSettings

FULL = {
    "ni_device": "Dev3",
    "hardware": {"puffer": True, "primary_led": True},
    "channels": {"puffer": "port0/line7", "primary_led": "port0/line2",
                 "wheel": "ai2"},
}
BARE = {
    "ni_device": "Dev2",
    "hardware": {"puffer": False, "primary_led": False},
    "channels": {"wheel": "ai2"},
}


def _write(tmp: Path, rigs: dict, active: str | None = None) -> None:
    """Point config at a temp rigs.json/acqapp_local.json pair."""
    config._RIGS_PATH = tmp / "rigs.json"
    config._CONFIG_PATH = tmp / "acqapp_local.json"
    config._RIGS_PATH.write_text(json.dumps(rigs), encoding="utf-8")
    config._CONFIG_PATH.write_text(
        json.dumps({"rig": active} if active else {}), encoding="utf-8")


def check_sanitize(r: Report, tmp: Path) -> None:
    _write(tmp, {
        "good": FULL,
        "not-a-dict": ["nope"],
        "bad-types": {"ni_device": 7, "hardware": None, "channels": ["x"]},
        "part-bad": {"ni_device": "Dev9",
                     "hardware": {"puffer": "yes", "primary_led": False},
                     "channels": {"puffer": "port0/line3", "wheel": 5}},
        "bad-dmd-cal": {"ni_device": "Dev2",
                        "dmd_calibration": {"model": "projective",
                                            "cross_frac": 4.0}},
    })
    rigs = config.load_rigs()
    r.check("good" in rigs and "bad-types" in rigs, "valid + salvageable kept")
    r.check("not-a-dict" not in rigs,
            "a non-dict profile is dropped, not raised")
    bad = rigs["bad-types"]
    r.check(bad["ni_device"] == config.DEFAULT_NI_DEVICE,
            "a non-str ni_device falls back to the default device")
    r.check(bad["hardware"] == {} and bad["channels"] == {},
            "null/list where a dict belongs becomes empty, not None")
    part = rigs["part-bad"]
    r.check(part["hardware"] == {"primary_led": False},
            "a non-bool hardware flag is dropped, the real bool kept")
    r.check(part["channels"] == {"puffer": "port0/line3"},
            "a non-str channel value is dropped, the str kept")
    dmd_cal = rigs["bad-dmd-cal"]["dmd_calibration"]
    r.check(dmd_cal == {"model": None, "cross_frac": None},
            "an unrecognized model name and an out-of-range cross_frac both "
            "fall back to None (\"use the module default\"), not raise")


def check_corrupt(r: Report, tmp: Path) -> None:
    _write(tmp, {})
    config._RIGS_PATH.write_text("{not json at all", encoding="utf-8")
    r.check(config.load_rigs() == {}, "an unreadable rigs.json loads as empty")
    r.check(config._RIGS_PATH.with_suffix(".corrupt.json").is_file(),
            "...and is quarantined, not discarded (the load_config policy)")


def check_resolution(r: Report, tmp: Path) -> None:
    _write(tmp, {"full": FULL}, active="full")
    r.check(config.rig_device() == "Dev3", "rig_device comes from the profile")
    r.check(config.rig_channel("puffer") == "Dev3/port0/line7",
            "a device-relative channel is qualified with the rig's device")
    r.check(config.rig_channel("nothing-here") is None,
            "an unnamed channel is None, so the caller keeps its own default")
    r.check(config.rig_has("puffer") is True, "a True hardware flag reads True")
    r.check(config.rig_has("never-listed") is True,
            "an unlisted flag defaults to fitted (pre-rigs.json behaviour)")

    _write(tmp, {"two-daq": {"ni_device": "Dev2",
                             "channels": {"wheel": "Dev4/ai0"}}},
           active="two-daq")
    r.check(config.rig_channel("wheel") == "Dev4/ai0",
            "a channel naming its own device is passed through, not prefixed")

    _write(tmp, {"full": FULL}, active="no-such-rig")
    r.check(config.rig_profile() == {} and config.rig_device() == "Dev3",
            "an unknown rig name falls back to defaults, not a half profile")


def check_profile_beats_saved(r: Report, tmp: Path) -> None:
    """The reason the file exists: a stale saved channel must not win."""
    _write(tmp, {"bare": BARE}, active="bare")
    cfg = json.loads(config._CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["settings"] = {"wheel":  {"channel": "Dev3/ai2"},   # from the old rig
                       "puffer": {"channel": "Dev3/port0/line7"}}
    config._CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")

    wheel = config.load_dataclass(EncoderSettings, "wheel")
    r.check(wheel.channel == "Dev2/ai2",
            "the rig profile overrides a channel saved from another rig")
    puffer = config.load_dataclass(PufferSettings, "puffer")
    r.check(puffer.channel == "",
            "hardware false blanks the line rather than aiming it somewhere")

    # Unrelated saved fields must still survive the override.
    cfg["settings"]["puffer"]["duration_s"] = 0.25
    config._CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    r.check(config.load_dataclass(PufferSettings, "puffer").duration_s == 0.25,
            "overriding the channel leaves the panel's other settings alone")

    # Only devices with a blank-channel path may be blanked: the encoder has
    # none, and EncoderWorker._add_channel would raise on "" inside its own
    # thread — so a hand-edited `wheel: false` must not reach it.
    _write(tmp, {"nowheel": {"ni_device": "Dev2",
                             "hardware": {"wheel": False},
                             "channels": {"wheel": "ai2"}}}, active="nowheel")
    r.check("wheel" not in config.RIG_BLANKABLE,
            "the wheel is not blankable (it has no not-fitted path)")
    r.check(config.load_dataclass(EncoderSettings, "wheel").channel
            == "Dev2/ai2",
            "so hardware false leaves the encoder aimed at real hardware")


def check_dmd_calibration(r: Report, tmp: Path) -> None:
    """A stable physical fact (camera tilt), not a per-session setting — see
    rig_dmd_calibration's docstring — so it lives in the profile, seeding the
    Calibration dialog's controls rather than being re-picked every run."""
    _write(tmp, {"tilted": {"ni_device": "Dev2",
                            "dmd_calibration": {"model": "homography",
                                                "cross_frac": 0.06}}},
           active="tilted")
    r.check(config.rig_dmd_calibration() == {"model": "homography",
                                             "cross_frac": 0.06},
            "an explicit override is passed straight through")

    _write(tmp, {"plain": {"ni_device": "Dev3"}}, active="plain")
    r.check(config.rig_dmd_calibration() == {"model": "affine",
                                             "cross_frac": None},
            "a rig that never mentions it gets affine / module-default cross "
            "-frac — byte-for-byte the pre-dmd_calibration behaviour")

    _write(tmp, {}, active="no-such-rig")
    r.check(config.rig_dmd_calibration() == {"model": "affine",
                                             "cross_frac": None},
            "no profile at all resolves the same way")


def check_no_profile_is_unchanged(r: Report, tmp: Path) -> None:
    """A machine with no rigs.json must behave exactly as it did before."""
    _write(tmp, {})
    cfg = {"settings": {"puffer": {"channel": "Dev3/port0/line1"}}}
    config._CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    s = config.load_dataclass(PufferSettings, "puffer")
    r.check(s.channel == "Dev3/port0/line1",
            "with no profile, the saved channel still wins (no regression)")
    config._CONFIG_PATH.write_text("{}", encoding="utf-8")
    r.check(config.load_dataclass(PufferSettings, "puffer").channel
            == PufferSettings().channel,
            "...and with nothing saved either, the dataclass default stands")


def check_puffer_skips_daq(r: Report) -> None:
    """An unfitted puffer must not touch nidaqmx at all."""
    from _harness import qt_app
    qt_app()
    from acqApp.devices.puffer.control import PufferController
    import builtins

    real_import, attempted = builtins.__import__, []

    def spy(name, *a, **kw):
        if name == "nidaqmx":
            attempted.append(name)
        return real_import(name, *a, **kw)

    builtins.__import__ = spy
    try:
        ctl = PufferController(PufferSettings(channel=""))
    finally:
        builtins.__import__ = real_import
    r.check(not attempted, "a blank channel never imports or opens nidaqmx")
    r.check(ctl._task is None, "...and leaves no task, so fire() no-ops")
    ctl.fire(0.01)          # must not raise
    r.check(True, "...and fire() on an unfitted puffer is silent, not an error")


def main() -> int:
    r = Report("rigs")
    saved = (config._RIGS_PATH, config._CONFIG_PATH)
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_rigs_"))
    try:
        check_sanitize(r, tmp)
        check_corrupt(r, tmp)
        check_resolution(r, tmp)
        check_dmd_calibration(r, tmp)
        check_profile_beats_saved(r, tmp)
        check_no_profile_is_unchanged(r, tmp)
        check_puffer_skips_daq(r)
    finally:
        config._RIGS_PATH, config._CONFIG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
