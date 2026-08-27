"""Shared scaffolding for the acqApp tests.

Plain scripts, not pytest — the rig installs only `requirements.txt`. Each test
runs on its own; `run_all.py` runs the set.

The important part is `isolate_user_state()`. The GUI tests drive the REAL
MainWindow, which persists as a side effect of ordinary use — the Save tab
writes `acqapp_local.json` on every field change, closing writes the dock layout
to QSettings — so without it the tests overwrite the operator's save folder,
subject ID and panel layout. Every test that builds a window calls it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# Run windowless by default — a test suite should not throw six windows across
# the operator's screen. Must be set before the first Qt import. Override it
# (QT_QPA_PLATFORM=windows) when you actually want to watch a test run.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make `import acqApp…` work no matter where the test was launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APP_DIR = REPO_ROOT / "acqApp"

from acqApp.console import enable_safe_console       # noqa: E402

# Test output is as exposed to the console-encoding trap as the app is, and
# these scripts are themselves run from arbitrary shells.
enable_safe_console()


# ── isolation ─────────────────────────────────────────────────────────────────

class MemorySettings:
    """Stand-in for QSettings, in-process only.

    QSettings cannot be redirected on Windows: `setPath` is IniFormat-only and
    `setDefaultFormat` applies only to `QSettings(parent)`, so
    `QSettings("acqApp", "acqApp")` reaches HKEY_CURRENT_USER regardless. The
    only reliable isolation is substituting the class before `main` imports it.
    """

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
    """A vendor driver module that refuses to do anything.

    Importing succeeds — the app's `import nidaqmx` lines are inside methods —
    but touching anything raises, which every device path already treats as "no
    hardware" and falls back from.
    """

    def __getattr__(self, name):
        raise RuntimeError(
            f"{self.__name__}.{name} is blocked by the test harness "
            f"(tests must never touch real hardware)")


def block_real_devices(*names: str) -> None:
    """Stand refusing stubs in front of the vendor drivers.

    `test_module_subsets` toggles **Emulate off**, rebuilding the real output
    controllers — the suite really was opening the DMD attached to this machine,
    and on the rig that is a DO task on the puffer's line with an animal in
    front of it. Tests that drive a fake device install their own module.
    """
    for name in (names or ("ALP4", "nidaqmx", "pylablib", "pypylon")):
        sys.modules[name] = _BlockedDriver(name)


def isolate_user_state() -> Path:
    """Redirect every persistent store the app writes, and return the temp dir.

    `acqapp_local.json` (theme, modules, panel settings), QSettings (the dock
    layout — substituted wholesale, since on Windows it is the registry), and
    the vendor drivers. Call BEFORE importing `acqApp.main`, which binds
    `QSettings` at import time.
    """
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_test_"))
    block_real_devices()

    from acqApp import config
    config._CONFIG_PATH = tmp / "acqapp_local.json"

    # The routine template library is a folder of files beside the package; an
    # unisolated run would save into, and delete from, the operator's own.
    from acqApp.routines import templates
    templates.DIR = tmp / "routine_templates"

    MemorySettings.store = {}
    import PyQt6.QtCore
    PyQt6.QtCore.QSettings = MemorySettings
    # `from PyQt6.QtCore import QSettings` binds its own reference, so patching
    # the source misses anything already imported. Both write: `main` the dock
    # layout, `dialogs` the settings window's geometry.
    for name in ("acqApp.main", "acqApp.dialogs"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "QSettings"):
            mod.QSettings = MemorySettings
    return tmp


# ── reporting ─────────────────────────────────────────────────────────────────

class Report:
    """Collects pass/fail lines and returns a process exit code.

    Not assert-based on purpose: stopping at the first failure hides the other
    five, and these tests are slow enough that one run should tell you all of it.

    **`-q` prints only what went wrong**, plus the closing summary. The passing
    lines are the point when a human reads a run — each one states a property in
    a sentence — but they are 50-130 lines of "ok" that a caller who only needs
    the verdict pays for. A failure prints in full either way.
    """

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


# ── Qt helpers ────────────────────────────────────────────────────────────────

def qt_app():
    """The QApplication, created once per process, with the app's own theme."""
    from PyQt6.QtWidgets import QApplication
    from acqApp import config, style
    app = QApplication.instance() or QApplication(sys.argv)
    style.apply_theme(app, config.get_theme())
    return app


def pump(app, seconds: float) -> None:
    """Run the Qt event loop for `seconds` without blocking it — the workers are
    real QThreads on real signals, so the loop has to actually turn."""
    import time
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)
