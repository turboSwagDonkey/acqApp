"""Configuration: rigs.json, modes.json, settings persistence, mirror startup.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_config.py [-q] [--part NAME]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import (Report, isolate_user_state, make_window, pump,
                      pump_until, qt_app, run_parts)
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


def check_corrupt(r: Report, tmp: Path) -> None:
    _write(tmp, {})
    config._RIGS_PATH.write_text("{not json at all", encoding="utf-8")
    r.check(config.load_rigs() == {}, "an unreadable rigs.json loads as empty")
    r.check(config._RIGS_PATH.with_suffix(".corrupt.json").is_file(),
            "...and is quarantined, not discarded (the load_config policy)")


def check_non_object_quarantined(r: Report, tmp: Path) -> None:
    """Valid JSON that isn't an object (a hand edit gone wrong) loads as empty
    too, and the next save would overwrite the only copy: moved aside."""
    _write(tmp, {})
    keep = config._CONFIG_PATH.with_suffix(".corrupt.json")
    keep.unlink(missing_ok=True)
    config._CONFIG_PATH.write_text('["rig", "Dev3"]', encoding="utf-8")
    r.check(config.load_config() == {}, "a JSON list config loads as empty")
    r.check(keep.is_file() and json.loads(keep.read_text(encoding="utf-8"))
            == ["rig", "Dev3"],
            "…and is kept as .corrupt.json before a save can overwrite it")


def check_transient_locks(r: Report, tmp: Path) -> None:
    """On Windows a file held open for a moment (a reader, a virus scanner)
    refuses a read or a replace with PermissionError. That used to drop the
    save, and a read was taken for damage: the config was moved aside as
    .corrupt.json and the app restarted from defaults."""
    import builtins
    import io
    from contextlib import redirect_stdout

    path = tmp / "locked.json"
    real_replace, real_sleep, real_open = config.os.replace, config.time.sleep, builtins.open
    config.time.sleep = lambda s: None          # the waits are not under test

    def flaky_replace(failures: int):
        left = [failures]

        def replace(src, dst):
            if left[0] > 0:
                left[0] -= 1
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)
        return replace, left

    try:
        config._atomic_write_json(path, {"v": 1})
        config.os.replace, left = flaky_replace(2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            config._atomic_write_json(path, {"v": 2})
        r.check(config._load_json(path) == {"v": 2} and "could not save" not in buf.getvalue(),
                "a save that finds the file briefly locked is retried and lands")

        config.os.replace, left = flaky_replace(99)
        buf = io.StringIO()
        with redirect_stdout(buf):
            config._atomic_write_json(path, {"v": 3})
        config.os.replace = real_replace
        r.check("could not save" in buf.getvalue() and config._load_json(path) == {"v": 2}
                and not list(tmp.glob("locked.json.*.tmp")),
                "a lock that never clears is reported, keeps the old file, leaves no temp")

        def flaky_open(failures: int):
            left = [failures]

            def opener(file, *a, **k):
                if str(file) == str(path) and left[0] > 0:
                    left[0] -= 1
                    raise PermissionError(13, "Access is denied")
                return real_open(file, *a, **k)
            return opener

        config.open = flaky_open(2)
        got = config._load_json(path)
        r.check(got == {"v": 2} and not path.with_suffix(".corrupt.json").exists(),
                "a read that finds the file briefly locked is retried")

        config.open = flaky_open(99)
        buf = io.StringIO()
        with redirect_stdout(buf):
            got = config._load_json(path)
        del config.open
        r.check(got == {} and path.is_file()
                and not path.with_suffix(".corrupt.json").exists()
                and "leaving the file alone" in buf.getvalue(),
                "a read that stays locked is NOT taken for damage: the file stays put")
        r.check(config._load_json(path) == {"v": 2},
                "…and is read normally once the lock clears")
    finally:
        config.os.replace, config.time.sleep = real_replace, real_sleep
        if "open" in vars(config):
            del config.open


def check_bad_values(r: Report, tmp: Path) -> None:
    """Values that used to raise out of a save or a load (a slot or startup,
    both fatal) are reported or dropped instead."""
    import io
    from contextlib import redirect_stdout

    path = tmp / "unencodable.json"
    config._atomic_write_json(path, {"v": 1})
    buf = io.StringIO()
    with redirect_stdout(buf):
        config._atomic_write_json(path, {"v": object()})
    r.check("could not save" in buf.getvalue() and config._load_json(path) == {"v": 1}
            and not list(tmp.glob("unencodable.json.*.tmp")),
            "an unencodable value is reported, keeps the old file, leaves no temp")

    config.save_config({"enabled_modules": ["wheel", ["junk"], {"x": 1}, 7]})
    r.check(config.load_enabled_modules() == config.order_modules(["wheel"]),
            "junk in enabled_modules is skipped, not raised at startup")

    # A __post_init__ that converts (the pupil's pins) raises ValueError.
    from acqApp.devices.pupil_cam.settings import PupilSettings
    config.save_config({"settings": {"pupil_cam": {"cr_pins": [["x", 1, 2]]}}})
    try:
        got = config.load_dataclass(PupilSettings, "pupil_cam")
        ok = got.cr_pins == []
    except Exception:                               # noqa: BLE001
        ok = False
    r.check(ok, "a saved value its __post_init__ rejects falls back to "
                "defaults, not a startup crash")


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
    r.check(ctl.fire(0.01) is False,
            "...and fire() on an unfitted puffer is silent, not an error, and "
            "says it didn't fire (the sidebar card counts only real puffs)")


def check_puffer_closes_on_error(r: Report) -> None:
    """A pulse that fails after opening the valve (here time.sleep refusing a
    negative duration) still closes it. Mock task: no puff."""
    qt_app()
    import time
    from acqApp.devices.puffer.control import PufferController

    class FakeTask:
        def __init__(self):
            self.writes: list[bool] = []

        def write(self, v):
            self.writes.append(bool(v))

    ctl = PufferController(PufferSettings(channel=""))
    task = ctl._task = FakeTask()
    r.check(ctl.fire(-0.1) is True, "a fire with a task reports it fired")
    deadline = time.perf_counter() + 2.0
    while len(task.writes) < 2 and time.perf_counter() < deadline:
        time.sleep(0.01)
    r.check(task.writes == [True, False],
            f"the valve is closed again after a failed pulse ({task.writes})")
    ctl._task = None


def check_file_only_keys(r: Report, tmp: Path) -> None:
    """Ports, DAQ lines and poll rates have no widget: a hand edit to the
    file survives the running app saving that module's other settings."""
    _write(tmp, {}, active=None)
    config.save_settings("wheel", {"channel": "Dev3/ai5", "rate": 50.0})
    # The app's stale in-memory values, plus a real panel edit.
    config.save_settings("wheel", {"channel": "Dev3/ai2", "rate": 100.0,
                                   "wheel_dia_mm": 9.0},
                         keep=("channel", "rate"))
    got = config.load_settings("wheel")
    r.check(got == {"channel": "Dev3/ai5", "rate": 50.0, "wheel_dia_mm": 9.0},
            f"the file's channel and rate win; the panel's edit lands ({got})")
    config.save_settings("puffer", {"channel": "Dev3/port0/line7",
                                    "duration_s": 0.1}, keep=("channel",))
    got = config.load_settings("puffer")
    r.check(got.get("channel") == "Dev3/port0/line7",
            f"control: a key not yet in the file is written, to edit ({got})")
    app = qt_app()          # held: a collected QApplication aborts the process
    from PyQt6.QtWidgets import QComboBox
    from acqApp.devices.puffer.control import SettingsPanel as PufferPanel
    from acqApp.devices.wheel.panel import SettingsPanel as WheelPanel
    for panel in (WheelPanel(), PufferPanel(PufferSettings())):
        r.check(not panel.findChildren(QComboBox),
                f"{type(panel).__module__}: no channel combo left")
    del app


