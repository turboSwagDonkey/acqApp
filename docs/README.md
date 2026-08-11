# docs/ — handoff and per-device notes

Background written during past sessions. Useful when picking up one subsystem in
isolation, but **historical**: where any of it disagrees with the code, the code
wins, and where it disagrees with [../PLAN.md](../PLAN.md), the plan wins.

| File | What it's for |
|------|---------------|
| [HANDOFF.md](HANDOFF.md) | The original design decisions and why (timebase, threading model, dock layout, hardware facts). Its *Status* table is superseded by PLAN.md §4. |
| [SESSION_HANDOFF.md](SESSION_HANDOFF.md) | One 2026-07 session: the camera crash (`qFatal` on an escaped exception), wheel speed/distance, the camera-link label. |
| [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md) | Voltage camera (Hamamatsu ORCA-Fire, DCAM). The deepest of these. |
| [PUPIL_CAMERA_TRANSFER.md](PUPIL_CAMERA_TRANSFER.md) | Pupil camera (Basler, pypylon) and the tracking algorithm. |
| [WHEEL_TRANSFER.md](WHEEL_TRANSFER.md) | Running-wheel encoder on NI `Dev3/ai2`. |
| [STAGE_TRANSFER.md](STAGE_TRANSFER.md) | Thorlabs MCM6101 XY stage, and its relationship to the sibling `stage_control/` app. |

**Start elsewhere:** [../README.md](../README.md) describes the app as it is now;
[../PLAN.md](../PLAN.md) is the live plan, checklist and next actions.
