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

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, QSettings, QSignalBlocker
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDockWidget, QFrame, QLabel,
    QHBoxLayout, QMainWindow, QPushButton, QScrollArea, QSplitter, QStatusBar,
    QToolButton, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from acqApp import adapters, style
from acqApp.dialogs import ModuleSelectDialog
from acqApp.saving import SaveConfig, SavePanel, write_routine_fov_sidecar
from acqApp.acq.sync import DEFAULT_TICK_MS, SyncController
from acqApp.acq.clock import SessionClock
from acqApp.acq.recorder import Recorder
from acqApp.acq.ring_buffer import RingBuffer
from acqApp.acq.writer import SessionWriter
from acqApp.sidebar import Fold, MiniText
from acqApp.widgets import align_grids, compact

pg.setConfigOptions(imageAxisOrder="row-major")

RING_FRAMES  = 512          # ring item cap (scalar streams)
# Payload cap. 512 MB shed 14-54 frames per 30 s run at 106 Hz on a writer
# stall; 2 GB shed none; 4 GB buys nothing more.
RING_BYTES   = 2048 << 20


def _folder_bytes(folder: Path) -> int:
    """Bytes in the session folder's files: stat() on the folder itself is its
    directory entry (0 on NTFS), not what it holds."""
    with os.scandir(folder) as it:
        return sum(e.stat().st_size for e in it if e.is_file())


def _sample_nbytes(item) -> int:
    """Payload bytes of a ring item (stream, ts, data); 0 for scalars."""
    return getattr(item[2], "nbytes", 0)


# "None" leaves everything as set; other modes come from modes.json.
MODE_NONE = "None"

_MODULES_TIP = ("Load or unload instruments without restarting the app, and "
                "see whether each one's hardware is detected")

# Saved dock layouts of an older version are ignored, not half-applied.
_LAYOUT_VERSION = 4
# An own-dock module's default place, (column, row). General tops column
# _GENERAL_COL; the others lie to its right.
_GENERAL_COL = -1
_SLOTS = {"stage": (_GENERAL_COL, 1),
          "voltage_cam": (0, 0), "routines": (0, 1),
          "dmd": (1, 0), "pupil_cam": (1, 1)}


def _rank(key: str) -> int:
    """Position in config.MODULES; unknown keys sort last."""
    keys = list(config.MODULES)
    return keys.index(key) if key in config.MODULES else len(keys)


def _section_rank(key: str) -> int:
    return -1 if key == "saving" else _rank(key)


def _slot(key: str) -> tuple[int, int]:
    return _SLOTS.get(key, (1, 99))


def _dress(panel: QWidget, key: str) -> None:
    """What every settings panel gets: its accent and aligned grids. Its
    boxes don't fold: the card does, so one click shows them all."""
    panel.setStyleSheet(style.accent_panel(key))
    align_grids(panel)