def _part_rigs() -> int:
    r = Report("rigs")
    saved = (config._RIGS_PATH, config._CONFIG_PATH)
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_rigs_"))
    try:
        check_sanitize(r, tmp)
        check_corrupt(r, tmp)
        check_non_object_quarantined(r, tmp)
        check_transient_locks(r, tmp)
        check_bad_values(r, tmp)
        check_resolution(r, tmp)
        check_profile_beats_saved(r, tmp)
        check_no_profile_is_unchanged(r, tmp)
        check_puffer_skips_daq(r)
        check_puffer_closes_on_error(r)
        check_file_only_keys(r, tmp)
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
    ("pupil_cam",   "exposure",  lambda p: p._exp.setValue(4321.0),
     lambda p: round(p._exp.value(), 3),        4321.0),
    ("pupil_cam",   "region X1", lambda p: p.set_limit(10.0, 10.0, 118.0, 90.0),
     lambda p: p.settings.limit_x1,             118.0),
    # The operator once lost tuning to a panel that never wrote these;
    # threshold sets the reported radius.
    ("pupil_cam",   "track on",  lambda p: p.tracking._chk_track.setChecked(True),
     lambda p: p.settings.track,                True),
    ("pupil_cam",   "threshold", lambda p: p.tracking._spn_thr.setValue(63),
     lambda p: p.settings.track_threshold,      63),
    ("pupil_cam",   "model",
     lambda p: p.tracking._cmb_model.setCurrentIndex(p.tracking._cmb_model.findData("circular")),
     lambda p: p.settings.track_model,          "circular"),
    ("pupil_cam",   "smooth",     lambda p: p.tracking._chk_smooth.setChecked(True),
     lambda p: p.settings.smooth,               True),
    ("pupil_cam",   "smooth win", lambda p: p.tracking._spn_smooth_win.setValue(11),
     lambda p: p.settings.smooth_window,        11),
    ("pupil_cam",   "blink on",   lambda p: p.tracking._chk_blink.setChecked(True),
     lambda p: p.settings.blink_detect,         True),
    ("pupil_cam",   "blink drop", lambda p: p.tracking._spn_blink_drop.setValue(0.42),
     lambda p: p.settings.blink_drop_frac,      0.42),
    ("pupil_cam",   "blink win",  lambda p: p.tracking._spn_blink_win.setValue(23),
     lambda p: p.settings.blink_baseline_window, 23),
    ("pupil_cam",   "CR reach",  lambda p: p.tracking._spn_cr_reach.setValue(0.55),
     lambda p: p.settings.cr_reach,             0.55),
    # A nested list: proves JSON's lost tuple type is normalised on load.
    ("pupil_cam",   "CR pins",   lambda p: p.set_pins([(11.0, 22.0, 3.0)]),
     lambda p: p.settings.cr_pins,              [(11.0, 22.0, 3.0)]),
    ("pupil_cam",   "region lock", lambda p: p.set_region_locked(True),
     lambda p: p.settings.limit_locked,         True),
    ("wheel",       "V/rev",     lambda p: p._spn_vpr.setValue(3.210),
     lambda p: p.settings.volts_per_rev,        3.210),
    ("wheel",       "diameter",  lambda p: p._spn_dia.setValue(123.0),
     lambda p: p.settings.wheel_dia_mm,         123.0),
    ("puffer",      "duration",  lambda p: p._spn_dur.setValue(0.321),
     lambda p: p.settings.duration_s,           0.321),
    ("stage",       "frame rotation", lambda p: p._spn_rotation.setValue(45.0),
     lambda p: p.settings.frame_rotation_deg,   45.0),
    ("dmd",         "sub-sampling", lambda p: p.set_sub_sampling(3),
     lambda p: p.settings.sub_sampling,         3),
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
]

