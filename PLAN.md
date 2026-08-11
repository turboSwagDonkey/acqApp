# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-11 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–4 done; **audit remediation ~25 %** (4 of 21 closed, 3 partial) |

---

## 1. Goal

A single PyQt6 app that runs and records six rig subsystems (voltage cam, pupil
cam, wheel, puffer, XY stage, DMD) against **one shared session clock**, into
**one HDF5 file per session**, so every stream is analysable on a common
timebase. Phases 0–4 of that are built and mock-verified. The work now is
(a) making it correct and trustworthy on real hardware, and (b) closed-loop.

## 2. Ground rules

These are invariants, not preferences. Breaking one has cost real time before.

- **Installs go ONLY into `acqApp/.venv`.** The bootstrap in `main.py` enforces
  this; never pip-install into another interpreter.
- **Mock first.** Every change must pass `tests/run_all.py` in Emulate mode
  before it goes near the rig. Real-hardware-only claims get flagged as such.
- **The laptop has no hardware.** Write + commit + push here; pull + run + fix
  on the rig. Anything unverifiable off-rig goes in §6 "Needs the rig".
- **Commit before restructuring.** See the warning in §3 — this is currently
  the single biggest risk to the project.
- **An exception escaping a `QThread.run()` aborts the process** (PyQt6
  `qFatal`). All worker bodies stay inside the `PullWorker.run()` guard.
- **Every runnable entry point calls `enable_safe_console()`** before its first
  print. `tests/test_console_safety.py` enforces this.

## 3. Backup status

`acqApp/` is a git repo — `turboSwagDonkey/acqApp` (private), branch `master`.
The laptop writes and pushes; the rig pulls, runs, fixes and pushes back.

**Caught up 2026-08-11.** Before that, the last commit was 2026-06-29: six
weeks and 68 files — `modules.py`, `config.py`, `console.py`, `saving.py`,
`probe.py`, the whole `stage/` package and the whole `tests/` suite — existed
only on this disk, and a 950-line refactor of `MainWindow` had already been
done on top of that. Don't let it drift like that again.

**Commit before restructuring, and push before leaving the machine.** The
`.gitignore` already excludes `sessions/`, `*.h5`, `*.csv`, `*_local.json` and
`.venv/`, so committing does not risk pushing experiment data.

## 4. Stages

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Hardware de-risk (encoder, camera throughput) | encoder ✅ · **camera MB/s never measured on the rig** ❌ |
| 1 | `acq/` skeleton: clock, ring buffer, recorder, writer | ✅ |
| 2 | Camera streaming + preview + HDF5 | ✅ mock-verified |
| 3 | Encoder streaming + plot | ✅ mock-verified |
| 4 | Unified session start/stop, shared clock, metadata | ✅ mock-verified |
| **4.5** | **Audit remediation + test net** (§5) | **in progress — where the work is now** |
| 5 | Closed-loop: trigger DMD/puffer from encoder state | not started (trigger bus exists) |
| 6 | Hardware sync: `DaqClock` on the PCIe-6363, triggered ORCA | future |

## 5. Checklist — audit remediation

From the full-app audit (2026-08-10). Ordered worst-first within each group.
Status: ✅ done · 🟡 partial · ⬜ open.

### Data integrity — these silently corrupt or lose recorded data

- [x] **#1 Camera frame timestamps were quantised to the read batch.** ✅
      Frames now carry the camera's own timestamp, anchored to the session
      clock on the first frame; `/voltage_cam_index` exposes driver-dropped
      frames; `cam_timestamp_source` records whether it worked.
      Guarded by `tests/test_camera_timestamps.py` (with a negative control).
- [ ] **#2 A recording can silently overwrite an existing file.** ⬜
      `acq/writer.py:83` opens `h5py.File(path, "w")` (truncate) with no
      existence check. Free-text filename template makes this reachable — set
      it to `{subject}` and the second recording of the day destroys the first.
      *Fix:* existence check in `saving.writable_error()` (the pre-flight hook
      already exists), or auto `_001` suffix. **Highest-value open item.**
- [x] **#3 Puffer channel and Test-puff duration were ignored.** ✅
      Panel now emits `settings_changed`; `PufferController.apply_settings()`
      re-opens the DO task on a channel change under a lock; `fire()` takes the
      panel duration. Recorded as `puffer_channel` / `puffer_duration_s`.
- [ ] **#4 Only 2 of 7 panels persist their settings.** ⬜
      Wheel, pupil, puffer, stage-port and DMD are built bare. Every launch
      resets V/rev and wheel diameter to defaults — and per the handoff notes
      the real wheel diameter still has to be measured, so that is exactly the
      value most likely to be silently wrong in a session file.
