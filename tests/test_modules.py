"""Module sets: every subset builds and runs; loading/unloading in place.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_modules.py [-q] [--part NAME]
"""
from __future__ import annotations

import shutil
import sys
import traceback

from _harness import (Report, isolate_user_state, make_window, pump,
                      pump_until, qt_app, run_parts)
from acqApp import config


# ═══ subsets (was test_module_subsets.py) ═══════════════════════════════

SUBSETS = [
    ["voltage_cam"],
    ["wheel"],                          # General only, no module dock
    ["pupil_cam"],                      # one camera dock, not the primary
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
            r.check(win._btn_emulate.text() == "Emulate", f"{label}: quiet off")
            win._btn_emulate.setChecked(True)
            r.check("no hardware" in win._btn_emulate.text(),
                    f"{label}: Emulate on says so")

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
    loaded = {m.key for m in win._modules}
    r.check(set(win._docks) <= loaded,
            f"no dock outlives its module ({sorted(win._docks)})")
    left = set(win._folds)
    r.check("saving" in left and left <= {"saving"} | loaded,
            f"only Save's section and the always-on modules' remain ({left})")
    try:
        win._display_tick()
        ok = True
    except Exception as e:                      # noqa: BLE001
        ok = False
        r.info(f"raised with no modules: {type(e).__name__}: {e}")
    r.check(ok, "the display tick survives an empty window")

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
    from PyQt6.QtWidgets import QDockWidget
    win.set_modules(["voltage_cam", "wheel", "pupil_cam"])
    r.check("pupil_cam" in win._docks and "pupil_cam" in win._folds,
            "control: the pupil camera has a dock and settings while loaded")
    docks_with = len(win.findChildren(QDockWidget))

    win.set_modules(["voltage_cam", "wheel"])
    r.check("pupil_cam" not in win._docks and "pupil_cam" not in win._folds,
            f"its dock and settings went ({sorted(win._folds)})")
    r.check(len(win.findChildren(QDockWidget)) == docks_with - 1,
            "…off the window, not just out of the dict")

    section = win._folds["wheel"]
    win.set_modules(["voltage_cam"])
    r.check("wheel" not in win._folds and win._sections.indexOf(section) < 0,
            "a General section goes with its module")


def check_sidebar_follows(r: Report, win) -> None:
    """Each loaded instrument owns a card, only while loaded — a stale one
    points at a deleted panel."""
    win.set_modules(["voltage_cam", "wheel"])
    r.check(set(win._folds) ==
            {"saving", "voltage_cam", "wheel"} | config.ALWAYS_ON,
            f"Save plus one per module ({sorted(win._folds)})")

    win.set_modules(["voltage_cam", "wheel", "puffer"])
    r.check("puffer" in _section_keys(win),
            f"a loaded module gains a section ({_section_keys(win)})")

    dead = win._folds["puffer"]
    win.set_modules(["voltage_cam", "wheel"])
    r.check("puffer" not in win._folds,
            f"an unloaded one loses it ({sorted(win._folds)})")
    r.check(win._sections.indexOf(dead) < 0,
            "…and the section really is off General, not just out of the dict")

    win.set_modules(["mirror", "wheel", "voltage_cam"])
    keys = _section_keys(win)
    r.check(keys[0] == "saving", f"Save leads ({keys})")
    order = list(config.MODULES)
    r.check(keys[1:] == sorted(keys[1:], key=order.index),
            f"modules in config.MODULES order ({keys})")


def check_sidebar_cards(r: Report, win) -> None:
    """Each card's mini view reads its module without raising, in General and
    in a dock alike. The DMD's card is its light switch alone."""
    from acqApp.sidebar import MiniView

    win.set_modules(["voltage_cam", "pupil_cam", "wheel", "puffer", "stage",
                     "dmd"])
    for key, fold in win._folds.items():
        if fold.body is None:
            continue
        for view in fold.card.findChildren(MiniView):
            view.refresh()          # unguarded: a raise fails the check
        if key == "dmd":
            r.check(win._module("dmd")._light_btn is not None
                    and fold.card.mini is not None,
                    "dmd: its card holds the light switch")
            continue
        r.check(bool(fold.card.findChildren(MiniView)),
                f"{key}: its card has a live view")


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
    """Modules and devices are one dialog: each row's box is whether it is
    loaded, beside whether its hardware is detected, loaded or not."""
    win.set_modules(["voltage_cam", "wheel"])
    dlg = win._modules_dialog()
    dlg.refresh()
    ticked = {k for k, cb in dlg._boxes.items() if cb.isChecked()}
    r.check(ticked == {"voltage_cam", "wheel"},
            f"the boxes are the loaded set ({sorted(ticked)})")
    r.check(set(dlg._rows) == set(dlg._boxes),
            "every module row reports its device, loaded or not")
    blank = [k for k, (s, _d) in dlg._rows.items() if not s.text().startswith("● ")
             or "checking" in s.text()]
    r.check(not blank, f"after a check every row has a verdict ({blank})")
    r.check("routines" not in dlg._rows,
            "the always-on routines gets no row (it has no device)")
    dlg.deleteLater()


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


def _boom(*_a):
    raise OSError("device gone")


def _catch_escapes() -> tuple[list, object]:
    """An excepthook that records, where PyQt would otherwise abort the run."""
    escaped: list = []
    hook = sys.excepthook
    sys.excepthook = lambda _t, e, _tb: escaped.append(e)
    return escaped, hook


def check_start_failure_rolls_back(r: Report, win) -> None:
    """A module raising at Live view: the session rolls back (workers
    stopped, clock off, button up) instead of aborting the app."""
    win.set_modules(["voltage_cam", "wheel"])
    first, last = win._modules[0], win._modules[-1]
    started: list = []
    real_start = first.start
    first.start = lambda: (real_start(), started.append(first.key))
    last.start = _boom
    escaped, hook = _catch_escapes()
    try:
        win._btn_run.setChecked(True)
    finally:
        sys.excepthook = hook
        del first.start, last.start
    r.check(bool(started), f"control: {first.key} started before {last.key} raised")
    r.check(not escaped, f"nothing escaped the Live view slot ({escaped})")
    r.check(not win._session_on and not win._btn_run.isChecked()
            and not win._sync.running,
            "…the session rolled back: not on, button up, clock off")
    r.check(all(m.worker is None or not m.worker.isRunning() for m in win._modules),
            "…and every worker already started was stopped")
    r.check("Live view failed" in win.statusBar().currentMessage(),
            f"…and says why ({win.statusBar().currentMessage()!r})")
    win._btn_run.setChecked(True)
    r.check(win._session_on, "Live view starts normally afterwards")
    win._btn_run.setChecked(False)


def check_display_failure_contained(r: Report, win) -> None:
    """One module's update_display raising: the tick carries on to the rest,
    tick after tick, and reports it once."""
    win._btn_run.setChecked(True)
    first, last = win._modules[0], win._modules[-1]
    bad: list = []
    ran: list = []
    real = last.update_display

    def raising():
        bad.append(1)
        raise ValueError("bad frame")

    first.update_display = raising
    last.update_display = lambda: (ran.append(1), real())
    try:
        win._display_tick()
        win._display_tick()
        escaped = ""
    except Exception as e:                          # noqa: BLE001
        escaped = f"{type(e).__name__}: {e}"
    finally:
        del first.update_display, last.update_display
        win._btn_run.setChecked(False)
    r.check(not escaped and len(bad) == 2,
            f"a raising update_display is contained, tick after tick ({escaped or len(bad)})")
    r.check(len(ran) == 2, f"…and {last.key} still refreshes after it")
    failed = getattr(win, "_display_failed", None)
    r.check(failed == {first.key}, f"…reported once per module ({failed})")


def check_close_survives_a_failing_device(r: Report, win) -> None:
    """App exit: one controller raising on close still closes the others."""
    first, last = win._modules[0], win._modules[-1]
    closed: list = []
    real = last.close_controller
    first.close_controller = _boom
    last.close_controller = lambda: (closed.append(last.key), real())
    escaped, hook = _catch_escapes()
    try:
        win.close()
    finally:
        sys.excepthook = hook
        del first.close_controller, last.close_controller
    r.check(not escaped, f"nothing escaped closeEvent ({escaped})")
    r.check(closed == [last.key],
            f"{last.key} closed although {first.key} raised first ({closed})")


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


def _section_keys(win) -> list[str]:
    lay = win._sections
    return [lay.itemAt(i).widget().key for i in range(lay.count())]


def _click(widget) -> None:
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))


