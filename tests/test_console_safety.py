"""
Guard test for the console-encoding crash.

A diagnostic print containing "≤" / "→" / "⚠" raises UnicodeEncodeError on a
non-UTF-8 console (which is what Python falls back to whenever stdout is a pipe
or a legacy terminal). Those prints live inside the acquisition loops, so the
exception escapes into PullWorker.run() and is reported as a device failure —
the camera simply doesn't start, and the message blames the hardware. The
voltage cam's "shorten exposure to ≤N µs" notice hits this on the DEFAULT
configuration, so it is not a corner case.

Three things are checked:
  1. every runnable entry point calls enable_safe_console()   (static)
  2. the real code path that crashed survives a cp1252 console (dynamic)
  3. that same path WITHOUT the fix still crashes             (control)

(3) is what makes (2) meaningful: without it the dynamic check would pass even
if cp1252 had stopped being fatal for some unrelated reason.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_console_safety.py
"""
from __future__ import annotations

import os
import subprocess
import sys

from _harness import APP_DIR, REPO_ROOT, Report

# The exact call that used to kill the camera worker: _query_timings prints
# "shorten exposure to ≤N µs" whenever the config is exposure-limited, which
# the default 10 ms full-frame config is.
CAMERA_PATH = r'''
import sys
sys.path.insert(0, r"{repo}")
{harden}
from PyQt6.QtCore import QCoreApplication
_app = QCoreApplication([])
from acqApp.devices.voltage_cam.acquisition import OrcaFireWorker
from acqApp.devices.voltage_cam.presets import AcqConfig

cfg = AcqConfig()                     # default: 10 ms exposure at full frame
assert cfg.exposure_limited, "default config is expected to be exposure-limited"

class _Timings:  frame_period = 1.0 / 115
class _FakeCam:
    def get_frame_timings(self): return _Timings()

w = OrcaFireWorker(0, cfg, cam=_FakeCam())
w._query_timings(_FakeCam(), cfg)     # <- the print that used to explode
print("REACHED-END")
'''

HARDEN = ("from acqApp.console import enable_safe_console\n"
          "enable_safe_console()")


def _hardens(text: str) -> bool:
    """Does this entry point make its console safe?

    Either directly, or by importing the test harness, which does it on import.
    The tests are scanned too: `run_all.py` relays output full of the offending
    characters, and skipping the directory let exactly that bug through once.
    """
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

    # ── the other half of the same problem: text written THROUGH cp1252 ──────
    # The console guard stops a print from raising. It cannot stop a file being
    # SAVED through the wrong codec, which is how `devices/pupil_cam/panel.py`
    # came to render its exposure suffix and its "Sample video" button as
    # mojibake — visible to the operator in the pupil tab (§7 (u) and (ab)).
    # Mojibake is UTF-8 bytes once decoded as cp1252 and re-encoded, so the
    # inverse round trip identifies it exactly: correct text cannot survive
    # encode('cp1252').decode('utf-8').
    #
    # The damaged forms below are written as escapes on purpose — spelling them
    # literally would make this file fail its own check.
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
    # CONTROL: the detector must actually fire on the real thing, or the check
    # above passes by being blind.
    micro = chr(0xB5)                       # µ
    broken = chr(0xC3 - 1) + micro + "s"    # what "µs" turns into: U+00C2 U+00B5
    r.check(broken.encode("cp1252").decode("utf-8") == micro + "s",
            "control: the round trip really does identify mojibake")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