- [ ] **#5 The DMD is a print-only stub presented as real hardware.** 🟡
      `apply_settings()` added; the device calls in `dmd/control.py:61-97` are
      still `print(...)`. With Emulate off, Display does nothing and the UI
      gives no sign. `alp4lib` is in the venv, so the ALP path is reachable.

### Bugs and robustness

- [ ] **#6** `stage/control.py:61-70` — `move_to_um`/`jog_um` use `self._dev`
      with no `None` check (unlike `stop`/`read_xy_um`). A move after a
      disconnect raises `AttributeError` instead of `StageControllerError`. ⬜
- [ ] **#7** `stage/settings.py:220` — unguarded `path.read_text()`. A missing
      calibration file makes `set_center_here()` raise *after* the axes were
      mutated in memory: live state and disk then disagree. ⬜
- [ ] **#8** `voltage_cam/acquisition.py` — failing `wait_for_frame` hits
      `except Exception: continue` with no backoff → hot spin, no message. ⬜
- [x] **#9** Puffer pulse thread could write to a task closed mid-sleep. ✅
      (fixed with #3: `_task_lock` + identity re-check before the trailing write)
- [ ] **#10** `_stop_recording` clears the sinks, but worker lambdas already
      captured `rec`; a mid-callback `put()` is dropped and never counted. ⬜
- [ ] **#11** `voltage_cam/settings.py:131` — `get_config()` omits `link`, so
      the saved value is discarded and the frame-rate label always uses
      `DEFAULT_LINK`. One-line fix. ⬜

### Performance

- [ ] **#12 Pupil tracking runs on the GUI thread** (30 Hz display timer).
      `coarse_seed`'s `distance_transform_edt` is ~100-200 ms on a degenerate
      mask; a genuinely lost pupil re-seeds repeatedly → visible stutter that
      also stalls the camera preview pull in the same tick. Belongs on the
      pupil worker thread (it already has one). ⬜
- [ ] **#13 The encoder is software-timed** — single-sample `task.read()` in a
      Python loop paced by `time.sleep`. The PCIe-6363 has a timing engine;
      `cfg_samp_clk_timing(rate)` + continuous block reads gives exact hardware
      sample times, less CPU, no jitter. Speed is derived from `dt` per sample,
      so this is the change that most improves wheel-speed accuracy. ⬜
- [ ] **#14 Ring-buffer count cap drops the oldest item regardless of type**,
      so it can discard the zero-byte event samples the byte cap protects.
      512 items ≈ 1 s of writer stall. ⬜
- [x] **#15** `np.polyfit` in `_EncoderBase._report` benchmarked: 44 µs/call at
      120 Hz = 0.5 % of a core. ✅ **Do not "optimise" this.**

### Structure and hygiene

- [ ] **#16 Tests.** 🟡 Integration net exists (`tests/`, 80 checks, ~17 s:
      session+HDF5, module subsets, camera timing, console safety). Still
      missing the cheap pure-function unit tests: `fit_circle_taubin` /
      `fit_ellipse` / `fit_circle_robust`, `presets.readout_fps` interpolation,
      `RingBuffer` eviction, `_EncoderBase._derive` reset-rejection,
      `SaveConfig.stem/resolve` sanitisation. `toy_output/wheel.csv` is a real
      capture to use as the encoder fixture.
- [x] **#17 `MainWindow` was ~950 lines of six-way repetition.** ✅
      Now `modules.py`: one `ModuleAdapter` per subsystem owning its panel,
      plot, worker, display tick, sink and metadata. `main.py` 1256 → ~740
      lines and holds no per-instrument logic. Adding a seventh instrument is
      a subclass + two registry lines.
- [ ] **#18 Metadata is stringified** — `writer.py:84` writes `{k: str(v)}`, so
      `wheel_volts_per_rev` lands as `"4.912"`. h5py stores numeric attrs
      natively; analysis shouldn't have to parse. ⬜
- [ ] **#19 README stale in two places that matter**: pupil cam described as
      "Basler GigE (mock for now)" when it's a real USB3 acA1920 path; stage
      described as "never sends a motion command" when the panel now jogs,
      go-tos, homes, and runs a calibration that drives into hard limits. ⬜
- [ ] **#20 Dead code.** 🟡 `--exposure` and `_on_binning` are gone with the
      `main.py` rewrite. Remaining: unused `evals` in `fit_ellipse`, and
      `acqapp_phase1_2.tar.gz` is a checked-in build artifact. ⬜

