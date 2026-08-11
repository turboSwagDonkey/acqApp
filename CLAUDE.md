# Working on acqApp

Multi-instrument in-vivo acquisition suite for the ICN rig (voltage cam, pupil
cam, wheel, puffer, XY stage, DMD on one shared clock, one HDF5 per session).

## Start here

**Read [PLAN.md](PLAN.md) before planning any work.** It is the single living
plan: stages, the audit checklist with per-item status and file:line pointers,
the three next actions, and the session log. There is exactly one such file —
update it, don't fork it.

- [README.md](README.md) — the authoritative *description* (architecture,
  recording format). PLAN.md is the *plan*. Keep that split.
- [docs/](docs/) — historical handoff and per-device notes. Where they disagree
  with the code, the code wins.

## Non-negotiables

- **Installs go ONLY into `acqApp/.venv`.** Never pip-install into another
  interpreter, even if the user's shell is running one.
- **Verify in Emulate/mock mode**: `.venv\Scripts\python.exe tests\run_all.py`
  (~17 s, no hardware, no windows). Say plainly when something is mock-verified
  only — most of this code has never run against real hardware.
- **An exception escaping a `QThread.run()` aborts the process** (PyQt6
  `qFatal`). Worker bodies stay inside the `PullWorker.run()` guard, and every
  runnable entry point calls `console.enable_safe_console()` before its first
  print — an unencodable character in a diagnostic print inside an acquisition
  loop reads as a device failure. `tests/test_console_safety.py` enforces this.
- **Never commit experiment data.** `sessions/`, `*.h5`, `*.csv`, `*_local.json`
  are gitignored; keep it that way. Commit before restructuring — the repo once
  went six weeks and 68 files without one.
- **Don't run tests or scratch scripts against real user state.** GUI tests must
  call `tests/_harness.isolate_user_state()`: the app persists settings and the
  dock layout as a side effect of ordinary use, and unisolated runs have
  destroyed the operator's saved layout before.

## End of session

Update [PLAN.md](PLAN.md) per its §8: tick only *verified* work, rewrite the
three next actions, add one dated line to the session log, refresh the header
date and progress figure. Do it before the context gets tight, not after.
