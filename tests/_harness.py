"""
Shared scaffolding for the acqApp tests.

These are plain scripts, not pytest: pytest is not a dependency of this project
and the rig machine installs only `requirements.txt`. Each test is runnable on
its own, and `run_all.py` runs the set.

The important part of this module is `isolate_user_state()`. The GUI tests drive
the REAL MainWindow, which persists things as a side effect of ordinary use —
the Save tab writes `acqapp_local.json` whenever its fields change, and closing
the window writes the dock layout to QSettings. Without isolation, running the
tests would silently overwrite the operator's save folder, subject ID and
carefully arranged panel layout. Every test that builds a window must call it.
"""
from __future__ import annotations

import os
import sys
import tempfile
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
    """Stand-in for QSettings that keeps values in this process only.

    QSettings cannot be redirected on Windows: `setPath` affects IniFormat only,
    and `setDefaultFormat` applies solely to the `QSettings(parent)`
    constructor — so `QSettings("acqApp", "acqApp")` goes to HKEY_CURRENT_USER
    no matter what you set. The only reliable isolation is to substitute the
    class before `main` imports it.
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


def isolate_user_state() -> Path:
    """Redirect every persistent store the app writes, and return the temp dir.

    Covers:
      * `acqapp_local.json` — theme, enabled modules, per-panel settings. The
        Save tab rewrites it on every field edit, so a test that sets a save
        folder would otherwise leave the operator pointing at a temp dir.
      * QSettings — the dock layout, written on every window close. Substituted
        wholesale (see MemorySettings), because on Windows it is the registry
        and cannot be pointed elsewhere.

    Call this BEFORE importing `acqApp.main`: it binds `QSettings` at import
    time, so the substitution has to be in place first.
    """
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_test_"))

    from acqApp import config
    config._CONFIG_PATH = tmp / "acqapp_local.json"

    MemorySettings.store = {}
    import PyQt6.QtCore
    PyQt6.QtCore.QSettings = MemorySettings
    main = sys.modules.get("acqApp.main")        # already imported? patch it too
    if main is not None:
        main.QSettings = MemorySettings
    return tmp


# ── reporting ─────────────────────────────────────────────────────────────────

class Report:
    """Collects pass/fail lines and returns a process exit code.

    Deliberately not assert-based: a device test that stops at the first failure
    hides the other five things that also broke, and these tests are slow enough
    that one run should tell you everything.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.n_ok = 0

    def check(self, cond: bool, msg: str) -> bool:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if cond:
            self.n_ok += 1
        else:
            self.failures.append(msg)
        return bool(cond)

    def info(self, msg: str) -> None:
        print(f"         {msg}")

    def note(self, msg: str) -> None:
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
    """Run the Qt event loop for `seconds` without blocking it.

    The workers are real QThreads posting to real signals, so the loop has to
    actually turn for a session to behave the way it does in front of a user.
    """
    import time
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)