def check_folds(r: Report, win) -> None:
    """Every panel, Save's included, folds under its card inside the main
    window: a General section, or the top of its module's own dock. A click
    on the card opens or shuts it, and is remembered."""
    from _harness import MemorySettings
    win.set_modules(["voltage_cam", "wheel"])

    panelled = {m.key for m in win._modules if m.panel is not None}
    folded = {k for k, f in win._folds.items() if f.body is not None}
    r.check(folded == panelled | {"saving"},
            f"a fold per panel, plus Save's ({sorted(folded)})")
    r.check(win._save_panel.window() is win
            and win._module("wheel").panel.window() is win,
            "…inside the main window, not a window of its own")
    sections = _section_keys(win)
    r.check("voltage_cam" in win._docks and "voltage_cam" not in sections
            and "wheel" in sections,
            f"the camera's settings sit in its dock, the wheel's in General "
            f"({sections})")

    fold = win._folds["wheel"]
    r.check(not fold.is_open() and not fold.body.isVisibleTo(fold),
            "a section starts folded")
    _click(fold.card)
    r.check(fold.is_open() and fold.body.isVisibleTo(fold),
            "a click on its card opens it")
    r.check(MemorySettings.store.get("fold/wheel") == "1", "…and is remembered")
    _click(fold.card)
    r.check(not fold.is_open(), "a second click shuts it")
    r.check(fold.maximumHeight() <= fold.card.sizeHint().height(),
            "shut, it is no taller than its card (a splitter ignores size "
            "policies)")


