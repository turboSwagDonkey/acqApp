# PLAN.md — the living plan for acqApp

**The single file that carries the project across sessions.** There is exactly
one; update it in place, don't fork it.

| | |
|---|---|
| **Last updated** | 2026-08-27 (be) |
| **What the app is** | [README.md](README.md) is the authoritative *description*; this file is the *plan*. |
| **Progress** | Phases 0–5 done, audit 22/22 closed, **suite 1118 checks / 27 files green**. `closed-loop` is a reconfirmed occasional flake — a cross-thread race in the test, not the app (`docs/SESSIONLOG.md` (ba)); rerun alone before trusting a red run. §6 item 1's number is in, still worse than assumed and still unexplained. §5b **A3** is the one open architecture item. |

---

## 0. Start here (fresh session orientation)

Read this, then §6. §5b is reference — consult the item you are working on.
Three archives hold the finished work so nobody reads it to get oriented:
[docs/DECISIONS.md](docs/DECISIONS.md) (closed items, kept for their reasoning),
[docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md) and
[docs/SESSIONLOG.md](docs/SESSIONLOG.md). **Open them only to chase a specific
item.**

**Run the suite before and after anything:**

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe c:\Users\User\Desktop\python\acqApp\tests\run_all.py
```

~65 s, no hardware, no windows. **1118 checks / 27 files is a whole run** — a
smaller count means something did not run. Use absolute paths: the shell usually
starts in this repo's parent, where the relative one fails obscurely. For one
test, run its script directly with `-q`; `run_all` selects by short name
(`routines`), not by filename.

**This machine IS the rig computer** (operator, 2026-08-17). Two consequences:
§2's actuation rule is live, not theoretical — an animal may be under the
objective; and "measure it at the rig" and "measure it here" are the same
errand, so anything §6 defers to a rig trip is doable now.

**Probe before concluding a device is absent.** This line twice read "the laptop
has no hardware" and was twice wrong. `probe_all` reports the ORCA, `Dev3
PCIe-6363` (wheel *and* puffer), COM54 (stage) and the DMD — everything but the
Basler. But a probe is *enumeration*: COM54 present is not a working link.

**Sibling projects are proven apps, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` already run on this hardware, and acqApp **shares their
config files** rather than duplicating state (`stage_control/config.json`,
`dmdGUI_project/dmd_config.json`). Look next door before writing a device path.

**The layout.** An instrument lives in two places and they are not duplicates:
`adapters/wheel.py` is how it plugs into THIS window, `devices/wheel/` is the
driver, worker, model and widgets. `main.py` → `adapters/` → `devices/`, and
nothing imports back; inside a device package `settings.py` is the model (**no
Qt**). The root is the shell, the acquisition core is `acq/`. README has the
long version, [docs/STRUCTURE.md](docs/STRUCTURE.md) the tree.

**`main.py` is the operator's file — ask before editing it.** The
settings-window work is theirs and ongoing, and don't restructure it: §5b A5
split `modules.py` out and left `main.py` alone deliberately.

**A warning that cost most of a session.** An editing pass aimed at "the
settings" rewrote every file with *settings* in its name, reducing
`devices/stage/settings.py` to stubs and gutting `DmdModule.metadata()`. Only
three of the five breakages failed a test. **If the stage or the DMD starts
behaving oddly, diff against `HEAD` before debugging** — a green suite did not
catch the one that mattered most.

**Two standing operator instructions:** write comments **terser than the
surrounding style** — the non-obvious *why* in a line, then stop; and **commit
freely, batch the pushes**.

**Gotchas that have each cost real time:**

- **Read `acqapp_local.json` before debugging any "it doesn't work".** It is
  what the app loads at launch, it is gitignored so it never appears in a diff,
  and it once held the whole answer to four sessions of work.
- **PowerShell 5.1 mangles quotes** passed to native exes — write the commit
  message to a file and use `git commit -F <file>`.
