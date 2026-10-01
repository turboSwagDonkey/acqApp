"""The console-encoding crash: a print of "≤" / "→" / "⚠" raises
UnicodeEncodeError on a non-UTF-8 console (a pipe, a legacy terminal). Inside
an acquisition loop that reads as a device failure — the camera just doesn't
start. The voltage cam's "≤N µs" notice hits it on the DEFAULT configuration.

  1. every runnable entry point calls enable_safe_console()   (static)
  2. the real code path that crashed survives a cp1252 console (dynamic)
  3. that same path WITHOUT the fix still crashes             (control for 2)
"""
from __future__ import annotations

import os
import subprocess
import sys

from _harness import APP_DIR, REPO_ROOT, Report

# The camera worker's start-up prints that used to kill it.
CAMERA_PATH = r'''
import sys
sys.path.insert(0, r"{repo}")
{harden}
from PyQt6.QtCore import QCoreApplication
_app = QCoreApplication([])
from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
from acqApp.devices.voltage_cam.presets import AcqConfig

cfg = AcqConfig()

class _Timings:  frame_period = 1.0 / 115
class _FakeCam:
    def get_frame_timings(self): return _Timings()

w = OrcaFireWorker(0, cfg, cam=_FakeCam())
w._query_timings(_FakeCam(), cfg)
w._warn_data_rate(cfg, 10_000.0)      # <- "⚠"/"≤", outside cp1252
print("REACHED-END")
'''

HARDEN = ("from acqApp.console import enable_safe_console\n"
          "enable_safe_console()")


def _hardens(text: str) -> bool:
    """Directly, or by importing the harness. tests/ is scanned too: run_all.py
    relays the offending characters, and skipping it let that bug through."""
    return "enable_safe_console" in text or "_harness" in text


def run_cp1252(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a subprocess whose stdout really is cp1252."""
    env = dict(os.environ, PYTHONIOENCODING="cp1252", ACQAPP_NO_REEXEC="1")
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=120)


def main() -> int:
    r = Report("console")

    # ── 1. static: every entry point opts in ─────────────────────────────────
    entries = sorted(p for p in APP_DIR.rglob("*.py")
                     if ".venv" not in p.parts
                     and 'if __name__ == "__main__":' in p.read_text(encoding="utf-8"))
    r.note(f"{len(entries)} runnable entry points")
    missing = [str(p.relative_to(APP_DIR)) for p in entries
               if not _hardens(p.read_text(encoding="utf-8"))]
    r.check(not missing,
            f"every entry point hardens the console (missing: {missing})")

    # ── 3. control: cp1252 really is fatal without the fix ───────────────────
    ctl = run_cp1252(CAMERA_PATH.format(repo=REPO_ROOT, harden=""))
    crashed = "UnicodeEncodeError" in ctl.stderr and "REACHED-END" not in ctl.stdout
    r.check(crashed,
            "control: the camera path still dies on a cp1252 console unhardened")
    if crashed:
        line = [ln for ln in ctl.stderr.splitlines() if "UnicodeEncodeError" in ln]
        r.info(f"failed as expected: {line[-1].strip()[:92]}")

    # ── 2. the fix ───────────────────────────────────────────────────────────
    fixed = run_cp1252(CAMERA_PATH.format(repo=REPO_ROOT, harden=HARDEN))
    r.check(fixed.returncode == 0,
            f"camera path survives a cp1252 console (rc={fixed.returncode})")
    r.check("REACHED-END" in fixed.stdout, "camera path ran to completion")
    r.check("UnicodeEncodeError" not in fixed.stderr, "no encoding error raised")
    if fixed.returncode != 0:
        print(fixed.stderr[-800:])

    # ── and the characters themselves ────────────────────────────────────────
    chars = run_cp1252(
        f'import sys\nsys.path.insert(0, r"{REPO_ROOT}")\n{HARDEN}\n'
        'print("\\u2264 \\u2192 \\u26a0 \\u0394 \\u2500 \\u03c0 \\u221e")\n'
        'print("DONE")')
    r.check(chars.returncode == 0 and "DONE" in chars.stdout,
            "every offending character prints without raising")

    # ── source SAVED through cp1252 (pupil_cam/panel.py showed mojibake) ─────
    # Correct text cannot survive encode('cp1252').decode('utf-8'); mojibake
    # round-trips exactly. The damaged forms below are escapes on purpose —
    # spelled literally, this file would fail its own check.
    import re
    runs = re.compile(r"[^\x00-\x7f]+")
    damaged: list[str] = []
    scanned = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            damaged.append(f"{path.name}: not valid UTF-8")
            continue
        for m in runs.finditer(text):
            try:
                back = m.group().encode("cp1252").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue                       # correct text — cannot round-trip
            if back != m.group():
                ln = text[:m.start()].count("\n") + 1
                damaged.append(f"{path.name}:{ln} {m.group()[:16]!r} "
                               f"should be {back[:16]!r}")
    r.check(not damaged,
            f"no source file is doubly-encoded ({scanned} scanned)"
            + ("" if not damaged else f" — {damaged[:3]}"))
    # Control: without it, the check above could pass by being blind.
    micro = chr(0xB5)                       # µ
    broken = chr(0xC3 - 1) + micro + "s"    # what "µs" turns into: U+00C2 U+00B5
    r.check(broken.encode("cp1252").decode("utf-8") == micro + "s",
            "control: the round trip really does identify mojibake")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
