"""Configuration: rigs.json, modes.json, settings persistence, mirror startup.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_config.py [-q] [--part NAME]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import (Report, isolate_user_state, make_window, pump, qt_app,
                      run_parts)
from acqApp import config
from acqApp.devices.puffer.control import PufferSettings
from acqApp.devices.wheel.settings import EncoderSettings
from acqApp.devices.mirror.startup import ensure_camera_default, AXIS
from acqApp.devices.stage.driver import (MIRROR_CHAN_GR, MIRROR_CHAN_CAMERA,
                                         MIRROR_OUT, MIRROR_IN)


# ═══ rigs (was test_rigs.py) ════════════════════════════════════════════

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

    cfg["settings"]["puffer"]["duration_s"] = 0.25
    config._CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    r.check(config.load_dataclass(PufferSettings, "puffer").duration_s == 0.25,
            "overriding the channel leaves the panel's other settings alone")

    # The encoder has no blank-channel path: "" would raise in its thread.
    _write(tmp, {"nowheel": {"ni_device": "Dev2",
                             "hardware": {"wheel": False},
                             "channels": {"wheel": "ai2"}}}, active="nowheel")
    r.check("wheel" not in config.RIG_BLANKABLE,
            "the wheel is not blankable (it has no not-fitted path)")
    r.check(config.load_dataclass(EncoderSettings, "wheel").channel
            == "Dev2/ai2",
            "so hardware false leaves the encoder aimed at real hardware")


def check_dmd_calibration(r: Report, tmp: Path) -> None:
    """Camera tilt is a physical fact, so it lives in the rig profile."""
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


def _part_rigs() -> int:
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


# ═══ modes (was test_modes.py) ══════════════════════════════════════════

def _write_modes(tmp: Path, modes: dict) -> None:
    """Point config at a temp modes.json."""
    config._MODES_PATH = tmp / "modes.json"
    config._MODES_PATH.write_text(json.dumps(modes), encoding="utf-8")


def check_modes_sanitize(r: Report, tmp: Path) -> None:
    _write_modes(tmp, {
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


def check_modes_corrupt(r: Report, tmp: Path) -> None:
    _write_modes(tmp, {})
    config._MODES_PATH.write_text("{not json at all", encoding="utf-8")
    r.check(config.load_modes() == {}, "an unreadable modes.json loads as empty")
    r.check(config._MODES_PATH.with_suffix(".corrupt.json").is_file(),
            "...and is quarantined, not discarded (the load_rigs policy, "
            "shared through _load_json)")


def check_round_trip(r: Report, tmp: Path) -> None:
    """The "Save as preset" path: lossless for every field set_mode() reads."""
    _write_modes(tmp, {})
    recipe = {
        "dmd_all_on": True,
        "dmd_sub_sampling": 2,
        "camera_presets": {"voltage_cam": "full"},
        "camera_rate_hz": {"voltage_cam": 30.0},
        "camera_binning": {"voltage_cam": 1},
        "camera_trigger": {"voltage_cam": False},
    }
    config.save_modes({"Scan": recipe})
    reloaded = config.load_modes()
    r.check(reloaded == {"Scan": recipe},
            f"a saved recipe round-trips byte-for-byte ({reloaded})")


def check_scan_mode_shipped(r: Report) -> None:
    """The shipped modes.json's "Scan" mode, not just the sanitizer."""
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
    hz = scan.get("camera_rate_hz", {}).get("voltage_cam")
    r.check(hz == 30.0, f"…30 Hz capture ({hz!r})")
    r.check(scan.get("dmd_all_on") is True,
            f"…full DMD display ({scan.get('dmd_all_on')})")
    r.check(scan.get("dmd_sub_sampling") == 2,
            f"…at 1-in-2 sub-sampling ({scan.get('dmd_sub_sampling')})")


def _part_modes() -> int:
    r = Report("modes")
    saved = config._MODES_PATH
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_modes_"))
    try:
        check_modes_sanitize(r, tmp)
        check_modes_corrupt(r, tmp)
        check_round_trip(r, tmp)
        check_scan_mode_shipped(r)
    finally:
        config._MODES_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ settings (was test_settings_persistence.py) ════════════════════════

def _add_step(panel) -> None:
    """A routine step, added the way the +Step button does."""
    from acqApp.routines.settings import Step
    panel._r.steps.append(Step(label="grid A", x_um=250.0, length=64,
                               unit="frames", settle_s=0.4))
    panel._reload_table()
    panel._emit()