SAVE_EDITS = [
    ("mouse_id", lambda p: p._ed_mouse_id.setText("m17"),        "m17"),
    ("folder",   lambda p: p._ed_folder.setText(r"C:\acq_roundtrip"),
     r"C:\acq_roundtrip"),
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

    enabled = set(config.MODULES)

    # ── first launch: edit every panel ───────────────────────────────────────
    win = make_window(enabled)

    # Edited below while folded: a lazily-built panel would leave the
    # controllers unconfigured.
    folds = {k: f for k, f in win._folds.items() if f.body is not None}
    # A module with no panel at all (the mirror's switch) gets only a card.
    panelled = {m.key for m in win._modules if m.panel is not None}
    r.check(set(folds) == panelled | {"saving"},
            f"a settings fold per panel, plus Save's ({sorted(folds)})")
    r.check(all(f.window() is win for f in folds.values()),
            "…each inside the main window, none a window of its own")
    r.check("mirror" not in panelled and "mirror" in {m.key for m in win._modules},
            "control: the mirror is loaded, just without a panel")
    mirror = next(m for m in win._modules if m.key == "mirror")
    sw = mirror.switch
    card = win._folds.get("mirror")
    r.check(sw is not None and card is not None and card.card.isAncestorOf(sw)
            and card.body is None,
            "the mirror's switch sits on a card that folds nothing")
    start = mirror.settings.state
    sw.click()
    flipped = mirror.settings.state
    sw.click()
    r.check(flipped != start and mirror.settings.state == start
            and mirror.metadata()["mirror_initial_state"] == start,
            f"a click flips the logged state and a second flips it back "
            f"({start} -> {flipped} -> {mirror.settings.state})")
    wheel, puffer = folds["wheel"], folds["puffer"]
    r.check(not wheel.is_open() and not puffer.is_open(),
            "General's sections start folded")
    wheel.set_open(True)
    r.check(wheel.body.isVisibleTo(wheel) and wheel.card.property("open"),
            "opening one shows its panel and lights its card")
    puffer.set_open(True)
    r.check(wheel.is_open() and puffer.is_open(),
            "a second opens beside the first")
    wheel.set_open(False)
    puffer.set_open(False)

    panels = {m.key: m.panel for m in win._modules}

    # ── a card folds; the boxes inside it don't, bar Advanced ────────────────
    from PyQt6.QtWidgets import QGroupBox, QWidget as _QW
    from acqApp import widgets as W
    flat = [b for f in folds.values() for b in f.body.findChildren(QGroupBox)]
    adv = [b for b in flat if b.objectName() == "advanced"]
    plain = [b for b in flat if b.objectName() != "advanced"]
    r.check(len(plain) >= 10,
            f"{len(plain)} group boxes across {len(folds)} panels")
    r.check(not any(b.isCheckable() for b in plain),
            "no box inside a card folds: opening the card shows all of it")
    r.check(len(adv) >= 2 and all(b.isCheckable() and not b.isChecked()
                                  for b in adv),
            f"…except the {len(adv)} Advanced boxes, which start folded")

    # `collapsible` itself (Advanced, the pupil review), on a real box.
    p = panels["pupil_cam"]
    box = W.collapsible(next(b for b in plain if b.title() == "Reflections"))
    # An arrow, not a tick box: a tick reads as "enable this section".
    r.check(box.title().startswith((W.OPEN, W.SHUT)),
            f"a folding box is drawn with a ▾/▸ arrow ({box.title()!r})")
    box.setChecked(False); box.setChecked(True); box.setChecked(False)
    r.check(box.title().count(W.SHUT) == 1 and "Reflections" in box.title(),
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
    r.check(not p.tracking._btn_pins_clear.isEnabled(),
            "control: Clear pins is disabled while none are pinned")
    box.setChecked(True)
    pump(app, 0.05)
    r.check(not p.tracking._btn_pins_clear.isEnabled(),
            "…and unfolding does not wrongly re-enable it")
    r.check(all(c.isVisibleTo(box) for c in
                (p.tracking._spn_cr_thr, p.tracking._spn_cr_pad,
                 p.tracking._spn_cr_ring, p.tracking._spn_cr_reach)),
            "…while the rest of the box comes back")

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
    r.check(set(saved.get("stage", {})) == {"port", "poll_hz", "frame_rotation_deg",
                                            "drag_speed_um_s", "cam_axes"},
            f"stage section is port/poll_hz/frame_rotation_deg/drag_speed_um_s/"
            f"cam_axes only — Z's "
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
    import builtins

    class _DiesPartway:
        """The temp file's handle: half of each write lands, then the disk fills."""
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def write(self, text):
            self._fh.write(text[:len(text) // 2])             # a partial record
            raise OSError("simulated: no space left on device")

    def dying_open(file, mode="r", *a, **k):
        fh = builtins.open(file, mode, *a, **k)
        return _DiesPartway(fh) if "w" in mode and str(file).endswith(".tmp") else fh

    config.open = dying_open
    try:
        config.save_config({"settings": {"wiped": True}})
    finally:
        del config.open
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

    flat2 = [b for f in win2._folds.values() if f.body is not None
             for b in f.body.findChildren(QGroupBox)]
    r.check(not any(b.isCheckable() for b in flat2
                    if b.objectName() != "advanced"),
            "…and the rebuilt window's boxes don't fold either")

    win2._btn_run.setChecked(True)
    r.check(win2._sync.running, "session starts with the restored settings")
    pump_until(app, lambda: win2._modules[0].worker is not None, 2.0)
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


def check_serial_error_reported(r: Report) -> None:
    """A pyserial write error after the port opened (an OSError, not an
    MCM6101Error) is reported, not raised out of startup."""
    fake = FakeDriver("COM54")

    def broken(axis, channel):
        raise OSError("WriteFile failed")
    fake.get_mirror_state = broken
    try:
        result = ensure_camera_default(driver_cls=lambda port: fake)
    except Exception as e:                                # noqa: BLE001
        r.check(False, f"a serial error raised {type(e).__name__} out of startup")
        return
    r.check(not result.ok and result.error, "serial error reported as not ok")
    r.check(fake.closed, "port closed after the serial error")


def _part_mirror() -> int:
    r = Report("mirror-startup")
    check_already_correct(r)
    check_corrects_mismatch(r)
    check_corrects_both(r)
    check_port_unavailable(r)
    check_serial_error_reported(r)
    return r.finish()


PARTS = {
    "rigs": _part_rigs,
    "modes": _part_modes,
    "settings": _part_settings,
    "mirror": _part_mirror,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
