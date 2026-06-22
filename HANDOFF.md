# HANDOFF — context for continuing acqApp

Read this first if you're a fresh Claude session (web or Claude Code on the rig)
or returning after a break. It captures decisions and state that aren't obvious
from the code alone. Last updated: 2026-06-22.

## What this project is

Unifying three instruments into one PyQt6 acquisition app:
1. **DMD pattern control** — already exists as `dmdGUI_project` (a sibling folder,
   NOT in this repo). We copy & adapt from it; we do not modify the original.
2. **Hamamatsu Orca Fire camera** — data acquisition, stream to disk.
3. **Rotary wheel encoder** on **NI PCIe-6363 ("Dev3")**.

The existing DMD app uses a clean PyQt6 mixin architecture
(`DMDApp(QMainWindow, UiMix, ProjectionMix, ImageProcessorMix)`) with `QThread`
workers for hardware and a JSON dataclass config. We extend that pattern.

## Locked architecture decisions

- **Sync: software timestamps first, hardware later.** Every device timestamps
  its data via a central `SessionClock`. v1 = `time.perf_counter()`. The PCIe-6363
  has the timing engine to later swap in a hardware-clocked `DaqClock` WITHOUT
  changing device code. Build the `SessionClock` interface on day one.
- **Storage: stream camera to disk** (don't buffer-then-save). Leaning HDF5
  (`h5py`) so frames + timestamps + encoder samples live in one session file.
  `Writer` interface so a TIFF writer is a drop-in alternative.
- **UI: dockable `QDockWidget`s** in the `QMainWindow`, individually
  collapsible/floatable/tabbable, so one instrument can be spotlighted. Persist
  layout via `saveState()`/`restoreState()` into the config JSON.
- **Threading per streaming device:** acquisition thread → bounded ring buffer →
  separate writer thread (disk) + decimated signal → GUI preview. Acquisition
  thread does NO disk I/O and NO Qt rendering. Ring buffer drops oldest with a
  visible counter if disk can't keep up.

## Hardware facts

- **DAQ:** NI PCIe-6363, device name **`Dev3`**.
- **Encoder:** read as an **analog voltage on `Dev3/ai2`** (single-ended / RSE,
  ±10 V), matching the lab's existing MATLAB script. NOTE: this is the analog
  approach, NOT a quadrature counter on ctr0 (an earlier draft assumed ctr0 —
  that was wrong for this rig). Scale to revolutions with `--volts-per-rev`.
- **Camera:** Orca Fire via `pylablib.devices.DCAM`. Needs Hamamatsu DCAM-API
  runtime installed (not pip-installable). NI-DAQmx runtime likewise needed for
  the encoder.

## Status

| Phase | State |
|-------|-------|
| 0 — hardware de-risk | encoder ✅ working (analog ai2). Camera throughput ❌ NOT yet run. |
| 1 — skeleton (clock/recorder/dock + DMD copy) | not started |
| 2 — camera streaming + preview + HDF5 | not started |
| 3 — encoder streaming + plot | not started |
| 4 — unified session start/stop + sync + metadata | not started |
| 5 — closed-loop (DMD from encoder) | not started |
| 6 — hardware sync upgrade (DaqClock, triggered Orca) | future |

## THE immediate next step

Run the camera throughput test on the rig and bring back the achieved **MB/s**:

```
.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save
```

That number sizes the ring buffer and confirms the SSD can keep up — it gates
the Phase 2 storage design. Until we have it, Phase 1 skeleton work can proceed
(clock/recorder/dock are independent of the camera rate).

## Repo / transfer workflow

- GitHub: `turboSwagDonkey/acqApp` (private).
- Laptop has no hardware → write code, commit, push. Rig → pull, run, commit fixes,
  push. `.venv` is gitignored; recreate with `pip install -r requirements.txt`.
- Phase 0 scripts live in `scratch/` and get DELETED once Phase 0 is validated.

## Open items to confirm

- Encoder `--volts-per-rev` value (what voltage span = one wheel revolution?) and
  wheel diameter, so position/speed read in real units. Currently unset.
- Whether the analog encoder voltage wraps (continuous-turn sensor) — affects how
  we unwrap cumulative position in the real app.
