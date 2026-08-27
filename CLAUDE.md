# acqApp

Multi-instrument acquisition suite (voltage/pupil cams, wheel, puffer, XY stage, DMD on shared clock; 1 HDF5/session).

## Directives

Code & Style: Follow SOLID. Keep comments plain and concise. Code overrides docs on discrepancy.

Planning: Read [PLAN.md](PLAN.md) §0 (orientation) and §6 (next actions). Update in-place; keep within §8 size budget.

Separation: Keep [README.md](README.md) (architecture) distinct from PLAN.md (tasks).

Tree Map: Update [docs/STRUCTURE.md](docs/STRUCTURE.md) on any move/rename/new module (tests/test_structure.py enforces).

Archives: Open docs/AUDIT-2026-08.md, DECISIONS.md, or SESSIONLOG.md only for targeted lookups.

## Non-Negotiables

Environment: Install strictly into `acqApp\.venv` — never another interpreter, even if the shell is running one.

Testing: `acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py`. Use the ABSOLUTE interpreter path — the shell starts in this repo's parent, where the relative one fails obscurely. For one test, run its script directly with `-q` (`tests\test_routines.py -q`); run_all selects by short name (`routines`), not filename, and ignores `-q`. State explicitly if mock-verified only (only wheel and DMD have run on physical hardware).

Hardware Safety: Device open/config is safe. Verify the whole path short of the actuating call, then ask explicit user permission before physical actuation (DMD light, puffer, stage) — there may be an animal on the rig. See PLAN.md §2.

Thread/Console Safety: An exception escaping QThread.run() aborts the process (PyQt6 qFatal). Keep worker bodies inside PullWorker.run() guards; call console.enable_safe_console() before the first print, since an unencodable character in an acquisition loop reads as a device failure. tests/test_console_safety.py enforces this.

Test Isolation: GUI tests must invoke tests/_harness.isolate_user_state() — unisolated runs have destroyed the operator's saved layout before.

Git: Never commit sessions/, *.h5, *.csv, or *_local.json. Commit changes prior to any structural refactoring.

## Session Close

Update PLAN.md (§8): Check off verified work, state 3 next actions, add dated log entry, update header date and progress.