class _VScroll(QScrollArea):
    """Scrolls only up and down: never narrower than what it holds, so a
    column can't be dragged so thin that its sections need a sideways bar."""

    def __init__(self, w: QWidget) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(w)
        w.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()       # a section opened or shut
        return super().eventFilter(obj, event)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        hint.setWidth(self.widget().minimumSizeHint().width() + bar)
        return hint


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
        # Set by the routine before it opens/rolls a file; None = Save tab naming.
        self._routine_save_ctx: tuple[str, int, tuple | None] | None = None
        self._routine_arming_trigger = False
        self._save_panel: SavePanel | None = None
        self._folds: dict[str, Fold] = {}           # each card, its panel under it
        self._docks: dict[str, QDockWidget] = {}    # the own_dock modules'
        self._pg_views: list = []      # recoloured on theme change
        # What each module added to the window, to take back on unload.
        # register_pg_view doesn't say who's calling, so the window tracks
        # whose build is running.
        self._building_key: str | None = None
        self._module_views: dict[str, list] = {}
        self._status_widgets: dict[str, QWidget] = {}   # beside Record

        self._modules = adapters.build_adapters(self, self._enabled)
        self._build_ui()
        # Controllers are configured from their panels, so after the UI.
        self._build_controllers()
        self._apply_title()

        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(33)   # ~30 Hz
        self._disp_timer.timeout.connect(self._display_tick)
        self._display_failed: set[str] = set()     # reported once per session

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
        return bool(self._first("records_dcimg"))

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

    def on_worker_error(self, msg: str) -> None:
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "device"
        self.status(f"{name}: {msg}")
        print(f"[worker error] {name}: {msg}")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """One window, all docks (no central widget, so every pane drags):
        General over stage | voltage cam over routines | DMD over pupil cam."""
        self.resize(1600, 900)
        # Nested, never tabbed: a tab would hide one region behind another
        # (General behind the stage, say).
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks
                            | QMainWindow.DockOption.AllowNestedDocks)

        self._build_general()
        # Every panel before any view: controllers read them.
        for m in self._modules:
            m.build_panel()
        for m in self._modules:
            self._place_module(m)

        self._build_status_bar()
        self._arrange_docks()
        self._restore_layout()

    def _build_general(self) -> None:
        """The left dock: Save and every module without a dock of its own,
        each folded under its card, over the session-wide controls."""
        save_cfg = config.load_dataclass(SaveConfig, "saving")
        if not save_cfg.mouse_id:
            # Legacy key: `subject` became mouse_id on 2026-09-14.
            old_subject = config.load_settings("saving").get("subject")
            if isinstance(old_subject, str) and old_subject.strip():
                save_cfg.mouse_id = old_subject.strip()
        self._save_panel = SavePanel(save_cfg)
        self._save_panel.settings_changed.connect(self._save_save_settings)
        _dress(self._save_panel, "saving")

        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(4, 4, 4, 4)
        self._sections = QVBoxLayout()
        self._sections.setSpacing(6)
        col.addLayout(self._sections)
        col.addStretch(1)
        self._add_section("saving", "Save", self._mini("saving"), self._save_panel)

        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(_VScroll(inner), 1)
        lay.addWidget(self._build_controls())
        self._general_dock = self._make_dock(
            "General", host, Qt.DockWidgetArea.LeftDockWidgetArea)

    def _build_controls(self) -> QWidget:
        """One line: Mode, then small buttons (icons, bar Modules)."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)

        self._modes = config.load_modes()
        self._mode_combo = compact(QComboBox())
        self._mode_combo.addItems((MODE_NONE, *self._modes))
        self._mode_combo.setToolTip(
            "Apply a named preset across modules (DMD illumination, camera "
            "capture area, ...), defined in modes.json. \"None\" leaves "
            "everything as set.")
        self._mode_combo.currentTextChanged.connect(self.set_mode)
        row.addWidget(QLabel("Mode:"))
        row.addWidget(self._mode_combo)

        self._save_mode_action = QAction("💾", self)
        self._save_mode_action.setToolTip(
            "Save as preset: capture the DMD's and voltage camera's CURRENT "
            "settings as a new (or replacement) entry in modes.json.")
        self._save_mode_action.triggered.connect(self._save_mode_as)

        self._theme_action = QAction("☾", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(config.get_theme() == "dark")
        self._theme_action.setToolTip("Dark / light theme")
        self._theme_action.toggled.connect(self._on_theme_toggled)

        self._modules_action = QAction("🧩 Modules", self)
        self._modules_action.setToolTip(_MODULES_TIP)
        self._modules_action.triggered.connect(self._open_modules_dialog)

        self._reset_action = QAction("⟲", self)
        self._reset_action.setToolTip(
            "Reset layout: put every pane back where it started (drag a pane "
            "by its title bar to move it; drag the gaps between panes to "
            "resize)")
        self._reset_action.triggered.connect(self._arrange_docks)

        row.addStretch()
        for act in (self._save_mode_action, self._theme_action,
                    self._modules_action, self._reset_action):
            b = QToolButton()
            b.setDefaultAction(act)
            row.addWidget(b)
        return box

    def _mini(self, key: str) -> QWidget | None:
        """A fresh live view for `key`'s card."""
        if key == "saving":
            return MiniText(self._save_mini_text)
        m = self._module(key)
        return m.mini_view() if m is not None else None

    def _place_module(self, m) -> None:
        """Its settings folded under its card: in its own dock over its
        view, else as a section of General."""
        if m.panel is not None:
            _dress(m.panel, m.key)
        mini = m.mini_view()
        if not m.own_dock:
            if m.panel is not None or mini is not None:
                self._add_section(m.key, m.tab_label, mini, m.panel)
            return
        with self._attributed_to(m.key):
            view = m.view_widget()
        body = _VScroll(m.panel) if m.panel is not None else None
        fold = Fold(m.key, "Settings", mini, body, default_open=view is None)
        self._folds[m.key] = fold
        self._docks[m.key] = self._make_dock(
            m.tab_label, self._dock_body(fold, view),
            Qt.DockWidgetArea.LeftDockWidgetArea, accent=m.key)

    def _add_section(self, key: str, label: str, mini: QWidget | None,
                     panel: QWidget | None) -> None:
        fold = Fold(key, label, mini, panel)
        at = sum(1 for k in self._folds
                 if k not in self._docks and _section_rank(k) < _section_rank(key))
        self._sections.insertWidget(at, fold)
        self._folds[key] = fold

    @staticmethod
    def _dock_body(fold: Fold, view: QWidget | None) -> QWidget:
        """The view over the fold (its Settings at the bottom), the line
        between them draggable; with no view, the fold fills the dock."""
        if view is None:
            host = QWidget()
            lay = QVBoxLayout(host)
            lay.setContentsMargins(4, 4, 4, 4)
            lay.addWidget(fold, 1)
            lay.addStretch(0)
            return host
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(view)
        split.addWidget(fold)
        split.setStretchFactor(0, 1)

        def opened(on: bool) -> None:
            total = sum(split.sizes())
            if total:       # shut, the card alone: no band left over it
                want = (min(fold.sizeHint().height(), total * 3 // 5) if on
                        else fold.maximumHeight())
                split.setSizes([total - want, want])
        fold.toggled.connect(opened)
        return split

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
        for m in self._modules:
            self._add_status_widget(m)
        self._size_status_boxes()

    def _add_status_widget(self, m) -> None:
        """Place the control a module offers (the DMD's light switch) beside Record."""
        w = m.status_widget()
        if w is not None:
            self.statusBar().addPermanentWidget(w)
            w.show()
            self._status_widgets[m.key] = w

    def _size_status_boxes(self) -> None:
        """Record and each module switch get one fixed box, wide enough for
        every label any of them shows: no resizing between states, no clipping."""
        rec, keep = self._btn_rec, self._btn_rec.text()
        w = h = 0
        for text in ("● Record", "■ Stop rec"):
            rec.setText(text)
            hint = rec.sizeHint()
            w, h = max(w, hint.width()), max(h, hint.height())
        rec.setText(keep)
        # Buttons only: a switch (the mirror's) keeps its own shape.
        buttons = [b for b in self._status_widgets.values()
                   if isinstance(b, QPushButton)]
        for b in buttons:
            w, h = max(w, b.minimumWidth(), b.sizeHint().width()), max(h, b.sizeHint().height())
        for b in (rec, *buttons):
            b.setFixedSize(w, h)

    def _remove_status_widget(self, key: str) -> None:
        w = self._status_widgets.pop(key, None)
        if w is not None:
            self.statusBar().removeWidget(w)
            w.setParent(None)
            w.deleteLater()

    def _make_dock(self, title: str, widget: QWidget,
                   area: "Qt.DockWidgetArea", accent: str = "sync") -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title}")
        zone = QFrame()                 # the accent frame under the title band
        zone.setObjectName("zone")
        zone.setStyleSheet(style.zone(accent))
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.addWidget(widget)
        dock.setWidget(zone)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        # Rearranged within the window, never torn out of it or closed.
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setStyleSheet(style.dock_accent(accent))
        self.addDockWidget(area, dock)
        return dock

    def _save_mini_text(self) -> tuple[str, str]:
        path = self.recording_path()
        if path is not None:
            return "● REC", path.name
        s = self._save_panel.settings
        return "", f"{s.mouse_id or 'no mouse ID'} → {s.resolved_folder().name}"

    # ── Dock layout ─────────────────────────────────────────────────────────────

    def _arrange_docks(self) -> None:
        """The default layout, also Reset layout: a column per `_SLOTS`
        column, its docks stacked by row, General on top of the first."""
        general = self._general_dock
        for d in (general, *self._docks.values()):
            d.setFloating(False)
            self.removeDockWidget(d)
        cols: dict[int, list] = {_GENERAL_COL: [general]}
        for key in sorted(self._docks, key=lambda k: (_slot(k), _rank(k))):
            cols.setdefault(_slot(key)[0], []).append(self._docks[key])
        stacks = [cols[c] for c in sorted(cols)]
        # Columns first, then each split into rows: a row split first would
        # put the next column beside one row only.
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, general)
        for prev, stack in zip(stacks, stacks[1:]):
            self.splitDockWidget(prev[0], stack[0], Qt.Orientation.Horizontal)
        for stack in stacks:
            for upper, lower in zip(stack, stack[1:]):
                self.splitDockWidget(upper, lower, Qt.Orientation.Vertical)
        for d in (general, *self._docks.values()):
            d.show()
        widths = [420, 700, 480][:len(stacks)]
        self.resizeDocks([s[0] for s in stacks], widths,
                         Qt.Orientation.Horizontal)
        for stack in stacks:
            if len(stack) == 2:
                # General's cards are one line each: its column's dock gets more.
                self.resizeDocks(stack, [2, 3] if stack[0] is general else [3, 2],
                                 Qt.Orientation.Vertical)

    def _dock_beside(self, key: str) -> None:
        """A dock loaded mid-session: under a docked neighbour in its column
        (General heads its own), else right of General."""
        dock = self._docks.get(key)
        if dock is None:
            return
        col = _slot(key)[0]
        sib = next((d for k, d in self._docks.items()
                    if k != key and _slot(k)[0] == col and not d.isFloating()),
                   self._general_dock if col == _GENERAL_COL else None)
        if sib is not None:
            self.splitDockWidget(sib, dock, Qt.Orientation.Vertical)
        else:
            self.splitDockWidget(self._general_dock, dock,
                                 Qt.Orientation.Horizontal)

    def _restore_layout(self) -> None:
        state = QSettings("acqApp", "acqApp").value("dockState")
        if state is not None:
            self.restoreState(state, _LAYOUT_VERSION)
        # A layout saved while floating was allowed would restore a dock outside.
        for d in (self._general_dock, *self._docks.values()):
            d.setFloating(False)

    def _save_layout(self) -> None:
        QSettings("acqApp", "acqApp").setValue(
            "dockState", self.saveState(_LAYOUT_VERSION))

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
        for key, fold in self._folds.items():   # the cards' muted text is per theme
            fold.card.set_mini(self._mini(key))

    # ── Device connection monitor ────────────────────────────────────────────────

    def _probe_kwargs(self) -> dict:
        return {k: v for m in self._modules for k, v in m.probe_kwargs().items()}

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
        where = "clock"
        try:
            for m in self._modules:
                where = m.key
                m.build_session(self._emulate)
            where = "clock"
            self._sync.start_all()
            for m in self._modules:
                where = m.key
                m.start()
        except Exception as e:      # noqa: BLE001 — a slot: an escape aborts the app
            self._abort_start(where, e)
            return

        self._display_failed.clear()
        self._session_on = True
        self._disp_timer.start()
        self._btn_run.setText("Stop")
        self._btn_emulate.setEnabled(False)

    def _abort_start(self, where: str, e: Exception) -> None:
        """Undo a half-started session: every module stopped, the clock off,
        the button back up (signals blocked, so the error stays on screen)."""
        print(f"[main] Live view failed at {where}: {type(e).__name__}: {e}")
        for m in self._modules:
            self._safe_stop(m)
        self._sync.stop_all()
        with QSignalBlocker(self._btn_run):
            self._btn_run.setChecked(False)
        self.status(f"Live view failed — {where}: {type(e).__name__}: {e}")

    def _guarded(self, m, method: str) -> None:
        """Guarded: one raise would strand every later module running or open
        (and, via closeEvent, skip the DCAM close)."""
        try:
            getattr(m, method)()
        except Exception as e:
            self.status(f"{m.key}: {method} failed ({type(e).__name__}: {e})")
            print(f"[main] {m.key}.{method}() raised: {type(e).__name__}: {e}")

    def _safe_stop(self, m) -> None:
        self._guarded(m, "stop")

    def _safe_close(self, m) -> None:
        self._guarded(m, "close_controller")

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

        self._modules.sort(key=lambda m: _rank(m.key))

        self._enabled = {m.key for m in self._modules}
        config.save_enabled_modules(list(self._enabled))
        for m in self._modules:
            m.on_modules_changed()
        return added, removed

    def _load_module(self, key: str) -> None:
        m = adapters.ADAPTERS[key](self)
        self._modules.append(m)             # the caller sorts

        m.build_panel()
        self._place_module(m)
        self._dock_beside(key)
        m.build_controller(self._emulate)
        self._add_status_widget(m)
        self._size_status_boxes()

        if self._session_on:
            try:
                m.build_session(self._emulate)
                m.start()
            except Exception as e:  # noqa: BLE001 — loaded, but not running
                self._safe_stop(m)
                self.status(f"{key}: start failed ({type(e).__name__}: {e})")
                print(f"[main] {key} start raised: {type(e).__name__}: {e}")

    def _unload_module(self, key: str) -> None:
        """Stop one adapter and take back everything it put on the window."""
        m = self._module(key)
        if m is None:
            return
        self._safe_stop(m)
        self._safe_close(m)

        # setParent(None) before deleteLater: removal only leaves the layout,
        # and a still-parented dock would come back on restoreState().
        # The panel and the card's view go with their fold.
        fold = self._folds.pop(key, None)
        dock = self._docks.pop(key, None)
        if dock is not None:
            self.removeDockWidget(dock)
            dock.setParent(None)
            dock.deleteLater()
        elif fold is not None:
            self._sections.removeWidget(fold)
            fold.setParent(None)
            fold.deleteLater()
        for view in self._module_views.pop(key, []):
            if view in self._pg_views:
                self._pg_views.remove(view)
        self._remove_status_widget(key)

        self._modules.remove(m)

    def _modules_dialog(self) -> ModuleSelectDialog:
        """The module picker, each row also reporting its hardware."""
        running = " — applied to the running session" if self._session_on else ""
        return ModuleSelectDialog(
            sorted(self._enabled), parent=self, title="Modules and devices",
            prompt=f"Instruments to load{running}:",
            probe_kwargs=self._probe_kwargs)

    def _open_modules_dialog(self) -> None:
        if self._recorder is not None:
            self.status("Stop the recording before changing which modules "
                        "are loaded")
            return
        dlg = self._modules_dialog()
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
            self._safe_close(m)
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
        # (FOV, trial) uses its folder scheme, not mouse_date_time.
        if ctx is not None:
            path = sp.resolve_routine_dir(ctx[0], ctx[1], now, unique=True)
        else:
            path = sp.resolve_dir(now, unique=True)
        metadata = {
            "created":  now.strftime("%Y%m%d_%H%M%S"),
            "emulated": self._emulate,
            "modules":  ",".join(sorted(self._enabled)),
            "mouse_id": sc.mouse_id,
        }
        for m in self._modules:
            metadata.update(m.metadata())

        # A folder per recording; a module may ask for its own image format
        # (the pupil camera's .avi), else TIFF.
        writer = SessionWriter({m.key: m.image_format for m in self._modules
                                if m.image_format})
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
                mb = _folder_bytes(self._rec_path) / (1 << 20)
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
            try:
                m.update_display()
            except Exception as e:  # noqa: BLE001 — a timer slot: an escape aborts the app
                if m.key not in self._display_failed:     # once, not at 30 Hz
                    self._display_failed.add(m.key)
                    self.status(f"{m.key}: display failed ({type(e).__name__}: {e})")
                    print(f"[main] {m.key}.update_display() raised: "
                          f"{type(e).__name__}: {e}")

    def _save_save_settings(self, *_args) -> None:
        if self._save_panel is not None:
            config.save_settings("saving", self._save_panel.as_dict())

    # ── Cleanup ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._save_layout()
        try:
            if self._session_on:
                self._stop_session()
        except Exception as e:      # noqa: BLE001 — an escape aborts the process
            print(f"[main] shutdown raised {type(e).__name__}: {e}")
        for m in self._modules:
            self._safe_close(m)
        # Always: a handle left open crashes the next launch.
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
