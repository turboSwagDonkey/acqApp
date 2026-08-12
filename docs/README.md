# docs/ — handoff and per-device notes

Background written during past sessions. Useful when picking up one subsystem in
isolation, but **historical**: where any of it disagrees with the code, the code
wins, and where it disagrees with [../PLAN.md](../PLAN.md), the plan wins.

The first two are **archives split out of PLAN.md** to keep it readable at the
start of every session. They are complete and current — not stale like the
handoff notes below them — but they are closed work: open them to chase a
specific audit item number or an old decision, not to get oriented.

| File | What it's for |
|------|---------------|
| [AUDIT-2026-08.md](AUDIT-2026-08.md) | The 2026-08-10 full-app audit, all 22 items, each with what the bug was, why it mattered on a rig and which test now guards it. Referenced by item number (#1–#20, C1–C3, B1–B2) from PLAN.md §5b and `tests/README.md`. |
| [SESSIONLOG.md](SESSIONLOG.md) | Session entries older than the three PLAN.md §7 keeps. |
| [HANDOFF.md](HANDOFF.md) | The original design decisions and why (timebase, threading model, dock layout, hardware facts), plus the measured encoder signal and the two crash causes folded in from the old `SESSION_HANDOFF.md`. Its *Status* table is superseded by PLAN.md §4. |
| [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md) | Voltage camera (Hamamatsu ORCA-Fire, DCAM). The deepest of these. |
| [PUPIL_CAMERA_TRANSFER.md](PUPIL_CAMERA_TRANSFER.md) | Pupil camera (Basler, pypylon) and the tracking algorithm. |
| [WHEEL_TRANSFER.md](WHEEL_TRANSFER.md) | Running-wheel encoder on NI `Dev3/ai2`. |
| [STAGE_TRANSFER.md](STAGE_TRANSFER.md) | Thorlabs MCM6101 XY stage, and its relationship to the sibling `stage_control/` app. |

**Start elsewhere:** [../README.md](../README.md) describes the app as it is now;
[../PLAN.md](../PLAN.md) is the live plan, checklist and next actions.