# (module key, label, setter, reader, expected) — one distinctive,
# non-default value per panel so a stuck default cannot pass.
EDITS = [
    ("voltage_cam", "capture rate", lambda p: p._spn_target_hz.setValue(321.0),
     lambda p: p.get_config().target_hz,       321.0),
    ("voltage_cam", "binning",   lambda p: p._cmb_binning.setCurrentIndex(1),
     lambda p: p.get_config().binning,          2),
    ("voltage_cam", "preview avg", lambda p: p._spn_preview_avg.setValue(4),
     lambda p: p.get_config().preview_avg,      4),
    ("pupil_cam",   "exposure",  lambda p: p._spn_exp.setValue(4321.0),
     lambda p: p._spn_exp.value(),              4321.0),
    ("pupil_cam",   "region X1", lambda p: p._spn_lx1.setValue(118.0),
     lambda p: p.settings.limit_x1,             118.0),
    # The operator once lost tuning to a panel that never wrote these;
    # threshold sets the reported radius.
    ("pupil_cam",   "track on",  lambda p: p._chk_track.setChecked(True),
     lambda p: p.settings.track,                True),
    ("pupil_cam",   "threshold", lambda p: p._spn_thr.setValue(63),
     lambda p: p.settings.track_threshold,      63),
    ("pupil_cam",   "model",
     lambda p: p._cmb_model.setCurrentIndex(p._cmb_model.findData("circular")),
     lambda p: p.settings.track_model,          "circular"),
    ("pupil_cam",   "smooth",     lambda p: p._chk_smooth.setChecked(True),
     lambda p: p.settings.smooth,               True),
    ("pupil_cam",   "smooth win", lambda p: p._spn_smooth_win.setValue(11),
     lambda p: p.settings.smooth_window,        11),
    ("pupil_cam",   "blink on",   lambda p: p._chk_blink.setChecked(True),
     lambda p: p.settings.blink_detect,         True),
    ("pupil_cam",   "blink drop", lambda p: p._spn_blink_drop.setValue(0.42),
     lambda p: p.settings.blink_drop_frac,      0.42),
    ("pupil_cam",   "blink win",  lambda p: p._spn_blink_win.setValue(23),
     lambda p: p.settings.blink_baseline_window, 23),
    ("pupil_cam",   "CR reach",  lambda p: p._spn_cr_reach.setValue(0.55),
     lambda p: p.settings.cr_reach,             0.55),
    # A nested list: proves JSON's lost tuple type is normalised on load.
    ("pupil_cam",   "CR pins",   lambda p: p.set_pins([(11.0, 22.0, 3.0)]),
     lambda p: p.settings.cr_pins,              [(11.0, 22.0, 3.0)]),
    ("wheel",       "V/rev",     lambda p: p._spn_vpr.setValue(3.210),
     lambda p: p.settings.volts_per_rev,        3.210),
    ("wheel",       "diameter",  lambda p: p._spn_dia.setValue(123.0),
     lambda p: p.settings.wheel_dia_mm,         123.0),
    ("wheel",       "rate",      lambda p: p._spn_rate.setValue(200.0),
     lambda p: p.settings.rate,                 200.0),
    ("puffer",      "channel",   lambda p: p._cmb_chan.setCurrentText("Dev3/port0/line1"),
     lambda p: p.settings.channel,              "Dev3/port0/line1"),
    ("puffer",      "duration",  lambda p: p._spn_dur.setValue(0.321),
     lambda p: p.settings.duration_s,           0.321),
    ("stage",       "port",      lambda p: p._cmb_port.setCurrentText("COM9"),
     lambda p: p.settings.port,                 "COM9"),
    ("stage",       "poll rate", lambda p: p._spn_rate.setValue(7.0),
     lambda p: p.settings.poll_hz,              7.0),
    ("stage",       "frame rotation", lambda p: p._spn_rotation.setValue(45.0),
     lambda p: p.settings.frame_rotation_deg,   45.0),
    ("dmd",         "trigger",   lambda p: p._cmb_trig.setCurrentText("Software"),
     lambda p: p.settings.trigger_mode,         "Software"),
    # Registration to the optics: at the wrong scale/rotation a session
    # cannot be located in the field of view afterwards.
    ("dmd",         "scale",     lambda p: p._spn_scale.setValue(132.4),
     lambda p: p.settings.scale_pct,            132.4),
    ("dmd",         "rotation",  lambda p: p._spn_rot.setValue(12.5),
     lambda p: p.settings.rotation_deg,         12.5),
    ("dmd",         "offset X",  lambda p: p._spn_dx.setValue(37.0),
     lambda p: p.settings.offset_x,             37.0),
    # One mode row only: the radios are exclusive.
    ("dmd",         "mode-roi",  lambda p: p._rb["roi"].setChecked(True),
     lambda p: p.settings.display_mode,         "roi"),
    # The operator's protocol — the only nested list here.
    ("routines",    "step list", _add_step,
     lambda p: [(s.label, s.x_um, s.length, s.unit, s.settle_s)
                for s in p.settings.steps],
     [("grid A", 250.0, 64.0, "frames", 0.4)]),
    ("routines",    "cycles",    lambda p: p._spn_cycles.setValue(4),
     lambda p: p.settings.cycles,               4),
    ("routines",    "save mode",
     lambda p: p._cmb_save.setCurrentIndex(p._cmb_save.findData("per_repeat")),
     lambda p: p.settings.save_mode,            "per_repeat"),
]