def check_dock_layout(r: Report, win) -> None:
    """The sketch: General over stage | camera over routines | DMD over pupil
    cam, every pane a dock that moves only within the window. Reset undoes a
    rearrangement; a module loaded later joins its column; a camera's
    settings open over its view."""
    from PyQt6.QtWidgets import QDockWidget
    app = qt_app()
    mods = ["voltage_cam", "routines", "dmd", "pupil_cam", "stage", "wheel"]
    win.set_modules(mods)
    r.check(win.centralWidget() is None,
            "no central widget, so every pane can be dragged")
    r.check(set(win._docks) == {"voltage_cam", "routines", "dmd", "pupil_cam",
                                "stage"},
            f"a dock per own-dock module ({sorted(win._docks)})")
    every = (win._general_dock, *win._docks.values())
    r.check(all(d.features() == QDockWidget.DockWidgetFeature.DockWidgetMovable
                for d in every),
            "every dock moves, but none floats out of the window or closes")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QMainWindow
    r.check(not win.dockOptions() & QMainWindow.DockOption.AllowTabbedDocks,
            "docks never stack as tabs: no region hides behind another")
    # Moved under routines: the geometry checks below prove Reset undid it.
    win.splitDockWidget(win._docks["routines"], win._docks["dmd"],
                        Qt.Orientation.Vertical)
    win._reset_action.trigger()

    win.resize(1600, 900)
    win.show()
    try:
        pump_until(app, lambda: win._docks["pupil_cam"].geometry().y() > 0)
        d = {k: win._docks[k].geometry() for k in win._docks}
        g = win._general_dock.geometry()
        r.check(g.x() < d["voltage_cam"].x() < d["dmd"].x()
                and d["voltage_cam"].x() == d["routines"].x()
                and d["dmd"].x() == d["pupil_cam"].x(),
                "three columns: General, camera + routines, DMD + pupil cam")
        r.check(d["voltage_cam"].y() < d["routines"].y()
                and d["dmd"].y() < d["pupil_cam"].y(),
                "the camera over routines, the DMD over the pupil cam")
        r.check(d["stage"].x() == g.x() and d["stage"].y() > g.y(),
                f"the stage under General, in its column ({d['stage']} vs {g})")

        win.set_modules([k for k in mods if k != "stage"])
        win.set_modules(mods)
        pump_until(app, lambda: win._docks["stage"].geometry().y() > 0)
        got = win._docks["stage"].geometry()
        g = win._general_dock.geometry()
        r.check(got.x() == g.x() and got.y() > g.y(),
                f"a stage loaded later lands under General again ({got} vs {g})")

        win.set_modules([k for k in mods if k != "pupil_cam"])
        win.set_modules(mods)
        pump_until(app, lambda: win._docks["pupil_cam"].geometry().y() > 0)
        got = win._docks["pupil_cam"].geometry()
        dmd = win._docks["dmd"].geometry()
        r.check(got.x() == dmd.x() and got.y() > dmd.y(),
                f"a pupil cam loaded later lands under the DMD ({got} vs {dmd})")

        fold = win._folds["voltage_cam"]
        fold.set_open(False)
        app.processEvents()
        shut = fold.height()
        fold.set_open(True)
        pump_until(app, lambda: fold.height() > shut + 50)
        split = fold.parentWidget()
        r.check(fold.height() > shut + 50 and split.sizes()[1] > 0,
                f"the camera's settings open over its view ({shut} -> "
                f"{fold.height()} px, view {split.sizes()[1]} px)")
        fold.set_open(False)
    finally:
        win.hide()