### Found in passing (not in the original 20)

- [x] **C1 `UnicodeEncodeError` killed the camera worker on a non-UTF-8
      console.** ✅ `_query_timings` prints "shorten exposure to ≤N µs" on the
      **default** config; on cp1252 (Git Bash, piped stdout) that raises inside
      the acquisition thread, `PullWorker` reports it as a *device failure*,
      and the camera just never starts — blaming the hardware.
      Fixed by `console.enable_safe_console()` at all 14 entry points.
- [x] **C2 Tests were writing to real user state.** ✅ They repointed
      `acqapp_local.json` and overwrote the saved dock layout on every run.
      `QSettings` cannot be redirected on Windows (`setPath` is IniFormat-only;
      `setDefaultFormat` only affects the `QSettings(parent)` ctor), so
      `tests/_harness.isolate_user_state()` substitutes the class outright.
      **Casualty: the operator's saved dock layout was lost and is not
      recoverable.** The test now asserts the substitution is on the live path.

### Backup / process

- [x] **B1 Commit and push the six weeks of uncommitted work.** ✅ 2026-08-11,
      `79209fb`, 68 files, +10324/−1500. No experiment data or local config
      entered the repo (verified against `.gitignore` before staging).
- [ ] **B2 Consolidate the handoff docs.** 🟡 Done: the six handoff/transfer
      docs moved to [docs/](docs/) with an index, their 83 cross-links repaired
      (every one used an `acqApp/` prefix that resolved to nothing from inside
      `acqApp/`), and `HANDOFF.md`'s stale Status table flagged as superseded
      by §4 here. Still open: `SESSION_HANDOFF.md` is a single past session and
      would be better folded into `HANDOFF.md` than kept as a peer.

## 6. Next actions

1. **#2 — the silent overwrite.** Small, self-contained, and the last remaining
   way the app can destroy data the operator already collected. Add the
   existence check to `saving.writable_error()` and a test alongside
   `tests/test_session_recording.py`.
2. **#4 — panel settings persistence.** Directly affects data correctness: the
   wheel scaling constants reset to defaults every launch, and they are exactly
   the values still unmeasured on the rig.
3. **Small-fix batch: #11, #6, #7.** One-liners and missing guards, cheap to do
   together and each currently produces a wrong value or an unhandled
   `AttributeError`.

**Needs the rig (can't be closed from the laptop):**
- Phase 0's camera throughput number:
  `.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save`
  → the achieved MB/s sizes the ring buffer (#14) and confirms the SSD keeps up.
- Encoder `volts_per_rev` and wheel diameter — still unmeasured; gates #4's value.
- Whether the analog encoder voltage wraps (continuous-turn sensor).
- Real-hardware validation of *everything* in phases 2–4: no rig hardware has
  ever run this code.

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what's next.

### 2026-08-11 — workflow, test suite relocation, repo caught up
- Adopted this file as the cross-session plan; added `CLAUDE.md` (in the repo,
  plus a pointer at the parent) so a fresh session picks it up unprompted.
- Moved the four test scripts from scratchpad into `tests/` with real user-state
  isolation (C2) and a README of the conventions.
- **B1 done:** `79209fb` committed and pushed 68 files / +10324 lines — six
  weeks of work that existed only on this disk (§3).
- **B2 mostly done:** handoff/transfer docs → [docs/](docs/) with an index;
  83 cross-links repaired; `HANDOFF.md`'s stale status table flagged.

### 2026-08-10 — audit + first fixes
- Full read of ~10k lines → the 21 items in §5.
- Closed #17 (`modules.py` adapter architecture), #1 (camera timestamps, with a
  negative control proving the old behaviour was broken), #3 + #9 (puffer
  settings + the mid-pulse task race), C1 (console encoding, 14 entry points).
- Added `acq/clock.at()`, `Recorder/Writer.update_metadata()` so close-time
  facts (`cam_dropped_frames`, `recorder_dropped_samples`) reach the file.

---

## 8. How to keep this file useful

**At the end of every session, before the context runs out:**

1. Tick what actually got done in §5 — and only what was *verified*, with the
   test or the command that verified it.
2. Rewrite §6 "Next actions" (3 items max, ordered).
3. Add one dated entry to §7, newest first.
4. Update the **Last updated** date and the **Progress** figure in the header.
5. Note anything discovered that contradicts §2 or the README.

Do this as a *small* edit to this file, never a rewrite — the history in §7 is
the part that's expensive to reconstruct.
