"""
Loading and unloading instruments WITHOUT restarting the app.

Until 2026-08-25 the module set was fixed at launch by the startup picker, so
adding the wheel meant closing the app, re-picking and losing the session. The
sidebar's Modules button now calls `MainWindow.set_modules`, which splices
adapters in and out of a live window.

Two things make that harder than it looks, and both are checked here:

  * **Everything a module put on the window has to come back off.** Its settings
    tab, its Signals tab, its docks, and its pyqtgraph views — that last one
    silently: `setCentralWidget` DELETES the old central widget, so a view left
    in `_pg_views` is a dangling C++ object and the next theme toggle takes the
    process down with no Python traceback.
  * **`config.MODULES` order is load-bearing.** `closed_loop` is last so every
    source-providing adapter exists before its panel asks what is on offer, and
    a module loaded later must land in that order, not at the end.

Recording is refused outright: the file's `modules` attribute is written once,
at record start.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_module_hotload.py
"""
from __future__ import annotations

import sys

from _harness import Report, isolate_user_state, qt_app

from acqApp import config


def _keys(win) -> list[str]:
    return [m.key for m in win._modules]


def check_add_remove(r: Report, win) -> None:
    """The set changes, and the adapters follow it."""
    r.check(_keys(win) == ["voltage_cam", "wheel"],
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
    """Unloading everything must not break the window.

    `ModuleSelectDialog` keeps OK disabled until something is ticked, so this
    is unreachable from the UI — but the guard is in the dialog, not in
    `set_modules`, and a window left in pieces by a code path nobody clicks is
    still a window left in pieces.
    """
    win.set_modules(["voltage_cam", "wheel"])
    win.set_modules([])
    r.check(_keys(win) == [], f"every module unloaded ({_keys(win)})")
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
    win.set_modules(["closed_loop", "voltage_cam"])
    r.check(_keys(win)[-1] == "closed_loop",
            f"closed_loop is last when loaded FIRST ({_keys(win)})")
    win.set_modules(["closed_loop", "voltage_cam", "wheel"])
    order = list(config.MODULES)
    got = _keys(win)
    r.check(got == sorted(got, key=order.index),
            f"a module added after closed_loop still sorts before it ({got})")
    # CONTROL: the order really is being imposed, not inherited from the
    # argument — which was given closed_loop first both times.
    r.check(got.index("wheel") < got.index("closed_loop"),
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
    r.check(win._plots_tabs.count() == plots_with,
            "the Signals tabs are untouched (the pupil camera has no plot)")

    # The wheel DOES have a plot, so removing it must drop a Signals tab.
    before = win._plots_tabs.count()
    win.set_modules(["voltage_cam"])
    r.check(win._plots_tabs.count() == before - 1,
            f"the wheel's Signals tab went ({win._plots_tabs.count()} vs "
            f"{before})")


def check_sidebar_follows(r: Report, win) -> None:
    """Each loaded instrument owns a sidebar item, and only while loaded.

    The sidebar is the settings selector since 2026-08-25, so a stale item is
    not cosmetic — it points at a panel that has been deleted.
    """
    win.set_modules(["voltage_cam", "wheel"])
    r.check(set(win._page_actions) == {"saving", "voltage_cam", "wheel"},
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

    # The order is the sidebar's whole readability: Save first, then
    # config.MODULES order.
    win.set_modules(["closed_loop", "wheel", "voltage_cam"])
    labels = [a.text() for a in win._sidebar.actions()
              if a in win._page_actions.values()]
    r.check(labels[0] == "Save", f"Save leads ({labels})")
    keys = [k for k, a in win._page_actions.items() if a.text() != "Save"]
    order = list(config.MODULES)
    r.check(keys == sorted(keys, key=order.index),
            f"modules in config.MODULES order ({keys})")


def check_theme_toggle_survives(r: Report, win) -> None:
    """The failure the view bookkeeping exists to prevent.

    Recolouring walks `_pg_views`; a deleted view there is a native crash, so
    this both toggles the theme and reads a view back afterwards.
    """
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

    # Not rebuilt when the owner is unchanged: central_widget() BUILDS a view
    # per call, so a needless rebuild throws away the live image.
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
    r.check(_keys(win) == ["voltage_cam", "wheel"],
            f"…and changed nothing ({_keys(win)})")
    # CONTROL: the same call succeeds once the recorder is gone, so the refusal
    # is about recording and not about the argument.
    added, removed = win.set_modules(["voltage_cam"])
    r.check(removed == ["wheel"],
            f"control: the same change works with no recorder ({removed})")


def check_closed_loop_offers(r: Report, win) -> None:
    """The closed loop's source list is rebuilt when its neighbours change."""
    win.set_modules(["closed_loop", "voltage_cam"])
    loop = next(m for m in win._modules if m.key == "closed_loop")
    without = set(loop._sources)
    win.set_modules(["closed_loop", "voltage_cam", "wheel"])
    with_wheel = set(loop._sources)
    r.check(len(with_wheel) > len(without),
            f"loading the wheel adds its signal to the loop's offers "
            f"({sorted(without)} -> {sorted(with_wheel)})")
    win.set_modules(["closed_loop", "voltage_cam"])
    r.check(set(loop._sources) == without,
            f"unloading it takes the offer away again ({sorted(loop._sources)})")


def check_camera_handle_survives(r: Report, win) -> None:
    """Unloading the voltage camera must NOT close the shared DCAM handle.

    The window opens the camera once at startup and every worker borrows it,
    because re-opening a just-closed DCAM device crashes the driver natively —
    no traceback, no exit code (docs/HANDOFF.md). Unload/reload is a new way to
    reach that, so the handle is checked across a round trip. `OrcaFireWorker`
    holds the line with `own_cam`: it closes only a handle it opened itself.
    """
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
    """The Devices monitor must not outlive the module set it was built for.

    `ConnectionMonitor` takes a SNAPSHOT of the keys at construction, so a
    cached one would go on probing an instrument that is no longer loaded —
    and on a rig that reads as "the stage is missing" rather than "the stage
    was unloaded".
    """
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
    """The point of the feature: change the set WITHOUT stopping.

    A module loaded into a running session has to build and start its own
    worker — the clock is already past t=0, and nothing is going to call
    `_start_session` again for it.
    """
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

        # The display tick must survive a set that changed under it.
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


def main() -> int:
    r = Report("hotload")
    isolate_user_state()
    app = qt_app()          # keep the reference: a GC'd QApplication aborts
    from acqApp.main import MainWindow

    win = MainWindow(mock=True, enabled={"voltage_cam", "wheel"})
    try:
        check_add_remove(r, win)
        check_empty_set(r, win)
        check_order(r, win)
        check_ui_released(r, win)
        check_sidebar_follows(r, win)
        check_theme_toggle_survives(r, win)
        check_central_pane(r, win)
        check_recording_refused(r, win)
        check_closed_loop_offers(r, win)
        check_devices_monitor(r, win)
        check_camera_handle_survives(r, win)
        check_live_session(r, win)
    finally:
        win.close()
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