def check_matplotlib_stays_lazy(r: Report, win) -> None:
    """pyqtgraph's LUT menu used to import matplotlib (~0.4 s) at window build;
    style.py defers it to the first open of the "Others" colormap submenu."""
    r.check("matplotlib" not in sys.modules,
            "building a window with an image view does not import matplotlib")
    from pyqtgraph.widgets.ColorMapMenu import ColorMapMenu
    menu = ColorMapMenu(showColorMapSubMenus=True)      # held: a collected one deletes its actions
    mpl = next(a for a in menu.actions() if a.text() == "matplotlib").menu()
    others = next(a for a in mpl.actions() if a.text() == "Others").menu()
    others.aboutToShow.emit()
    r.check(len(others.actions()) > 0, "the Others colormap submenu still fills on open")


def check_dmd_shared_alignment(r: Report) -> None:
    """The standalone DMD app's file is another program's: a junk value used
    to raise from `float()` at startup and on every DMD settings save."""
    from acqApp.adapters import dmd as dmd_adapter
    from acqApp.devices.dmd import alp

    real = alp.sibling_config
    try:
        alp.sibling_config = lambda: {"defaultScale": "abc", "defaultRot": "12.5"}
        r.check(dmd_adapter._shared_alignment() == {"rotation_deg": 12.5},
                "a non-numeric shared value is skipped, a numeric string kept")
        alp.sibling_config = lambda: {"defaultScale": None, "defaultRot": float("nan")}
        r.check(dmd_adapter._shared_alignment() == {},
                "null and NaN are skipped, not raised or adopted")
        alp.sibling_config = lambda: ["not", "a", "dict"]
        r.check(dmd_adapter._shared_alignment() == {},
                "a file that isn't an object is ignored")
    finally:
        alp.sibling_config = real


def _part_idle_ui() -> int:
    """What the window says before and around a session."""
    r = Report("idle-ui")
    isolate_user_state()
    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from PyQt6.QtWidgets import QLabel

    win = make_window({"voltage_cam", "stage"})
    cam = next(m for m in win._modules if m.key == "voltage_cam")
    hints = win.findChildren(QLabel, "idleHint")
    r.check(len(hints) == 1 and not hints[0].isHidden(),
            "an idle camera view says to press Live view")

    cam.panel._spn_target_hz.setValue(100.0)
    r.check(cam._mini_text()[1].startswith("⚠ only"),
            f"a rate the writer can't keep shows on the folded row "
            f"({cam._mini_text()[1]!r})")
    cam.panel._spn_target_hz.setValue(10.0)
    r.check("⚠" not in cam._mini_text()[1],
            f"control: a rate it can keep doesn't ({cam._mini_text()[1]!r})")

    win._btn_run.setChecked(True)
    r.check(pump_until(app, lambda: hints[0].isHidden()),
            "the hint goes with the first frame")
    win._btn_run.setChecked(False)
    win.close()
    pump(app, 0.1)
    return r.finish()


def _part_hotload() -> int:
    r = Report("hotload")
    isolate_user_state()
    app = qt_app()          # held: a collected QApplication aborts

    win = make_window({"voltage_cam", "wheel"})
    try:
        check_matplotlib_stays_lazy(r, win)
        check_always_on(r, win)
        check_folds(r, win)
        check_add_remove(r, win)
        check_empty_set(r, win)
        check_order(r, win)
        check_ui_released(r, win)
        check_sidebar_follows(r, win)
        check_sidebar_cards(r, win)
        check_dock_layout(r, win)
        check_recording_refused(r, win)
        check_signal_offers(r, win)
        check_devices_monitor(r, win)
        check_camera_handle_survives(r, win)
        check_live_session(r, win)
        check_start_failure_rolls_back(r, win)
        check_display_failure_contained(r, win)
        check_dmd_shared_alignment(r)
        check_close_survives_a_failing_device(r, win)
    finally:
        win.close()
    return r.finish()


PARTS = {
    "subsets": _part_subsets,
    "idle_ui": _part_idle_ui,
    "hotload": _part_hotload,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
