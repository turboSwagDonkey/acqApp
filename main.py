"""
In vivo acquisition suite — top-level entry point.

Wires together voltage_cam, pupil_cam, wheel, puffer, stage and dmd around ONE
shared SessionClock: every device timestamps its data against the same origin,
and a single Recorder streams all streams into one HDF5 session file (gap-free,
on a common timebase).

This file owns only what is session-wide — the clock, the sync/trigger bus, the
recorder, the save destination, the docks and the theme. Everything specific to
one instrument lives in a `ModuleAdapter` in `adapters/`, and this window just
iterates over the adapters for the modules the user loaded.

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
from pathlib import Path

# A segfault deep in the DCAM SDK can't be caught by try/except — the process
# just dies. faulthandler dumps the C-level + Python stack on a fatal signal.
faulthandler.enable()


# ── Environment bootstrap (must run before any third-party import) ────────────
def _bootstrap() -> None:
    """
    Make `main.py` robust to how/where it was launched, WITHOUT ever touching an
    environment other than the project's own `acqApp/.venv`:
      1. Put the acqApp parent dir on sys.path so `import acqApp` resolves when
         the file is run directly (not just via `-m acqApp.main`), then make the
         console safe — both before anything is printed.
      2. If we're not running the project venv, create it (if missing) and
         re-exec into it — so the wrong interpreter is never used.
      3. Install requirements.txt if a core dependency is missing — but ONLY when
         we're inside the venv, so pip never installs into an unrelated Python.
    """
    here = Path(__file__).resolve().parent            # …/acqApp
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    venv_dir = here / ".venv"
    venv_py = venv_dir / scripts / exe

    # (1) make the package importable, then harden stdout — FIRST, so every
    # print below can use arrows and symbols without risking
    # UnicodeEncodeError, which kills device threads (see console.py). That
    # module imports only `sys`, so this is safe pre-re-exec.
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

# ── Hardware pre-init (must precede any Qt / scipy import) ────────────────────
# Open the camera ONCE and keep the handle — the worker reuses it. Re-opening a
# just-closed DCAM device crashes the driver natively, and a fresh open costs
# ~7 s. Closed in MainWindow.closeEvent.
_cam_info = None
_cam_handle = None
_mock = "--mock" in sys.argv     # start in Emulate mode; real hardware otherwise
if not _mock:
    try:
        from pylablib.devices import DCAM as _DCAM
        if _DCAM.get_cameras_number() > 0:
            _cam_handle = _DCAM.DCAMCamera(idx=0)
            _cam_info = _cam_handle.get_device_info()
            print(f"Voltage cam: {_cam_info}")
        else:
            # No silent fallback to fake data — real is the default.
            print("No DCAM camera detected — use Emulate to run without hardware")
    except Exception as _e:
        print(f"Camera detect failed ({_e}) — use Emulate to run without hardware")
# ─────────────────────────────────────────────────────────────────────────────

# Force pyqtgraph onto PyQt6 (both bindings may be installed in the venv).
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDockWidget, QLabel, QMainWindow, QPushButton,
    QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from acqApp import adapters, config, style
from acqApp.dialogs import ConnectionMonitor, ModuleSelectDialog, SettingsDialog
from acqApp.saving import SaveConfig, SavePanel
from acqApp.sync import SyncController
from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import HDF5Writer

pg.setConfigOptions(imageAxisOrder="row-major")

RING_FRAMES  = 512          # recording ring-buffer item cap (scalar streams)
RING_BYTES   = 512 << 20    # …and a 512 MB payload cap so full frames can't OOM


def _sample_nbytes(item) -> int:
    """Payload bytes of a Recorder ring item (stream, ts, data); 0 for scalars."""
    return getattr(item[2], "nbytes", 0)



class MainWindow(QMainWindow):
    """Session-wide shell: clock, triggers, recorder, save target, docks, theme.

    Per-instrument behaviour is NOT here — each loaded subsystem is a
    `adapters.ModuleAdapter` that owns its own panel, worker, display tick,
    recording sink and metadata. This window only iterates over them, which is
    why adding an instrument is a new adapter class rather than a new branch in
    four different methods.
    """

    def __init__(self, cam_info=None, mock=False, enabled: set[str] | None = None,
                 cam_handle=None):
        super().__init__()
        # Simulated signals for dev/testing; OFF by default, and togglable only
        # between sessions.
        self._emulate = mock
        # Devices without the session clock, so nothing can be recorded. Never
        # persisted: a launch that quietly came up unable to record would be
        # worse than useless on a rig (as with the closed loop's `armed`).
        self._free_run = False
        # Tracked rather than read off `_sync.running`: in free run the sync
        # controller is deliberately not running, so that would answer no
        # mid-session.
        self._session_on = False
        self._enabled = enabled if enabled is not None else set(config.MODULES)
        self._cam_info = cam_info
        # Opened once at startup and reused by every session's worker (see the
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

    def register_pg_view(self, view) -> None:
        """Track a pyqtgraph view so the theme toggle can recolour it."""
        self._pg_views.append(view)

    def set_expected_rate(self, mbps: float) -> None:
        """Feed the acquisition data rate to the Save tab's capacity estimate."""
        if self._save_panel is not None:
            self._save_panel.set_expected_rate(mbps)

    def add_dock(self, title: str, widget: QWidget, area: "Qt.DockWidgetArea",
                 accent: str = "sync") -> QDockWidget:
        return self._make_dock(title, widget, area, accent)

    def on_worker_error(self, msg: str) -> None:
        # A device thread raised. Without this the exception would escape
        # QThread.run() and PyQt6 would abort the whole process.
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "device"
        self.status(f"{name}: {msg}")
        print(f"[worker error] {name}: {msg}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.resize(1600, 900)
        # (the app-wide stylesheet is applied once on the QApplication in main())
        # Let docks be tabbed together and nested, so the user can drag panels
        # into any arrangement.
        self.setDockNestingEnabled(True)

        self._build_central()
        self._build_settings_dialog()
        self._build_plots_dock()
        for m in self._modules:          # extra docks (e.g. the pupil video box)
            m.build_views()

        # Initial width ≈ the classic 420 for the signals column; the user can
        # drag from here and the layout is remembered across runs.
        self.resizeDocks([self._plots_dock], [420], Qt.Orientation.Horizontal)

        self._build_status_bar()
        self._build_sidebar()

        self._restore_layout()

    def _build_central(self) -> None:
        """The centre pane belongs to whichever module claims it (the primary
        camera); a placeholder stands in when that module isn't loaded."""
        owner = view = None
        for m in self._modules:
            w = m.central_widget()
            if w is not None and view is None:
                owner, view = m, w
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

        # Save destination is session-wide (not tied to a module), and it is the
        # first thing to get right before recording — so it leads the tabs.
        self._save_panel = SavePanel(config.load_dataclass(SaveConfig, "saving"))
        self._save_panel.settings_changed.connect(self._save_save_settings)
        self._settings_dialog.add_panel(self._save_panel, "Save", "saving")

        for m in self._modules:
            panel = m.build_panel()
            if panel is not None:
                self._settings_dialog.add_panel(panel, m.tab_label, m.key)

    def _build_plots_dock(self) -> None:
        tabs = QTabWidget()
        tabs.setMovable(True)
        for m in self._modules:
            pw = m.build_plot()
            if pw is None:
                continue
            idx = tabs.addTab(pw, m.plot_label)
            tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[m.key]))
            self.register_pg_view(pw)
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

        # Free run: devices and previews with NO session clock — what the
        # `_toy.py` harnesses provided, minus a second copy of every panel to
        # keep in step. It answers "is it the camera or is it my session code?"
        # on a rig with limited time.
        #
        # Recording is impossible here and the button says so: with no clock
        # started, `SessionClock.at()` raises rather than invent a timebase.
        self._btn_free = QPushButton("Free run")
        self._btn_free.setCheckable(True)
        self._btn_free.setStyleSheet(style.toggle_btn("stage"))
        self._btn_free.setToolTip(
            "Bring devices up with no session clock and no recording — for "
            "isolating a device from the session machinery.\n"
            "Pair with the startup module picker to run one instrument alone.")
        self._btn_free.toggled.connect(self._on_free_toggled)

        # Record: live view + save to HDF5 (auto-starts live view if needed).
        self._btn_rec = QPushButton("Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setStyleSheet(style.toggle_btn("puffer"))
        self._btn_rec.setToolTip("Live view and save every stream to disk")
        self._btn_rec.toggled.connect(self._on_record_toggled)

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addPermanentWidget(self._btn_emulate)
        sb.addPermanentWidget(self._btn_free)
        sb.addPermanentWidget(self._btn_run)
        sb.addPermanentWidget(self._btn_rec)
        sb.showMessage("Ready")

    def _build_sidebar(self) -> None:
        """Left side-bar: a 'Settings' tab that pops the settings *window* up,
        plus the theme toggle and the device monitor. The window starts hidden;
        clicking the tab toggles it open/shut."""
        self._sidebar = QToolBar("Sidebar")
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setMovable(False)
        self._sidebar.setOrientation(Qt.Orientation.Vertical)
        self._sidebar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self._sidebar)

        # Checkable action ↔ window visibility, kept in sync both ways: closing
        # the window (title-bar ✕ or Esc, both of which reach `finished`) also
        # un-checks the tab, so the next click re-opens instead of doing nothing.
        self._settings_action = QAction("⚙ Settings", self)
        self._settings_action.setCheckable(True)
        self._settings_action.setToolTip("Open the settings window")
        self._settings_action.toggled.connect(self._on_settings_toggled)
        self._settings_dialog.finished.connect(
            lambda _result: self._settings_action.setChecked(False))
        self._sidebar.addAction(self._settings_action)

        # Dark/light theme toggle (persisted to config; default dark).
        self._theme_action = QAction("☾ Theme", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(config.get_theme() == "dark")
        self._theme_action.setToolTip("Toggle dark / light theme")
        self._theme_action.toggled.connect(self._on_theme_toggled)
        self._sidebar.addAction(self._theme_action)

        # Device connection monitor (probe-based; safe to open any time).
        self._devices_action = QAction("🔌 Devices", self)
        self._devices_action.setToolTip("Check which devices are detected")
        self._devices_action.triggered.connect(self._show_devices)
        self._sidebar.addAction(self._devices_action)

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

    def _on_settings_toggled(self, on: bool) -> None:
        if on:
            self._settings_dialog.show()
            # Re-open in front: it may have been left behind the main window.
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
        else:
            self._settings_dialog.save_geometry()
            self._settings_dialog.hide()

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
        # Recolour existing pyqtgraph views (setConfigOption only affects new
        # ones). Axis label colours don't repaint live but are correct next run.
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

        # Free run deliberately leaves the clock unstarted: workers stamp with
        # `perf_counter` and only the Recorder converts, which cannot exist here.
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
        # Guarded per module, because teardown touches hardware. Unguarded, one
        # raise skips every module after it — worker threads still running — and
        # skips stop_all(), leaving the clock and trigger bus alive with the UI
        # saying "Stopped". Through closeEvent it also skips the DCAM handle
        # close, which is the native crash the pre-init note describes.
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
                # Belt and braces: the button is disabled in free run, but a
                # programmatic setChecked would otherwise reach the Recorder,
                # and every put() would raise out of a worker thread.
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
        # Destination comes from the Save tab. Fail *before* any data is taken
        # rather than after it has nowhere to go.
        err = self._save_panel.writable_error()
        if err is not None:
            self.status(f"Cannot record — {err}")
            self._btn_rec.setChecked(False)
            return

        now = datetime.now()
        # unique=True: never hand the writer a path that already holds a
        # recording. The writer refuses to truncate one anyway (mode "x"), but
        # failing to record is also a lost session — take the next free name.
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
            # Opening happens on this thread, before the writer thread exists —
            # a full disk or a name taken between resolve() and open() must
            # un-toggle Record, not leave a half-built Recorder attached.
            self.status(f"Cannot record → {path}: {e}")
            self._btn_rec.setChecked(False)
            return
        self._recorder = rec
        self._rec_path = path

        # Attach per-device sinks. The Recorder stamps each sample on the shared
        # clock, so all streams in the file share one time origin.
        for m in self._modules:
            m.attach_sink(self._recorder)

        self._btn_rec.setText("Stop rec")
        self.status(f"Recording → {path}")

    def _stop_recording(self) -> None:
        for m in self._modules:
            m.detach_sink()
        rec = self._recorder
        self._recorder = None
        if rec is not None:
            # What the run actually did, not just how it was configured — a file
            # that shed samples should say so itself. A callback because the
            # counts are final only after the drain, while the file is still
            # open; Recorder.stop() sequences both.
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
        self._btn_rec.setText("Record")

    # ── Sync callbacks ──────────────────────────────────────────────────────────

    def _on_tick(self, elapsed: float) -> None:
        msg = f"t = {elapsed:.1f} s"
        if self._recorder is not None:
            msg += f"   REC   drops: {self._recorder.drop_count}"
        self.status(msg)

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
        # A top-level settings window would outlive the main window and keep the
        # app alive with no way back to it.
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

    # Startup module picker, pre-checked from the last-used selection.
    dlg = ModuleSelectDialog(config.load_enabled_modules())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    enabled = dlg.selected()
    config.save_enabled_modules(enabled)

    win = MainWindow(cam_info=_cam_info, mock=_mock, enabled=set(enabled),
                     cam_handle=_cam_handle)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
