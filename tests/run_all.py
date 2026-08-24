"""
Run the acqApp test suite.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py -v      (full output)
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py console  (one test)

Each test runs in its own process: three of them build a QApplication, and
creating and tearing one down repeatedly inside a single process is not
reliable. It also means a hard crash in one test (these drive real device
threads) reports as a failure instead of taking the whole run down.

Everything here runs in Emulate mode against fakes — no rig hardware, no windows
on screen, and no writes to the operator's real config or dock layout.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# This runner relays the tests' own output, which contains the very characters
# ("Δ", "≤", "→") that kill a print on a non-UTF-8 console — importing the
# harness hardens stdout. Yes, that bug caught this file first.
import _harness  # noqa: F401  (imported for its console hardening)

HERE = Path(__file__).resolve().parent

# Cheapest and most diagnostic first: if the console guard fails, the GUI tests
# are about to fail for a reason that has nothing to do with what they test.
TESTS = [
    ("console",   "test_console_safety.py"),
    ("undefined", "test_undefined_names.py"),
    ("structure", "test_structure.py"),
    ("readout",   "test_readout_fps.py"),
    ("encoder",   "test_encoder_derive.py"),
    ("enc-timing", "test_encoder_timing.py"),
    ("contracts", "test_device_contracts.py"),
    ("dmd",       "test_dmd.py"),
    ("dmd-calib", "test_dmd_calibration.py"),
    ("dmd-sweep", "test_dmd_sweep.py"),
    ("dmd-roi",   "test_dmd_roi.py"),
    ("closed-loop", "test_closed_loop.py"),
    ("save-paths", "test_save_paths.py"),
    ("stage-state", "test_stage_state.py"),
    ("stage-panel", "test_stage_panel.py"),
    ("pupil-limit", "test_pupil_limit.py"),
    ("pupil-video", "test_pupil_video.py"),
    ("losses",    "test_recording_losses.py"),
    ("camera-ts", "test_camera_timestamps.py"),
    ("subsets",   "test_module_subsets.py"),
    ("settings",  "test_settings_persistence.py"),
    ("session",   "test_session_recording.py"),
]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args
    wanted = [a for a in args if not a.startswith("-")]
    tests = [t for t in TESTS if not wanted or t[0] in wanted]
    if not tests:
        print(f"no test matches {wanted}; known: {[n for n, _ in TESTS]}")
        return 2

    print(f"running {len(tests)} test(s) under {sys.executable}\n")
    results, total_ok = [], 0
    t_start = time.perf_counter()

    for name, script in tests:
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(HERE / script)],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        dt = time.perf_counter() - t0
        out = proc.stdout + proc.stderr
        n_ok = sum(1 for ln in proc.stdout.splitlines()
                   if ln.startswith("  ok   "))
        total_ok += n_ok
        ok = proc.returncode == 0
        results.append((name, ok, n_ok, dt))

        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {name:<12} {n_ok:>3} checks  {dt:5.1f}s")
        if verbose or not ok:
            print("  " + "-" * 68)
            for line in out.splitlines():
                print("  | " + line)
            print("  " + "-" * 68)

    failed = [n for n, ok, _, _ in results if not ok]
    print(f"\n{'=' * 72}")
    print(f"{total_ok} checks, {len(results) - len(failed)}/{len(results)} "
          f"tests passed in {time.perf_counter() - t_start:.1f}s")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
