"""
In vivo acquisition suite — entry point.

Owns what is session-wide: the clock, the sync/trigger bus, the recorder, the
save destination, docks and theme. Each instrument is a `ModuleAdapter` in
`adapters/`; this window only iterates over them.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\main.py
  python acqApp\\main.py                  (re-execs into the venv)
  python -m acqApp.main --mock

ACQAPP_NO_REEXEC=1 skips the venv re-exec; ACQAPP_NO_INSTALL=1 skips
installing requirements.
"""

from __future__ import annotations
import argparse
import faulthandler
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# A segfault in the DCAM SDK can't be caught; this at least dumps the stacks.
faulthandler.enable()


# ── Environment bootstrap (before any third-party import) ─────────────────────
def _bootstrap() -> None:
    """Run from anywhere, but only ever inside `acqApp/.venv`: create it and
    re-exec if needed, and pip-install only from inside it."""
    here = Path(__file__).resolve().parent
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    venv_dir = here / ".venv"
    venv_py = venv_dir / scripts / exe

    parent = str(here.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    # Before any print (console.py imports only sys).
    from acqApp.console import enable_safe_console
    enable_safe_console()

    try:
        in_venv = Path(sys.executable).resolve() == venv_py.resolve()
    except OSError:
        in_venv = False

    if not in_venv and not os.environ.get("ACQAPP_NO_REEXEC"):
        if not venv_py.exists():
            print(f"[bootstrap] creating project venv at {venv_dir} …")
            try:
                subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            except (subprocess.CalledProcessError, OSError) as e:
                sys.exit(f"[bootstrap] couldn't create venv ({e}); create it "
                         f"manually:\n    python -m venv {venv_dir}")
        os.environ["ACQAPP_NO_REEXEC"] = "1"          # no re-exec loops
        print(f"[bootstrap] launching under {venv_py}")
        os.execv(str(venv_py), [str(venv_py), str(here / "main.py"), *sys.argv[1:]])

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
        req = here / "requirements.txt"
        print(f"[bootstrap] installing dependencies into the venv from {req} …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])


_bootstrap()

from datetime import datetime
from typing import Any

from acqApp import config

# ── Hardware pre-init ─────────────────────────────────────────────────────────
# The camera is opened ONCE and the handle reused: re-opening a just-closed
# DCAM device crashes natively. Closed in MainWindow.closeEvent.
_cam_info = None
_cam_handle = None
_cam_thread = None
_mock = "--mock" in sys.argv


def _open_camera() -> None:
    """The startup open, on a worker thread; never raises."""
    global _cam_handle, _cam_info
    t0 = time.perf_counter()
    dcam = None
    try:
        from pylablib.devices import DCAM as dcam
        from acqApp.devices.voltage_cam.acquisition import open_camera
        # Open first, count only on failure: get_cameras_number()
        # re-enumerates every call (~5 s).
        handle = open_camera(0)
        # Kept even if the info read fails: a second open would crash natively.
        _cam_handle = handle
        try:
            _cam_info = handle.get_device_info()
        except Exception as e:                    # noqa: BLE001
            print(f"Voltage cam: device info unreadable ({e})")
        print(f"Voltage cam: {_cam_info or 'opened'} "
              f"(in {time.perf_counter() - t0:.1f} s)")
    except Exception as e:                        # noqa: BLE001
        _cam_handle = None
        n = -1
        if dcam is not None:
            try:
                n = dcam.get_cameras_number()
            except Exception:
                pass
        if n == 0:
            print("No DCAM camera detected — use Emulate to run without hardware")
        else:
            print(f"Camera unavailable ({type(e).__name__}: {e}) — if HCImage "
                  f"or another app has it open, close that first. Use Emulate "
                  f"to run without it.")


if not _mock and "voltage_cam" in config.load_enabled_modules():
    # Overlaps the ~8 s open with the Qt import and module picker. Keyed on
    # the LAST selection; if voltage_cam is enabled only now, the worker opens
    # its own handle on first Start.
    _cam_thread = threading.Thread(target=_open_camera, name="cam-open")
    _cam_thread.start()


def _await_camera() -> None:
    if _cam_thread is not None:
        if _cam_thread.is_alive():
            print("Waiting for the camera to finish opening…")
        _cam_thread.join()


def _close_camera(handle) -> None:
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
# ─────────────────────────────────────────────────────────────────────────────

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QAction, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDockWidget, QLabel, QMainWindow,
    QPushButton, QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from acqApp import adapters, style
from acqApp.dialogs import (ConnectionMonitor, ModuleSelectDialog, PanelWindow,
                            SettingsDialog)
from acqApp.saving import SaveConfig, SavePanel, write_routine_fov_sidecar
from acqApp.acq.sync import DEFAULT_TICK_MS, SyncController
from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import HDF5Writer, SplitWriter

pg.setConfigOptions(imageAxisOrder="row-major")

RING_FRAMES  = 512          # ring item cap (scalar streams)
# Payload cap. 512 MB shed 14-54 frames per 30 s run at 106 Hz on a writer
# stall; 2 GB shed none; 4 GB buys nothing more.
RING_BYTES   = 2048 << 20


def _sample_nbytes(item) -> int:
    """Payload bytes of a ring item (stream, ts, data); 0 for scalars."""
    return getattr(item[2], "nbytes", 0)


# "None" leaves everything as set; other modes come from modes.json.
MODE_NONE = "None"

_MODULES_TIP = "Load or unload instruments without restarting the app"


def _rank(key: str) -> int:
    """Position in config.MODULES; unknown keys sort last."""
    keys = list(config.MODULES)
    return keys.index(key) if key in config.MODULES else len(keys)


class MainWindow(QMainWindow):
    """Session-wide shell. Implements `acq.devices.ModuleHost`, which
    documents the services adapters use."""

    def __init__(self, cam_info=None, mock=False, enabled: set[str] | None = None,
                 cam_handle=None):
        super().__init__()
        self._emulate = mock
        self._session_on = False
        self._enabled = (enabled if enabled is not None
                         else set(config.MODULES)) | config.ALWAYS_ON
        self._cam_info = cam_info
        self._cam_handle = cam_handle

        self._clock = SessionClock()
        self._sync  = SyncController(self._clock, tick_ms=DEFAULT_TICK_MS)
        self._sync.tick.connect(self._on_tick)
        self._sync.trigger_fired.connect(self._on_trigger)

        self._recorder: Recorder | None = None
        self._rec_path: Path | None = None
        self._rec_t0: float = 0.0     # session-clock time Record was pressed
        # File size is stat()ed at ~1 Hz: on a network share it can block.
        self._rec_size_t0: float = 0.0
        self._rec_size_txt: str = ""
        self._rec_warn: bool | None = None     # repaint only on change
        # Set by the routine before it opens/rolls a file; None = Save tab template.
        self._routine_save_ctx: tuple[str, int, tuple | None] | None = None
        self._routine_arming_trigger = False
        self._save_panel: SavePanel | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._panel_windows: dict[str, PanelWindow] = {}
        self._devices_dialog: ConnectionMonitor | None = None
        self._pg_views: list = []      # recoloured on theme change
        # What each module added to the window, to take back on unload.
        # add_dock/register_pg_view don't say who's calling, so the window
        # tracks whose build is running.
        self._building_key: str | None = None
        self._module_docks: dict[str, list] = {}
        self._module_views: dict[str, list] = {}
        self._module_plots: dict[str, QWidget] = {}
        self._central_owner: str | None = None

        self._modules = adapters.build_adapters(self, self._enabled)
        self._build_ui()
        # Controllers are configured from their panels, so after the UI.
        self._build_controllers()
        self._apply_title()

        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)   # ~30 Hz
        self._disp_timer.timeout.connect(self._display_tick)

    # ── Services the module adapters use (see ModuleHost) ─────────────────────

    @property
    def sync(self) -> SyncController:
        return self._sync

    @property
    def cam_handle(self):
        return self._cam_handle

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def module_keys(self) -> list[str]:
        return [m.key for m in self._modules]

    def signal_sources(self) -> list:
        return [s for m in self._modules for s in m.signal_sources()]

    def stage_target(self):
        return self._first("stage_target")

    def pattern_target(self):
        return self._first("pattern_target")

    def led_target(self):
        return self._first("led_target")

    def puffer_target(self):
        return self._first("puffer_target")

    def frame_rate_hz(self) -> float | None:
        return self._first("frame_rate_hz")

    def active_fov_name(self) -> str:
        stage = self.stage_target()
        return stage.active_fov_name() if stage is not None else ""

    def dcimg_enabled(self) -> bool:
        """Split mode only: a .dcimg can't live inside a composite .h5."""
        sc = self._save_panel.settings
        return bool(sc.split and sc.orca_format == "dcimg")

    def dcimg_target(self, stream: str) -> Path | None:
        if not self.dcimg_enabled() or self._rec_path is None:
            return None
        return self._rec_path / f"{self._rec_path.name}_{stream}.dcimg"

    def _first(self, name: str):
        for m in self._modules:
            got = getattr(m, name)()
            if got is not None:
                return got
        return None

    def set_live(self, on: bool) -> bool:
        """Through the button, so the UI stays in step. Returns the previous state."""
        was = self._btn_run.isChecked()
        if bool(on) != was:
            self._btn_run.setChecked(bool(on))
        return was

    def set_recording(self, on: bool) -> bool:
        """Through the button (which starts the session if needed). Returns
        the previous state."""
        was = self._btn_rec.isChecked()
        if bool(on) != was:
            self._btn_rec.setChecked(bool(on))
        return was

    def is_recording(self) -> bool:
        return self._btn_rec.isChecked()

    def _module(self, key: str):
        for m in self._modules:
            if m.key == key:
                return m
        return None

    def _call(self, key: str, name: str, *args, default=None):
        """`module[key].name(*args)`, or `default` if the module isn't loaded
        or has no such method — modes.json keys are hand-edited."""
        m = self._module(key)
        fn = getattr(m, name, None) if m is not None else None
        return fn(*args) if fn is not None else default

    def camera_preset(self, key: str) -> str | None:
        return self._call(key, "preset_key")

    def _swap(self, key: str, get: str, put: str, value):
        """`module[key].put(value)`; returns the `get()` from before."""
        m = self._module(key)
        if m is None or not hasattr(m, put):
            return None
        prev = getattr(m, get)()
        getattr(m, put)(value)
        return prev

    def set_camera_preset(self, key: str, preset: str) -> str | None:
        """Returns the previous preset. Takes effect at the next session start."""
        return self._swap(key, "preset_key", "set_preset", preset)

    def camera_binning(self, key: str) -> int | None:
        return self._call(key, "binning")

    def set_camera_binning(self, key: str, n: int) -> int | None:
        """Returns the previous value. Takes effect at the next session start."""
        return self._swap(key, "binning", "set_binning", n)

    def set_camera_trigger(self, key: str, on: bool) -> bool | None:
        return self._call(key, "set_external_trigger", on)

    def set_camera_burst(self, key: str, n: int) -> bool | None:
        return self._call(key, "set_burst_frames", n)

    def camera_burst_frames(self, key: str) -> int | None:
        return self._call(key, "burst_frames_done")

    def rearm_camera_trigger(self, key: str) -> bool | None:
        return self._call(key, "rearm_trigger")

    def arm_camera_with_next_file(self, key: str) -> bool | None:
        return self._call(key, "arm_with_next_file")

    def seal_camera_file(self, key: str) -> bool | None:
        return self._call(key, "seal_dcimg")

    def camera_trigger_gate(self, key: str) -> tuple[int, int] | None:
        return self._call(key, "trigger_gate")

    def dcimg_frames(self, key: str) -> int | None:
        return self._call(key, "dcimg_frames")

    def camera_ready(self, key: str) -> bool:
        return self._call(key, "dcimg_ready", default=True)

    def set_mode(self, name: str) -> None:
        """Apply a modes.json recipe. Keys:
          dmd_all_on: true
          dmd_sub_sampling: n            (1-10, "1 in n" pixels off; 1 = off)
          camera_presets: {key: preset}  (a preset key, or "full")
          camera_rate_hz: {key: hz}      (0 = Max; exposure follows)
          camera_exposure_us: {key: us}  (legacy: read as rate 1e6/us)
          camera_binning: {key: n}       (1/2/4)
          camera_trigger: {key: bool}    (True = External edge)

        DMD keys, presets and binning take effect at the next DMD Display /
        session start; the rate applies at once; camera_trigger restarts live
        view itself (refused while recording). Unknown modules and presets
        are skipped."""
        from acqApp.devices.voltage_cam.presets import resolve_preset_key

        recipe = self._modes.get(name, {})
        dmd = self._module("dmd")
        if recipe.get("dmd_all_on") and dmd is not None:
            dmd.set_all_on()
        sub_sampling = recipe.get("dmd_sub_sampling")
        if (sub_sampling is not None and dmd is not None
                and hasattr(dmd, "set_sub_sampling")):
            dmd.set_sub_sampling(int(sub_sampling))
        for key, preset in recipe.get("camera_presets", {}).items():
            self.set_camera_preset(key, resolve_preset_key(preset))
        rates = {k: 1e6 / us for k, us in
                 recipe.get("camera_exposure_us", {}).items() if us > 0}
        rates.update(recipe.get("camera_rate_hz", {}))
        for key, hz in rates.items():
            self._call(key, "set_rate", hz)
        for key, n in recipe.get("camera_binning", {}).items():
            self.set_camera_binning(key, n)
        for key, on in recipe.get("camera_trigger", {}).items():
            self.set_camera_trigger(key, on)
        self.status(f"Mode: {name}")

    def _save_mode_as(self) -> None:
        """Capture the DMD's and voltage camera's live settings as a new
        modes.json entry."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        from acqApp.devices.voltage_cam.presets import (TRIGGER_MODES,
                                                        preset_alias)

        name, ok = QInputDialog.getText(self, "Save as preset",
                                        "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        if name == MODE_NONE:
            QMessageBox.warning(self, "Reserved name",
                                f'"{MODE_NONE}" is reserved for "no preset" '
                                f"— choose another name.")
            return

        # Re-read from disk: the file may have been hand-edited since startup.
        modes = config.load_modes()
        if name in modes:
            if QMessageBox.question(
                    self, "Replace preset",
                    f'A preset named "{name}" already exists — replace it '
                    f"with the current settings?") != QMessageBox.StandardButton.Yes:
                return

        recipe: dict = {}
        captured: list[str] = []

        dmd = self._module("dmd")
        if dmd is not None and dmd.panel is not None:
            from acqApp.devices.dmd.control import MODE_ALL_ON
            if dmd.panel.mode == MODE_ALL_ON:
                recipe["dmd_all_on"] = True
                captured.append("DMD all-on")
            n = dmd.panel.settings.sub_sampling
            if n > 1:
                recipe["dmd_sub_sampling"] = n
                captured.append(f"DMD sub-sampling 1 in {n}")

        vcam = self._module("voltage_cam")
        if vcam is not None:
            if hasattr(vcam, "preset_key"):
                key = vcam.preset_key()
                recipe["camera_presets"] = {"voltage_cam": preset_alias(key)}
                captured.append(f"voltage_cam preset {key!r}")
            if vcam.panel is not None:
                hz = vcam.panel.rate_request_hz
                recipe["camera_rate_hz"] = {"voltage_cam": hz}
                captured.append(f"voltage_cam rate {hz:g} Hz" if hz > 0
                                else "voltage_cam rate Max")
            if hasattr(vcam, "binning"):
                n = vcam.binning()
                recipe["camera_binning"] = {"voltage_cam": n}
                captured.append(f"voltage_cam binning {n}x{n}")
            if vcam.panel is not None:
                mode = vcam.panel.get_config().trigger_mode
                recipe["camera_trigger"] = {
                    "voltage_cam": mode == TRIGGER_MODES[1]}
                captured.append(f"voltage_cam trigger {mode!r}")

        if not captured:
            QMessageBox.information(
                self, "Nothing to capture",
                "Neither the DMD (in All ON) nor the voltage camera is "
                "loaded, so there's no current state to save. Load them "
                "(or set the DMD to All ON) and try again, or write the "
                "entry into modes.json by hand.")
            return

        modes[name] = recipe
        config.save_modes(modes)
        self._modes = modes
        if self._mode_combo.findText(name) < 0:
            self._mode_combo.addItem(name)
        self._mode_combo.setCurrentText(name)
        self.status(f'Saved preset "{name}": {", ".join(captured)}')

    def latest_frame(self, key: str):
        """The cached newest frame; never commands the camera."""
        return self._call(key, "last_frame")

    def latest_frame_preset(self, key: str) -> str | None:
        """The preset the cached frame was captured under — not
        `camera_preset`, which can name a switch not yet in effect."""
        return self._call(key, "last_frame_preset")

    @contextmanager
    def _attributed_to(self, key: str):
        """Charge add_dock/register_pg_view calls in this block to `key`."""
        self._building_key = key
        try:
            yield
        finally:
            self._building_key = None

    def register_pg_view(self, view) -> None:
        self._pg_views.append(view)
        if self._building_key is not None:
            self._module_views.setdefault(self._building_key, []).append(view)

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        if self._save_panel is not None:
            self._save_panel.set_expected_rate(mbps, writer_mbps)

    def add_dock(self, title: str, widget: QWidget, area: "Qt.DockWidgetArea",
                 accent: str = "sync") -> QDockWidget:
        dock = self._make_dock(title, widget, area, accent)
        if self._building_key is not None:
            self._module_docks.setdefault(self._building_key, []).append(dock)
        return dock

    def on_worker_error(self, msg: str) -> None:
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "device"
        self.status(f"{name}: {msg}")
        print(f"[worker error] {name}: {msg}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.resize(1600, 900)
        self.setDockNestingEnabled(True)

        self._build_central()
        self._build_settings_dialog()
        self._build_plots_dock()
        for m in self._modules:
            self._build_views_for(m)

        self.resizeDocks([self._plots_dock], [420], Qt.Orientation.Horizontal)

        self._build_status_bar()
        self._build_sidebar()

        self._restore_layout()

    def _build_views_for(self, m) -> None:
        with self._attributed_to(m.key):
            m.build_views()

    def _central_claimant(self):
        """The first module with a `central_title` (asking `central_widget()`
        would build one)."""
        for m in self._modules:
            if m.central_title:
                return m
        return None

    def _build_central(self) -> None:
        # setCentralWidget DELETES the old widget; its pyqtgraph views must
        # leave `_pg_views` too, or the next theme toggle crashes natively.
        if self._central_owner is not None:
            for v in self._module_views.pop(self._central_owner, []):
                if v in self._pg_views:
                    self._pg_views.remove(v)
        owner = self._central_claimant()
        view = None
        if owner is not None:
            with self._attributed_to(owner.key):
                view = owner.central_widget()
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
        """Every panel, hidden — built now because controllers read them."""
        self._settings_dialog = SettingsDialog(self)

        save_cfg = config.load_dataclass(SaveConfig, "saving")
        if not save_cfg.mouse_id:
            # Legacy key: `subject` became mouse_id on 2026-09-14.
            old_subject = config.load_settings("saving").get("subject")
            if isinstance(old_subject, str) and old_subject.strip():
                save_cfg.mouse_id = old_subject.strip()
        self._save_panel = SavePanel(save_cfg)
        self._save_panel.settings_changed.connect(self._save_save_settings)
        self._settings_dialog.add_panel(self._save_panel, "Save", "saving")

        for m in self._modules:
            m.build_panel()
            self._place_panel(m)

    def _place_panel(self, m, index: int | None = None) -> None:
        if m.panel is None:
            return
        if not m.own_window:
            self._settings_dialog.add_panel(m.panel, m.tab_label, m.key,
                                            index=index)
            return
        win = PanelWindow(m.panel, m.tab_label, m.key, parent=self,
                          size=m.own_window_size)
        win.visibility_changed.connect(
            lambda vis, k=m.key: self._on_panel_window(k, vis))
        self._panel_windows[m.key] = win

    def _on_panel_window(self, key: str, visible: bool) -> None:
        act = self._page_actions.get(key)
        if act is not None:
            act.setChecked(visible)

    def _build_plots_dock(self) -> None:
        self._plots_tabs = QTabWidget()
        self._plots_tabs.setMovable(True)
        for m in self._modules:
            self._add_plot_tab(m)
        self._plots_dock = self._make_dock("Signals", self._plots_tabs,
                                           Qt.DockWidgetArea.RightDockWidgetArea)

    def _add_plot_tab(self, m, index: int | None = None) -> None:
        pw = m.build_plot()
        if pw is None:
            return
        idx = (self._plots_tabs.addTab(pw, m.plot_label) if index is None
              else self._plots_tabs.insertTab(index, pw, m.plot_label))
        self._plots_tabs.tabBar().setTabTextColor(idx, QColor(style.HEX[m.key]))
        self._module_plots[m.key] = pw
        with self._attributed_to(m.key):
            self.register_pg_view(pw)

    def _build_status_bar(self) -> None:
        self._btn_emulate = QPushButton("Emulate")
        self._btn_emulate.setCheckable(True)
        self._btn_emulate.setChecked(self._emulate)
        self._btn_emulate.setStyleSheet(style.toggle_btn("wheel"))
        self._btn_emulate.setToolTip("Use simulated signals instead of hardware")
        self._btn_emulate.toggled.connect(self._on_emulate_toggled)

        self._btn_run = QPushButton("Live view")
        self._btn_run.setCheckable(True)
        self._btn_run.setStyleSheet(style.toggle_btn("sync"))
        self._btn_run.setToolTip("Show live signals from all devices (not saved)")
        self._btn_run.toggled.connect(self._on_run_toggled)

        self._btn_rec = QPushButton("● Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setStyleSheet(style.record_btn("puffer"))
        self._btn_rec.setToolTip("Live view and save every stream to disk")
        self._btn_rec.toggled.connect(self._on_record_toggled)

        # Permanent labels, not status(): any status() call would wipe them.
        self._lbl_rec = QLabel("")
        self._lbl_rec.setStyleSheet("color:#9aa0a6;")
        self._lbl_time = QLabel("")
        self._lbl_time.setStyleSheet("color:#9aa0a6;")

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addPermanentWidget(self._lbl_time)
        sb.addPermanentWidget(self._lbl_rec)
        sb.addPermanentWidget(self._btn_emulate)
        sb.addPermanentWidget(self._btn_run)
        sb.addPermanentWidget(self._btn_rec)
        sb.showMessage("Ready")

    def _build_sidebar(self) -> None:
        self._sidebar = QToolBar("Sidebar")
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setMovable(False)
        self._sidebar.setOrientation(Qt.Orientation.Vertical)
        self._sidebar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._sidebar.setStyleSheet(
            "QToolButton { text-align:left; padding:4px 10px; }")
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self._sidebar)

        # One item per settings page, above the separator.
        self._page_actions: dict[str, QAction] = {}
        self._sidebar_sep = self._sidebar.addSeparator()
        self._rebuild_page_actions()
        # ✕ and Esc both reach `finished`; un-check, or the next click does nothing.
        self._settings_dialog.finished.connect(
            lambda _result: self._check_page(None))
        self._settings_dialog.tabs.currentChanged.connect(
            self._on_settings_tab_changed)

        self._modes = config.load_modes()
        self._sidebar.addWidget(QLabel("  Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems((MODE_NONE, *self._modes))
        self._mode_combo.setToolTip(
            "Apply a named preset across modules (DMD illumination, camera "
            "capture area, ...), defined in modes.json. \"None\" leaves "
            "everything as set.")
        self._mode_combo.currentTextChanged.connect(self.set_mode)
        self._sidebar.addWidget(self._mode_combo)

        self._btn_save_mode = QPushButton("💾 Save as preset…")
        self._btn_save_mode.setToolTip(
            "Capture the DMD's and voltage camera's CURRENT settings as a "
            "new (or replacement) entry in modes.json.")
        self._btn_save_mode.clicked.connect(self._save_mode_as)
        self._sidebar.addWidget(self._btn_save_mode)

        self._theme_action = QAction(self._swatch(None), "☾ Theme", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(config.get_theme() == "dark")
        self._theme_action.setToolTip("Toggle dark / light theme")
        self._theme_action.toggled.connect(self._on_theme_toggled)
        self._sidebar.addAction(self._theme_action)

        self._modules_action = QAction(self._swatch(None), "🧩 Modules", self)
        self._modules_action.setToolTip(_MODULES_TIP)
        self._modules_action.triggered.connect(self._open_modules_dialog)
        self._sidebar.addAction(self._modules_action)

        # Checkable only as an "open" indicator.
        self._devices_action = QAction(self._swatch(None), "🔌 Devices", self)
        self._devices_action.setCheckable(True)
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
        """An accent chip; transparent for None, because a QToolButton without
        an icon ignores `text-align:left`."""
        pm = QPixmap(12, 12)
        pm.fill(QColor(style.HEX[key]) if key else Qt.GlobalColor.transparent)
        return QIcon(pm)

    def _rebuild_page_actions(self) -> None:
        """Rebuilt wholesale: a stale item would point at a deleted panel."""
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
            act.setToolTip(f"{label} — its own window" if key in self._panel_windows
                           else f"{label} settings")
            act.triggered.connect(
                lambda _checked=False, p=panel, k=key: self._show_page(k, p))
            self._sidebar.insertAction(self._sidebar_sep, act)
            self._page_actions[key] = act
            self._page_panels[key] = panel
        self._stretch_sidebar()
        # New actions start unchecked; re-light whatever is showing.
        if self._settings_dialog.isVisible():
            self._on_settings_tab_changed(0)
        for key, win in self._panel_windows.items():
            self._on_panel_window(key, win.isVisible())

    def _stretch_sidebar(self) -> None:
        """One minimum width for all buttons: QToolBarLayout centres each at
        its own size whatever the size policy says."""
        btns = [self._sidebar.widgetForAction(a) for a in self._sidebar.actions()]
        btns = [b for b in btns if b is not None]
        if not btns:
            return
        widest = max(b.sizeHint().width() for b in btns)
        for b in btns:
            b.setMinimumWidth(widest)

    def _on_settings_tab_changed(self, _index: int) -> None:
        if not self._settings_dialog.isVisible():
            return                       # a page removed, not chosen
        panel = self._settings_dialog.current_panel()
        self._check_page(next((k for k, p in self._page_panels.items()
                               if p is panel), None))

    def _check_page(self, key: str | None) -> None:
        # Own-window modules are lit by their own window instead.
        for k, act in self._page_actions.items():
            if k not in self._panel_windows:
                act.setChecked(k == key)

    def _show_page(self, key: str, panel) -> None:
        """Open the settings window on this page, or shut it if already there."""
        own = self._panel_windows.get(key)
        if own is not None:
            self._toggle_own_window(own)
            return
        showing = (self._settings_dialog.isVisible()
                   and self._settings_dialog.current_panel() is panel)
        if showing:
            self._settings_dialog.save_geometry()
            self._settings_dialog.hide()
            self._check_page(None)
            return
        self._settings_dialog.show_panel(panel)
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()
        self._check_page(key)

    @staticmethod
    def _toggle_own_window(win: PanelWindow) -> None:
        if win.isVisible() and win.isActiveWindow():
            win.close()
            return
        win.show()
        win.raise_()
        win.activateWindow()

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
        # setConfigOption only reaches new views.
        bg = style.plot_colors(theme)[0]
        for v in self._pg_views:
            try:
                v.setBackground(bg)
            except Exception:
                pass

    # ── Device connection monitor ────────────────────────────────────────────────

    def _probe_kwargs(self) -> dict:
        return {k: v for m in self._modules for k, v in m.probe_kwargs().items()}

    def _show_devices(self) -> None:
        if self._devices_dialog is None:
            self._devices_dialog = ConnectionMonitor(
                [m.key for m in self._modules], self._probe_kwargs, parent=self)
            self._devices_dialog.visibility_changed.connect(
                self._devices_action.setChecked)
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

    def _start_session(self) -> None:
        # A plain Live/Record press resets the camera to Internal, undoing a
        # previous routine's External edge. Set on the panel directly:
        # set_camera_trigger would re-enter here through set_live.
        if not self._routine_arming_trigger:
            self._call("voltage_cam", "set_trigger_mode_manual")

        # Build first, start after: the clock must be at t=0 before any sample.
        for m in self._modules:
            m.build_session(self._emulate)

        self._sync.start_all()
        for m in self._modules:
            m.start()

        self._session_on = True
        self._disp_timer.start()
        self._btn_run.setText("Stop")
        self._btn_emulate.setEnabled(False)

    def _safe_stop(self, m) -> None:
        """Guarded: one raise would strand every later module running (and,
        via closeEvent, skip the DCAM close)."""
        try:
            m.stop()
        except Exception as e:
            self.status(f"{m.key}: stop failed ({type(e).__name__}: {e})")
            print(f"[main] {m.key}.stop() raised: {type(e).__name__}: {e}")

    def _stop_session(self) -> None:
        if self._btn_rec.isChecked():
            self._btn_rec.setChecked(False)

        self._disp_timer.stop()
        for m in self._modules:
            self._safe_stop(m)
        self._sync.stop_all()

        self._session_on = False
        self._btn_run.setText("Live view")
        self._btn_emulate.setEnabled(True)
        self._lbl_time.setText("")
        self.status("Stopped")

    def _on_emulate_toggled(self, on: bool) -> None:
        self._emulate = on
        self._build_controllers()
        self._apply_title()
        self.status("Emulate ON — simulated signals" if on
                    else "Emulate OFF — real hardware")

    # ── Loading and unloading instruments in place ───────────────────────────

    def set_modules(self, keys) -> tuple[list[str], list[str]]:
        """Refused while recording (the file names its modules once). A module
        loaded mid-session starts against the running clock, which is safe:
        workers stamp in perf_counter and only the Recorder converts."""
        if self._recorder is not None:
            raise RuntimeError("stop the recording first")
        for m in self._modules:
            why = m.busy_reason()
            if why:
                raise RuntimeError(why)

        keys = set(keys) | config.ALWAYS_ON
        want = [k for k in config.MODULES if k in keys and k in adapters.ADAPTERS]
        have = [m.key for m in self._modules]
        removed = [k for k in have if k not in want]
        added = [k for k in want if k not in have]
        if not removed and not added:
            return [], []

        for key in removed:
            self._unload_module(key)
        for key in added:
            self._load_module(key)

        # Order matters: closed_loop is last, after every signal source.
        self._modules.sort(key=lambda m: _rank(m.key))

        self._enabled = {m.key for m in self._modules}
        config.save_enabled_modules(list(self._enabled))
        # The monitor snapshots module keys at build.
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
        m = adapters.ADAPTERS[key](self)
        self._modules.append(m)             # the caller sorts

        m.build_panel()
        self._place_panel(m, index=self._settings_tab_index(key))
        self._add_plot_tab(m, index=self._plot_tab_index(key))
        self._build_views_for(m)
        m.build_controller(self._emulate)

        if self._session_on:
            m.build_session(self._emulate)
            m.start()

    def _unload_module(self, key: str) -> None:
        """Stop one adapter and take back everything it put on the window."""
        m = self._module(key)
        if m is None:
            return
        self._safe_stop(m)
        m.close_controller()

        # setParent(None) before deleteLater: removal only leaves the layout,
        # and a still-parented dock would come back on restoreState().
        win = self._panel_windows.pop(key, None)
        if win is not None:
            # A QScrollArea owns its widget; release, or the panel dies too.
            win.release()
            win.setParent(None)
            win.deleteLater()
        elif m.panel is not None:
            self._settings_dialog.remove_panel(m.panel)
        if m.panel is not None:
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
        """After the last page preceding `key` in MODULES, read off the live
        (draggable) tab positions."""
        dlg = self._settings_dialog
        last = dlg.panel_index(self._save_panel) if self._save_panel else -1
        for m in self._modules:
            if (m.key != key and m.panel is not None
                    and _rank(m.key) < _rank(key)):
                last = max(last, dlg.panel_index(m.panel))
        return last + 1

    def _plot_tab_index(self, key: str) -> int:
        return sum(1 for k in self._module_plots
                   if k != key and _rank(k) < _rank(key))

    def _refresh_central(self) -> None:
        """Rebuild the centre pane only if its owner changed."""
        claimant = self._central_claimant()
        want = claimant.key if claimant is not None else None
        if want != self._central_owner:
            self._build_central()

    def _open_modules_dialog(self) -> None:
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
        """(Re)create the output controllers for the current emulate mode."""
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
            if not self._session_on:
                self._btn_run.setChecked(True)
            if not self._session_on:              # start failed
                self._btn_rec.setChecked(False)
                return
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self._save_panel is None:
            return
        err = self._save_panel.writable_error()
        if err is not None:
            self.status(f"Cannot record — {err}")
            self._btn_rec.setChecked(False)
            return

        now = datetime.now()
        sp = self._save_panel
        sc = sp.settings
        ctx = self._routine_save_ctx
        # unique=True: take the next free name rather than refuse. A routine's
        # (FOV, trial) uses the fixed folder scheme, not the template.
        if ctx is not None:
            resolve = (sp.resolve_routine_dir if sc.split
                       else sp.resolve_routine)
            path = resolve(ctx[0], ctx[1], now, unique=True)
        else:
            resolve = sp.resolve_dir if sc.split else sp.resolve
            path = resolve(now, unique=True)
        metadata = {
            "created":  now.strftime("%Y%m%d_%H%M%S"),
            "emulated": self._emulate,
            "modules":  ",".join(sorted(self._enabled)),
            "mouse_id": sc.mouse_id,
            "project":  sc.project,
        }
        for m in self._modules:
            metadata.update(m.metadata())

        writer = SplitWriter() if sc.split else HDF5Writer()
        rec = Recorder(
            self._clock, writer,
            RingBuffer(RING_FRAMES, maxbytes=RING_BYTES, sizeof=_sample_nbytes))
        try:
            rec.start(path, metadata)
        except OSError as e:
            self.status(f"Cannot record → {path}: {e}")
            self._btn_rec.setChecked(False)
            return
        if ctx is not None and ctx[2] is not None:
            # Raw coordinates: "FOVcustom" in the name alone loses them.
            write_routine_fov_sidecar(path, *ctx[2])
        self._recorder = rec
        self._rec_path = path
        sp.set_recording_active(True)
        self._rec_t0 = self._sync.elapsed()
        self._rec_size_t0 = 0.0
        self._rec_size_txt = ""
        self._rec_warn = None

        for m in self._modules:
            m.attach_sink(self._recorder)

        self._btn_rec.setText("■ Stop rec")
        self._modules_action.setEnabled(False)
        self._modules_action.setToolTip(
            "Not while recording — the file names its modules once, at the start")
        self.status(f"Recording → {path}")

    def _stop_recording(self) -> None:
        self._modules_action.setEnabled(True)
        self._modules_action.setToolTip(_MODULES_TIP)
        for m in self._modules:
            m.detach_sink()
        rec = self._recorder
        self._recorder = None
        if self._save_panel is not None:
            self._save_panel.set_recording_active(False)
        if rec is not None:
            # A callback: counts are final only between drain and close.
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

    def roll_recording(self) -> bool:
        """Close and immediately reopen, for a routine splitting files. No
        event-loop turn between, so the button never shows "stopped"."""
        self._stop_recording()
        self._start_recording()
        return self._recorder is not None

    def set_routine_save_context(self, fov: str | None, trial: int | None,
                                 coords: tuple[float | None, float | None,
                                              float | None] | None = None
                                 ) -> None:
        self._routine_save_ctx = (None if fov is None or trial is None
                                  else (fov, trial, coords))

    def routine_arming_trigger(self, on: bool) -> None:
        self._routine_arming_trigger = bool(on)

    def recording_path(self) -> Path | None:
        return self._rec_path if self._recorder is not None else None

    def routine_folder(self) -> Path:
        return self._save_panel.settings.routine_base(datetime.now())

    # ── Sync callbacks ──────────────────────────────────────────────────────────

    def _on_tick(self, elapsed: float) -> None:
        self._lbl_time.setText(f"t = {elapsed:.1f} s")
        self._refresh_rec_readout(elapsed)
        if self._save_panel is not None:
            self._save_panel.set_active_fov(self.active_fov_name())

    def _refresh_rec_readout(self, elapsed: float) -> None:
        """Elapsed / size on disk / drops. Size is from the file, not what was
        enqueued: they differ exactly when the ring sheds."""
        if self._recorder is None or self._rec_path is None:
            self._lbl_rec.setText("")
            return
        mins, secs = divmod(int(elapsed - self._rec_t0), 60)
        txt = f"● REC  {mins:d}:{secs:02d}"
        now = time.monotonic()
        if now - self._rec_size_t0 >= 1.0:
            self._rec_size_t0 = now
            try:
                mb = self._rec_path.stat().st_size / (1 << 20)
                self._rec_size_txt = (f"   {mb / 1024:.2f} GB" if mb >= 1024
                                      else f"   {mb:.0f} MB")
            except OSError:
                pass
        txt += self._rec_size_txt
        dropped = self._recorder.drop_count + self._recorder.late_count
        warn = bool(dropped)
        if dropped:
            txt += f"   ⚠ {dropped} samples shed"
        if warn != self._rec_warn:
            self._rec_warn = warn
            self._lbl_rec.setStyleSheet("color:#c62828; font-weight:bold;" if warn
                                        else "color:#2e7d32; font-weight:bold;")
        self._lbl_rec.setText(txt)

    def _on_trigger(self, name: str, duration: float) -> None:
        for m in self._modules:
            m.on_trigger(name, duration)

    def _display_tick(self) -> None:
        for m in self._modules:
            m.update_display()

    def _save_save_settings(self, *_args) -> None:
        if self._save_panel is not None:
            config.save_settings("saving", self._save_panel.as_dict())

    # ── Cleanup ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._save_layout()
        # Top-level windows left open would keep the app alive.
        self._settings_dialog.save_geometry()
        self._settings_dialog.close()
        for win in self._panel_windows.values():
            win.save_geometry()
            win.close()
        if self._session_on:
            self._stop_session()
        for m in self._modules:
            m.close_controller()
        _close_camera(self._cam_handle)
        self._cam_handle = None
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true",
                    help="start in Emulate mode (simulated signals, no hardware)")
    ap.parse_args()

    app = QApplication(sys.argv)
    style.apply_theme(app, config.get_theme())

    dlg = ModuleSelectDialog(config.load_enabled_modules())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        # Release the handle anyway, or the next launch double-opens and crashes.
        _await_camera()
        _close_camera(_cam_handle)
        return
    enabled = dlg.selected()
    config.save_enabled_modules(enabled)

    _await_camera()
    win = MainWindow(cam_info=_cam_info, mock=_mock, enabled=set(enabled),
                     cam_handle=_cam_handle)
    win.show()

    from acqApp.devices.mirror.startup import ensure_camera_default
    mirror_result = ensure_camera_default()
    if not mirror_result.ok:
        win.status(f"Mirror check skipped: {mirror_result.error}")
    elif mirror_result.corrected:
        win.status("Mirror wasn't on CAMERA/epi — corrected at launch.")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