SAVE_EDITS = [
    ("mouse_id", lambda p: p._ed_mouse_id.setText("m17"),        "m17"),
    ("template", lambda p: p._ed_template.setText("{mouse_id}_{time}"),
     "{mouse_id}_{time}"),
]


def same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def _part_settings() -> int:
    r = Report("settings")
    tmp = isolate_user_state()

    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    import acqApp.main as M

    enabled = set(config.MODULES)

    # ── first launch: edit every panel ───────────────────────────────────────
    win = make_window(enabled)

    # Edited below while never shown: a lazily-built window would leave the
    # controllers unconfigured.
    dlg = win._settings_dialog
    r.check(dlg is not None and dlg.isWindow(), "settings are a top-level window")
    r.check(not isinstance(dlg, M.QDockWidget), "…and not a dock widget")
    r.check(not dlg.isVisible(), "settings window starts hidden")
    # Modules with `own_window` are not pages, but get a sidebar item.
    paged = len(config.MODULES) - sum(1 for m in win._modules if m.own_window)
    r.check(dlg.tabs.count() == paged + 1,
            f"a page per module plus Save (got {dlg.tabs.count()}, "
            f"want {paged + 1})")
    r.check(any(m.own_window for m in win._modules)
            and all(dlg.panel_index(m.panel) < 0
                    for m in win._modules if m.own_window),
            "…and a module with its own window is not among them")
    # Two selectors, kept in step: the tab bar and the sidebar.
    r.check(set(win._page_actions) == set(config.MODULES) | {"saving"},
            f"a sidebar item per page (got {sorted(win._page_actions)})")
    win._page_actions["wheel"].trigger()
    pump(app, 0.2)
    r.check(dlg.isVisible(), "a sidebar page item opens the window")
    r.check(dlg.current_panel() is
            next(m.panel for m in win._modules if m.key == "wheel"),
            "…on that module's page")
    r.check(win._page_actions["wheel"].isChecked(),
            "…and checks only that item")
    r.check(dlg.tabs.tabBar().isVisible(),
            "the tab bar is still there — both ways to reach a page")
    dlg.tabs.setCurrentIndex(dlg.panel_index(
        next(m.panel for m in win._modules if m.key == "puffer")))
    pump(app, 0.2)
    r.check(win._page_actions["puffer"].isChecked()
            and not win._page_actions["wheel"].isChecked(),
            "choosing a TAB moves the sidebar highlight to match")

    # The Devices monitor is lit while open too.
    r.check(not win._devices_action.isChecked(),
            "the Devices item starts unlit")
    win._devices_action.trigger()
    pump(app, 0.1)
    r.check(win._devices_action.isChecked() and win._devices_dialog.isVisible(),
            "opening it lights the sidebar item")
    win._devices_dialog.close()
    pump(app, 0.1)
    r.check(not win._devices_action.isChecked(),
            "…and closing it with the window's own Close button un-lights it, "
            "the same way a panel window's own X does (_on_panel_window)")

    # On default_size(): the window manager has the last word on the shown one.
    want  = dlg.default_size()
    from PyQt6.QtGui import QGuiApplication
    avail = QGuiApplication.primaryScreen().availableGeometry()
    r.info(f"opens at {want.width()}x{want.height()} "
           f"(screen {avail.width()}x{avail.height()})")
    r.check(want.width() >= min(dlg.tabs.sizeHint().width(),
                                int(avail.width() * 0.9)),
            "default size covers the widest panel without scrolling")
    r.check(want.width() <= avail.width() and want.height() <= avail.height(),
            "…and still fits on the screen it opens on")

    # An insane SAVED size (interrupted write, unplugged monitor) must not win
    # forever; restoreGeometry() can succeed on one. Fresh panel-less dialogs.
    from PyQt6.QtCore import QByteArray, QSize

    from acqApp.dialogs import SettingsDialog, _looks_sane

    r.check(not _looks_sane(QSize(50, 40), SettingsDialog._MIN_DEFAULT),
            "control: _looks_sane flags a tiny size")
    r.check(_looks_sane(QSize(*SettingsDialog._MIN_DEFAULT),
                        SettingsDialog._MIN_DEFAULT),
            "control: _looks_sane accepts a floor-sized window")

    bad = SettingsDialog()
    bad._saved_geom = QByteArray(b"not a real geometry blob")
    bad.show()
    pump(app, 0.1)
    r.check(bad.width() >= SettingsDialog._MIN_DEFAULT[0] // 2
            and bad.height() >= SettingsDialog._MIN_DEFAULT[1] // 2,
            f"a corrupt saved geometry falls back to the computed default, "
            f"not whatever a failed restore leaves behind "
            f"({bad.width()}x{bad.height()})")
    bad.close()

    # A validly-encoded tiny geometry: restoreGeometry() succeeds on it.
    seed = SettingsDialog()
    seed.show()
    pump(app, 0.05)
    seed.resize(60, 50)
    pump(app, 0.05)
    tiny_geom = seed.saveGeometry()
    seed.close()

    tiny = SettingsDialog()
    tiny._saved_geom = tiny_geom
    tiny.show()
    pump(app, 0.1)
    r.check(tiny.width() >= SettingsDialog._MIN_DEFAULT[0] // 2
            and tiny.height() >= SettingsDialog._MIN_DEFAULT[1] // 2,
            f"a validly-restored but implausibly tiny geometry is overridden "
            f"too, not trusted just because restoreGeometry() succeeded "
            f"({tiny.width()}x{tiny.height()})")
    tiny.close()

    # The tab switch above left `puffer` current.
    win._page_actions["wheel"].trigger()
    pump(app, 0.2)
    r.check(dlg.isVisible(), "clicking a different page switches, not hides")
    win._page_actions["wheel"].trigger()
    pump(app, 0.2)
    r.check(not dlg.isVisible(), "clicking the open page again hides it")

    win._page_actions["wheel"].trigger()
    pump(app, 0.2)
    dlg.close()                       # the title-bar ✕ / Esc path
    pump(app, 0.2)
    r.check(not dlg.isVisible(), "closing the window hides it")
    r.check(not any(a.isChecked() for a in win._page_actions.values()),
            "…and un-checks every page item, so the next click re-opens it")

    panels = {m.key: m.panel for m in win._modules}

    # ── every settings box folds away, and stays folded ──────────────────────
    # Applied in add_panel(), so checked across every tab.
    from PyQt6.QtWidgets import QGroupBox, QWidget as _QW
    from acqApp import widgets as W
    tabs = {dlg.tabs.tabText(i): dlg.tabs.widget(i).findChildren(QGroupBox)
            for i in range(dlg.tabs.count())}
    flat = [b for v in tabs.values() for b in v]
    r.check(len(flat) >= 10, f"{len(flat)} group boxes across {len(tabs)} tabs")
    r.check(all(b.isCheckable() for b in flat),
            "every settings box has a fold toggle in its title")
    # An arrow, not a tick box: a tick reads as "enable this section".
    r.check(all(b.title().startswith((W.OPEN, W.SHUT)) for b in flat),
            "…drawn as a ▾/▸ dropdown arrow")

    def find(title):
        return next(b for b in flat if getattr(b, "_base_title", "") == title)

    p = panels["pupil_cam"]
    box = find("Eye region")
    box.setChecked(False); box.setChecked(True); box.setChecked(False)
    r.check(box.title().count(W.SHUT) == 1 and "Eye region" in box.title(),
            f"control: toggling three times leaves one arrow ({box.title()!r})")
    box.setChecked(True)
    pump(app, 0.05)
    tall = box.sizeHint().height()
    box.setChecked(False)
    pump(app, 0.05)
    short = box.sizeHint().height()
    r.check(short < tall, f"folding shrinks the box ({tall} → {short} px)")
    # isVisibleTo: the window is closed, so isVisible() would pass vacuously.
    r.check(not any(c.isVisibleTo(box) for c in box.findChildren(_QW)),
            "…and its contents are hidden, not merely greyed out")

    # Unfolding must restore each child's own enabled state, not enable all.
    r.check(not p._btn_limit_clear.isEnabled(),
            "control: Clear is disabled while no region is set")
    box.setChecked(True)
    pump(app, 0.05)
    r.check(not p._btn_limit_clear.isEnabled(),
            "…and unfolding does not wrongly re-enable it")
    r.check(all(c.isVisibleTo(box) for c in
                (p._spn_lx0, p._spn_ly0, p._spn_lx1, p._spn_ly1)),
            "…while the rest of the box comes back")

    box.setChecked(False)       # left folded, read back after the restart below
    pump(app, 0.05)
    for key, label, setter, reader, expected in EDITS:
        setter(panels[key])
        r.check(same(reader(panels[key]), expected),
                f"{key}: {label} accepted the edit")
    for label, setter, _expected in SAVE_EDITS:
        setter(win._save_panel)
    win._save_panel._on_edited()

    # Runtime state: restored, it would light an empty rig at launch.
    panels["pupil_cam"]._chk_led.setChecked(True)

    win.close()
    pump(app, 0.2)

    # ── the config file itself ───────────────────────────────────────────────
    cfg_path = tmp / "acqapp_local.json"
    if not r.check(cfg_path.is_file(), "config written to the isolated path"):
        return r.finish()
    saved = json.loads(cfg_path.read_text(encoding="utf-8")).get("settings", {})
    r.note(f"sections: {sorted(saved)}")
    for key in ("voltage_cam", "pupil_cam", "wheel", "puffer", "stage", "dmd",
                "routines", "saving"):
        r.check(key in saved, f"'{key}' section present in the config")

    # Axis calibration belongs to the shared stage_control config.
    r.check(set(saved.get("stage", {})) == {"port", "poll_hz", "frame_rotation_deg"},
            f"stage section is port/poll_hz/frame_rotation_deg only — Z's "
            f"calibration lives in the shared stage_control config, not here "
            f"(got {sorted(saved.get('stage', {}))})")
    # Scoped to the two sections that own an LED.
    led_sections = {k: saved.get(k, {}) for k in ("voltage_cam", "pupil_cam")}
    scrubbed = (json.dumps(led_sections).lower()
               .replace("led_follow_live", "").replace("led_intensity", ""))
    r.check("led" not in scrubbed,
            "no LED on/off runtime state was persisted as a setting "
            "(led_follow_live/led_intensity, a mode and a dial, are allowed)")

    # ── surviving a write that dies partway ──────────────────────────────────
    # Rewritten on every spinbox step, while the app can die natively mid-write
    # (qFatal, a DCAM segfault).
    intact = cfg_path.read_text(encoding="utf-8")
    real_dump = config.json.dump

    def die_partway(obj, fh, **kw):
        fh.write('{"settings": {"voltage_cam": {"expos')     # a partial record
        raise OSError("simulated: no space left on device")

    config.json.dump = die_partway
    try:
        config.save_config({"theme": "light", "settings": {"wiped": True}})
    finally:
        config.json.dump = real_dump
    r.check(cfg_path.read_text(encoding="utf-8") == intact,
            "a write that dies partway leaves the previous config untouched")
    r.check(not list(tmp.glob("*.tmp")),
            f"…and cleans up after itself ({[p.name for p in tmp.glob('*.tmp')]})")
    # CONTROL: the same partial content written in place.
    (tmp / "wrecked.json").write_text('{"settings": {"voltage_cam": {"expos',
                                      encoding="utf-8")
    config._CONFIG_PATH, keep_path = tmp / "wrecked.json", config._CONFIG_PATH
    r.check(config.load_config() == {},
            "control: a truncated config really is unreadable")
    r.check((tmp / "wrecked.corrupt.json").is_file(),
            "…and is moved aside rather than silently overwritten with defaults")
    config._CONFIG_PATH = keep_path

    # ── second launch: read the panels back ──────────────────────────────────
    win2 = make_window(enabled)
    panels2 = {m.key: m.panel for m in win2._modules}
    for key, label, _setter, reader, expected in EDITS:
        got = reader(panels2[key])
        r.check(same(got, expected),
                f"{key}: {label} restored ({got!r})")
    for label, _setter, expected in SAVE_EDITS:
        got = getattr(win2._save_panel.settings, label)
        r.check(got == expected, f"saving: {label} restored ({got!r})")

    r.check(not panels2["pupil_cam"]._chk_led.isChecked(),
            "the LED came back OFF, not restored on")

    dlg2 = win2._settings_dialog
    flat2 = [b for i in range(dlg2.tabs.count())
             for b in dlg2.tabs.widget(i).findChildren(QGroupBox)]
    r.check(all(b.title().startswith((W.OPEN, W.SHUT)) for b in flat2),
            "…and the arrows are there on the rebuilt window too")
    def find2(title):
        return next(b for b in flat2
                    if getattr(b, "_base_title", "") == title)

    r.check(not find2("Eye region").isChecked(),
            "a folded settings box comes back folded")
    r.check(find2("Camera").isChecked(),
            "control: a box that was left open comes back open")

    win2._btn_run.setChecked(True)
    r.check(win2._sync.running, "session starts with the restored settings")
    pump(app, 0.5)
    win2._display_tick()
    r.check(win2._modules[0].worker is not None, "workers built")
    win2._btn_run.setChecked(False)
    win2.close()
    pump(app, 0.2)

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ mirror (was test_mirror_startup.py) ════════════════════════════════

