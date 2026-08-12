# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-12 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–4 done; **audit remediation 95 %** (21 of 22 closed; only #5, the DMD stub, is left) |

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
- [x] **#2 A recording could silently overwrite an existing file.** ✅
      Two independent defences: `SaveConfig.resolve(unique=True)` returns the
      next free `_001`/`_002` name (auto-number rather than refuse — the Record
      button must work with an animal on the rig), and `HDF5Writer.open()` now
      uses mode `"x"`, so any caller that skips that raises `FileExistsError`
      instead of truncating. `main._start_recording` handles it and un-toggles
      Record. The Save tab previews the numbered name before you press it.
      Guarded by `tests/test_save_paths.py` (21 checks).
- [x] **#3 Puffer channel and Test-puff duration were ignored.** ✅
      Panel now emits `settings_changed`; `PufferController.apply_settings()`
      re-opens the DO task on a channel change under a lock; `fire()` takes the
      panel duration. Recorded as `puffer_channel` / `puffer_duration_s`.
- [x] **#4 All 7 panels persist their settings.** ✅
      Wheel, pupil, puffer, stage and DMD now load from `acqapp_local.json` and
      save on every edit. The wheel's V/rev and diameter used to reset each
      launch and were then written into the session file as though measured.
      Deliberate exclusions: the eye-tracking LED (runtime state — restoring it
      would switch the illumination on in an empty rig) and the stage's axis
      calibration (belongs to the shared `stage_control/config.json`; only
      `port`/`poll_hz` are the panel's own). Guarded by
      `tests/test_settings_persistence.py`, which edits all seven panels,
      closes the window and reads them back from a second one.
- [ ] **#5 The DMD is a print-only stub presented as real hardware.** 🟡
      `apply_settings()` added; the device calls in `dmd/control.py:61-97` are
      still `print(...)`. With Emulate off, Display does nothing and the UI
      gives no sign. `alp4lib` is in the venv, so the ALP path is reachable.

### Bugs and robustness

- [x] **#6** `stage/control.py` — `move_to_um`/`jog_um` raise
      `StageControllerError` when disconnected, like the other methods. ✅
      Every `self._dev` use in the file is now guarded.
- [x] **#7** `stage/settings.py:save_axis_updates()` — a missing or corrupt
      calibration file no longer raises. It starts from an empty config and
      writes; the old contents (if any) stay in the `.bak`. Both callers mutate
      the live axes *first*, and `establish_frame()` gets there only after
      minutes of driving into both hard limits, so raising there threw the
      measurement away. ✅ Both guarded by `tests/test_stage_state.py`, which
      repoints `config_path()` at a temp file and asserts the operator's real
      calibration was never written.
- [x] **#8** `voltage_cam/acquisition.py` — a failing `wait_for_frame` now
      backs off and reports. ✅ Timeouts and device errors arrive as the same
      exception, so they are told apart by *how long the call took*: a wait
      that fails in well under its timeout is an error (pause, then retry),
      one that uses its timeout is a trigger that hasn't fired (already paced,
      say nothing). Measured 8 retries/s against ~5.5 M/s unpaced.
