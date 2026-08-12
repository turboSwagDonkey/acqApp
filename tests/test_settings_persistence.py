"""
Panel settings survive a restart (audit #4).

Only the camera and Save tabs used to persist. Every other panel was built
bare, so each launch reset the wheel's V/rev and diameter to defaults — the two
constants that scale every wheel number written into a session file, and both
still unmeasured on this rig. A value that resets silently is worse than one
that is missing: the file records the default as though it were measured.

The test is a real restart: build a MainWindow, edit every panel, close it,
build a second window in the same process and read the panels back.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_settings_persistence.py
"""
from __future__ import annotations

import json
import shutil
import sys

from _harness import Report, isolate_user_state, pump, qt_app

# (module key, panel attribute, setter, reader, expected) — one distinctive,
# non-default value per panel so a stuck default cannot pass.
EDITS = [
    ("voltage_cam", "exposure",  lambda p: p._spn_exposure.setValue(7321.0),
     lambda p: p._spn_exposure.value(),        7321.0),
    ("voltage_cam", "binning",   lambda p: p._cmb_binning.setCurrentIndex(1),
     lambda p: p.get_config().binning,          2),
    ("pupil_cam",   "threshold", lambda p: p._spn_thr.setValue(77),
     lambda p: p._spn_thr.value(),              77),
    ("pupil_cam",   "fit shape", lambda p: p._cmb_fit.setCurrentText("ellipse"),
     lambda p: p._cmb_fit.currentText(),        "ellipse"),
    ("pupil_cam",   "rays",      lambda p: p._spn_rays.setValue(96),
     lambda p: p._spn_rays.value(),             96),
    ("wheel",       "V/rev",     lambda p: p._spn_vpr.setValue(3.210),
     lambda p: p.settings.volts_per_rev,        3.210),
    ("wheel",       "diameter",  lambda p: p._spn_dia.setValue(123.0),
     lambda p: p.settings.wheel_dia_mm,         123.0),
    ("wheel",       "rate",      lambda p: p._spn_rate.setValue(200.0),
     lambda p: p.settings.rate,                 200.0),
    ("puffer",      "channel",   lambda p: p._cmb_chan.setCurrentText("Dev3/port0/line2"),
     lambda p: p.settings.channel,              "Dev3/port0/line2"),
    ("puffer",      "duration",  lambda p: p._spn_dur.setValue(0.321),
     lambda p: p.settings.duration_s,           0.321),
    ("stage",       "port",      lambda p: p._cmb_port.setCurrentText("COM9"),
     lambda p: p.settings.port,                 "COM9"),
    ("stage",       "poll rate", lambda p: p._spn_rate.setValue(7.0),
     lambda p: p.settings.poll_hz,              7.0),
    ("dmd",         "on-time",   lambda p: p._spn_on.setValue(250.0),
     lambda p: p.settings.on_time_ms,           250.0),
    ("dmd",         "static",    lambda p: p._chk_static.setChecked(True),
     lambda p: p.settings.static_hold,          True),
    ("dmd",         "trigger",   lambda p: p._cmb_trig.setCurrentText("Software"),
     lambda p: p.settings.trigger_mode,         "Software"),
    ("dmd",         "repeats",   lambda p: p._spn_rep.setValue(7),
     lambda p: p.settings.n_repeats,            7),
]

SAVE_EDITS = [
    ("subject",  lambda p: p._ed_subject.setText("m17"),         "m17"),
    ("template", lambda p: p._ed_template.setText("{subject}_{time}"),
     "{subject}_{time}"),
]


def same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def main() -> int:
    r = Report("settings")
    tmp = isolate_user_state()

    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from acqApp import config
    import acqApp.main as M

    enabled = set(config.MODULES)

    # ── first launch: edit every panel ───────────────────────────────────────
    win = M.MainWindow(cam_info=None, mock=True, enabled=enabled, cam_handle=None)
    panels = {m.key: m.panel for m in win._modules}
    for key, label, setter, reader, expected in EDITS:
        setter(panels[key])
        r.check(same(reader(panels[key]), expected),
                f"{key}: {label} accepted the edit")
    for label, setter, _expected in SAVE_EDITS:
        setter(win._save_panel)
    win._save_panel._on_edited()

    # The LED is runtime state, not a setting: restoring it would switch the
    # illumination on in an empty rig at launch.
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
                "saving"):
        r.check(key in saved, f"'{key}' section present in the config")

    # The stage's axis calibration belongs to the shared stage_control config;
    # StageSettings nests two StageAxis objects that would not survive this
    # flat JSON, so only the panel's own two fields may be written here.
    r.check(set(saved.get("stage", {})) == {"port", "poll_hz"},
            f"stage section is port/poll_hz only (got {sorted(saved.get('stage', {}))})")
    r.check("led" not in json.dumps(saved).lower(),
            "the eye-tracking LED was not persisted as a setting")

    # ── second launch: read the panels back ──────────────────────────────────
    win2 = M.MainWindow(cam_info=None, mock=True, enabled=enabled, cam_handle=None)
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

    # A session must actually run on the restored values.
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


if __name__ == "__main__":
    sys.exit(main())
