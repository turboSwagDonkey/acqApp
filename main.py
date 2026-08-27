"""
In vivo acquisition suite — top-level entry point.

Wires voltage_cam, pupil_cam, wheel, puffer, stage and dmd around ONE shared
SessionClock, so every device timestamps against the same origin and one
Recorder streams the lot into a single HDF5 session file.

This file owns only what is session-wide — the clock, the sync/trigger bus, the
recorder, the save destination, the docks and the theme. Anything specific to
one instrument is a `ModuleAdapter` in `adapters/`; this window iterates.

Run it any of these ways — the bootstrap below makes them all work:
  acqApp\\.venv\\Scripts\\python.exe acqApp\\main.py          (run the file)
  python acqApp\\main.py                                       (any interpreter)
  python -m acqApp.main --mock                                 (as a module)

Escape hatches (env vars): ACQAPP_NO_REEXEC=1 skips the venv re-exec,
ACQAPP_NO_INSTALL=1 skips auto-installing requirements.
"""

from __future__ import annotations
import argparse
import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

# A segfault deep in the DCAM SDK can't be caught by try/except — the process
# just dies. faulthandler dumps the C-level + Python stack on a fatal signal.
faulthandler.enable()


# ── Environment bootstrap (must run before any third-party import) ────────────
def _bootstrap() -> None:
    """
    Launchable from anywhere, never touching an environment but `acqApp/.venv`:
      1. parent dir on sys.path, then harden the console — both before any print.
      2. not in the project venv → create it if missing and re-exec into it.
      3. a core dependency missing → install requirements.txt, but ONLY from
         inside the venv, so pip never reaches an unrelated Python.
    """
    here = Path(__file__).resolve().parent            # …/acqApp
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    venv_dir = here / ".venv"
    venv_py = venv_dir / scripts / exe

    # (1) importable, then stdout hardened FIRST, so every print below can use
    # arrows and symbols without the UnicodeEncodeError that kills device
    # threads (console.py). That module imports only `sys`, so it is safe here.
    parent = str(here.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from acqApp.console import enable_safe_console
    enable_safe_console()

    try:
        in_venv = Path(sys.executable).resolve() == venv_py.resolve()
    except OSError:
        in_venv = False

    # (2) get into the project venv, creating it first if it doesn't exist
    if not in_venv and not os.environ.get("ACQAPP_NO_REEXEC"):
        if not venv_py.exists():
            print(f"[bootstrap] creating project venv at {venv_dir} …")
            import subprocess
            try:
                subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            except (subprocess.CalledProcessError, OSError) as e:
                sys.exit(f"[bootstrap] could not create venv ({e}); create it "
                         f"manually:\n    python -m venv {venv_dir}")
        os.environ["ACQAPP_NO_REEXEC"] = "1"          # guard against re-exec loops
        print(f"[bootstrap] launching under {venv_py}")
        os.execv(str(venv_py), [str(venv_py), str(here / "main.py"), *sys.argv[1:]])

    # (3) install dependencies if a core one is missing — venv only
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        if not in_venv:
            sys.exit("[bootstrap] dependencies are missing and we're not in the "
                     "project venv. Remove ACQAPP_NO_REEXEC so it can re-exec "
                     "into acqApp/.venv, or install deps into your environment "
                     "yourself — refusing to pip-install into an unknown Python.")
        if os.environ.get("ACQAPP_NO_INSTALL"):
            raise
        import subprocess
        req = here / "requirements.txt"
        print(f"[bootstrap] installing dependencies into the venv from {req} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])


_bootstrap()

from datetime import datetime
from typing import Any

# ── Hardware pre-init ─────────────────────────────────────────────────────────
# Open the camera ONCE and keep the handle; the worker reuses it. Re-opening a
# just-closed DCAM device crashes natively (docs/HANDOFF.md), and a fresh open
# costs ~6.7 s. Closed in MainWindow.closeEvent.
_cam_info = None
_cam_handle = None
_cam_thread = None
_mock = "--mock" in sys.argv     # start in Emulate mode; real hardware otherwise


def _open_camera() -> None:
    """The startup open. Runs on a worker thread; never raises out of it."""
    global _cam_handle, _cam_info
    t0 = time.perf_counter()
    dcam = None
    try:
        from pylablib.devices import DCAM as dcam
        # Open OPTIMISTICALLY: `get_cameras_number()` re-enumerates on EVERY
        # call, not once (measured 6.5/5.3/5.3 s), so asking first added ~5.3 s
        # to every launch. Ask only if the open fails, where it is free.
        handle = dcam.DCAMCamera(idx=0)
        _cam_info = handle.get_device_info()
        _cam_handle = handle
        print(f"Voltage cam: {_cam_info} "
              f"(opened in {time.perf_counter() - t0:.1f} s)")
    except Exception as e:                        # noqa: BLE001
        _cam_handle = None
        n = -1
        if dcam is not None:
            try:
                n = dcam.get_cameras_number()
            except Exception:
                pass
        if n == 0:
            # No silent fallback to fake data — real is the default.
            print("No DCAM camera detected — use Emulate to run without hardware")
        else:
            print(f"Camera unavailable ({type(e).__name__}: {e}) — if HCImage "
                  f"or another app has it open, close that first. Use Emulate "
                  f"to run without it.")


if not _mock:
    # Threaded, so the ~7.9 s open overlaps the Qt import and the module
    # picker. Verified on the real camera: opening on a worker and driving the
    # handle from the GUI thread works and closes cleanly. The load-bearing
    # rule is lifetime, not threads — see the pre-init note above.
    _cam_thread = threading.Thread(target=_open_camera, name="cam-open")
    _cam_thread.start()


def _await_camera() -> None:
    """Block until the startup open has finished. Safe to call more than once,
    and a no-op under --mock."""
    if _cam_thread is not None and _cam_thread.is_alive():
        print("Waiting for the camera to finish opening…")
    if _cam_thread is not None:
        _cam_thread.join()
# ─────────────────────────────────────────────────────────────────────────────

# Force pyqtgraph onto PyQt6 (both bindings may be installed in the venv).
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QAction, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDockWidget, QLabel, QMainWindow, QPushButton,
    QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from acqApp import adapters, config, style
from acqApp.dialogs import ConnectionMonitor, ModuleSelectDialog, SettingsDialog
from acqApp.saving import SaveConfig, SavePanel
from acqApp.acq.sync import SyncController
from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import HDF5Writer

pg.setConfigOptions(imageAxisOrder="row-major")

RING_FRAMES  = 512          # recording ring-buffer item cap (scalar streams)
# …and a payload cap so full frames can't OOM. 2 GB is 102 full-frame bin-1
# frames, ~0.97 s of slack. 512 MB (25 frames, 0.24 s) was too tight to ride out
# a transient writer stall: measured 2026-08-25 over 30 s at 106 fps, it shed
# 14-54 frames a run where 2 GB shed none, twice. 4 GB buys nothing more.
RING_BYTES   = 2048 << 20


def _sample_nbytes(item) -> int:
    """Payload bytes of a Recorder ring item (stream, ts, data); 0 for scalars."""
    return getattr(item[2], "nbytes", 0)



class MainWindow(QMainWindow):
    """Session-wide shell: clock, triggers, recorder, save target, docks, theme.

    Per-instrument behaviour is NOT here — each subsystem is an
    `adapters.ModuleAdapter` owning its own panel, worker, display tick, sink
    and metadata, and this window only iterates. That is why adding an
    instrument is a new adapter class, not a new branch in four methods.
    """

    def __init__(self, cam_info=None, mock=False, enabled: set[str] | None = None,
                 cam_handle=None):
        super().__init__()
        # Simulated signals; OFF by default, togglable only between sessions.
        self._emulate = mock
        # Devices without the session clock, so nothing can be recorded. Never
        # persisted — a launch that quietly came up unable to record would be
        # worse than useless on a rig (as with the closed loop's `armed`).
        self._free_run = False
        # Tracked, not read off `_sync.running`: free run leaves the sync
        # controller deliberately stopped, so that would answer no mid-session.
        self._session_on = False
        self._enabled = enabled if enabled is not None else set(config.MODULES)
        self._cam_info = cam_info
        # Opened once at startup, reused by every session's worker (see the
        # pre-init note). None in emulate/no-camera runs.
        self._cam_handle = cam_handle

        # ── The single session-wide clock, shared by sync + recorder + devices ──
        self._clock = SessionClock()
        self._sync  = SyncController(self._clock, tick_ms=100)
        self._sync.tick.connect(self._on_tick)
        self._sync.trigger_fired.connect(self._on_trigger)

        self._recorder: Recorder | None = None
        self._rec_path: Path | None = None      # file the last recording went to
        self._save_panel: SavePanel | None = None
        self._settings_dialog: SettingsDialog | None = None   # built in _build_ui
        self._devices_dialog: ConnectionMonitor | None = None
        self._pg_views: list = []      # pyqtgraph views to recolour on theme change
        # Undo bookkeeping for a module unloaded mid-session. `add_dock` and
        # `register_pg_view` are called *by* the adapter during its build, and
        # neither says who is calling, so the window notes whose build is in
        # progress instead of changing the ModuleHost surface.
        self._building_key: str | None = None
        self._module_docks: dict[str, list] = {}
        self._module_views: dict[str, list] = {}
        self._module_plots: dict[str, QWidget] = {}
        self._central_owner: str | None = None

        # One adapter per loaded instrument, in config.MODULES display order.
        self._modules = adapters.build_adapters(self, self._enabled)
        self._build_ui()
        # After the UI: controllers are configured from their own panels, so
        # those must exist before the device is opened.
        self._build_controllers()
        self._apply_title()

        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)   # ~30 Hz display
        self._disp_timer.timeout.connect(self._display_tick)

    # ── Services the module adapters use ──────────────────────────────────────

    @property
    def sync(self) -> SyncController:
        return self._sync

    @property
    def cam_handle(self):
        """The pre-opened DCAM handle, so no worker ever re-opens the device."""
        return self._cam_handle

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def module_keys(self) -> list[str]:
        """The module keys loaded this session, in display order.

        Which modules exist, not the modules themselves — the closed loop uses
        it to offer only outputs that are actually loaded, since a rule aimed
        at an absent one would fire onto the bus with nothing listening and
        look armed and working.
        """
        return [m.key for m in self._modules]

    def signal_sources(self) -> list:
        """Live scalar signals a closed-loop rule can watch (`SignalSource`s).

        Contributed by the loaded modules, so the loop depends on *a signal*
        rather than on the wheel: making pupil radius triggerable is one method
        on that adapter and no change here.
        """
        out: list = []
        for m in self._modules:
            out.extend(m.signal_sources())
        return out

    def stage_target(self):
        """The loaded module an experiment routine may move, or None.

        Pooled here for the same reason `signal_sources` is: the routine has to
        reach the stage without importing its adapter, and the stage stays
        ignorant that routines exist. First one wins — there is one stage.
        """
        return self._first(lambda m: m.stage_target())

    def pattern_target(self):
        """The loaded module a routine may project through, or None."""
        return self._first(lambda m: m.pattern_target())

    def _first(self, ask):
        """The first loaded module that answers `ask` with something."""
        for m in self._modules:
            got = ask(m)
            if got is not None:
                return got
        return None

    def set_live(self, on: bool) -> bool:
        """Turn the live view on/off for a module that needs frames flowing.

        Returns the PREVIOUS state, so a caller that started it can put it back
        — the DMD calibration does exactly that. Goes through the button rather
        than `_start_session` so the UI, the tooltip and the status line all
        stay in step with reality.
        """
        was = self._btn_run.isChecked()
        if bool(on) != was:
            self._btn_run.setChecked(bool(on))
        return was

    def set_recording(self, on: bool) -> bool:
        """Start or stop recording for a module that needs a file open.

        The twin of `set_live`, added for the same reason (§5b A4): an
        experiment routine has to be recording before it can run a step, and
        making the operator find the Record button in another part of the
        window — then come back and press Start — is a worse design than
        letting the panel do it. Returns the PREVIOUS state, so a caller that
        started the recording can stop it again and leave one it did not start
        alone. Goes through the button, so the UI and the status line stay in
        step with reality; the toggle already starts the session if it is not
        running.
        """
        was = self._btn_rec.isChecked()
        if bool(on) != was:
            self._btn_rec.setChecked(bool(on))
        return was

    def latest_frame(self, key: str):
        """The newest frame from module `key`'s camera, or None. Why it exists:
        `devices.ModuleHost`. Why it reads the cache: `ModuleAdapter.last_frame`.

        Never commands the camera. Grabbing on demand would mean deciding when
        the DMD is all-on, and that is the operator's call, not this method's.
        """
        for m in self._modules:
            if m.key == key:
                return m.last_frame()
        return None

    def register_pg_view(self, view) -> None:
        self._pg_views.append(view)
        if self._building_key is not None:
            self._module_views.setdefault(self._building_key, []).append(view)

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """See `devices.ModuleHost.set_expected_rate`."""
        if self._save_panel is not None:
            self._save_panel.set_expected_rate(mbps, writer_mbps)

    def add_dock(self, title: str, widget: QWidget, area: "Qt.DockWidgetArea",
                 accent: str = "sync") -> QDockWidget:
        dock = self._make_dock(title, widget, area, accent)
        if self._building_key is not None:
            self._module_docks.setdefault(self._building_key, []).append(dock)
        return dock

    def on_worker_error(self, msg: str) -> None:
        # A device thread raised. Without this it escapes QThread.run() and
        # PyQt6 aborts the whole process.
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "device"
        self.status(f"{name}: {msg}")
        print(f"[worker error] {name}: {msg}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.resize(1600, 900)
        # (the app-wide stylesheet is applied once on the QApplication in main())
        self.setDockNestingEnabled(True)      # docks tabbable and nestable

        self._build_central()
        self._build_settings_dialog()
        self._build_plots_dock()
        for m in self._modules:          # extra docks (e.g. the pupil video box)
            self._build_views_for(m)

        # A starting width for the signals column only — dragged from here, and
        # the layout is remembered across runs.
        self.resizeDocks([self._plots_dock], [420], Qt.Orientation.Horizontal)

        self._build_status_bar()
        self._build_sidebar()

        self._restore_layout()

    def _build_views_for(self, m) -> None:
        """Run a module's `build_views()` with its docks and views attributed."""
        self._building_key = m.key
        try:
            m.build_views()
        finally:
            self._building_key = None

    def _central_claimant(self):
        """The first loaded module that wants the centre pane, or None.

        `central_title` is the claim, not `central_widget()`, which cannot be
        asked without building one. `_build_central` is its only caller.
        """
        for m in self._modules:
            if m.central_title:
                return m
        return None

    def _build_central(self) -> None:
        """The centre pane belongs to whichever module claims it (the primary
        camera); a placeholder stands in when that module isn't loaded."""
        # setCentralWidget DELETES the widget it replaces, so the outgoing
        # owner's pyqtgraph views have to leave `_pg_views` with it — a dangling
        # one is a native crash on the next theme toggle.
        if self._central_owner is not None:
            for v in self._module_views.pop(self._central_owner, []):
                if v in self._pg_views:
                    self._pg_views.remove(v)
        # Ask the claimant only. `central_widget()` BUILDS a view, so calling it
        # on every module would construct and discard one per module — and each
        # discarded one registers views that nothing will ever take back.
        owner = self._central_claimant()
        view = None
        if owner is not None:
            self._building_key = owner.key
            try:
                view = owner.central_widget()
            finally:
                self._building_key = None
        self._central_owner = owner.key if view is not None else None
        if view is None:
            placeholder = QLabel("Voltage camera not loaded")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color:#888; font-style:italic;")
            self.setCentralWidget(placeholder)
            return

        header = QLabel(owner.central_title)
        header.setStyleSheet(
            f"background:{style.HEX[owner.key]}; color:white; "
            "font-weight:bold; padding:3px 8px;")
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        lay.addWidget(view)
        self.setCentralWidget(wrap)

    def _build_settings_dialog(self) -> None:
        """Build the settings window (hidden until the sidebar tab is clicked).

        Built at startup rather than on first click: the module controllers are
        configured from these panels, so they have to exist before
        `_build_controllers()` opens any device."""
        self._settings_dialog = SettingsDialog(self)

        # Session-wide, not a module's, and the first thing to get right before
        # recording — so it leads the tabs.
        self._save_panel = SavePanel(config.load_dataclass(SaveConfig, "saving"))
        self._save_panel.settings_changed.connect(self._save_save_settings)
        self._settings_dialog.add_panel(self._save_panel, "Save", "saving")

        for m in self._modules:
            panel = m.build_panel()
            if panel is not None:
                self._settings_dialog.add_panel(panel, m.tab_label, m.key)

    def _build_plots_dock(self) -> None:
        self._plots_tabs = tabs = QTabWidget()
        tabs.setMovable(True)
        for m in self._modules:
            pw = m.build_plot()
            if pw is None:
                continue
            idx = tabs.addTab(pw, m.plot_label)
            tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[m.key]))
            self._module_plots[m.key] = pw
            self._building_key = m.key
            try:
                self.register_pg_view(pw)
            finally:
                self._building_key = None
        self._plots_dock = self._make_dock("Signals", tabs,
                                           Qt.DockWidgetArea.RightDockWidgetArea)

    def _build_status_bar(self) -> None:
        # Emulate: simulated signals for testing (off = real hardware).
        self._btn_emulate = QPushButton("Emulate")
        self._btn_emulate.setCheckable(True)
        self._btn_emulate.setChecked(self._emulate)
        self._btn_emulate.setStyleSheet(style.toggle_btn("wheel"))
        self._btn_emulate.setToolTip("Use simulated signals instead of hardware")
        self._btn_emulate.toggled.connect(self._on_emulate_toggled)

        # Live view: run all hardware + preview WITHOUT saving.
        self._btn_run = QPushButton("Live view")
        self._btn_run.setCheckable(True)
        self._btn_run.setStyleSheet(style.toggle_btn("sync"))
        self._btn_run.setToolTip("Show live signals from all devices (not saved)")
        self._btn_run.toggled.connect(self._on_run_toggled)

        # Devices and previews with NO session clock — what the `_toy.py`
        # harnesses did, without a second copy of every panel. Answers "camera
        # or my session code?" on limited rig time. Recording is impossible
        # here: with no clock, `SessionClock.at()` raises.
        self._btn_free = QPushButton("Free run")
        self._btn_free.setCheckable(True)
        self._btn_free.setStyleSheet(style.toggle_btn("stage"))
        self._btn_free.setToolTip(
            "Bring devices up with no session clock and no recording — for "
            "isolating a device from the session machinery.\n"
            "Pair with the startup module picker to run one instrument alone.")
        self._btn_free.toggled.connect(self._on_free_toggled)

        # Record: live view + save to HDF5 (auto-starts live view if needed).
        # Deliberately the largest control here — it is the only one whose wrong
        # state costs an experiment, and it used to be the same size as Emulate.
        self._btn_rec = QPushButton("● Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setStyleSheet(style.record_btn("puffer"))
        self._btn_rec.setToolTip("Live view and save every stream to disk")
        self._btn_rec.toggled.connect(self._on_record_toggled)

        # Elapsed, size on disk, drops. Not in the status message: that is
        # transient, and any module calling `status()` wipes it.
        self._lbl_rec = QLabel("")
        self._lbl_rec.setStyleSheet("color:#9aa0a6;")

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addPermanentWidget(self._lbl_rec)
        sb.addPermanentWidget(self._btn_emulate)
        sb.addPermanentWidget(self._btn_free)
        sb.addPermanentWidget(self._btn_run)
        sb.addPermanentWidget(self._btn_rec)
        sb.showMessage("Ready")

    def _build_sidebar(self) -> None:
        """Left side-bar: a 'Settings' tab that pops the settings *window* up,
        plus the theme toggle, the module picker and the device monitor. The
        window starts hidden; clicking the tab toggles it open/shut."""
        self._sidebar = QToolBar("Sidebar")
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setMovable(False)
        self._sidebar.setOrientation(Qt.Orientation.Vertical)
        self._sidebar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # Left-aligned, full-width buttons: a vertical toolbar centres each one
        # by default, so a list of differently-sized labels comes out ragged and
        # unreadable as a list.
        self._sidebar.setStyleSheet(
            "QToolButton { text-align:left; padding:4px 10px; }")
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self._sidebar)

        # One item per settings PAGE — Save, then a loaded instrument each —
        # so the sidebar doubles as the list of what is loaded. Filled by
        # _rebuild_page_actions, which runs again whenever the module set
        # changes; everything below the separator is fixed.
        self._page_actions: dict[str, QAction] = {}
        self._sidebar_sep = self._sidebar.addSeparator()
        self._rebuild_page_actions()
        # Closing the window (✕ or Esc, both of which reach `finished`) has to
        # un-check whichever page was showing, or its next click does nothing.
        self._settings_dialog.finished.connect(
            lambda _result: self._check_page(None))
        # Both ways round: the tab bar is still there, so switching page inside
        # the window has to move the sidebar's highlight with it.
        self._settings_dialog.tabs.currentChanged.connect(
            self._on_settings_tab_changed)

        # Dark/light theme toggle (persisted to config; default dark).
        self._theme_action = QAction(self._swatch(None), "☾ Theme", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(config.get_theme() == "dark")
        self._theme_action.setToolTip("Toggle dark / light theme")
        self._theme_action.toggled.connect(self._on_theme_toggled)
        self._sidebar.addAction(self._theme_action)

        # Load/unload instruments without restarting. Disabled only while
        # recording — see set_modules.
        self._modules_action = QAction(self._swatch(None), "🧩 Modules", self)
        self._modules_action.setToolTip(
            "Load or unload instruments without restarting the app")
        self._modules_action.triggered.connect(self._open_modules_dialog)
        self._sidebar.addAction(self._modules_action)

        # Device connection monitor (probe-based; safe to open any time).
        self._devices_action = QAction(self._swatch(None), "🔌 Devices", self)
        self._devices_action.setToolTip("Check which devices are detected")
        self._devices_action.triggered.connect(self._show_devices)
        self._sidebar.addAction(self._devices_action)
        self._stretch_sidebar()

    def _make_dock(self, title: str, widget: QWidget,
                   area: "Qt.DockWidgetArea", accent: str = "sync") -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title}")
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        dock.setStyleSheet(style.dock_accent(accent))
        self.addDockWidget(area, dock)
        return dock

    # ── Settings window ─────────────────────────────────────────────────────────

    @staticmethod
    def _swatch(key: str | None) -> "QIcon":
        """A chip in the subsystem's accent for its sidebar item.

        `None` gives a transparent one, which the items below the separator
        carry so every button lays out the same way: a QToolButton WITHOUT an
        icon ignores the stylesheet's `text-align:left` and centres its label.
        """
        pm = QPixmap(12, 12)
        pm.fill(QColor(style.HEX[key]) if key else Qt.GlobalColor.transparent)
        return QIcon(pm)

    def _rebuild_page_actions(self) -> None:
        """One sidebar item per settings page, in `config.MODULES` order.

        Rebuilt wholesale rather than patched: the module set changes rarely and
        a stale item points at a deleted panel.
        """
        for act in self._page_actions.values():
            self._sidebar.removeAction(act)
        self._page_actions.clear()
        self._page_panels: dict[str, QWidget] = {}

        pages: list[tuple[str, str, QWidget]] = []
        if self._save_panel is not None:
            pages.append(("saving", "Save", self._save_panel))
        pages += [(m.key, m.tab_label, m.panel)
                  for m in self._modules if m.panel is not None]

        for key, label, panel in pages:
            act = QAction(self._swatch(key), label, self)
            act.setCheckable(True)
            act.setToolTip(f"{label} settings")
            act.triggered.connect(
                lambda _checked=False, p=panel, k=key: self._show_page(k, p))
            self._sidebar.insertAction(self._sidebar_sep, act)
            self._page_actions[key] = act
            self._page_panels[key] = panel
        self._stretch_sidebar()
        # Fresh QActions default to unchecked, so an open window would be left
        # showing a page nothing in the sidebar is lit for.
        if self._settings_dialog.isVisible():
            self._on_settings_tab_changed(0)

    def _stretch_sidebar(self) -> None:
        """All buttons one width, so the left edges line up.

        A size policy will not do it — `QToolBarLayout` sizes each button to its
        own content and centres it, whatever the child asks for. Setting the
        same minimum width on all of them is what actually aligns the labels.
        """
        btns = [self._sidebar.widgetForAction(a) for a in self._sidebar.actions()]
        btns = [b for b in btns if b is not None]
        if not btns:
            return
        widest = max(b.sizeHint().width() for b in btns)
        for b in btns:
            b.setMinimumWidth(widest)

    def _on_settings_tab_changed(self, _index: int) -> None:
        if not self._settings_dialog.isVisible():
            return                       # a page removed, not a page chosen
        panel = self._settings_dialog.current_panel()
        self._check_page(next((k for k, p in self._page_panels.items()
                               if p is panel), None))

    def _check_page(self, key: str | None) -> None:
        """Exactly one page item checked — or none, with the window shut."""
        for k, act in self._page_actions.items():
            act.setChecked(k == key)

    def _show_page(self, key: str, panel) -> None:
        """Open the settings window on this page, or shut it if already there.

        Clicking the page you are on closes the window, which is what the single
        Settings toggle used to do.
        """
        showing = (self._settings_dialog.isVisible()
                   and self._settings_dialog.current_panel() is panel)
        if showing:
            self._settings_dialog.save_geometry()
            self._settings_dialog.hide()
            self._check_page(None)
            return
        self._settings_dialog.show_panel(panel)
        self._settings_dialog.show()
        # Re-open in front: it may have been left behind the main window.
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()
        self._check_page(key)

    # ── Dock layout persistence ─────────────────────────────────────────────────

    def _restore_layout(self) -> None:
        state = QSettings("acqApp", "acqApp").value("dockState")
        if state is not None:
            self.restoreState(state)

    def _save_layout(self) -> None:
        QSettings("acqApp", "acqApp").setValue("dockState", self.saveState())

    # ── Theme ───────────────────────────────────────────────────────────────────

    def _on_theme_toggled(self, dark: bool) -> None:
        theme = "dark" if dark else "light"
        config.set_theme(theme)
        style.apply_theme(QApplication.instance(), theme)
        # Recolour existing pyqtgraph views — setConfigOption only affects new
        # ones. Axis labels don't repaint live but are right next run.
        bg = style.plot_colors(theme)[0]
        for v in self._pg_views:
            try:
                v.setBackground(bg)
            except Exception:
                pass

    # ── Device connection monitor ────────────────────────────────────────────────

    def _probe_kwargs(self) -> dict:
        """Per-module arguments for probe.probe_all (e.g. the stage's port)."""
        kwargs: dict = {}
        for m in self._modules:
            kwargs.update(m.probe_kwargs())
        return kwargs

    def _show_devices(self) -> None:
        if self._devices_dialog is None:
            # Probe in load-order, only the modules this session loaded.
            self._devices_dialog = ConnectionMonitor(
                [m.key for m in self._modules], self._probe_kwargs, parent=self)
        else:
            self._devices_dialog.refresh()
        self._devices_dialog.show()
        self._devices_dialog.raise_()
        self._devices_dialog.activateWindow()

    # ── Session start / stop ──────────────────────────────────────────────────

    def _on_run_toggled(self, on: bool) -> None:
        if on:
            self._start_session()
        else:
            self._stop_session()

    def _on_free_toggled(self, on: bool) -> None:
        """Free run: devices up, no clock, no recording."""
        self._free_run = on
        self._btn_rec.setEnabled(not on)
        self._btn_rec.setToolTip(
            "Not available in free run — with no session clock there is no "
            "timebase to stamp samples against"
            if on else "Live view and save every stream to disk")
        self.status("Free run — devices only, no clock and no recording"
                    if on else "Ready")

    def _start_session(self) -> None:
        # Build the workers but don't start them: the shared clock must reach
        # t=0 BEFORE any device pushes a timestamped sample.
        for m in self._modules:
            m.build_session(self._emulate)

        # Free run leaves the clock unstarted on purpose: workers stamp with
        # `perf_counter` and only the Recorder converts — and there isn't one.
        if not self._free_run:
            self._sync.start_all()
        for m in self._modules:
            m.start()

        self._session_on = True
        self._disp_timer.start()
        self._btn_run.setText("Stop")
        self._btn_emulate.setEnabled(False)   # can't switch real/mock mid-session
        self._btn_free.setEnabled(False)      # …nor the clock, for the same reason

    def _stop_session(self) -> None:
        # Ensure recording is closed before tearing down the clock/workers.
        if self._btn_rec.isChecked():
            self._btn_rec.setChecked(False)   # triggers _on_record_toggled(False)

        self._disp_timer.stop()
        # Guarded per module: teardown touches hardware, and unguarded one
        # raise strands every module after it — threads running, stop_all()
        # skipped, clock alive with the UI saying "Stopped". Via closeEvent it
        # also skips the DCAM close, which is the native crash.
        for m in self._modules:
            try:
                m.stop()
            except Exception as e:
                self.status(f"{m.key}: stop failed ({type(e).__name__}: {e})")
                print(f"[main] {m.key}.stop() raised: {type(e).__name__}: {e}")
        self._sync.stop_all()          # harmless if free run never started it

        self._session_on = False
        self._btn_run.setText("Live view")
        self._btn_emulate.setEnabled(True)
        self._btn_free.setEnabled(True)
        self.status("Stopped (free run)" if self._free_run else "Stopped")

    def _on_emulate_toggled(self, on: bool) -> None:
        # Only togglable between sessions (the button is disabled while running).
        self._emulate = on
        self._build_controllers()               # swap puffer/LED/DMD real↔mock
        self._apply_title()
        self.status("Emulate ON — simulated signals" if on
                    else "Emulate OFF — real hardware")

    # ── Loading and unloading instruments in place ───────────────────────────

    def set_modules(self, keys) -> tuple[list[str], list[str]]:
        """Load/unload instruments without restarting → (loaded, unloaded).

        Refused while RECORDING: the file's `modules` attribute is written once
        at record start, and a stream that appears or vanishes part-way through
        is not describable by it. Between sessions and during a live/free run
        are both fine — a module loaded into a running session builds its worker
        and starts it against the clock already at t=0, which is safe because
        workers stamp in `perf_counter` and only the Recorder converts.
        """
        if self._recorder is not None:
            raise RuntimeError("stop the recording first")
        # A running routine is the second reason the set must hold still; the
        # adapter says so, so the window never learns what a routine is.
        for m in self._modules:
            why = m.busy_reason()
            if why:
                raise RuntimeError(why)

        want = [k for k in config.MODULES if k in set(keys) and k in adapters.ADAPTERS]
        have = [m.key for m in self._modules]
        removed = [k for k in have if k not in want]
        added = [k for k in want if k not in have]
        if not removed and not added:
            return [], []

        for key in removed:
            self._unload_module(key)
        for key in added:
            self._load_module(key)

        # config.MODULES order is load-bearing: closed_loop is last so that
        # every source-providing adapter exists before its panel asks.
        order = {k: i for i, k in enumerate(config.MODULES)}
        self._modules.sort(key=lambda m: order.get(m.key, len(order)))

        self._enabled = {m.key for m in self._modules}
        config.save_enabled_modules(list(self._enabled))
        # The monitor is built once around a SNAPSHOT of the module keys, so it
        # has to go rather than list instruments that are no longer loaded.
        if self._devices_dialog is not None:
            self._devices_dialog.close()
            self._devices_dialog.deleteLater()
            self._devices_dialog = None
        self._refresh_central()
        self._rebuild_page_actions()
        for m in self._modules:
            m.on_modules_changed()
        return added, removed

    def _load_module(self, key: str) -> None:
        """Build one adapter and splice its UI in at the right place."""
        m = adapters.ADAPTERS[key](self)
        self._modules.append(m)             # sorted into place by the caller

        panel = m.build_panel()
        if panel is not None:
            self._settings_dialog.add_panel(panel, m.tab_label, m.key,
                                            index=self._settings_tab_index(key))
        plot = m.build_plot()
        if plot is not None:
            idx = self._plots_tabs.insertTab(self._plot_tab_index(key), plot,
                                             m.plot_label)
            self._plots_tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[key]))
            self._module_plots[key] = plot
            self._building_key = key
            try:
                self.register_pg_view(plot)
            finally:
                self._building_key = None
        self._build_views_for(m)
        m.build_controller(self._emulate)

        if self._session_on:
            m.build_session(self._emulate)
            m.start()

    def _unload_module(self, key: str) -> None:
        """Stop one adapter and take back everything it put on the window."""
        m = next((x for x in self._modules if x.key == key), None)
        if m is None:
            return
        # Same guard as _stop_session: teardown touches hardware, and a raise
        # here would strand the widgets attached to a dead device.
        try:
            m.stop()
        except Exception as e:
            self.status(f"{key}: stop failed ({type(e).__name__}: {e})")
            print(f"[main] {key}.stop() raised: {type(e).__name__}: {e}")
        m.close_controller()

        # setParent(None) before deleteLater on all three: removeTab and
        # removeDockWidget only take the widget out of the LAYOUT, leaving it a
        # child of the window. Deferred deletion then keeps it alive across the
        # next restoreState(), which would put the dock back.
        if m.panel is not None:
            self._settings_dialog.remove_panel(m.panel)
            m.panel.setParent(None)
            m.panel.deleteLater()
        plot = self._module_plots.pop(key, None)
        if plot is not None:
            i = self._plots_tabs.indexOf(plot)
            if i >= 0:
                self._plots_tabs.removeTab(i)
            plot.setParent(None)
            plot.deleteLater()
        for dock in self._module_docks.pop(key, []):
            self.removeDockWidget(dock)
            dock.setParent(None)
            dock.deleteLater()
        for view in self._module_views.pop(key, []):
            if view in self._pg_views:
                self._pg_views.remove(view)

        self._modules.remove(m)

    def _settings_tab_index(self, key: str) -> int:
        """Where a hot-loaded module's tab goes: after the last page that
        precedes it in `config.MODULES`.

        Read off the LIVE tab positions rather than counted, because the tabs
        are draggable — counting assumes an order the operator may have changed.
        """
        dlg = self._settings_dialog
        order = list(config.MODULES)
        last = dlg.panel_index(self._save_panel) if self._save_panel else -1
        for m in self._modules:
            if m.key == key or m.panel is None:
                continue
            if order.index(m.key) < order.index(key):
                last = max(last, dlg.panel_index(m.panel))
        return last + 1

    def _plot_tab_index(self, key: str) -> int:
        order = list(config.MODULES)
        return len([k for k in self._module_plots
                    if k != key and order.index(k) < order.index(key)])

    def _refresh_central(self) -> None:
        """Rebuild the centre pane only if its owner changed.

        `central_widget()` builds a fresh view every call, so rebuilding when
        nothing moved would throw away the live image for no reason.
        """
        claimant = self._central_claimant()
        want = claimant.key if claimant is not None else None
        if want != self._central_owner:
            self._build_central()

    def _open_modules_dialog(self) -> None:
        """The startup picker again, mid-session."""
        if self._recorder is not None:
            self.status("Stop the recording before changing which modules "
                        "are loaded")
            return
        running = " — applied to the running session" if self._session_on else ""
        dlg = ModuleSelectDialog(
            sorted(self._enabled), parent=self, title="Modules",
            prompt=f"Instruments to load{running}:")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            added, removed = self.set_modules(dlg.selected())
        except RuntimeError as e:
            self.status(f"Cannot change modules — {e}")
            return
        if not added and not removed:
            self.status("Modules unchanged")
            return
        bits = []
        if added:
            bits.append("loaded " + ", ".join(added))
        if removed:
            bits.append("unloaded " + ", ".join(removed))
        self.status("; ".join(bits)
                    + (" — running" if self._session_on else ""))

    def _build_controllers(self) -> None:
        """(Re)create the persistent output controllers (puffer, LED, DMD) for
        the current emulate mode. Real by default; mock when emulating. Real
        controllers that can't reach their hardware fall back to a mock so the UI
        stays usable — the Devices monitor is the source of truth for presence."""
        for m in self._modules:
            m.close_controller()
        for m in self._modules:
            m.build_controller(self._emulate)

    def _apply_title(self) -> None:
        title = "Acquisition suite"
        if self._cam_info is not None:
            title += f"  —  {getattr(self._cam_info, 'serial_number', self._cam_info)}"
        if self._emulate:
            title += "  [emulated]"
        self.setWindowTitle(title)

    # ── Recording ──────────────────────────────────────────────────────────────

    def _on_record_toggled(self, on: bool) -> None:
        if on:
            if self._free_run:
                # The button is disabled in free run, but a programmatic
                # setChecked would still reach the Recorder, and every put()
                # would raise out of a worker thread.
                self.status("Cannot record in free run — no session clock")
                self._btn_rec.setChecked(False)
                return
            # Record implies live view — start the session if it isn't running.
            if not self._session_on:
                self._btn_run.setChecked(True)   # → _start_session()
            if not self._session_on:              # start failed → abort record
                self._btn_rec.setChecked(False)
                return
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self._save_panel is None:
            return
        # Fail *before* any data is taken, not after it has nowhere to go.
        err = self._save_panel.writable_error()
        if err is not None:
            self.status(f"Cannot record — {err}")
            self._btn_rec.setChecked(False)
            return

        now = datetime.now()
        # unique=True: the writer refuses to truncate an existing session
        # (mode "x"), but failing to record is also a lost session — so take
        # the next free name rather than raise.
        path = self._save_panel.resolve(now, unique=True)
        sc = self._save_panel.settings
        metadata = {
            "created":  now.strftime("%Y%m%d_%H%M%S"),
            "emulated": self._emulate,
            "modules":  ",".join(sorted(self._enabled)),
            "subject":  sc.subject,
            "session":  sc.session,
        }
        for m in self._modules:
            metadata.update(m.metadata())

        rec = Recorder(
            self._clock, HDF5Writer(),
            RingBuffer(RING_FRAMES, maxbytes=RING_BYTES, sizeof=_sample_nbytes))
        try:
            rec.start(path, metadata)
        except OSError as e:
            # Opening is on this thread, before the writer thread exists: a
            # full disk, or a name taken between resolve() and open(), must
            # un-toggle Record rather than leave a half-built Recorder.
            self.status(f"Cannot record → {path}: {e}")
            self._btn_rec.setChecked(False)
            return
        self._recorder = rec
        self._rec_path = path

        # The Recorder stamps each sample on the shared clock, so every stream
        # in the file shares one time origin.
        for m in self._modules:
            m.attach_sink(self._recorder)

        self._btn_rec.setText("■ Stop rec")
        # Greyed rather than left to fail: set_modules refuses while recording,
        # and a button that explains itself beats an error after the click.
        self._modules_action.setEnabled(False)
        self._modules_action.setToolTip(
            "Not while recording — the file names its modules once, at the start")
        self.status(f"Recording → {path}")

    def _stop_recording(self) -> None:
        self._modules_action.setEnabled(True)
        self._modules_action.setToolTip(
            "Load or unload instruments without restarting the app")
        for m in self._modules:
            m.detach_sink()
        rec = self._recorder
        self._recorder = None
        if rec is not None:
            # What the run actually did, not how it was configured — a file
            # that shed samples should say so itself. A callback because the
            # counts are final only after the drain and before the close, which
            # only Recorder.stop() can sequence.
            def final() -> dict[str, Any]:
                d: dict[str, Any] = {
                    "recorder_dropped_samples":   rec.drop_count,
                    "recorder_late_samples":      rec.late_count,
                    "recorder_unstamped_samples": rec.unstamped_count,
                }
                for m in self._modules:
                    d.update(m.final_metadata())
                return d

            remaining = rec.stop(final_metadata=final)
            lost = rec.drop_count + rec.late_count + remaining
            msg = f"Recording stopped (dropped {rec.drop_count} samples while running"
            for n, what in ((rec.late_count, "late"), (remaining, "un-drained")):
                if n:
                    msg += f", {n} {what}"
            msg += ")"
            if lost:
                msg += "  — see the file's recorder_* attributes"
            self.status(msg)
        self._btn_rec.setText("● Record")
        self._lbl_rec.setText("")

    # ── Sync callbacks ──────────────────────────────────────────────────────────

    def _on_tick(self, elapsed: float) -> None:
        self.status(f"t = {elapsed:.1f} s")
        self._refresh_rec_readout(elapsed)

    def _refresh_rec_readout(self, elapsed: float) -> None:
        """Elapsed / size on disk / drops, while recording.

        The size comes from the file itself rather than from a running total of
        what was enqueued: those differ exactly when it matters — a ring that is
        shedding, or a writer that has fallen behind — and the number worth
        trusting is the one on the disk. HDF5 grows in blocks, so it steps.
        """
        if self._recorder is None or self._rec_path is None:
            self._lbl_rec.setText("")
            return
        mins, secs = divmod(int(elapsed), 60)
        txt = f"● REC  {mins:d}:{secs:02d}"
        try:
            mb = self._rec_path.stat().st_size / (1 << 20)
            txt += f"   {mb / 1024:.2f} GB" if mb >= 1024 else f"   {mb:.0f} MB"
        except OSError:
            pass                          # not created yet, or on a flaky share
        dropped = self._recorder.drop_count + self._recorder.late_count
        if dropped:
            txt += f"   ⚠ {dropped} samples shed"
            self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;")
        else:
            self._lbl_rec.setStyleSheet("color:#2e7d32; font-weight:bold;")
        self._lbl_rec.setText(txt)

    def _on_trigger(self, name: str, duration: float) -> None:
        for m in self._modules:
            m.on_trigger(name, duration)

    # ── Display tick (preview only; recording is fed straight from the workers) ──

    def _display_tick(self) -> None:
        for m in self._modules:
            m.update_display()

    # ── Save configuration ──────────────────────────────────────────────────────

    def _save_save_settings(self, *_args) -> None:
        if self._save_panel is not None:
            config.save_settings("saving", self._save_panel.as_dict())

    # ── Cleanup ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._save_layout()
        # A top-level settings window outlives the main one and keeps the app
        # alive with no way back to it.
        self._settings_dialog.save_geometry()
        self._settings_dialog.close()
        if self._session_on:
            self._stop_session()
        for m in self._modules:
            m.close_controller()
        if self._cam_handle is not None:
            try:
                self._cam_handle.close()
            except Exception:
                pass
            self._cam_handle = None
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true",
                    help="start in Emulate mode (simulated signals, no hardware)")
    ap.parse_args()

    app = QApplication(sys.argv)
    style.apply_theme(app, config.get_theme())     # dark by default

    # Startup module picker, pre-checked from the last-used selection. The
    # camera is opening on its thread while this is up.
    dlg = ModuleSelectDialog(config.load_enabled_modules())
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    if not accepted:
        # Still join, and release: a half-open camera outliving the process is
        # the double-open crash waiting for the next launch.
        _await_camera()
        if _cam_handle is not None:
            try:
                _cam_handle.close()
            except Exception:
                pass
        return
    enabled = dlg.selected()
    config.save_enabled_modules(enabled)

    _await_camera()          # the handle must exist before any adapter asks
    win = MainWindow(cam_info=_cam_info, mock=_mock, enabled=set(enabled),
                     cam_handle=_cam_handle)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
