"""Module sets: every subset builds and runs; loading/unloading in place.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_modules.py [-q] [--part NAME]
"""
from __future__ import annotations

import shutil
import sys
import traceback

from _harness import (Report, isolate_user_state, make_window, pump, qt_app,
                      run_parts)
from acqApp import config


# ═══ subsets (was test_module_subsets.py) ═══════════════════════════════

SUBSETS = [
    ["voltage_cam"],
    ["wheel"],                          # no central view at all
    ["pupil_cam"],                      # extra dock, no central view
    ["stage"],                          # panel only, no plot
    ["puffer", "dmd"],                  # controllers only, no workers
    ["voltage_cam", "wheel"],
    ["pupil_cam", "wheel", "stage"],
    ["vis_stim"],                       # visuomotor with no wheel to read
    ["wheel", "vis_stim"],              # a signal and its consumer
]


def _part_subsets() -> int:
    r = Report("subsets")
    tmp = isolate_user_state()

    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from acqApp import probe

    for subset in SUBSETS + [list(config.MODULES)]:
        label = "+".join(subset)
        try:
            win = make_window(set(subset))
            win._btn_run.setChecked(True)
            pump(app, 0.4)
            for _ in range(4):
                win._display_tick()
                pump(app, 0.03)
            win._btn_run.setChecked(False)

            # The Emulate toggle rebuilds the output controllers in place.
            win._btn_emulate.setChecked(False)
            win._btn_emulate.setChecked(True)

            # Every kwarg an adapter offers must be one probe_all accepts, or
            # the Devices window dies with a TypeError for this subset.
            kw = win._probe_kwargs()
            assert isinstance(kw, dict)
            probe.probe_all(subset, **kw)

            win.close()
            pump(app, 0.1)
            r.check(True, label)
        except Exception as e:
            traceback.print_exc()
            r.check(False, f"{label}: {type(e).__name__}: {e}")

    # ── teardown survives one module failing to stop ─────────────────────────
    # Unguarded, the first raise in `_stop_session` left later workers running,
    # the clock and trigger bus alive under a "Stopped" UI, and (via
    # closeEvent) the DCAM handle unclosed — a native crash.
    try:
        win = make_window({"wheel", "stage", "puffer"})
        win._btn_run.setChecked(True)
        pump(app, 0.3)

        victim = win._modules[0]
        later = win._modules[1:]
        stopped: list[str] = []
        for m in later:
            original = m.stop
            m.stop = (lambda mod=m, orig=original: (stopped.append(mod.key),
                                                    orig())[1])

        def boom():
            raise RuntimeError("serial port went away")
        victim.stop = boom

        win._stop_session()
        r.check([m.key for m in later] == stopped,
                f"one module raising in stop() does not strand the others "
                f"(stopped {stopped})")
        r.check(not win._sync.running,
                "…and the clock/trigger bus is still torn down")
        r.check(win._btn_run.text() == "Live view",
                "…and the UI returns to a consistent state")

        # CONTROL: the same failure through the old unguarded loop.
        reached: list[str] = []
        adapters = [("victim", boom)] + [(m.key, lambda: None) for m in later]
        try:
            for key, fn in adapters:
                fn()
                reached.append(key)
        except RuntimeError:
            pass
        r.check(reached == [],
                f"control: the unguarded loop strands every later module "
                f"(reached {reached})")

        win.close()
        pump(app, 0.1)
    except Exception as e:
        traceback.print_exc()
        r.check(False, f"teardown-failure: {type(e).__name__}: {e}")

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


# ═══ hotload (was test_module_hotload.py) ═══════════════════════════════

def _keys(win) -> list[str]:
    return [m.key for m in win._modules]


def _chosen(win) -> list[str]:
    """The loaded keys the operator picked (not `config.ALWAYS_ON`)."""
    return [k for k in _keys(win) if k not in config.ALWAYS_ON]