class FakeDriver:
    """Stands in for MCM6101: no serial port, records every call."""

    def __init__(self, port: str, states: dict[int, int] | None = None,
                 open_fails: bool = False):
        self.port = port
        self._states = dict(states or {})
        self._open_fails = open_fails
        self.opened = False
        self.closed = False
        self.set_calls: list[tuple[int, int, int]] = []

    def open(self):
        if self._open_fails:
            raise PermissionError("Access is denied.")  # ThorImage holds it
        self.opened = True

    def close(self):
        self.closed = True

    def get_mirror_state(self, axis: int, channel: int) -> int:
        return self._states.get(channel, MIRROR_OUT)

    def set_mirror_state(self, axis: int, channel: int, state: int):
        self._states[channel] = state
        self.set_calls.append((axis, channel, state))


def check_already_correct(r: Report) -> None:
    """Both channels already OUT: no SET sent, reported as not corrected."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_OUT, MIRROR_CHAN_CAMERA: MIRROR_OUT})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok, "check runs when the port opens")
    r.check(not result.corrected, "already-correct state is not reported as corrected")
    r.check(fake.set_calls == [], "no SET sent when nothing was wrong")
    r.check(fake.closed, "port closed after a successful check")


def check_corrects_mismatch(r: Report) -> None:
    """GR left on PMT (IN): only GR is corrected, CAMERA (already OUT) is left alone."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_IN, MIRROR_CHAN_CAMERA: MIRROR_OUT})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok and result.corrected, "mismatch is detected and corrected")
    r.check(fake.set_calls == [(AXIS, MIRROR_CHAN_GR, MIRROR_OUT)],
            "SET sent for the wrong channel only, not the one already correct")


