# HANDOFF — context for continuing acqApp

> **Superseded as a starting point.** Read [../PLAN.md](../PLAN.md) first — it
> carries the current plan, checklist and next actions. This file is kept for
> the *decisions and their reasons*, which remain valid. The **Status** table
> and **THE immediate next step** below are from 2026-07-27 and are now stale;
> PLAN.md §4 and §6 replace them.

It captures decisions and state that aren't obvious from the code alone.
Last updated: 2026-08-12.

## Update 2026-08 — camera crash, the wheel derivation, the camera link

Folded in from `SESSION_HANDOFF.md`, which used to sit beside this file as a
peer (PLAN.md item B2). Its session narrative and its list of touched files are
gone — most of that wiring has since moved into `modules.py` — but three things
in it are expensive to reconstruct and are kept here.

**The camera crash on Start, and why the process vanished with no traceback.**
Two independent causes, both fixed and both now load-bearing rules:

1. A Python exception escaping a `QThread.run()` makes PyQt6 call
   `qFatal()`/abort — the whole process dies, no traceback. Any unwrapped DCAM
   call in a worker did it. `PullWorker.run()` is now a guard that calls
   `self._run()` and emits `error`; **every worker's loop is `_run`, never
   `run`**. This is PLAN.md §2's invariant, and it started here.
2. A native DCAM crash from a **double-open**: startup probed the camera
   open→info→**close** and the worker then re-opened it. Re-opening a
   just-closed DCAM device segfaults below Python, so nothing can catch it.
   `main.py` now opens once and keeps the handle, which also removed ~7 s per
   Start. `faulthandler.enable()` was added so a future native crash at least
   dumps a stack.

Confirmed working **on the real camera** — one of the few things here that has
been.

**The wheel's reset is smeared across 2–3 samples.** This is the single most
valuable measurement in the old file, because every naive derivation fails on
it. A real capture caught the reset as `0.02 → 3.21 → 4.60 V`: each sub-step is
below a half-turn, so a plain `np.unwrap` cannot see the wrap and reads the
reset as **+1 rev of real motion**, cancelling the turn — cumulative distance
then sawtooths back to zero once per revolution, invisibly. That is why
`_derive` rejects any step implying more than `_MAX_REV_S` and *coasts* through
the sensor's blind spot instead of unwrapping. Against the real capture
(`toy_output/wheel.csv`, 7 resets) the final version leaks **0** resets.

Two earlier derivations that also failed, so they don't get retried:
differentiating the *raw* voltage (per-sample ADC noise of ±0.045 V ≈ 0.46 rev/s
of phantom speed — a velocity deadband cannot tell that from motion), and
rectified distance (`+= |Δ|`), which accumulated that noise. Speed is now an LSQ
slope over a ±0.25 s window and distance is the deadband-gated integral of it.