- The Bash tool's working directory is not always this repo. `cd` first.
- **Assign `qt_app()`.** A `QApplication` with no live Python reference is
  garbage-collected and widget construction then aborts natively — an exit
  code, no traceback, no output.
- Tests are plain scripts, **not pytest**, one process each. A new one follows
  [tests/README.md](tests/README.md): isolate user state, and include a control
  wherever the test could be vacuous.
- Python here is **3.14**, and **cv2 does install on it** (opencv-python
  5.0.0.93, a `cp37-abi3` wheel).
- Four diagnostic tools are run directly and never imported, so they look dead
  and are not. Check the docs before deleting anything "nothing imports".

**Experiment routines are the one feature whose purpose is to actuate.**
`routines/` is the protocol and the engine, both **Qt-free and
callable-driven**; `adapters/routines.py` is the only part that touches a
device. It is **always loaded** (`config.ALWAYS_ON`) and its panel is **its own
window** (`ModuleAdapter.own_window`) — both are declarations the shell reads,
so `main.py` still names no module. `routines/estimate.py` is the **only** place
frames become seconds and it names the rate it used. Read `engine.py`'s
docstring before changing any of it: an interrupted step's data is **kept and
marked**, and **Resume repeats that step**.

## 1. Goal

One PyQt6 app running six rig subsystems on **one shared session clock** into
**one HDF5 per session**, so every stream is analysable on a common timebase,
plus a closed loop that fires an output from one of those streams. All built and
mock-verified; the work now is making it trustworthy **on real hardware**, where
almost none of it is proven.

## 2. Ground rules

Invariants, not preferences — each has cost real time. The everyday list is in
[CLAUDE.md](CLAUDE.md); these two need their reasoning kept here, and are what
the rest of the repo cites "§2" for.

- **Ask before actuating anything physical.** Opening, configuring and uploading
  to a device are safe and reversible; **emitting light, firing the puffer and
  driving the stage are not** — this is an in-vivo rig and there may be an
  animal under the objective. The pattern that worked for the DMD: verify the
  whole path *short of* the actuating call (open → render → upload → release,
  which projects nothing), report that, and ask before the last step.
- **Commit before restructuring** — see §3. Currently the single biggest risk to
  the project.

The rest, one line each: installs go only into `acqApp/.venv`; mock-first, and
say plainly when a claim is mock-only; SOLID, judged against §5b's "what is
strong"; worker bodies stay inside the `PullWorker.run()` guard, because an
exception escaping `QThread.run()` aborts the process (PyQt6 `qFatal`); every
runnable entry point calls `enable_safe_console()` before its first print
(`tests/test_console_safety.py` enforces it); never commit experiment data.

## 3. Backup status

`turboSwagDonkey/acqApp` (private), branch `master`. This box is the rig, so a
commit here is already on the machine that runs it — push anyway, the remote is
the backup, and `.gitignore` means a commit cannot carry experiment data.
**Commit before restructuring, push before leaving**: this once drifted six
weeks and 68 files with a 950-line refactor on top.

## 4. Stages

**Phases 0–5 are done and mock-verified**; phase 0's throughput numbers are in
§6 item 1. Phase 6 (hardware sync — `DaqClock` on the PCIe-6363, triggered ORCA)
is future work and nothing depends on it. The per-phase table is in
[docs/SESSIONLOG.md](docs/SESSIONLOG.md).

## 5. Checklist — audit remediation