def check_corrects_both(r: Report) -> None:
    """Both left on PMT (IN): both channels corrected."""
    fake = FakeDriver("COM54", states={MIRROR_CHAN_GR: MIRROR_IN, MIRROR_CHAN_CAMERA: MIRROR_IN})
    result = ensure_camera_default(driver_cls=lambda port: fake)
    r.check(result.ok and result.corrected, "both-wrong case is detected and corrected")
    r.check(set(fake.set_calls) == {(AXIS, MIRROR_CHAN_GR, MIRROR_OUT),
                                     (AXIS, MIRROR_CHAN_CAMERA, MIRROR_OUT)},
            "SET sent for both channels")


def check_port_unavailable(r: Report) -> None:
    """ThorImage holding COM54 (or the controller absent): reported, not raised."""
    fake = FakeDriver("COM54", open_fails=True)
    try:
        result = ensure_camera_default(driver_cls=lambda port: fake)
    except Exception as e:                                # noqa: BLE001
        r.check(False, f"a closed port raised {type(e).__name__} instead of being reported")
        return
    r.check(not result.ok and result.error, "unopenable port reported as not ok, with a reason")
    r.check(not result.corrected, "no correction attempted when the port never opened")
    r.check(fake.set_calls == [], "no SET sent when the port never opened")


def _part_mirror() -> int:
    r = Report("mirror-startup")
    check_already_correct(r)
    check_corrects_mismatch(r)
    check_corrects_both(r)
    check_port_unavailable(r)
    return r.finish()


PARTS = {
    "rigs": _part_rigs,
    "modes": _part_modes,
    "settings": _part_settings,
    "mirror": _part_mirror,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