**Camera link — still open, and the reason the label exists.** The frame-rate
label used to be a hardcoded `DEFAULT_LINK = USB`; this rig is CoaXPress-only,
so it read the wrong column of the readout table (audit #11). The panel now
shows the camera's *measured* rate once running. **Watch that number at full
frame:** ~115 fps means CoaXPress is live; ~15.8 fps means it is still
bandwidth-capped like USB3, which would be a DCAM/grabber enumeration problem
*below* this app — the camera's USB interface also enumerating, or the FireBird
grabber not being the enumerated path. The app cannot choose the link; the
measured label is only how you tell which one you got.

The two diagnostic tools from that session are still in the tree and still the
way to answer a wheel question: `wheel/capture_raw.py` (hardware-clocked 1 kHz
raw capture) and `wheel/analyze_raw.py` (measures V/rev peak-to-peak, net
rotation, path).

## Update 2026-07-27 — main is wired up

The app grew past the original 3-device plan into a 5-subsystem suite
(voltage_cam, pupil_cam, wheel, puffer, dmd) — see `README.md`, which is now the
authoritative overview. Key changes since the last handoff:

- **Standardized on PyQt6.** An earlier split (app on PyQt5, skeleton on PyQt6)
  is gone. `main` forces `PYQTGRAPH_QT_LIB=PyQt6`.
- **`main.py` is fully wired:** Start/Stop builds+runs the workers, Record
  streams to disk. Previously the app opened to a dead static window.
- **One shared `SessionClock`** across all devices (owned by `SyncController`,
  shared with the `Recorder`). This is the "software timestamps" decision below,
  now actually implemented end to end.
- **Single-file HDF5 recording** via `acq/Recorder` + `HDF5Writer` — every frame
  is kept (the old camera path silently dropped all but the newest frame).
- Dead PyQt6 skeleton (`app/`, `acq/camera_worker.py`) deleted; its good infra
  (`acq/clock|ring_buffer|recorder|writer`) is what the real app now uses.
- Fixed a DAQ line clash: puffer stays on `port0/line0`, pupil LED moved to
  `line1` (two tasks can't own one physical line).
- Fixed a double camera-open: `main` only probes DCAM at import (open→info→
  close); the worker owns the streaming handle.

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
- **Encoder signal, measured on the rig** (1 kHz capture, 0 glitches): a clean
  **single-turn 0–5 V sawtooth**, range 0.10–5.02 V, span **4.91 V**, so
  `volts_per_rev = 4.912` — that is a measurement, not a default. Sample noise
  is **±0.045 V**, which is large enough to dominate any per-sample derivative
  (see the 2026-08 update above). The voltage **does wrap**: it is a
  continuous-turn sensor and the capture contains real resets.
- **Camera:** Orca Fire via `pylablib.devices.DCAM`. Needs Hamamatsu DCAM-API
  runtime installed (not pip-installable). NI-DAQmx runtime likewise needed for
  the encoder.

## Status

| Phase | State |
|-------|-------|
| 0 — hardware de-risk | encoder ✅ (analog ai2). Camera throughput ❌ still NOT run on the rig. |
| 1 — skeleton (clock/recorder) | ✅ done — `acq/` infra in use by main |
| 2 — camera streaming + preview + HDF5 | ✅ done (mock-verified); needs rig validation |
| 3 — encoder streaming + plot | ✅ done (mock-verified) |
| 4 — unified session start/stop + shared clock + metadata | ✅ done (mock-verified) |
| 5 — closed-loop (DMD/puffer from encoder) | not started (trigger bus exists) |
| 6 — hardware sync upgrade (DaqClock, triggered Orca) | future |

**Everything above is verified against the `--mock` workers only.** No real
hardware has run this code yet — the immediate next step is unchanged.

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
- Phase 0 scripts live in `scratch/` and get deleted as Phase 0 is validated.
  **Three went on 2026-08-13** — `cam_app.py` (a LabVIEW-front-panel emulator),
  `cam_live.py` (a matplotlib preview) and `encoder_read.py`, all superseded by
  the app itself and by the per-package `_toy.py` bring-up harnesses, and
  referenced by nothing. `cam_grab.py` **stays**: the camera throughput number
  is the one Phase 0 item still open, and that script is how it gets measured
  (PLAN §6). The `encoder_*.csv` captures stay too — they are the rig data the
  4.912 V/rev measurement came from, not code.

## Open items to confirm

- ~~Encoder `--volts-per-rev`~~ — **answered: 4.912 V/rev**, measured from a rig
  capture (see Hardware facts). The **wheel diameter** is still unmeasured, and
  until it is set the app reports rev/s and rev rather than mm/s and mm.
- ~~Whether the analog encoder voltage wraps~~ — **answered: yes.** It is a
  continuous-turn sensor; a real capture (`toy_output/wheel.csv`) contains 7
  resets, and they are smeared across 2–3 samples, which is what the reset
  rejection in `_derive` exists for.
