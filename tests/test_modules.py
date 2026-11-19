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
    """Each mini view reads its module without raising: on its card in
    General, on its tab's title bar in a dock (no card row there). The DMD's
    is its light switch alone."""
    from acqApp.sidebar import MiniView

    win.set_modules(["voltage_cam", "pupil_cam", "wheel", "puffer", "stage",
                     "dmd"])
    for key, fold in win._folds.items():
        if fold.body is None:
            continue
        dock = win._docks.get(key)
        home = dock.titleBarWidget() if dock is not None else fold.card
        if dock is not None:
            r.check(fold.card.isHidden() and fold.card.mini is None,
                    f"{key}: no card row in its tab")
        for view in home.findChildren(MiniView):
            view.refresh()          # unguarded: a raise fails the check
        if key == "dmd":
            btn = win._module("dmd")._light_btn
            r.check(btn is not None and home.isAncestorOf(btn),
                    "dmd: its title bar holds the light switch")
            continue
        r.check(bool(home.findChildren(MiniView)),
                f"{key}: its {'title bar' if dock else 'card'} has a live view")


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


def check_picker_columns(r: Report) -> None:
    """The picker's columns: every pickable module exactly once, each
    column's title box ticks its whole bundle."""
    from PyQt6.QtCore import Qt

    from acqApp.dialogs import ModuleSelectDialog

    groups = config.module_groups()
    flat = [k for _t, keys in groups for k in keys]
    pickable = [k for k in config.MODULES if k not in config.ALWAYS_ON]
    r.check(sorted(flat) == sorted(pickable) and len(flat) == len(set(flat)),
            f"every pickable module sits in one column, once ({groups})")
    r.check([t for t, _k in groups] == ["Path 1", "Path 2", "Shared"],
            "columns: Path 1 | Path 2 | Shared")
    real = config.MODULE_GROUPS
    try:
        config.MODULE_GROUPS = {"Path 1": ("voltage_cam", "nonsense"),
                                "Shared": ("wheel",)}
        got = dict(config.module_groups())
        r.check("nonsense" not in got["Path 1"] and "dmd" in got["Shared"],
                "an unknown key is dropped, an ungrouped module lands in the "
                "last column")
    finally:
        config.MODULE_GROUPS = real

    dlg = ModuleSelectDialog(["voltage_cam", "etl"])
    heads = {h.text(): (h, keys) for h, keys in dlg._heads}
    p1, p1_keys = heads["Path 1"]
    p2, p2_keys = heads["Path 2"]
    r.check(p1.checkState() == Qt.CheckState.PartiallyChecked
            and p2.checkState() == Qt.CheckState.PartiallyChecked,
            "a column with some ticked shows partial")
    p2.click()
    r.check(all(dlg._boxes[k].isChecked() for k in p2_keys)
            and p2.checkState() == Qt.CheckState.Checked,
            "the title box ticks the whole path")
    p2.click()
    r.check(not any(dlg._boxes[k].isChecked() for k in p2_keys)
            and p2.checkState() == Qt.CheckState.Unchecked,
            "…and, all ticked, unticks it")
    r.check(dlg._boxes["voltage_cam"].isChecked() and "voltage_cam" in dlg.selected()
            and not set(p2_keys) & set(dlg.selected()),
            f"control: other columns untouched ({dlg.selected()})")
    r.check(dlg._boxes["voltage_cam_2"].text() == "Camera"
            and dlg._boxes["voltage_cam_2"].toolTip() == "Camera (path 2)",
            "the column names the path, so its rows drop the '(path 2)'")
    for k in config.MODULES:
        if k not in config.ALWAYS_ON and k != "voltage_cam":
            dlg._boxes[k].setChecked(False)
    dlg._boxes["voltage_cam"].setChecked(False)
    ok = dlg._buttons.button(dlg._buttons.StandardButton.Ok)
    r.check(not ok.isEnabled(), "nothing ticked: OK is off")
    p1.click()
    r.check(ok.isEnabled(), "…and a title box's tick turns it back on")
    dlg.deleteLater()


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

        title = win._docks["voltage_cam"].titleBarWidget()
        btn, fold = title.button, win._folds["voltage_cam"]
        fold.set_open(False)
        btn.click()
        r.check(btn is not None and fold.is_open() and btn.isChecked(),
                "⚙ Settings on the tab's title bar opens its settings")
        btn.click()
        fold.set_open(True)         # opened elsewhere: the button follows
        r.check(btn.isChecked(), "…and shows when they are open")
        fold.set_open(False)
        app.processEvents()

        for key in ("voltage_cam", "dmd"):
            fold, dock = win._folds[key], win._docks[key]
            view = fold.parentWidget().layout().itemAt(0).widget()  # the dock's own
            fold.set_open(False)
            app.processEvents()
            shut, size = fold.height(), dock.size()
            fold.set_open(True)
            pump_until(app, lambda f=fold, s=shut: f.height() > s + 50)
            r.check(fold.height() > shut + 50 and not view.isVisible()
                    and dock.size() == size,
                    f"{key}: open Settings take the view's place, the dock "
                    f"keeps its size ({shut} -> {fold.height()} px, "
                    f"{size.width()}x{size.height()} -> "
                    f"{dock.size().width()}x{dock.size().height()})")
            fold.set_open(False)
            app.processEvents()
            r.check(view.isVisible() and dock.size() == size,
                    f"{key}: shut, the view is back at the same dock size "
                    f"(view shown {view.isVisible()}, {dock.size().width()}x"
                    f"{dock.size().height()})")
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
    r.check(cam._mini_text()[1].startswith("⚠ ") and "writable" in cam._mini_text()[1],
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
        check_picker_columns(r)
        check_folds(r, win)
        check_add_remove(r, win)
        check_empty_set(r, win)
        check_order(r, win)
        check_ui_released(r, win)
        check_sidebar_follows(r, win)
        check_sidebar_cards(r, win)
        check_dock_layout(r, win)
        check_pupil_quiet_lut(r, win)
        check_friction(r, win)
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


_PATH2 = {"voltage_cam", "voltage_cam_2", "dmd", "dmd_2", "etl", "stage"}


def check_path2_isolated(r: Report, win) -> None:
    """Path 2 pairs with its own camera and stays out of the lookups that
    routines, the LED menu and the probe make for path 1."""
    mod = {m.key: m for m in win._modules}
    r.check(mod["dmd_2"].camera_key == "voltage_cam_2"
            and mod["dmd"].camera_key == "voltage_cam",
            "each DMD is paired with its own path's camera")
    r.check(win.led_target() is mod["voltage_cam"]
            and win.pattern_target() is mod["dmd"],
            "the window's LED and pattern targets are path 1's")
    r.check(win.frame_rate_hz() == mod["voltage_cam"].frame_rate_hz(),
            "the routine frame rate is path 1's camera's")
    r.check("dmd" in win._status_widgets and "dmd_2" not in win._status_widgets,
            "one hold-to-light switch beside Record: path 1's")
    mod["voltage_cam_2"].panel._spn_target_hz.setValue(7.0)
    from acqApp import config
    r.check(config.load_settings("voltage_cam_2").get("target_hz") == 7.0
            and config.load_settings("voltage_cam").get("target_hz") != 7.0,
            "path 2's camera saves to its own settings section")


def check_path2_files(r: Report, win, tmp) -> None:
    """Each camera's own format decides its .dcimg; the disk estimate sums
    both, and an unloaded camera's share goes."""
    mod = {m.key: m for m in win._modules}

    def fmt(key: str, value: str) -> None:
        cmb = mod[key].panel._cmb_format
        cmb.setCurrentIndex(cmb.findData(value))

    fmt("voltage_cam", "dcimg")
    fmt("voltage_cam_2", "tiff")
    win._rec_path = tmp / "sess"
    try:
        r.check(win.dcimg_target("voltage_cam") is not None
                and win.dcimg_target("voltage_cam_2") is None,
                "path 1 DCIMG + path 2 TIFF: only path 1 gets a .dcimg")
        fmt("voltage_cam", "tiff")
        fmt("voltage_cam_2", "dcimg")
        t = win.dcimg_target("voltage_cam_2")
        r.check(t is not None and t.name == "sess_voltage_cam_2.dcimg"
                and win.dcimg_target("voltage_cam") is None,
                f"…and the other way round, named for its stream ({t})")
    finally:
        win._rec_path = None

    rates = win._expected_rates
    both = rates["voltage_cam"][0] + rates["voltage_cam_2"][0]
    r.check(rates["voltage_cam_2"][0] > 0
            and abs(win._save_panel._rate_mbps - both) < 1e-9,
            f"the Save tab's rate is both cameras' sum ({both:.1f} MB/s)")
    win.set_modules(_PATH2 - {"voltage_cam_2", "dmd_2"})
    r.check(abs(win._save_panel._rate_mbps - rates["voltage_cam"][0]) < 1e-9,
            "unloading path 2's camera takes its share off the estimate")
    win.set_modules(_PATH2)


def check_path2_device_ids(r: Report, win) -> None:
    """rigs.json device_ids reach the ALP serial and the DCAM index; a
    non-int is ignored, not passed to a driver."""
    from acqApp import config
    from acqApp.adapters import dmd as dmd_adapter

    mod = {m.key: m for m in win._modules}
    real_profile, real_ctrl = config.rig_profile, dmd_adapter.DmdController
    asked: list = []

    def fake_ctrl(_s, serial=None):
        asked.append(serial)
        raise OSError("no ALP here")    # the adapter falls back to the mock

    try:
        config.rig_profile = lambda: {"device_ids": {
            "dmd_2": 4321, "voltage_cam_2": 3, "dmd": True, "etl": "5"}}
        r.check(config.rig_device_id("dmd_2") == 4321
                and config.rig_device_id("dmd") is None
                and config.rig_device_id("etl") is None
                and config.rig_device_id("pupil_cam") is None,
                "rig_device_id: an int is kept; a bool, a string, unset -> None")
        dmd_adapter.DmdController = fake_ctrl
        mod["dmd_2"].build_controller(False)
        mod["dmd"].build_controller(False)
        r.check(asked == [4321, None], f"each ALP opens by its own serial ({asked})")
        r.check(mod["voltage_cam_2"]._index() == 3 and mod["voltage_cam"]._index() == 0,
                "the DCAM index comes from device_ids, else the class default")
    finally:
        config.rig_profile, dmd_adapter.DmdController = real_profile, real_ctrl
        for key in ("dmd", "dmd_2"):
            mod[key].close_controller()
            mod[key].build_controller(True)
    r.check(mod["voltage_cam_2"]._index() == 1, "path 2's camera defaults to index 1")

    from acqApp.devices.voltage_cam import acquisition
    real_open, opened = acquisition.open_camera, []
    acquisition.open_camera = lambda i: opened.append(i) or f"handle{i}"
    try:
        win._extra_cams.clear()
        first, again = win.camera_handle(1), win.camera_handle(1)
        r.check(first == again == "handle1" and opened == [1],
                f"camera_handle(1) opens once and is kept (opened {opened})")
        r.check(win.camera_handle(0) is win._cam_handle,
                "camera_handle(0) is the startup handle")
    finally:
        acquisition.open_camera = real_open
        win._extra_cams.clear()     # a fake handle must not reach close_camera


def check_etl(r: Report, win) -> None:
    """The setpoint is clamped to the driver's range before it is written."""
    from acqApp.devices.etl.control import MockEtlController
    from acqApp.devices.etl.settings import EtlSettings

    c = MockEtlController(settings=EtlSettings(min_volts=1.0, max_volts=2.0))
    seen: list[float] = []
    c.set_sink(seen.append)
    c.apply_settings(EtlSettings(volts=9.0))
    c.apply_settings(EtlSettings(volts=-3.0))
    r.check(seen == [2.0, 1.0], f"out-of-range setpoints clamp to the range ({seen})")
    r.check(EtlSettings(volts=3.0, min_volts=4.0, max_volts=2.0).clamped() == 3.0,
            "reversed limits still bound the value")
    etl = next(m for m in win._modules if m.key == "etl")
    etl.set_volts(0.5)
    r.check(etl.controller.volts == 0.5 and "0.500 V" in etl._output_text(),
            f"the panel's setpoint reaches the controller ({etl._output_text()!r})")
    r.check(etl.metadata()["etl_volts"] == 0.5 and etl.metadata()["etl_device"] == "mock",
            "metadata files the held value and that it was emulated")


def check_etl_offset(r: Report, win) -> None:
    """Offset (µm) and Setpoint (V) are one value through the calibration;
    without one, only volts."""
    from acqApp.devices.etl.settings import EtlSettings

    s = EtlSettings(volts=3.0, cofocal_volts=2.0, um_per_volt=40.0)
    r.check(s.offset_um() == 40.0 and s.volts_for(-20.0) == 1.5,
            "offset = (V − co-focal) × µm/V, and back")
    r.check(EtlSettings(volts=3.0).offset_um() is None,
            "control: uncalibrated has no offset")

    etl = next(m for m in win._modules if m.key == "etl")
    p = etl.panel
    p._spn_gain.setValue(0.0)
    r.check(not p.set_offset_um(10.0) and not p._spn_um.isEnabled()
            and etl.focus_offset().um is None,
            "uncalibrated: the offset box is off and set_offset_um refuses")
    p._spn_gain.setValue(40.0)
    p._spn_cofocal.setValue(2.0)
    r.check(etl.set_offset_um(60.0) and abs(etl.controller.volts - 3.5) < 1e-9,
            f"60 µm at 40 µm/V from 2 V co-focal writes 3.5 V "
            f"(got {etl.controller.volts})")
    p.set_volts(1.0)
    r.check(abs(p._spn_um.value() + 40.0) < 1e-9 and etl.focus_offset().um == -40.0,
            f"a volts edit moves the offset box (−40 µm, got {p._spn_um.value()})")
    p.set_offset_um(9999.0)
    lo, hi = p.settings.limits
    r.check(abs(etl.controller.volts - hi) < 1e-9,
            "an offset past the driver's range stops at its edge")
    meta = etl.metadata()
    r.check(meta["etl_um_per_volt"] == 40.0 and meta["etl_cofocal_volts"] == 2.0
            and abs(meta["etl_offset_um"] - (hi - 2.0) * 40.0) < 1e-6,
            "metadata files the offset and the calibration that made it")


def check_stage_overlay(r: Report, win) -> None:
    """The ETL's plane rides the stage's Z gauge; unloaded, it goes."""
    from acqApp.acq.devices import FocusOffset
    from acqApp.devices.stage.map_widget import ZGauge

    g = ZGauge()
    g.set_position(100.0)
    r.check(g.path2_um is None, "control: no ETL, no path 2 plane")
    g.set_offset(FocusOffset(volts=1.0, um=None))
    r.check(g.path2_um is None, "uncalibrated: no plane to place")
    g.set_offset(FocusOffset(volts=1.0, um=60.0))
    r.check(g.path2_um == 160.0, "path 2's plane is Z + offset")
    g.clear_position()
    r.check(g.path2_um is None, "no position: no plane")

    etl = win._module("etl")
    stage = win._module("stage")
    gauge = stage.panel.z_gauge
    if not r.check(gauge is not None, "the stage page has its Z gauge back"):
        return
    etl.panel._spn_gain.setValue(40.0)
    etl.panel._spn_cofocal.setValue(2.0)
    etl.set_offset_um(20.0)
    stage.update_display()
    r.check(gauge._offset == etl.focus_offset() and gauge._offset.um == 20.0,
            f"the stage's tick puts the ETL offset on the gauge ({gauge._offset})")
    r.check(stage._disp_timer.isActive(),
            "the stage's tick runs (so the overlay follows the ETL)")
    win.set_modules(_PATH2 - {"etl"})
    r.check(stage.panel.z_gauge._offset is None,
            "unloading the ETL takes its plane off at once")
    win.set_modules(_PATH2)


def check_overlay_math(r: Report) -> None:
    """Each camera in its colour, stretched by its own levels; the other
    resized to fit; complementary colours add to white."""
    import numpy as np

    from acqApp.devices.voltage_cam import overlay

    own = np.zeros((4, 6))
    own[:, :3] = 100.0
    other = np.zeros((2, 3))
    other[:, 1:] = 5000.0           # brighter units: its own levels must cope
    rgb = overlay.composite(own, other, "green", "magenta", (0.0, 100.0),
                            (0.0, 5000.0))
    r.check(rgb.shape == (4, 6, 3), f"the composite has own's shape ({rgb.shape})")
    r.check(np.allclose(rgb[0, 0], (0, 1, 0)) and np.allclose(rgb[0, 5], (1, 0, 1))
            and np.allclose(rgb[0, 2], (1, 1, 1)),
            f"own alone = green, other alone = magenta, both = white "
            f"({rgb[0, 0]}, {rgb[0, 5]}, {rgb[0, 2]})")
    r.check(overlay.fit_to(other, (2, 3)) is other,
            "control: a frame that fits is used as is")
    lo, hi = overlay.levels(np.full((3, 3), 7.0))
    r.check(hi > lo, f"a flat frame still gets a usable stretch ({lo}, {hi})")


def check_cameras_overlay(r: Report, win) -> None:
    """Overlay (on path 1's view) combines the two cameras into that one
    view, each in its colour, and hides path 2's; off gives both back."""
    app = qt_app()
    mod = {m.key: m for m in win._modules}
    c1, c2 = mod["voltage_cam"], mod["voltage_cam_2"]
    dock2 = win._docks["voltage_cam_2"]
    r.check(c1.overlay_color() == "green" and c2.overlay_color() == "magenta",
            f"default colours: path 1 green, path 2 magenta "
            f"({c1.overlay_color()}, {c2.overlay_color()})")
    r.check(win.camera_overlay_color("voltage_cam_2") == "magenta",
            "the window hands one camera the other's colour")
    r.check(c1._chk_overlay is not None and c1._chk_overlay.isEnabled()
            and c2._chk_overlay is None,
            "one Overlay toggle, on path 1's view (path 2's has none)")

    win.resize(1600, 900)
    win.show()
    c1._chk_overlay.setChecked(True)
    win._btn_run.setChecked(True)
    try:
        r.check(not dock2.isVisible() and "voltage_cam_2" in win._hidden_views,
                "Overlay on: path 2's camera view goes, the two are one")
        got = pump_until(app, lambda: (win._display_tick(), c1._img.image is not None
                                       and c1._img.image.ndim == 3)[1], timeout=8.0)
        r.check(got, "…and path 1's view paints both as an RGB composite")
        r.check(not c1._hist.isVisible() and c1._lbl_legend.isVisible()
                and "Path 1" in c1._lbl_legend.text()
                and "Path 2" in c1._lbl_legend.text(),
                "…its LUT bar hidden, a legend saying which colour is which path")
        r.check(c2.worker is not None and c2.last_frame() is not None,
                "path 2 keeps acquiring while its view is hidden")
        win._reset_action.trigger()
        pump(app, 0.1)
        r.check(not dock2.isVisible(), "Reset layout keeps the merged view merged")
        pump_until(app, lambda: win._docks["dmd_2"].geometry().y() > 0)
        d = {k: win._docks[k].geometry() for k in ("voltage_cam", "dmd", "dmd_2")}
        r.check(d["dmd_2"].x() == d["dmd"].x() and d["dmd_2"].y() != d["dmd"].y()
                and d["dmd"].x() - d["voltage_cam"].right() < 20,
                f"merged: path 2's DMD joins path 1's column, no empty column "
                f"between ({d})")
        r.check(config.load_settings("voltage_cam").get("overlay") is True,
                "the toggle is saved with path 1's settings")

        c1._chk_overlay.setChecked(False)
        win._display_tick()
        pump(app, 0.2)
        win._display_tick()
        r.check(dock2.isVisible() and c1._img.image.ndim == 2 and not c1._overlaid
                and not c1._lbl_legend.isVisible()
                and c1._hist.isVisibleTo(c1._hist.parentWidget())
                == c1.panel.get_config().show_lut,
                "Overlay off: both views again, grayscale, the LUT bar as set")
    finally:
        win._btn_run.setChecked(False)

    win.set_modules(_PATH2 - {"voltage_cam_2", "dmd_2"})
    r.check(not c1._chk_overlay.isEnabled(),
            "the other camera unloaded: Overlay is off-limits, its tooltip says why")
    win.set_modules(_PATH2)

    c1 = win._module("voltage_cam")
    c1._chk_overlay.setChecked(True)
    win.set_modules(_PATH2 - {"voltage_cam"})
    pump(app, 0.1)
    r.check(win._docks["voltage_cam_2"].isVisible(),
            "path 1 unloaded mid-overlay: path 2's view comes back, not lost")
    win.set_modules(_PATH2)
    pump(app, 0.1)
    r.check(not win._docks["voltage_cam_2"].isVisible(),
            "…and path 1 back with Overlay saved on: merged again")
    win._module("voltage_cam")._chk_overlay.setChecked(False)
    pump(app, 0.1)
    r.check(win._docks["voltage_cam_2"].isVisible(), "control: off shows it")
    win.hide()


def check_cameras_side_by_side(r: Report, win) -> None:
    """Path 2's camera right of path 1's, top row; its DMD under it."""
    app = qt_app()
    win._reset_action.trigger()
    win.resize(1800, 900)
    win.show()
    try:
        pump_until(app, lambda: win._docks["dmd_2"].geometry().y() > 0)
        d = {k: win._docks[k].geometry() for k in win._docks}
        r.check(d["voltage_cam"].right() < d["voltage_cam_2"].x()
                and d["voltage_cam"].y() == d["voltage_cam_2"].y(),
                f"the cameras sit side by side ({d['voltage_cam']}, "
                f"{d['voltage_cam_2']})")
        r.check(d["dmd_2"].x() == d["voltage_cam_2"].x()
                and d["dmd_2"].y() > d["voltage_cam_2"].y(),
                "path 2's DMD under its camera")
        r.check(d["dmd"].x() > d["voltage_cam_2"].x(),
                "path 1's DMD moves one column right, past path 2's camera")
    finally:
        win.hide()


def check_etl_rides_stage(r: Report, win) -> None:
    """With a Stage tab, the ETL lives on it: its strip over the stage
    controls, its calibration in the Stage's Settings; no General section.
    Without one, General again; each way survives hot-loading."""
    etl = win._module("etl")
    p, sfold = etl.panel, win._folds["stage"]
    sdock = win._docks["stage"]
    r.check("etl" not in win._folds and sdock.isAncestorOf(p.strip)
            and sfold.body.isAncestorOf(p.calibration),
            "the ETL rides on the Stage tab: strip on top, calibration in "
            "its Settings, no General section")
    sfold.set_open(True)
    hidden = p.strip.isHidden()
    sfold.set_open(False)
    r.check(hidden and not p.strip.isHidden(),
            "…its strip steps aside while the Stage's Settings are open")
    keep = sorted(m.key for m in win._modules)
    win.set_modules([k for k in keep if k != "stage"])
    r.check("etl" in win._folds and p.strip.parentWidget() is p
            and not win._riding,
            "Stage unloaded: the ETL is a General section again")
    win.set_modules(keep)
    stage_panel = win._module("stage").panel
    r.check("etl" not in win._folds and win._docks["stage"].isAncestorOf(p.strip),
            "Stage back: it rides on it again")
    win.set_modules([k for k in keep if k != "etl"])
    r.check(win._folds["stage"].body.widget() is stage_panel and not win._riding,
            "ETL unloaded: the Stage's Settings are its own again")
    win.set_modules(keep)

    from PyQt6.QtGui import QShortcut
    p = win._module("etl").panel
    p._spn_gain.setValue(40.0)
    p._spn_cofocal.setValue(2.0)
    p.set_offset_um(10.0)
    p.nudge(1)
    up = p._spn_um.value()
    p.nudge(-10)
    keys = sorted(s.key().toString() for s in p.strip.findChildren(QShortcut))
    r.check(abs(up - 11.0) < 1e-6 and abs(p._spn_um.value() - 1.0) < 1e-6
            and keys == ["Alt+Down", "Alt+Shift+Down", "Alt+Shift+Up", "Alt+Up"],
            f"Alt+↑/↓ nudges path 2's focus 1 µm, Shift 10× ({up}, "
            f"{p._spn_um.value()}, {keys})")


def check_overlay_keeps_layout(r: Report, win) -> None:
    """Overlay on then off gives back the operator's own arrangement, not
    the default."""
    from PyQt6.QtCore import Qt
    app = qt_app()
    win.show()
    try:
        chk = win._module("voltage_cam")._chk_overlay
        chk.setChecked(False)
        pump_until(app, lambda: win._docks["voltage_cam_2"].isVisible())
        cams = [win._docks["voltage_cam"], win._docks["voltage_cam_2"]]
        win.resizeDocks(cams, [900, 300], Qt.Orientation.Horizontal)   # by hand
        pump(app, 0.1)
        mine = {k: d.geometry() for k, d in win._docks.items()}
        chk.setChecked(True)
        pump_until(app, lambda: not win._docks["voltage_cam_2"].isVisible())
        chk.setChecked(False)
        pump_until(app, lambda: win._docks["voltage_cam_2"].isVisible())
        pump(app, 0.1)
        back = {k: d.geometry() for k, d in win._docks.items()}
        moved = {k: (mine[k], back.get(k)) for k in mine if back.get(k) != mine[k]}
        r.check(not moved, f"Overlay off puts the hand arrangement back ({moved})")
    finally:
        win.hide()


def check_friction(r: Report, win) -> None:
    """Small conveniences: a title-bar warning opens Settings; an empty DMD
    preview offers its next step; Record by hand wants a mouse ID; Live and
    Record have Ctrl shortcuts."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from acqApp.sidebar import MiniText
    app = qt_app()
    win.set_modules(["voltage_cam", "dmd", "wheel"])
    win.show()
    try:
        title = win._docks["voltage_cam"].titleBarWidget()
        mini = title.findChild(MiniText)
        fold = win._folds["voltage_cam"]
        fold.set_open(False)
        keep = mini._source
        mini._source = lambda: ("idle", "⚠ 50% of frames writable", "x")
        mini.refresh()
        QTest.mouseClick(mini, Qt.MouseButton.LeftButton)
        r.check(fold.is_open() and "Click to open Settings" in mini.toolTip(),
                "a warning on the title bar opens that tab's Settings")
        fold.set_open(False)
        mini._source = lambda: ("idle", "all fine", "")
        mini.refresh()
        QTest.mouseClick(mini, Qt.MouseButton.LeftButton)
        r.check(not fold.is_open(), "control: a plain status click does nothing")
        mini._source = keep
        mini.refresh()

        # Only the stub is ever clicked: the real steps open modal dialogs.
        panel = win._module("dmd").panel
        hits = []
        stub = lambda: hits.append("browse")        # noqa: E731
        panel._browse = stub
        panel._rb["pattern"].setChecked(True)
        panel._update_preview()
        action = panel._empty_action
        if action is not None and action[1] is stub:
            QTest.mouseClick(panel._preview, Qt.MouseButton.LeftButton)
        r.check(hits == ["browse"] and "pattern" in action[0],
                f"an empty Image preview's click opens Browse ({action}, {hits})")
        panel._rb["roi"].setChecked(True)
        panel._update_preview()
        r.check(bool(panel._rois) or (panel._empty_action is not None
                                      and "ROIs" in panel._empty_action[0]),
                f"…and an empty ROIs one offers to draw them ({panel._empty_action})")
        panel._rb["pattern"].setChecked(True)
        del panel._browse

        sp = win._save_panel
        sp._ed_mouse_id.setText("")
        sp._cfg.mouse_id = ""
        win._btn_rec.setChecked(True)
        r.check(not win._btn_rec.isChecked() and win.recording_path() is None
                and win._folds["saving"].is_open()
                and "mouse ID" in win.statusBar().currentMessage(),
                f"Record without a mouse ID asks for one instead "
                f"({win.statusBar().currentMessage()!r})")
        win._btn_run.setChecked(False)
        pump(app, 0.1)

        r.check(bool(sp._ed_mouse_id.property("flashing")),
                "…and lights the Mouse ID box for a moment")

        # Start routine asks the same, before its own pre-flight dialog.
        routines = win._module("routines")
        asked = []
        keep_confirm = routines._confirm_start
        routines._confirm_start = lambda: asked.append(1) or False
        routines._on_start_clicked()
        r.check(not asked and "Start routine" in win.statusBar().currentMessage(),
                f"Start routine without a mouse ID asks for one first "
                f"({win.statusBar().currentMessage()!r})")
        sp._ed_mouse_id.setText("m1")
        routines._on_start_clicked()
        r.check(asked == [1], "…with one, on to its pre-flight check")
        routines._confirm_start = keep_confirm

        # Past mouse IDs, from the folder's session and routine folders.
        import tempfile
        from pathlib import Path
        from acqApp.saving.config import past_mouse_ids
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("M12_20261119_142031", "M12_20261118_090000_001",
                        "ab-7_20261001_120000", "mouse_id_20261119_000000",
                        "R3/20261119/FOV1_T1", "notes"):
                (root / sub).mkdir(parents=True)
            (root / "M99_20261119_142031.txt").write_text("")
            got = past_mouse_ids(root)
        r.check(got == ["ab-7", "M12", "R3"],
                f"past mouse IDs: sessions + routine folders, no placeholder ({got})")

        # The capacity warning's link opens the camera's Settings.
        cam_fold = win._folds["voltage_cam"]
        cam_fold.set_open(False)
        sp._lbl_space.linkActivated.emit("camera")
        r.check(cam_fold.is_open(), "Save's capacity link opens the camera's Settings")
        cam_fold.set_open(False)

        # After a recording: where it went, one click away.
        from pathlib import Path as _P
        win._offer_last_recording(_P("C:/x/M12_20261119_142031"))
        shown = win._btn_open_last.isVisibleTo(win)
        win._offer_last_recording(None)
        r.check(shown and not win._btn_open_last.isVisibleTo(win),
                "an Open-recording button after a stop, gone at the next start")

        # Closing mid-recording asks first.
        import acqApp.window.main_window as MW
        keep_q, keep_rec = MW.QMessageBox.question, win.is_recording
        win.confirm_close, win.is_recording = True, lambda: True
        try:
            MW.QMessageBox.question = lambda *a, **k: MW.QMessageBox.StandardButton.No
            no = win._close_while_recording_ok()
            MW.QMessageBox.question = lambda *a, **k: MW.QMessageBox.StandardButton.Yes
            yes = win._close_while_recording_ok()
        finally:
            MW.QMessageBox.question = keep_q
            win.confirm_close = False
            del win.is_recording
        r.check(not no and yes and keep_rec() is False,
                "closing mid-recording asks first (No keeps it running)")

        clicked = []
        win._btn_run.clicked.connect(lambda: clicked.append("live"))
        win._shortcuts["Ctrl+L"].activated.emit()
        win._shortcuts["Ctrl+L"].activated.emit()
        r.check(clicked == ["live", "live"] and not win._btn_run.isChecked()
                and set(win._shortcuts) == {"Ctrl+L", "Ctrl+R"},
                "Ctrl+L / Ctrl+R start and stop Live view / Record")
        pump(app, 0.1)
    finally:
        win.hide()


def check_pupil_quiet_lut(r: Report, win) -> None:
    """The pupil cam's LUT bar: the Orcas' quiet style, kept upright."""
    hist = win._module("pupil_cam")._hist
    p = hist.item
    r.check(p.orientation == "vertical" and not p.axis.isVisible()
            and not p.gradient.isVisible() and len(hist.level_labels) == 2,
            "the pupil cam's bar is the same quiet style, upright")


def check_quiet_lut(r: Report, win) -> None:
    """The Orcas' LUT strip: horizontal, no axis or gradient, a smoothed
    histogram, the two edges as numbers."""
    import numpy as np
    cam = win._module("voltage_cam")
    item = cam._hist.item
    r.check(item.orientation == "horizontal" and not item.axis.isVisible()
            and not item.gradient.isVisible(),
            "the camera's LUT strip is horizontal, axis and gradient hidden")
    frame = np.random.default_rng(3).integers(1000, 3000, (256, 512), dtype=np.uint16)
    cam._img.setImage(frame, autoLevels=False, levels=(1500, 2500))
    item.imageChanged()
    raw = cam._img.getHistogram()[1]
    shown = item.plot.getData()[1]
    rough = lambda y: float(np.abs(np.diff(y)).mean())   # noqa: E731
    r.check(shown is not None and rough(shown) < rough(raw) / 3,
            f"its histogram is smoothed (step {rough(shown):.1f} vs raw {rough(raw):.1f})")
    item.setLevels(1600, 2400)
    texts = [t.textItem.toPlainText() for t in cam._hist.level_labels]
    r.check(texts == ["1600", "2400"],
            f"the edges read as numbers and follow a drag ({texts})")

    rng = np.random.default_rng(5)
    def refresh(offset: int = 0, i: int = 0) -> list:
        f = rng.integers(1500, 2500, (256, 512), dtype=np.uint16) + offset
        if i % 3 == 1:          # content that comes and goes, as live frames do
            f[:40, :40] = 1200
        elif i % 3 == 2:
            f[:40, :40] = 2800
        cam._img.setImage(f, autoLevels=False, levels=item.getLevels())
        item.imageChanged()
        return item.vb.viewRange()
    from acqApp.adapters.base import _LUT_MEMORY
    warm = _LUT_MEMORY + 2      # the earlier frames' spans age out first
    for i in range(warm):
        refresh(i=i)
    steady = refresh(i=warm)
    seen = [refresh(i=i) for i in range(warm + 1, warm + 13)]
    r.check(all(v == steady for v in seen),
            f"frames whose dim/bright patches come and go leave the strip "
            f"still ({steady} vs {seen})")
    # A hand zoom (pyqtgraph's wheel/drag signal) is kept through refreshes.
    from types import SimpleNamespace
    item.vb.setXRange(1700, 1900, padding=0)
    item.vb.sigRangeChangedManually.emit([True, False])
    kept = [refresh(i=i)[0] for i in range(3)]
    r.check(cam._hist.zoom_is_manual()
            and all(abs(v[0] - 1700) < 1 and abs(v[1] - 1900) < 1 for v in kept),
            f"a hand zoom on the strip stays put ({kept[-1]})")
    item.vb.mouseDoubleClickEvent(SimpleNamespace(accept=lambda: None))
    r.check(not cam._hist.zoom_is_manual() and item.vb.viewRange()[0] == steady[0],
            f"a double-click fits it again ({item.vb.viewRange()[0]})")
    moved = refresh(offset=4000)
    r.check(moved[0][1] > 5000, f"a real change still refits it ({moved[0]})")

    from acqApp.adapters.base import _settled_levels
    old = (1000.0, 2000.0)
    r.check(_settled_levels(old, (1020, 1970)) == old
            and _settled_levels(old, (1000, 2300)) == (1000.0, 2300.0),
            "Auto ignores percentile jitter, follows a real change")


def _part_path2() -> int:
    r = Report("path2")
    tmp = isolate_user_state()
    app = qt_app()          # held: a collected QApplication aborts
    win = make_window(_PATH2)
    try:
        check_path2_isolated(r, win)
        check_path2_files(r, win, tmp)
        check_path2_device_ids(r, win)
        check_etl(r, win)
        check_etl_offset(r, win)
        check_stage_overlay(r, win)
        check_overlay_math(r)
        check_cameras_overlay(r, win)
        check_cameras_side_by_side(r, win)
        check_overlay_keeps_layout(r, win)
        check_etl_rides_stage(r, win)
        check_quiet_lut(r, win)
    finally:
        win.close()
        pump(app, 0.1)
    return r.finish()


PARTS = {
    "subsets": _part_subsets,
    "idle_ui": _part_idle_ui,
    "hotload": _part_hotload,
    "path2": _part_path2,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