- [x] **#9** Puffer pulse thread could write to a task closed mid-sleep. ✅
      (fixed with #3: `_task_lock` + identity re-check before the trailing write)
- [x] **#10** Samples that miss the file are now all counted. ✅ `Recorder`
      gates `put()` behind a lock that closes before `remaining` is measured,
      so a straggler is either in the buffer (un-drained) or counted as late —
      never silently discarded. Pre-clock samples get their own counter too.
      All three land in the file (`recorder_dropped_samples`,
      `recorder_late_samples`, `recorder_unstamped_samples`) via a
      `final_metadata` callback that `stop()` runs after the drain and before
      the close — the counts are only final at that one moment.
- [x] **#11** `voltage_cam/settings.py` — `get_config()` now carries `link`
      through from the config the panel was built with. ✅ It has no widget, so
      dropping it reverted a USB3 rig to the CoaXPress readout table and every
      pre-Start fps estimate read ~7× high (115 vs 15.7 at full frame).

### Performance

- [x] **#12 Pupil tracking moved off the GUI thread.** ✅
      `pupil_cam/track_worker.py`: the tracker gets its own thread, is the sole
      consumer of the camera worker's frames, and republishes each frame *with*
      the fit made from it — so the overlay can no longer drift a frame away
      from the image under it, which pulling the two separately would allow.
      Panel edits are queued and applied between frames, never written into the
      tracker from the GUI thread. Measured: 20 display-side reads in 0.03 ms
      while a 150 ms/frame tracker was running; the same tracker called inline
      (what the display tick used to do) costs 150 ms *per tick*. Guarded by
      `tests/test_pupil_tracking_thread.py` (17 checks, with that control).
- [x] **#13 The encoder is hardware-timed.** ✅ `cfg_samp_clk_timing` +
      continuous block reads, so the sample interval is the board's 100 MHz
      divider rather than a `time.sleep` on a thread competing with the GUI.
      Since speed is a slope, that jitter *was* the wheel speed. Block reads
      then reintroduce the camera's batching problem (#1), so the first block
      anchors index 0 into the perf_counter domain and every sample after it is
      `anchor + i/rate`; the sink carries that instant to `Recorder.put(at=)`.
      Measured against a fake board: ±90 ms of arrival jitter, and the recorded
      intervals exact to 0.1 ns. A board that refuses the timing configuration
      falls back to the old paced loop rather than losing the wheel for the
      session, and `wheel_timestamp_source` / `wheel_rate_actual_hz` say in the
      file which timebase and rate the run actually got. Guarded by
      `tests/test_encoder_timing.py` (19 checks, with the arrival-time control).
- [x] **#14 Ring-buffer count cap now sheds frames first**, like the byte cap,
      and only drops a zero-byte event once nothing sized remains. ✅ It was
      discarding exactly the sparse puff/DMD events the byte cap goes out of
      its way to protect, and at 512 items ≈ 1 s of writer stall it is the cap
      that bites first. The test carries a control reproducing the old rule.
- [x] **#15** `np.polyfit` in `_EncoderBase._report` benchmarked: 44 µs/call at
      120 Hz = 0.5 % of a core. ✅ **Do not "optimise" this.**

### Structure and hygiene

- [x] **#16 Tests.** ✅ `tests/` is now **272 checks in ~27 s** across 12 files.
      The pure-function gap is closed: `test_pupil_fits` (the three fits, each
      against a control that fails the property it was chosen for),
      `test_readout_fps` (table, log-log interpolation, clamps, binning) and
      `test_encoder_derive` (a synthetic wheel past two controls that both lose
      all 9 revolutions). `RingBuffer` eviction was covered by #14's test.
      A real `toy_output/wheel.csv` capture is still the better encoder fixture
      once the rig confirms whether the voltage wraps — the synthetic trace
      assumes it does.
- [x] **#17 `MainWindow` was ~950 lines of six-way repetition.** ✅
      Now `modules.py`: one `ModuleAdapter` per subsystem owning its panel,
      plot, worker, display tick, sink and metadata. `main.py` 1256 → ~740
      lines and holds no per-instrument logic. Adding a seventh instrument is
      a subclass + two registry lines.
- [x] **#18 Metadata keeps its own type.** ✅ `writer.attr_value()` passes
      ints, floats, bools, strings and arrays through to h5py and `str()`s only
      what HDF5 can't hold; `None` becomes `""` (HDF5 has no null, and 0.0
      would be indistinguishable from a measured zero). `emulated` used to read
      back as `"False"` — which is truthy. Type checks in
      `tests/test_session_recording.py`.
- [x] **#19 README brought back in line with the code.** ✅ Six subsystems, not
      five; pupil cam is the real USB3 acA1920 path, not "GigE (mock for now)";
      the stage jogs/go-tos/homes and drives into hard limits to calibrate,
      rather than "never sends a motion command". Also added: what has actually
      run on hardware (almost nothing), settings persistence, the Save tab and
      auto-numbering, the four loss counters, native attribute types. The
      roadmap now points at PLAN.md instead of drifting from it.
- [x] **#20 Dead code.** ✅ `--exposure` and `_on_binning` went with the
      `main.py` rewrite; the unused `evals` in `fit_ellipse` is now `_`, and
      `acqapp_phase1_2.tar.gz` (a June snapshot, `.pyc` files and all) is out of
      the tree — `*.tar.gz` / `*.zip` are gitignored so the next one doesn't
      land. It is still in history if it is ever wanted.

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

