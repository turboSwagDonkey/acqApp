# Working on acqApp

Multi-instrument in-vivo acquisition suite for the ICN rig (voltage cam, pupil
cam, wheel, puffer, XY stage, DMD on one shared clock, one HDF5 per session).

## Start here

**Read [PLAN.md](PLAN.md) before planning any work** — its **§0 "Start here"**
first, which carries the orientation a fresh session needs (how to run the
suite, which hardware is actually on this machine, the sibling projects worth
copying from, and anything left uncommitted). Then §6, the next actions. §5b is
reference: consult the item you're working on rather than reading it through.
There is exactly one such file — update it, don't fork it.

PLAN.md is kept short on purpose, because it is read in full every session.
Finished work is archived, not deleted: the closed 2026-08-10 audit is in
[docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md) and older session entries in
[docs/SESSIONLOG.md](docs/SESSIONLOG.md). **Open those only to chase a specific
item number or an old decision** — never as part of getting oriented.

- [README.md](README.md) — the authoritative *description* (architecture,
  recording format). PLAN.md is the *plan*. Keep that split.
- [docs/](docs/) — historical handoff and per-device notes. Where they disagree
  with the code, the code wins.

## Non-negotiables

- **Installs go ONLY into `acqApp/.venv`.** Never pip-install into another
  interpreter, even if the user's shell is running one.
- **Verify in Emulate/mock mode**: `acqApp\.venv\Scripts\python.exe
  acqApp\tests\run_all.py` — 530 checks, ~50 s, no hardware, no windows. Use the
  **absolute** interpreter path; the shell usually starts in this repo's parent,
  where the relative one fails obscurely. Say plainly when something is
  mock-verified only: apart from the wheel encoder and the DMD, none of this
  code has run against real hardware.
- **Ask before actuating anything physical.** Opening and configuring a device
  is safe; projecting light, firing the puffer and driving the stage are not —
  there may be an animal on the rig. Verify the whole path short of the
  actuating call, then ask. See PLAN.md §2.
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
