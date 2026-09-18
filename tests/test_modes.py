"""
Cross-module "Mode" recipes (modes.json), loaded by `config.load_modes`/
`save_modes` and applied by `MainWindow.set_mode` (main.py).

A recipe is hand-edited JSON, so `load_modes` sanitizes it field by field the
same way `load_rigs` does (test_rigs.py's `check_sanitize`) — a malformed
entry must degrade to "skip that one setting", never crash the app on
startup. This covers the fields `set_mode()` reads: `camera_presets`,
`camera_exposure_us`, `camera_binning`, `camera_trigger` (added for the
"Scan" mode — internal trigger, full frame, 1x1 binning, ~30 Hz) and
`dmd_sub_sampling` (added alongside it — full DMD display at 1-in-2
sub-sampling, half the light of plain All ON).

Cheap and hardware-free: no QApplication, ~0.5 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_modes.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report

from acqApp import config


def _write(tmp: Path, modes: dict) -> None:
    """Point config at a temp modes.json."""
    config._MODES_PATH = tmp / "modes.json"
    config._MODES_PATH.write_text(json.dumps(modes), encoding="utf-8")


def check_sanitize(r: Report, tmp: Path) -> None:
    _write(tmp, {
        "good": {
            "dmd_all_on": True,
            "dmd_sub_sampling": 2,
            "camera_presets": {"voltage_cam": "full"},
            "camera_exposure_us": {"voltage_cam": 33333.0},
            "camera_binning": {"voltage_cam": 1},
            "camera_trigger": {"voltage_cam": False},
        },
        "not-a-dict": ["nope"],
        "bad-types": {
            "camera_presets": ["nope"],
            "camera_exposure_us": None,
            "camera_binning": "nope",
            "camera_trigger": 7,
            "dmd_sub_sampling": [2],
        },
        "part-bad": {
            "camera_presets": {"voltage_cam": "full", "other_cam": 7},
            "camera_exposure_us": {"voltage_cam": 33333.0,
                                   "typo_key": True},   # bool, not a number
            "camera_binning": {"voltage_cam": 1, "typo_key": True},   # bool, not int
            "camera_trigger": {"voltage_cam": False, "typo_key": "nope"},
            "dmd_sub_sampling": True,   # bool, not an int
        },
    })
    modes = config.load_modes()
    r.check("good" in modes and "bad-types" in modes,
            "valid + salvageable recipes are kept")
    r.check("not-a-dict" not in modes,
            "a non-dict recipe is dropped, not raised")

    good = modes["good"]
    r.check(good["camera_binning"] == {"voltage_cam": 1},
            f"a well-formed camera_binning entry passes through "
            f"({good['camera_binning']})")
    r.check(good["camera_trigger"] == {"voltage_cam": False},
            f"…and camera_trigger, bool value kept as a bool "
            f"({good['camera_trigger']})")
    r.check(good["dmd_sub_sampling"] == 2,
            f"…and dmd_sub_sampling, a plain int like dmd_all_on's plain "
            f"bool, not a per-module dict ({good['dmd_sub_sampling']})")

    bad = modes["bad-types"]
    r.check("camera_presets" not in bad and "camera_exposure_us" not in bad,
            "a list/null where a dict belongs is dropped entirely (existing "
            "fields, control)")
    r.check("camera_binning" not in bad and "camera_trigger" not in bad,
            "…and the same for the two new fields: wrong type entirely -> "
            "dropped, not raised")
    r.check("dmd_sub_sampling" not in bad,
            "…a list where dmd_sub_sampling wants a plain number is dropped "
            "too, not passed to int() and left to raise")

    part = modes["part-bad"]
    r.check(part["camera_presets"] == {"voltage_cam": "full"},
            "a non-str value is dropped, the real entry kept")
    r.check(part["camera_exposure_us"] == {"voltage_cam": 33333.0},
            "a bool value is dropped from camera_exposure_us — bool is an "
            "int subclass in Python, so this must be checked explicitly")
    r.check(part["camera_binning"] == {"voltage_cam": 1},
            "…the same guard applies to camera_binning (a bool would "
            "otherwise pass an int check silently)")
    r.check(part["camera_trigger"] == {"voltage_cam": False},
            "a non-bool value is dropped from camera_trigger, the real bool "
            "kept")
    r.check("dmd_sub_sampling" not in part,
            "a bool dmd_sub_sampling is dropped too — the same int-subclass "
            "guard, in the direction dmd_sub_sampling actually needs it")


def check_corrupt(r: Report, tmp: Path) -> None:
    _write(tmp, {})
    config._MODES_PATH.write_text("{not json at all", encoding="utf-8")
    r.check(config.load_modes() == {}, "an unreadable modes.json loads as empty")
    r.check(config._MODES_PATH.with_suffix(".corrupt.json").is_file(),
            "...and is quarantined, not discarded (the load_rigs policy, "
            "shared through _load_json)")


def check_round_trip(r: Report, tmp: Path) -> None:
    """save_modes -> load_modes must be lossless for every field set_mode()
    reads, the new two included — this is the "Save as preset" path."""
    _write(tmp, {})
    recipe = {
        "dmd_all_on": True,
        "dmd_sub_sampling": 2,
        "camera_presets": {"voltage_cam": "full"},
        "camera_exposure_us": {"voltage_cam": 33333.0},
        "camera_binning": {"voltage_cam": 1},
        "camera_trigger": {"voltage_cam": False},
    }
    config.save_modes({"Scan": recipe})
    reloaded = config.load_modes()
    r.check(reloaded == {"Scan": recipe},
            f"a saved recipe round-trips byte-for-byte ({reloaded})")


def check_scan_mode_shipped(r: Report) -> None:
    """The repo's own modes.json ships a "Scan" mode — internal trigger,
    full frame, 1x1 binning, ~30 Hz, full DMD display at 1-in-2
    sub-sampling — so this is a regression guard on the actual shipped
    file, not just the sanitizer logic above."""
    saved = config._MODES_PATH
    config._MODES_PATH = Path(__file__).resolve().parent.parent / "modes.json"
    try:
        modes = config.load_modes()
    finally:
        config._MODES_PATH = saved
    r.check("Scan" in modes, "the shipped modes.json has a Scan entry")
    scan = modes.get("Scan", {})
    r.check(scan.get("camera_presets", {}).get("voltage_cam") == "full",
            f"…full frame, the largest possible size "
            f"({scan.get('camera_presets')})")
    r.check(scan.get("camera_binning", {}).get("voltage_cam") == 1,
            f"…1x1 binning ({scan.get('camera_binning')})")
    r.check(scan.get("camera_trigger", {}).get("voltage_cam") is False,
            f"…internal trigger ({scan.get('camera_trigger')})")
    us = scan.get("camera_exposure_us", {}).get("voltage_cam")
    r.check(us is not None and abs(1e6 / us - 30.0) < 0.1,
            f"…exposure set for ~30 Hz capture ({us!r} us)")
    r.check(scan.get("dmd_all_on") is True,
            f"…full DMD display ({scan.get('dmd_all_on')})")
    r.check(scan.get("dmd_sub_sampling") == 2,
            f"…at 1-in-2 sub-sampling ({scan.get('dmd_sub_sampling')})")


def main() -> int:
    r = Report("modes")
    saved = config._MODES_PATH
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_modes_"))
    try:
        check_sanitize(r, tmp)
        check_corrupt(r, tmp)
        check_round_trip(r, tmp)
        check_scan_mode_shipped(r)
    finally:
        config._MODES_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
