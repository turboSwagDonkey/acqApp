"""
Run the acqApp test suite.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py -v      (full output)
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py console  (one test)

Each test (and each part of a multi-part file) runs in its own process:
QApplications and module patches don't mix in one, and a hard crash reports as
a failure instead of ending the run. Emulate mode against fakes only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Relays "Δ", "≤", "→", which kill a print on a non-UTF-8 console (this file
# hit that first); importing the harness hardens stdout.
import _harness  # noqa: F401  (imported for its console hardening)

HERE = Path(__file__).resolve().parent

# Cheapest and most diagnostic first: a console-guard failure would fail the
# GUI tests for an unrelated reason.
TESTS = [
    ("console",   "test_console_safety.py"),
    ("undefined", "test_undefined_names.py"),
    ("structure", "test_structure.py"),
    ("contracts", "test_device_contracts.py"),
    ("encoder",   "test_encoder.py"),
    ("saving",    "test_saving.py"),
    ("config",    "test_config.py"),
    ("stage",     "test_stage.py"),
    ("stage-panel", "test_stage_panel.py"),
    ("stage-focus-ui", "test_stage_focus_ui.py"),
    ("dmd",       "test_dmd.py"),
    ("pickers",   "test_pickers.py"),
    ("vis-stim",  "test_vis_stim.py"),
    ("routines",  "test_routines.py"),
    ("pupil",     "test_pupil.py"),
    ("camera",    "test_camera.py"),
    ("closed-loop", "test_closed_loop.py"),
    ("modules",   "test_modules.py"),
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

    # The children's passing lines are counted, so never pass ACQAPP_QUIET.
    env = {k: v for k, v in os.environ.items() if k != "ACQAPP_QUIET"}

    for name, script in tests:
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(HERE / script)],
                              capture_output=True, text=True, env=env,
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
            # Drop only the passing lines; keep FAILs, `info`, tracebacks.
            print("  " + "-" * 68)
            for line in out.splitlines():
                if verbose or not line.startswith("  ok   "):
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