def check_add_remove(r: Report, win) -> None:
    """The set changes, and the adapters follow it."""
    r.check(_chosen(win) == ["voltage_cam", "wheel"],
            f"built with the two asked for ({_keys(win)})")

    added, removed = win.set_modules(["voltage_cam", "wheel", "puffer"])
    r.check(added == ["puffer"] and removed == [],
            f"puffer reported as loaded ({added}, {removed})")
    r.check("puffer" in _keys(win), f"…and is in the list ({_keys(win)})")

    added, removed = win.set_modules(["voltage_cam", "puffer"])
    r.check(added == [] and removed == ["wheel"],
            f"wheel reported as unloaded ({added}, {removed})")
    r.check("wheel" not in _keys(win), f"…and is gone ({_keys(win)})")

    added, removed = win.set_modules(["voltage_cam", "puffer"])
    r.check(added == [] and removed == [],
            f"a no-op change reports nothing ({added}, {removed})")


def check_empty_set(r: Report, win) -> None:
    """Unloading everything must not break the window — unreachable from the
    UI, but the guard is in the dialog, not in `set_modules`."""
    win.set_modules(["voltage_cam", "wheel"])
    win.set_modules([])
    r.check(_chosen(win) == [],
            f"every module the operator chose unloaded ({_keys(win)})")
    r.check(win._central_owner is None, "the centre pane fell back")
    r.check(win._plots_tabs.count() == 0, "no Signals tabs left")
    r.check(win._settings_dialog.tabs.count() == 1,
            f"only the Save tab remains "
            f"({win._settings_dialog.tabs.count()})")
    try:
        win._display_tick()
        win._on_theme_toggled(True)
        ok = True
    except Exception as e:                      # noqa: BLE001
        ok = False
        r.info(f"raised with no modules: {type(e).__name__}: {e}")
    r.check(ok, "the display tick and theme toggle survive an empty window")

    added, _ = win.set_modules(["voltage_cam", "wheel"])
    r.check(sorted(added) == ["voltage_cam", "wheel"],
            f"…and everything loads back ({added})")


def check_order(r: Report, win) -> None:
    """A module loaded later still lands in config.MODULES order."""
    win.set_modules(["mirror", "voltage_cam"])
    got = _keys(win)
    r.check(got.index("voltage_cam") < got.index("mirror"),
            f"mirror sorts after the camera though loaded FIRST ({got})")
    win.set_modules(["mirror", "voltage_cam", "wheel"])
    order = list(config.MODULES)
    got = _keys(win)
    r.check(got == sorted(got, key=order.index),
            f"a module added after mirror still sorts before it ({got})")
    # CONTROL: imposed, not inherited — the argument put mirror first.
    r.check(got.index("wheel") < got.index("mirror"),
            f"…and that is not the order it was asked for ({got})")


def check_ui_released(r: Report, win) -> None:
    """A module takes its whole UI with it."""
    win.set_modules(["voltage_cam", "wheel", "pupil_cam"])
    tabs_with = win._settings_dialog.tabs.count()
    plots_with = win._plots_tabs.count()
    views_with = len(win._pg_views)
    docks_with = len(win.findChildren(type(win._plots_dock)))
    r.check("pupil_cam" in win._module_docks and win._module_docks["pupil_cam"],
            "the pupil camera's dock was attributed to it")

    win.set_modules(["voltage_cam", "wheel"])
    r.check(win._settings_dialog.tabs.count() == tabs_with - 1,
            f"its settings tab went ({win._settings_dialog.tabs.count()} vs "
            f"{tabs_with})")
    r.check(len(win.findChildren(type(win._plots_dock))) == docks_with - 1,
            "its dock went")
    r.check(len(win._pg_views) < views_with,
            f"its pyqtgraph views were unregistered ({len(win._pg_views)} vs "
            f"{views_with}) — a stale one crashes the theme toggle natively")
    r.check(win._plots_tabs.count() == plots_with - 1,
            f"its Signals tab went too ({win._plots_tabs.count()} vs "
            f"{plots_with})")

    before = win._plots_tabs.count()
    win.set_modules(["voltage_cam"])
    r.check(win._plots_tabs.count() == before - 1,
            f"the wheel's Signals tab went ({win._plots_tabs.count()} vs "
            f"{before})")


