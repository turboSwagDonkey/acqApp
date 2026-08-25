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
    ("pupil_cam",   "exposure",  lambda p: p._spn_exp.setValue(4321.0),
     lambda p: p._spn_exp.value(),              4321.0),
    ("pupil_cam",   "region R",  lambda p: p._spn_lr.setValue(118.0),
     lambda p: p.settings.limit_r,              118.0),
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
    ("dmd",         "trigger",   lambda p: p._cmb_trig.setCurrentText("Software"),
     lambda p: p.settings.trigger_mode,         "Software"),
    # The geometry is the registration to the optics — the DMD settings that
    # most need to survive a restart, since a session recorded at the wrong
    # scale/rotation cannot be located in the field of view afterwards.
    # (On-time / static-hold / repeats are no longer panel controls: the panel
    # hardcodes static_hold and the DMD innately holds one image.)
    ("dmd",         "scale",     lambda p: p._spn_scale.setValue(132.4),
     lambda p: p.settings.scale_pct,            132.4),
    ("dmd",         "rotation",  lambda p: p._spn_rot.setValue(12.5),
     lambda p: p.settings.rotation_deg,         12.5),
    ("dmd",         "offset X",  lambda p: p._spn_dx.setValue(37.0),
     lambda p: p.settings.offset_x,             37.0),
    # The display mode replaced the all-on checkbox. `all_on` is still written,
    # because the session metadata has always carried it.
    # Only ONE mode row: the three are exclusive radios, so a second would
    # simply overwrite the first and prove nothing.
    ("dmd",         "mode-roi",  lambda p: p._rb["roi"].setChecked(True),
     lambda p: p.settings.display_mode,         "roi"),
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

    # The panels live in a pop-up window, not a dock. Everything below edits them
    # while it has never been shown, which is the point: the settings window is
    # built (and wired to the controllers) at startup and only made visible on
    # demand — a lazily-built one would leave the controllers unconfigured.
    dlg = win._settings_dialog
    r.check(dlg is not None and dlg.isWindow(), "settings are a top-level window")
    r.check(not isinstance(dlg, M.QDockWidget), "…and not a dock widget")
    r.check(not dlg.isVisible(), "settings window starts hidden")
    r.check(dlg.tabs.count() == len(config.MODULES) + 1,
            f"a page per module plus Save (got {dlg.tabs.count()})")
    # One sidebar item per page since 2026-08-25; the tab bar is hidden and the
    # sidebar is the selector.
    r.check(not dlg.tabs.tabBar().isVisible(), "the tab bar is hidden")
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

    # First-run size is measured from the panels, then clamped to the screen —
    # checked on default_size() rather than on the shown window, whose final
    # size the window manager has the last word on.
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
    # Clicking the page you are already on shuts the window, as the single
    # ⚙ Settings toggle used to.
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
    # Applied in add_panel(), not in each panel, so a new instrument gets it
    # without doing anything — which is why this checks across every tab.
    from PyQt6.QtWidgets import QGroupBox, QWidget as _QW
    from acqApp import widgets as W
    tabs = {dlg.tabs.tabText(i): dlg.tabs.widget(i).findChildren(QGroupBox)
            for i in range(dlg.tabs.count())}
    flat = [b for v in tabs.values() for b in v]
    r.check(len(flat) >= 10, f"{len(flat)} group boxes across {len(tabs)} tabs")
    r.check(all(b.isCheckable() for b in flat),
            "every settings box has a fold toggle in its title")
    # The affordance is a disclosure arrow, not a tick box: a tick reads as
    # "enable this section". Every title carries one, and the base title is
    # kept so toggling cannot accumulate them.
    r.check(all(b.title().startswith((W.OPEN, W.SHUT)) for b in flat),
            "…drawn as a ▾/▸ dropdown arrow")

    def find(title):
        return next(b for b in flat if getattr(b, "_base_title", "") == title)

    p = panels["pupil_cam"]
    box = find("Eye region")
    box.setChecked(False); box.setChecked(True); box.setChecked(False)
    r.check(box.title().count(W.SHUT) == 1 and "Eye region" in box.title(),
            f"control: toggling three times leaves one arrow ({box.title()!r})")
    box.setChecked(True)            # back open, so the height below is the
    pump(app, 0.05)                 # expanded one
    tall = box.sizeHint().height()
    box.setChecked(False)
    pump(app, 0.05)
    short = box.sizeHint().height()
    r.check(short < tall, f"folding shrinks the box ({tall} → {short} px)")
    # isVisibleTo, not isVisible: the settings window is closed at this point,
    # so isVisible() is False for every widget in it and would pass vacuously.
    r.check(not any(c.isVisibleTo(box) for c in box.findChildren(_QW)),
            "…and its contents are hidden, not merely greyed out")

    # A control the panel had deliberately disabled must not come back enabled.
    # Qt disables the children of an unticked checkable group box, and this
    # leans on it restoring each child's own state rather than enabling all.
    r.check(not p._btn_limit_clear.isEnabled(),
            "control: Clear is disabled while no region is set")
    box.setChecked(True)
    pump(app, 0.05)
    r.check(not p._btn_limit_clear.isEnabled(),
            "…and unfolding does not wrongly re-enable it")
    r.check(all(c.isVisibleTo(box) for c in (p._spn_lx, p._spn_ly, p._spn_lr)),
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

    # ── surviving a write that dies partway ──────────────────────────────────
    # This file is the operator's whole working setup and is rewritten on every
    # spinbox step, while the app can die natively mid-write (a qFatal out of a
    # worker, a DCAM segfault — the reason main enables faulthandler). Writing
    # beside it and renaming is what keeps a half-written file from becoming
    # "no settings at all".
    from acqApp import config as C
    intact = cfg_path.read_text(encoding="utf-8")
    real_dump = C.json.dump

    def die_partway(obj, fh, **kw):
        fh.write('{"settings": {"voltage_cam": {"expos')     # a partial record
        raise OSError("simulated: no space left on device")

    C.json.dump = die_partway
    try:
        C.save_config({"theme": "light", "settings": {"wiped": True}})
    finally:
        C.json.dump = real_dump
    r.check(cfg_path.read_text(encoding="utf-8") == intact,
            "a write that dies partway leaves the previous config untouched")
    r.check(not list(tmp.glob("*.tmp")),
            f"…and cleans up after itself ({[p.name for p in tmp.glob('*.tmp')]})")
    # CONTROL: the damage this prevents is real — the same partial content
    # written in place is what load_config() would then have to read.
    (tmp / "wrecked.json").write_text('{"settings": {"voltage_cam": {"expos',
                                      encoding="utf-8")
    C._CONFIG_PATH, keep_path = tmp / "wrecked.json", C._CONFIG_PATH
    r.check(C.load_config() == {},
            "control: a truncated config really is unreadable")
    r.check((tmp / "wrecked.corrupt.json").is_file(),
            "…and is moved aside rather than silently overwritten with defaults")
    C._CONFIG_PATH = keep_path

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

    # Which boxes were folded is remembered too — collapsing the ones a rig
    # never touches is worth nothing if it has to be redone every launch.
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
