"""
Run the acqApp test suite.

    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py -v      (full output)
    acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\run_all.py console  (one test)

Tests run DEFAULT_JOBS at a time (`-j N` to change, `-j 1` for serial).
Each test (and each part of a multi-part file) runs in its own process:
QApplications and module patches don't mix in one, and a hard crash reports as
a failure instead of ending the run. Emulate mode against fakes only.

Quiet by default: only failing tests print, and only their FAIL lines and
tracebacks — a full run's output costs an AI session thousands of tokens.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Relays "Δ", "≤", "→", which kill a print on a non-UTF-8 console (this file
# hit that first); importing the harness hardens stdout.
import _harness  # noqa: F401  (imported for its console hardening)

HERE = Path(__file__).resolve().parent
MAX_FAIL_LINES = 40         # per failing test, without -v
DEFAULT_JOBS = min(8, os.cpu_count() or 1)

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
    ("pupil-review", "test_pupil_review.py"),
    ("pupil-auto", "test_pupil_auto.py"),
    ("camera",    "test_camera.py"),
    ("modules",   "test_modules.py"),
    ("session",   "test_session_recording.py"),
]


def _failure_lines(out: str) -> list[str]:
    """FAIL lines with their indented detail, part summaries, tracebacks."""
    keep, in_tb, after_fail = [], False, False
    for line in out.splitlines():
        if line.startswith("Traceback"):
            in_tb = True
        if in_tb:
            keep.append(line)
            if line and not line.startswith((" ", "Traceback")):
                in_tb = False           # the exception line ends it
            continue
        if "FAIL" in line:
            keep.append(line)
            after_fail = True
        elif after_fail and line.startswith("       "):
            keep.append(line)           # a FAIL's `info` detail
        else:
            after_fail = False
            if line.lstrip().startswith("- "):
                keep.append(line)       # the part's failure list
    return keep


def main() -> int:
    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args
    jobs = DEFAULT_JOBS
    if "-j" in args:
        i = args.index("-j")
        jobs = max(1, int(args[i + 1]))
        del args[i:i + 2]
    wanted = [a for a in args if not a.startswith("-")]
    tests = [t for t in TESTS if not wanted or t[0] in wanted]
    if not tests:
        print(f"no test matches {wanted}; known: {[n for n, _ in TESTS]}")
        return 2

    jobs = min(jobs, len(tests))
    print(f"running {len(tests)} test(s) under {sys.executable}"
          f" ({jobs} at a time)\n")
    results, total_ok = [], 0
    t_start = time.perf_counter()

    # The children's passing lines are counted, so they must print them.
    env = {**os.environ, "ACQAPP_VERBOSE": "1"}

    def run_one(test):
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(HERE / test[1])],
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace")
        return proc, time.perf_counter() - t0

    # map() yields in TESTS order, so the report reads the same as a serial run.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        finished = list(zip(tests, pool.map(run_one, tests)))

    for (name, script), (proc, dt) in finished:
        out = proc.stdout + proc.stderr
        n_ok = sum(1 for ln in proc.stdout.splitlines()
                   if ln.startswith("  ok   "))
        total_ok += n_ok
        ok = proc.returncode == 0
        results.append((name, ok, n_ok, dt))

        status = "PASS" if ok else "FAIL"
        if verbose or not ok:
            print(f"  {status}  {name:<12} {n_ok:>3} checks  {dt:5.1f}s")
        if verbose:
            for line in out.splitlines():
                print("  | " + line)
        elif not ok:
            lines = _failure_lines(out)
            for line in lines[:MAX_FAIL_LINES]:
                print("  | " + line)
            if len(lines) > MAX_FAIL_LINES:
                print(f"  | … {len(lines) - MAX_FAIL_LINES} more; rerun "
                      f"{script} directly for all of it")

    failed = [n for n, ok, _, _ in results if not ok]
    print(f"\n{'=' * 72}")
    print(f"{total_ok} checks, {len(results) - len(failed)}/{len(results)} "
          f"tests passed in {time.perf_counter() - t_start:.1f}s")
    slow = sorted(results, key=lambda r: -r[3])[:3]
    print("slowest: " + ", ".join(f"{n} {dt:.0f}s" for n, _, _, dt in slow))
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
