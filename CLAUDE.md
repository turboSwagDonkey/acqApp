# Working on acqApp

Multi-instrument in-vivo acquisition suite for the ICN rig (voltage cam, pupil
cam, wheel, puffer, XY stage, DMD on one shared clock, one HDF5 per session).

## Start here

**Follow SOLID Principles**
**Add explanatory comments, but keep them plain and short** 

**Read [PLAN.md](PLAN.md) before planning any work** — **§0** for orientation,
then **§6** for the next actions; that is the whole of it. §5b is reference,
consulted per item. There is exactly one such file: update it, don't fork it,
and keep it inside the size budget in its §8 — it is read in full every session,
so every line is paid for again in every future one. Finished work is archived,
not deleted, into [docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md),
[docs/DECISIONS.md](docs/DECISIONS.md) and
[docs/SESSIONLOG.md](docs/SESSIONLOG.md); **open those only to chase a specific
item, never to get oriented.**

- [docs/STRUCTURE.md](docs/STRUCTURE.md) — the map of the tree. **Any move,
  rename or new module updates it in the same commit**, and
  `tests/test_structure.py` fails the suite if you don't.
- [README.md](README.md) — the authoritative *description* (architecture,
  recording format); PLAN.md is the *plan*. Keep that split.
- [docs/](docs/) — handoff and per-device notes. Where they disagree with the
  code, the code wins.

## Non-negotiables

- **Installs go ONLY into `acqApp/.venv`.** Never pip-install into another
  interpreter, even if the user's shell is running one.
- **Verify in Emulate/mock mode**: `acqApp\.venv\Scripts\python.exe
  acqApp\tests\run_all.py` — ~65 s, no hardware, no windows; add `-q` to a
  single test file for failures only. Use the **absolute** interpreter path; the
  shell usually starts in this repo's parent, where the relative one fails
  obscurely. Say plainly when something is mock-verified only: apart from the
  wheel encoder and the DMD, none of this code has run against real hardware.
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