**All 22 closed** (2026-08-12) → [docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md).
The item numbers (#1–#20, C1–C3, B1–B2) are cited from §5b, the session log and
`tests/README.md`; look them up there.

## 5b. Architecture review — SOLID (2026-08-12)

About the shape of the code, where §5 was about wrong data. Numbered `A*` so the
two cannot be confused.

### What is strong — do not "improve" these

- **Open/Closed is real.** A new instrument is a `ModuleAdapter` subclass plus a
  registry line; the only module names in `main.py` are two colour lookups.
  Defend it in review.
- **Dependency inversion in `acq/`.** `Recorder` depends on `Writer` and
  `AbstractClock`, never on h5py or `perf_counter`, which keeps `DaqClock`
  (phase 6) a drop-in. Vendor SDKs are imported inside methods, not at module
  scope.
- **One adapter owning panel+plot+worker+sink+metadata is a deliberate trade**,
  not an SRP failure: SRP by *instrument* rather than by *concern*, and what
  killed the six-way repetition in the old `MainWindow`.
- **The DMD's coarse-stripe calibration.** Gray coding, checkerboards,
  homography fitting and the decode were all built, run at the rig, **failed and
  were deleted** — it is scattering in the sample and relay, so it does not
  improve with a coarser code. **Do not rebuild them without new evidence that
  fine patterns survive.** The measured numbers are in
  `devices/dmd/calibration.py`'s docstring.

### Open

**A1, A2, A4 and A5 are closed**, with their reasoning in
[docs/DECISIONS.md](docs/DECISIONS.md). **A3 is the one still open:**

- [ ] **A3 Mock/real selection is hard-wired inside each adapter.** Every
      `build_session`/`build_controller` imports both concrete classes and picks
      with `if emulate:`, so a test cannot inject a device — it has to
      monkeypatch `sys.modules`, which is exactly why `block_real_devices()`
      exists rather than a one-line fake injection (C3). **Triggered
      2026-08-18**, and not from the direction this item predicted: not a
      seventh *module* but a **third variant of one device**, when
      `VideoFileCameraWorker` made `PupilCamModule.build_session` an `if` chain
      over three concrete classes it imports itself. A
      `build_session(source_factory)` seam would have made it a one-line
      injection. Still not urgent, but the condition is met.

## 6. Next actions

**THE NEXT THREE THINGS**, per §8's own rule. Everything after them is reference
kept for its reasoning, not a queue.

1. **Find why the camera+writer path alone only sustains ~510 MB/s.**
   **Measured 2026-08-27, twice.** First at the ordinary Record button with
   all six streams running: 77 % of frames lost (693/3068, 513 MB/s). The
   plan was then to blame the other five device threads — but a second 30 s
   run with **only `voltage_cam` selected** (nothing else, no other live
   view) lost frames just as badly (949/4264, 508 MB/s). **Same number
   either way — it rules out multi-stream contention.** The ceiling is in
   the camera-read + writer path itself, under conditions the isolated bench
   (2464 MB/s, synthetic frames, no camera, no Qt event loop) never
   recreated. `WRITER_MBPS` now carries ~510 with this history in its
   comment. **Open**: prime suspects are the per-frame copy out of the DCAM
   driver buffer (`_skip_report` in `devices/voltage_cam/acquisition.py`
   names it) and the live preview holding the GIL on the main thread —
   neither is confirmed. Next step is profiling one recording with the live
   preview OFF (no GUI redraw) before reaching for DCAM's own `.dcimg`
   recorder (costs the one-file/one-clock invariant, DECISIONS.md item 7).

2. **Save a calibration and check it optically.** The sweep runs; what has not
   happened is anyone confirming where the light actually lands. Run Calibrate…,
   save the JSON, then draw one ROI on a landmark, project the mask, and look.
   **An affine has no keystone term**, so expect it best near the centre and
   worst at the edges — and no residual replaces looking.
   - **Preconditions**: voltage camera *running*, `dmdGUI_project` **closed**
     (one process owns the USB), illumination on.
   - **Read the residual first** (scatter of stripe centroids about a line, in
     camera px; single digits is good), then judge from `holdout_px` — refit
     without a stripe and predict it — which is the honest number. If it is
     poor, the known cause is that ~6 of 18 stripes run off the frame; a
     two-pass sweep would fix it for ~6 exposures and ~20 lines. Re-run first.
   - The ALP once refused to open and opened on an identical retry. **A single
     "not found or not ready" is not proof the DMD is absent.**

3. **Measure the wheel diameter** — the last unmeasured constant, and a ruler
   answers it. Until it is set the app reports rev/s and rev instead of mm/s and
   mm, and the closed loop's threshold has to be set in revolutions.
   `volts_per_rev` is a measured 4.912 and the sign is settled: **forward =
   rising voltage = positive speed and distance**.

**Everything below is the open tail, one line each.** The reasoning lives in the
doc named beside it, which is where to read before touching any of them.

**Experiment routines** (`routines/` + `adapters/routines.py`, 185 checks,
mock-verified):

- **`per_step` save mode does not roll files.** Both modes still write one
  session file with `/routine` boundaries. Rolling means re-entering
  `MainWindow._start_recording` mid-session — **main.py is the operator's file,
  ask first.**
- **`RoutineHooks.moving` is a seam nothing fills.** Arrival is `settle_s`
  (operator's answer). It is also why every time estimate is a floor: nothing
  times a stage move.
- **Its first real run is a rig trip.** A routine that places ROIs inherits a
  calibration nobody has checked optically, and **confirm that Resume repeating
  the step is what you want** — the one decision of six you did not state
  yourself (`routines/engine.py`).

**EyeLoop pupil tracking** — complete in the app. Measurements in
[docs/EYELOOP.md](docs/EYELOOP.md), how each step closed and the traps table in
docs/EYELOOP-INTEGRATION.md. **Read the traps before changing any of it** —
every one of them reports success while being wrong.

- **Step 8, the live Basler**, is all that is left of the build. Not an
  actuation: the pupil camera only looks.
- **GPL-3.0 is still the operator's call.** Nothing is vendored — a fresh
  machine needs the clone plus the patch diff, or `ACQAPP_EYELOOP_DIR`.
  Vendoring makes acqApp a derived work and **this repo is public**.
  `EyeLoopUnavailable` leaves the pupil camera as it was, so it blocks nothing.
- **The operator's tuned numbers are not written down** — they are in
  `acqapp_local.json` on this machine only. And **whether one threshold holds
  for a session** is the most consequential number that nothing measures.

**Older items** — closed ones, and the long-tail open ones — are in
[docs/DECISIONS.md](docs/DECISIONS.md) under "Still open".

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what was
learned. Older entries are in [docs/SESSIONLOG.md](docs/SESSIONLOG.md).

### 2026-08-27 (be) — isolating the writer number, a recording-timer bug, and Rate next to Exposure

- **The multi-stream theory in (bd) was wrong.** A second 30 s bin-1 run with
  only `voltage_cam` selected lost frames just as badly as the six-stream
  run (949/4264 vs 693/3068, both ~510 MB/s). Rewrote §6 item 1: it's the
  camera+writer path itself under the real app, not ring contention between
  streams — see the item for the two remaining suspects.
- **The recording readout counted from Live, not from Record.** `_on_tick`
  fed the session clock's own elapsed time straight into
  `_refresh_rec_readout`, so starting Record after Live had already been
  running for a while showed that whole head start as recording time.
  `MainWindow._rec_t0` now captures `self._sync.elapsed()` at Record, and the
  readout subtracts it (`main.py`).
- **`SettingsPanel` (voltage_cam) gained a Rate (Hz) field beside Exposure**,
  with a Link checkbox. Rate always caps Exposure's maximum to `1e6/rate` —
  a frame can't expose longer than its own period — regardless of Link;
  Link additionally drives Exposure to that cap (and back) so the two move
  together. Unlinked, Rate is just an operator-set ceiling. No new
  `AcqConfig` field: Exposure is still the one persisted value, Rate is
  derived from it at load.
- Suite 1118/27 (`closed-loop` reconfirmed flaky in isolation, see header).

### 2026-08-27 (bd) — committed a stray session's fixes, then measured the real writer number

- **Committed six files left uncommitted from the prior session**: puffer
  fire-order (don't log a puff that never happened), DMD reload guard (both
  real and mock controllers skipped clearing a stale frame on MODE_PATTERN
  with no file), the stage `_FrameWorker` moved off `QThread` onto
  `PullWorker` so a bug in `establish_frame()` reports instead of
  qFatal-aborting the process, and the DMD adapter re-adopting the sibling
  app's scale/rotation whenever it moves, not just on first install. Suite
  unchanged at 1118/27 both before and after — these were finished, tested
  work with nothing left to verify, just sitting.
- **§6 item 1 done, and it was worse than the plan expected.** The 30 s
  full-frame bin-1 recording was run at the ordinary Record button, which
  meant the full six-stream session, not the camera alone — 77 % of frames
  lost, `WRITER_MBPS` 1800 → **513**. The ring contention that number now
  documents is itself unfixed; see the rewritten item 1.

### 2026-08-27 (bc) — the routines panel, and a window of its own

The operator's four asks, then two more. Suite 1049 → **1118 checks / 27 files**.

- **Progress is position-based**, not `steps_done()` — Resume repeats a step,
  and a bar that went backwards would read as a fault. Drawn as a bar plus
  elapsed/left, and the **row header carries the ▶ marker**, which survives a
  scrolled table where the bold row does not.
- **One `StepTable.move_row`** behind drag, Ctrl+Up/Down and the arrows. Qt's
  `InternalMove` shuffles the *cells*: the table would look reordered while the
  engine ran the old protocol.
- **`estimate.py` converts frames to seconds and names the rate**; recording
  still never does. `ModuleHost.frame_rate_hz()` supplies it, pooled by `_first`
  like `stage_target` — 6 lines of main.py, **operator approved**.
- **`templates.py`: a folder of JSON, not another config key**, so a protocol
  copies to the rig machine. `isolate_user_state()` redirects it (an unisolated
  run would delete from the operator's library) and `test_structure`'s SKIP_DIRS
  ignores it like `sessions/`.
- **`config.ALWAYS_ON` + `ModuleAdapter.own_window`.** Always-on is enforced in
  three places — no checkbox, `selected()` re-adds, `set_modules` re-adds —
  because any one alone leaves a way to drop it. `PanelWindow.release()` runs
  before the window is deleted: a `QScrollArea` owns its widget and would
  otherwise take the adapter's panel with it.
- **Then this file and `acqApp/CLAUDE.md` were compressed** (§8's budget), and
  the testing instruction in CLAUDE.md was corrected: `run_all` selects by short
  name and ignores `-q`, which belongs to the individual test script.

## 8. How to keep this file useful

At the end of every session, before the context runs out:

1. Tick only *verified* work, with the command that verified it.
2. Rewrite §6 (3 items max, ordered).
3. Add one dated §7 entry, newest first, **and move the oldest out in the same
   commit** if that makes four — archiving later is what never happens.
4. Update the header's **Last updated** and **Progress** (the check count).
5. Note anything that contradicts §2 or the README. An invariant that has
   quietly stopped being true is worse than no rule.
6. Refresh **§0** if what a fresh session needs has changed — a new gotcha that
   cost you time, and above all anything left **uncommitted**.
7. If anything moved, was renamed or was added, update
   [docs/STRUCTURE.md](docs/STRUCTURE.md) in the same commit;
   `tests/test_structure.py` fails the suite if you don't.

Make it a *small* edit, never a rewrite — the reasoning in §7 is the expensive
part to reconstruct.

**The size budget: ~350 lines, ~4.7k tokens** — which is where it sits now, so the next §7 entry means archiving one. Check with `wc -l` when you edit
this file: it is read in full at the start of every session, so every line is
paid for again on every future one, and a soft "aim to stay short" is what let
it reach 818 lines. When it is over, archive finished §7 entries and cut
anything a `docs/` file already says in full — **§7 holds the newest ~3
sessions**. Move finished work out rather than trimming detail; the detail is
the value.

**Numbering is load-bearing.** 27 places in the code and docs cite these
sections by number — `devices/dmd/sweep.py` and `calibration.py` cite **§2** for
the actuation rule, six files cite §6, five cite §7. **Add a "§5b"-style suffix
rather than renumbering**, or update every reference in the same commit.