1. **#5 — the DMD stub.** `dmd/control.py:61-97` is still `print(...)` where
   the device calls belong, and with Emulate off the UI gives no sign that
   Display did nothing. `alp4lib` is in the venv. Either wire the ALP path or
   make the panel say plainly that it is a stub — the last item on this list
   that actively misleads the operator mid-experiment. (The README now says so;
   the app itself still doesn't.)
2. **B2 — fold `SESSION_HANDOFF.md` into `HANDOFF.md`.** The last of the doc
   consolidation: it is a single past session kept as a peer of the standing
   handoff. Small, and it removes the last "which of these do I read?".
3. **Phase 5 — closed-loop**, once the rig has confirmed #13 and the DMD is
   real: trigger the DMD or the puffer from encoder state. The trigger bus
   (`SyncController.schedule_trigger`) already exists and the puffer already
   fires from it on a timed schedule; what is missing is a condition on the
   live wheel speed rather than on the clock.

**Needs the rig (can't be closed from the laptop):**
- Phase 0's camera throughput number:
  `.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save`
  → the achieved MB/s sizes the ring buffer (#14) and confirms the SSD keeps up.
- Encoder `volts_per_rev` and wheel diameter — still unmeasured. The panel now
  keeps whatever is typed in, so measure once and it stays measured.
- Whether the analog encoder voltage wraps (continuous-turn sensor). The
  synthetic trace in `test_encoder_derive` assumes it does; if it doesn't, the
  reset rejection is unnecessary rather than wrong.
- **#13 on a real board:** the first session should report
  `wheel_timestamp_source = "hardware"` and a `wheel_rate_actual_hz` at or very
  near the requested rate. `"software"` means the 6363 refused
  `cfg_samp_clk_timing` on that channel — the run is still valid, but the speed
  carries scheduler jitter and the printed reason is in the console.
- Real-hardware validation of *everything* in phases 2–4: no rig hardware has
  ever run this code.

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what's next.

### 2026-08-12 (f) — #13: the encoder comes off the board's clock
- `cfg_samp_clk_timing` + continuous block reads replace the single-sample
  `task.read()` loop. The old pacing put the GUI scheduler's jitter directly
  into wheel speed, because speed is a slope over `dt`.
- Block reads bring back #1's problem, so the fix is #1's: anchor the first
  block into the perf_counter domain, space the rest by the board's rate, and
  carry that instant to `Recorder.put(at=)`. Against a fake board with
  deliberately irregular reads: ±90 ms of arrival jitter, recorded intervals
  exact to 0.1 ns. `test_encoder_timing` (19 checks) uses those arrival times
  as its control.
- A board that refuses the timing config falls back to the paced loop — losing
  the wheel for a session is worse than a jittery timebase — and the file says
  which it got (`wheel_timestamp_source`, `wheel_rate_actual_hz`), the same
  admission the camera makes. The session test asserts the mock reports
  `software` rather than quietly implying a hardware clock.
- Suite 272 → **294 checks / 13 files / 29.7 s**.
- **Audit remediation is done except #5.** Everything left is either the DMD
  stub, doc tidying (B2), or needs the rig.

### 2026-08-12 (e) — #12 tracking gets a thread, #16 the pure-function tests, #20
- **#12**: `pupil_cam/track_worker.py`. The tracker now owns a thread, is the
  only consumer of the camera worker's frames, and publishes each frame
  *together with* its fit — which also fixes something nobody had noticed: read
  separately, the overlay and the image under it could be a frame apart. Panel
  edits are queued and applied between frames. 20 display-side reads in 0.03 ms
  against a 150 ms/frame tracker; inline, that was 150 ms of frozen GUI *per
  tick*, and the voltage camera's preview shares that tick.
- **#16 closed**: `test_pupil_fits` (28), `test_readout_fps` (18),
  `test_encoder_derive` (14), `test_pupil_tracking_thread` (17). Suite
  195 → **272 checks / 27.1 s / 12 files**, all passing.
- Writing the encoder test made the reset rule concrete: the plain half-turn
  unwrap doesn't *mis*-count a smeared reset, it misses it entirely — both
  controls lose all 9 revolutions of a 6 s run and report ~0.
- **#20 closed**: unused `evals` in `fit_ellipse`; `acqapp_phase1_2.tar.gz` (a
  June snapshot with `.pyc` files) out of the tree, `*.tar.gz`/`*.zip` ignored.
- Left alone deliberately: `_toy.py` still tracks on its GUI timer. It is one
  device on one timer with nothing else to stall, and it is the harness used to
  bring the camera up on the rig — worth keeping as the simple path.

### 2026-08-12 (d) — #18, #19: the file and the README stop lying
- **#18**: `writer.attr_value()` — attributes keep their own type, `str()` only
  for what HDF5 can't hold, `None` → `""`. `emulated` had been reading back as
  the string `"False"`, which is truthy. Type assertions added to the session
  test (66 checks there now; suite 195 / 22.5 s).
- **#19**: README said five subsystems, a GigE mock pupil camera and a stage
  that "never sends a motion command" — the stage drives into hard limits to
  calibrate. Also documented what has actually run on hardware (almost
  nothing), settings persistence, the Save tab and auto-numbering, the four
  loss counters and native attribute types. Roadmap now defers to this file.
- The README's honesty about the DMD stub raises #5 in priority: the document
  now says the DMD does nothing with Emulate off, but the app still doesn't.

### 2026-08-12 (c) — the robustness bugs: #14, #10, #8
- **#14**: the ring buffer's count cap was dropping the oldest item outright,
  discarding the sparse zero-byte events the byte cap protects. Both caps now
  shed frames first. The test carries a control that reproduces the old rule.
- **#10**: `Recorder` now counts every sample that misses the file — shed,
  late (arrived after close), and unstamped (before the clock started) — and
  writes all three as attributes via a `final_metadata` callback that
  `stop()` runs between the drain and the close. Closing the put-gate before
  measuring `remaining` removes the window where a straggler vanished.
- **#8**: a `wait_for_frame` that fails in well under its timeout is a device
  error (back off, report); one that uses its timeout is a trigger that hasn't
  fired (already paced, stay quiet). Measured 8 retries/s vs ~5.5 M/s unpaced.
- `test_recording_losses` (22 checks). Suite 187 checks / 22.1 s.
- **Open question for the rig:** #10's counters are now honest, so the first
  real recording will say plainly whether the writer keeps up. If
  `recorder_dropped_samples` is nonzero at full frame rate, that is the ring
  buffer sizing (#14's `RING_FRAMES`/`RING_BYTES`) needing the Phase 0 MB/s
  number, not a bug.

### 2026-08-12 (b) — #4, panel settings persistence
- All five bare panels now load and save through `config`. Two panels had no
  change signal to hang it on: the pupil panel got one, and the DMD's was
  declared but never emitted. The stage's port/poll-rate widgets were silent
  too, so nothing downstream ever heard about a port change.
- Wheel spinboxes moved from `editingFinished` to `valueChanged`: the spin
  arrows don't count as "editing finished", so a V/rev nudged with the arrows
  never reached the running worker either.
- DMD: a restored pattern is shown (and reported *missing* if the file has
  gone), and gets uploaded to the controller on build — which also fixes the
  pattern being silently lost when Emulate is toggled.
- `test_settings_persistence` (47 checks) does a real restart: edit all seven
  panels, close, rebuild, read back. Suite 163 checks / 19.1 s.

### 2026-08-12 (a) — the data-destruction item, plus the small-fix batch
- **#2 closed.** Recordings can no longer overwrite each other: `resolve(
  unique=True)` picks the next free `_001` name and `HDF5Writer` opens with
  mode `"x"` so the truncating path no longer exists. Two layers on purpose —
  the app should never *fail* to record either.
- **#11, #6, #7 closed** (camera `link` dropped on the way out of the panel;
  stage motion while disconnected; calibration save over a missing/corrupt
  file). Each was small; each produced a wrong number or an unhandled
  exception on the rig rather than in the lab.
- Two new tests, `test_save_paths` (21 checks, no Qt) and `test_stage_state`
  (14 checks). Suite is 116 checks / 17.0 s, all passing.
- `test_session_recording` now asks the window where it recorded
  (`win._rec_path`) instead of re-resolving the template — the old form named a
  different file if it ran across a second boundary.

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