def check_sidebar_follows(r: Report, win) -> None:
    """Each loaded instrument owns a sidebar item, only while loaded — a
    stale item points at a deleted panel."""
    win.set_modules(["voltage_cam", "wheel"])
    r.check(set(win._page_actions) ==
            {"saving", "voltage_cam", "wheel"} | config.ALWAYS_ON,
            f"Save plus one per module ({sorted(win._page_actions)})")

    win.set_modules(["voltage_cam", "wheel", "puffer"])
    r.check("puffer" in win._page_actions,
            f"a loaded module gains one ({sorted(win._page_actions)})")

    dead = win._page_actions["puffer"]
    win.set_modules(["voltage_cam", "wheel"])
    r.check("puffer" not in win._page_actions,
            f"an unloaded one loses it ({sorted(win._page_actions)})")
    r.check(dead not in win._sidebar.actions(),
            "…and the item really is off the toolbar, not just out of the dict")

    win.set_modules(["mirror", "wheel", "voltage_cam"])
    labels = [a.text() for a in win._sidebar.actions()
              if a in win._page_actions.values()]
    r.check(labels[0] == "Save", f"Save leads ({labels})")
    keys = [k for k, a in win._page_actions.items() if a.text() != "Save"]
    order = list(config.MODULES)
    r.check(keys == sorted(keys, key=order.index),
            f"modules in config.MODULES order ({keys})")


def check_theme_toggle_survives(r: Report, win) -> None:
    """Recolouring walks `_pg_views`; a deleted view there is a native
    crash."""
    win.set_modules(["voltage_cam", "wheel"])
    win.set_modules(["wheel"])              # drops the CENTRAL view's items
    try:
        win._on_theme_toggled(False)
        win._on_theme_toggled(True)
        ok = True
    except Exception as e:                  # noqa: BLE001
        ok = False
        r.info(f"theme toggle raised: {type(e).__name__}: {e}")
    r.check(ok, "the theme still toggles after the centre pane's owner was "
                "unloaded")
    r.check(all(v is not None for v in win._pg_views),
            "no dead entries left in _pg_views")


def check_central_pane(r: Report, win) -> None:
    """The centre follows its owner, and is not rebuilt when nothing moved."""
    win.set_modules(["wheel"])
    r.check(win._central_owner is None,
            f"no owner with the camera unloaded ({win._central_owner})")
    win.set_modules(["voltage_cam", "wheel"])
    r.check(win._central_owner == "voltage_cam",
            f"the camera claims it when loaded ({win._central_owner})")

    # central_widget() BUILDS a view: a needless rebuild drops the live image.
    was = win.centralWidget()
    win.set_modules(["voltage_cam", "wheel", "puffer"])
    r.check(win.centralWidget() is was,
            "loading an unrelated module leaves the centre pane alone")


def check_recording_refused(r: Report, win) -> None:
    """No module change mid-file, and the button says so."""
    win.set_modules(["voltage_cam", "wheel"])

    class _FakeRec:
        pass

    win._recorder = _FakeRec()
    try:
        win.set_modules(["voltage_cam"])
        raised = False
    except RuntimeError:
        raised = True
    finally:
        win._recorder = None
    r.check(raised, "set_modules refuses while a recorder is open")
    r.check(_chosen(win) == ["voltage_cam", "wheel"],
            f"…and changed nothing ({_keys(win)})")
    # CONTROL: the refusal is about recording, not the argument.
    added, removed = win.set_modules(["voltage_cam"])
    r.check(removed == ["wheel"],
            f"control: the same change works with no recorder ({removed})")


