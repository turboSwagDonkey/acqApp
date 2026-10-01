"""Shared scaffolding for the acqApp tests (plain scripts: the rig installs
only `requirements.txt`, which has no pytest).

`isolate_user_state()` is the important part: the REAL MainWindow persists as
a side effect of ordinary use (the Save tab on every edit, the dock layout on
close), so without it a test overwrites the operator's save folder, mouse ID
and panel layout. Every test that builds a window calls it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# Windowless unless QT_QPA_PLATFORM=windows; before the first Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APP_DIR = REPO_ROOT / "acqApp"

from acqApp.console import enable_safe_console       # noqa: E402

# Test output hits the console-encoding trap as the app's does.
enable_safe_console()


# ── isolation ─────────────────────────────────────────────────────────────────

class MemorySettings:
    """In-process QSettings. On Windows `QSettings("acqApp", "acqApp")`
    reaches HKEY_CURRENT_USER whatever `setPath`/`setDefaultFormat` say, so
    the class itself is substituted before `main` imports it."""

    store: dict[str, object] = {}

    def __init__(self, *_args, **_kw) -> None:
        pass

    def value(self, key, default=None):
        return self.store.get(key, default)

    def setValue(self, key, value) -> None:
        self.store[key] = value

    def sync(self) -> None:
        pass


class _BlockedDriver(types.ModuleType):
    """Imports fine; touching anything raises, which every device path
    already treats as "no hardware"."""

    def __getattr__(self, name):
        raise RuntimeError(
            f"{self.__name__}.{name} is blocked by the test harness "
            f"(tests must never touch real hardware)")


def block_real_devices(*names: str) -> None:
    """Stand refusing stubs in front of the vendor drivers. Toggling Emulate
    off rebuilds the real controllers: the suite really did open this rig's
    DMD, and the puffer's DO line may have an animal in front of it."""
    for name in (names or ("ALP4", "nidaqmx", "pylablib", "pypylon")):
        sys.modules[name] = _BlockedDriver(name)
    if not names:
        _block_stage()


def _block_stage() -> None:
    """Stage auto-detect OPENS an MCM301 via its DLL and probes serial for an
    MCM6101; both go through `backend` (imported at call time)."""
    from acqApp.devices.stage import backend

    def refuse(*_a, **_k):
        raise backend.BackendError(
            "stage connect is blocked by the test harness "
            "(tests must never touch real hardware)")
    backend.connect_auto = refuse
    backend.open_backend = refuse


def isolate_user_state() -> Path:
    """Redirect every persistent store the app writes (and block the vendor
    drivers); return the temp dir. Call BEFORE importing `acqApp.main`, which
    binds `QSettings` at import time."""
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_test_"))
    block_real_devices()

    from acqApp import config
    config._CONFIG_PATH = tmp / "acqapp_local.json"

    # Unisolated, a run writes, deletes and rotates the operator's own files.
    from acqApp.routines import templates
    templates.DIR = tmp / "routine_templates"

    from acqApp.devices.dmd import roi_store
    roi_store.SESSION_DIR = tmp / "rois" / "session"
    roi_store.ARCHIVE_DIR = tmp / "rois" / "archive"
    roi_store._rotated = False

    # Missing here once, a picker test rotated the operator's real FOVs.
    from acqApp.devices.stage import fov_store
    fov_store.SESSION_DIR = tmp / "fovs" / "session"
    fov_store.ARCHIVE_DIR = tmp / "fovs" / "archive"
    fov_store._rotated = False

    MemorySettings.store = {}
    import PyQt6.QtCore
    PyQt6.QtCore.QSettings = MemorySettings
    # Modules already imported hold their own reference; both write.
    for name in ("acqApp.main", "acqApp.dialogs"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "QSettings"):
            mod.QSettings = MemorySettings
    return tmp


# ── reporting ─────────────────────────────────────────────────────────────────

class Report:
    """Collects pass/fail lines and returns a process exit code. Not
    assert-based: one run should report every failure, not the first.

    `-q` prints only failures plus the closing summary."""

    QUIET = "-q" in sys.argv or os.environ.get("ACQAPP_QUIET") == "1"

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.n_ok = 0

    def check(self, cond: bool, msg: str) -> bool:
        if cond:
            self.n_ok += 1
            if not self.QUIET:
                print("  ok   " + msg)
        else:
            self.failures.append(msg)
            print("  FAIL " + msg)
        return bool(cond)

    def info(self, msg: str) -> None:
        if not self.QUIET:
            print(f"         {msg}")

    def note(self, msg: str) -> None:
        if not self.QUIET:
            print(f"[{self.name}] {msg}")

    def finish(self) -> int:
        print()
        if self.failures:
            print(f"[{self.name}] {len(self.failures)} FAILURE(S) "
                  f"({self.n_ok} passed):")
            for f in self.failures:
                print(f"   - {f}")
            return 1
        print(f"[{self.name}] PASS ({self.n_ok} checks)")
        return 0


# ── multi-part test files ─────────────────────────────────────────────────────

def run_parts(parts: dict) -> int:
    """Run each part of a test file in its own process, as separate files did:
    parts patch modules and build QApplications, which don't mix in one.
    `--part NAME` runs one part here; `-q` is passed through."""
    import subprocess
    if "--part" in sys.argv:
        return parts[sys.argv[sys.argv.index("--part") + 1]]()
    rc = 0
    for name in parts:
        sys.stdout.flush()
        args = [sys.executable, sys.argv[0], "--part", name]
        args += [a for a in sys.argv[1:] if a == "-q"]
        rc |= subprocess.run(args).returncode
    return rc


# ── Qt helpers ────────────────────────────────────────────────────────────────

def qt_app():
    """The QApplication, created once per process, with the app's own theme."""
    from PyQt6.QtWidgets import QApplication
    from acqApp import config, style
    app = QApplication.instance() or QApplication(sys.argv)
    style.apply_theme(app, config.get_theme())
    return app


def pump(app, seconds: float) -> None:
    """Turn the Qt event loop for `seconds`: the workers are real QThreads."""
    import time
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def make_window(enabled, **kw):
    """A MainWindow built the way every GUI test builds one: mocked, no real
    camera handle, an explicit module set."""
    import acqApp.main as M
    return M.MainWindow(cam_info=None, mock=True, enabled=enabled,
                        cam_handle=None, **kw)


def npoints(item) -> int:
    """How many points a PlotCurveItem is drawing (None before any setData)."""
    xs = item.getData()[0]
    return 0 if xs is None else len(xs)
