# docs/ — handoff and per-device notes

Background written during past sessions. Useful when picking up one subsystem in
isolation, but **historical**: where any of it disagrees with the code, the code
wins, and where it disagrees with the lab's live plan (kept in its private
notes), the plan wins.

Two exceptions to "historical". **STRUCTURE.md is live** — a test fails when it
stops matching the code, so it is the one file here you can trust on sight.
The audit (2026-08) and the session log are archives split out of the live plan
and kept in the private notes: closed work, for chasing a specific audit item
number or an old decision, not for getting oriented.

| File | What it's for |
|------|---------------|
| [USER_GUIDE.md](USER_GUIDE.md) | **Not historical — the operator quick-start.** Screenshots included; run and re-shoot after any GUI-visible change worth showing. |
| [STRUCTURE.md](STRUCTURE.md) | **The map: what is where, and what may import what.** A mermaid flow of the layering plus the annotated tree. Not historical and not prose-on-trust — `tests/test_structure.py` checks the tree against the filesystem and the arrows against the AST, so it fails the suite rather than rotting. Update it in the same commit as any move or new module. |
| [HANDOFF.md](HANDOFF.md) | The original design decisions and why (timebase, threading model, dock layout, hardware facts), plus the measured encoder signal and the two crash causes folded in from the old `SESSION_HANDOFF.md`. Its *Status* table is superseded by the live plan. |
| [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md) | Voltage camera (Hamamatsu ORCA-Fire, DCAM). The deepest of these. |
| [PUPIL_CAMERA_TRANSFER.md](PUPIL_CAMERA_TRANSFER.md) | Pupil camera (Basler, pypylon) and the tracking algorithm. |
| [WHEEL_TRANSFER.md](WHEEL_TRANSFER.md) | Running-wheel encoder on NI `Dev3/ai2`. |
| [STAGE_TRANSFER.md](STAGE_TRANSFER.md) | Thorlabs MCM6101 XY stage, and its relationship to the sibling `stage_control/` app. |

**Start elsewhere:** [../README.md](../README.md) describes the app as it is now;
[USER_GUIDE.md](USER_GUIDE.md) is the operator walkthrough.