def check_signal_offers(r: Report, win) -> None:
    """Signal sources follow the loaded modules: visuomotor looks the wheel
    up per call, so a stale offer would read an unloaded worker."""
    def offered() -> set[str]:
        return {s.key for s in win.signal_sources()}
    win.set_modules(["vis_stim", "voltage_cam"])
    without = offered()
    win.set_modules(["vis_stim", "voltage_cam", "wheel"])
    with_wheel = offered()
    r.check("wheel_speed_live" in with_wheel - without,
            f"loading the wheel adds its signal to the offers "
            f"({sorted(without)} -> {sorted(with_wheel)})")
    win.set_modules(["vis_stim", "voltage_cam"])
    r.check(offered() == without,
            f"unloading it takes the offer away again ({sorted(offered())})")


def check_camera_handle_survives(r: Report, win) -> None:
    """Unloading the voltage camera must NOT close the shared DCAM handle:
    re-opening a just-closed DCAM device crashes the driver natively."""
    sentinel = object()
    win._cam_handle = sentinel
    try:
        win.set_modules(["voltage_cam", "wheel"])
        win._btn_run.setChecked(True)
        win.set_modules(["wheel"])                  # camera OUT, mid-session
        r.check(win._cam_handle is sentinel,
                "unloading the camera left the shared handle open")
        win.set_modules(["voltage_cam", "wheel"])   # …and back IN
        r.check(win._cam_handle is sentinel,
                "reloading it reused that same handle rather than re-opening")
        win._btn_run.setChecked(False)
    finally:
        win._cam_handle = None


def check_devices_monitor(r: Report, win) -> None:
    """`ConnectionMonitor` snapshots the keys: a cached one reports an
    unloaded stage as "missing"."""
    win.set_modules(["voltage_cam", "wheel"])
    win._show_devices()
    r.check(win._devices_dialog is not None, "the monitor opens")
    built_for = win._devices_dialog
    win._devices_dialog.close()

    win.set_modules(["voltage_cam"])
    r.check(win._devices_dialog is None,
            "changing the module set discards it")
    win._show_devices()
    r.check(win._devices_dialog is not built_for,
            "…so the next open builds a fresh one for the new set")
    win._devices_dialog.close()


def check_live_session(r: Report, win) -> None:
    """Change the set WITHOUT stopping: nothing calls `_start_session` again
    for a module loaded mid-session."""
    win.set_modules(["voltage_cam", "wheel"])
    win._btn_run.setChecked(True)
    try:
        r.check(win._session_on, "a session is running")
        running = [m for m in win._modules if m.worker is not None]
        r.check(len(running) >= 1,
                f"…with workers ({[m.key for m in running]})")

        win.set_modules(["voltage_cam", "wheel", "pupil_cam"])
        r.check(win._session_on, "still running after loading a module")
        pupil = next(m for m in win._modules if m.key == "pupil_cam")
        r.check(pupil.worker is not None,
                "the module loaded mid-session built its own worker")
        r.check(bool(pupil.worker.isRunning()),
                "…and started it, without _start_session being called again")

        wheel = next(m for m in win._modules if m.key == "wheel")
        w = wheel.worker
        win.set_modules(["voltage_cam", "pupil_cam"])
        r.check(win._session_on, "still running after unloading a module")
        r.check(w is None or not w.isRunning(),
                "the unloaded module's worker was stopped, not abandoned")
        r.check("wheel" not in _keys(win), f"…and it is gone ({_keys(win)})")

        try:
            win._display_tick()
            ticked = True
        except Exception as e:                      # noqa: BLE001
            ticked = False
            r.info(f"display tick raised: {type(e).__name__}: {e}")
        r.check(ticked, "the ~30 Hz display tick runs over the new set")
    finally:
        win._btn_run.setChecked(False)
    r.check(not win._session_on, "and it stops cleanly afterwards")


def check_always_on(r: Report, win) -> None:
    """`config.ALWAYS_ON` (the routine panel) cannot be switched off: the
    picker offers no checkbox, `selected()` puts it back, and so does
    `set_modules` — each alone leaves a way to drop it."""
    from PyQt6.QtWidgets import QCheckBox

    from acqApp.dialogs import ModuleSelectDialog

    r.check(config.ALWAYS_ON and config.ALWAYS_ON <= set(config.MODULES),
            f"ALWAYS_ON names real modules ({sorted(config.ALWAYS_ON)})")

    dlg = ModuleSelectDialog(["wheel"])
    boxes = {cb.text() for cb in dlg.findChildren(QCheckBox)}
    labels = {config.MODULES[k] for k in config.ALWAYS_ON}
    r.check(not (boxes & labels),
            f"the picker offers no checkbox for it ({sorted(boxes)})")
    r.check(config.ALWAYS_ON <= set(dlg.selected()),
            f"…and hands it back anyway ({dlg.selected()})")
    # CONTROL: or selected() could be returning everything.
    r.check("wheel" in dlg.selected() and "puffer" not in dlg.selected(),
            f"control: the ticked modules are still what comes back "
            f"({dlg.selected()})")
    dlg.deleteLater()

    win.set_modules(["voltage_cam"])
    r.check(config.ALWAYS_ON <= set(_keys(win)),
            f"set_modules cannot drop it either ({_keys(win)})")
    _, removed = win.set_modules(["voltage_cam"])
    r.check(removed == [], f"…and asking again unloads nothing ({removed})")
    r.check(config.ALWAYS_ON <= set(config.load_enabled_modules()),
            f"a saved config that omits it reads back with it "
            f"({config.load_enabled_modules()})")


def check_own_window(r: Report, win) -> None:
    """A module with `own_window` gets a window, not a settings page, from
    the same sidebar gesture."""
    key = next(iter(config.ALWAYS_ON))
    win.set_modules(["voltage_cam", "wheel"])

    m = next(x for x in win._modules if x.key == key)
    r.check(m.own_window, f"{key} asks for its own window")
    own = win._panel_windows.get(key)
    r.check(own is not None and own.isWindow(),
            f"…and the window exists ({own})")
    r.check(win._settings_dialog.panel_index(m.panel) < 0,
            "…while its panel is NOT a page of the settings window")
    # CONTROL: a module that did not ask for one is still a page.
    other = next(x for x in win._modules if x.key == "wheel")
    r.check(not other.own_window
            and win._settings_dialog.panel_index(other.panel) >= 0,
            "control: the wheel's panel is still a settings page")

    r.check(not own.isVisible(), "the window starts hidden, as the settings do")
    win._page_actions[key].trigger()
    r.check(own.isVisible(), "its sidebar item opens it")
    r.check(win._page_actions[key].isChecked(),
            "…and lights the item while it is open")
    win._page_actions["wheel"].trigger()
    r.check(own.isVisible() and win._settings_dialog.isVisible(),
            "opening a settings page leaves the own window open")
    r.check(win._page_actions[key].isChecked()
            and win._page_actions["wheel"].isChecked(),
            "…and both items stay lit — they are different windows")

    own.close()
    r.check(not win._page_actions[key].isChecked(),
            "closing it with its own X un-lights the item")
    win._settings_dialog.hide()


def _part_hotload() -> int:
    r = Report("hotload")
    isolate_user_state()
    app = qt_app()          # held: a collected QApplication aborts

    win = make_window({"voltage_cam", "wheel"})
    try:
        check_always_on(r, win)
        check_own_window(r, win)
        check_add_remove(r, win)
        check_empty_set(r, win)
        check_order(r, win)
        check_ui_released(r, win)
        check_sidebar_follows(r, win)
        check_theme_toggle_survives(r, win)
        check_central_pane(r, win)
        check_recording_refused(r, win)
        check_signal_offers(r, win)
        check_devices_monitor(r, win)
        check_camera_handle_survives(r, win)
        check_live_session(r, win)
    finally:
        win.close()
    return r.finish()


PARTS = {
    "subsets": _part_subsets,
    "hotload": _part_hotload,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
