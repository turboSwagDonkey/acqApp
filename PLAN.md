# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-26 (ay) |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done; audit remediation 100 % (22 of 22). The pupil tracker was retired 2026-08-24 into `archive/pupil_tracking/`, eye region kept **2026-08-24: DMD calibration is built, wired and has run at the rig** — a narrow stripe stepped across each axis, two line fits, an affine. Gray coding was tried and deleted; this relay scatters enough to erase any periodic pattern (§7). **2026-08-25: full-frame bin 1 records complete** — the writer's direct-chunk path (1304 → 2696 MB/s) and a 2 GB ring ended the 53 %-of-frames loss, in the bench; it has not run against the camera, which is §6 item 1. Also 2026-08-25: **instruments load and unload without restarting** (sidebar → Modules), and the sidebar carries one item per settings page. **2026-08-26: experiment routines are built and wired** — a protocol of stage positions and DMD patterns executed step by step, engine Qt-free over callables, loadable as a module. Mock-verified only; the per-step *file rolling* is the one piece deferred (§6). Suite **912 checks, 25 files — all green**. §5b **A3 triggered** and is the one open architecture item. Next: §6 — the three rig measurements, none of which can be done from a keyboard. |

---

## 0. Start here (fresh session orientation)

Read this section, then §6 (next actions). That is the whole of what a fresh
session needs. §5b is reference — consult the item you're working on. Three
archives hold the finished work and exist so nobody reads it to start:
**[docs/DECISIONS.md](docs/DECISIONS.md)** (closed items, kept for their
reasoning), [docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md) (the closed audit)
and [docs/SESSIONLOG.md](docs/SESSIONLOG.md) (older §7 entries). **Open them
only when chasing a specific item number or an old decision.**

**Where the project stands.** Phases 0–5 are built and mock-verified and the
2026-08-10 audit is closed. **Phase 0 closed 2026-08-17** with the camera
throughput number, so the roadmap is clear through phase 5. The test suite is
the contract: **934 checks, 26 files, ~63 s**, and it is ALL GREEN. Run it
before and after anything.

**A pupil tracker is back on master — EyeLoop, since 2026-08-26** (operator's
call, superseding the 2026-08-24 retirement). `devices/pupil_cam/` now has
`eyeloop_tracker.py` (the only file that touches EyeLoop) and `tracking.py`
(settings + a frame in, a `PupilFit` out). **The hand-rolled tracker stays
retired** in `archive/pupil_tracking/`; do not restore *that* one.

**EyeLoop is GPL-3.0 and none of it is vendored.** It is imported from a clone
at `../eyeloop`, which is **not in any repo** — so a fresh machine needs
`git clone` + `git apply docs/eyeloop-3.14-patches.diff`, or
`ACQAPP_EYELOOP_DIR` pointed at one. Without a clone the tracker raises
`EyeLoopUnavailable` and **the pupil camera works exactly as before** — that
is a tested contract, not a hope. **This repo is public**, so vendoring EyeLoop
would be distribution and would make acqApp a derived work. That decision is
still open; see docs/EYELOOP-INTEGRATION.md.

**Tracker work happens on the `pupil-tracking` branch, not here** (2026-08-24,
operator's request: somewhere a colleague can try changes separately). It has
the tracker live and green — 830 checks, 24 files — and **`PUPIL_TRACKING.md`**
is its entry point, written for someone who has never seen the repo. Master's
decision is unchanged; the branch merges back by pull request. **EyeLoop was tried on
2026-08-26 and it works** — 151/151 on both clips, nothing integrated,
[docs/EYELOOP.md](docs/EYELOOP.md). It lands on that branch, not here, and it
is **GPL-3.0**, so it stays a sibling clone until the operator says otherwise.

**There are SIX pupil clips on `E:`, not one** (found 2026-08-22 — four sessions
measured the first and generalised from it). Still worth knowing, since they are
the only real footage on this machine:

    E:\pAce\VF203.2R\20260701\{FOV1_T1,FOV1_T2,FOV1_T3,FOV2_T1,FOV2_T2}\*_Pupil.avi
    E:\State\VF182.6B\20260709\FOV1_T1\FOV1_T1_Pupil.avi

All 1928x1208 IYUV, 151 frames, 15 fps, and **not equivalent**. `pAce` FOV1_T1
is the easy one every earlier number came from (frame median 49). `State`
FOV1_T1 is the **operator's** — median 37, 61 % below threshold 60, the eye a
low-contrast almond whose pupil barely separates from the iris. **Measure any
pupil change on both.** No clip contains a blink.

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py
```

Use the **absolute** path to that interpreter. The shell usually starts in
`Desktop\python` (the *parent* of this repo), where `.venv\Scripts\python.exe`
resolves to nothing and Python reports a baffling "the module '.venv' could not
be loaded". Python here is **3.14** — no cv2 wheels exist for it, which is why
the archived tracker is hand-rolled numpy and `avi.py` reads RIFF by hand.
(**Retired 2026-08-26**: opencv-python 5.0.0.93 ships a `cp37-abi3` wheel and
installs on 3.14. It is in `.venv` now, suite still green. `avi.py` still works
and is still what reads the clips — it is no longer *forced*. docs/EYELOOP.md.)

**Sibling projects are proven code, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` are standalone apps that already work on this hardware, and
acqApp *shares their config files* rather than duplicating their state — the
stage's calibration in `stage_control/config.json`, the DMD's ALP path and
optical alignment in `dmdGUI_project/dmd_config.json`. Look next door before
writing a device path from scratch: `devices/dmd/alp.py` is a port of
`dmdCommandLine.py`, and it is why #5 took one session instead of several.

**Practical gotchas that have each cost real time:**
- **Read `acqapp_local.json` before debugging any "it doesn't work".** It is
  what the app loads at launch and it is gitignored, so it never appears in a
  diff — and on 2026-08-22 it held the whole answer to four sessions of pupil
  work. The operator's live settings are not the shipped defaults.
- **PowerShell 5.1 mangles quotes** passed to native executables. Write commit
  messages to a scratch file and use `git commit -F <file>` — a `-m` with an
  apostrophe or an embedded quote gets re-tokenised and git sees a bogus
  pathspec.
- The Bash tool's working directory is not always this repo. `cd` first.
- **A `QApplication` with no live Python reference is garbage-collected**, and
  widget construction then aborts natively — an exit code, no traceback, no
  output at all. Assign `qt_app()`; don't just call it.
- **A package `__init__` that eagerly re-exports pulls its heaviest submodule
  into every import of the package.** `closed_loop/`, `routines/` and `saving/`
  re-export lazily (PEP 562) so their Qt-free halves stay Qt-free.
- **An import-graph search is the wrong test for a *script*.** Four diagnostic
  tools here are run directly and never imported, so they look dead and are not
  — see §7 (q). Check the docs before deleting anything that "nothing imports".
- Tests are plain scripts, **not pytest**, and each runs in its own process.
- When adding a test, follow the two conventions in
  [tests/README.md](tests/README.md): isolate user state, and include a control
  wherever the test could be vacuous.

**`_SIGN` is settled** (operator, 2026-08-19): **a mouse running forward reads
positive, and the encoder voltage ramps UP as it does.** So `_SIGN = +1.0` was
right and three *fixtures* were wrong — they generated a falling ramp and
asserted in a docstring that "the rig's forward direction is the falling one".
**The rig fact to remember: forward = rising voltage = positive speed and
distance.**

**Why the tracker was retired, in one line:** the operator's "jitter and
dropouts" were `edge_select="strongest"` and `smooth_sigma=12` in their saved
settings — each alone takes a 151/151 clip to ~45/151 (§7 (ah)). They chose to
retire it anyway rather than re-tune. **The numbers are in
`archive/pupil_tracking/README.md`**, not here.

**PICK UP HERE — the EyeLoop integration is half done.** The model and the
seam are in and tested (`test_pupil_eyeloop`, 22 checks, both rig clips at
151/151 through the app path). **What is NOT done: the panel.** Nothing draws
the fit on the preview, no controls set the tracking or corneal-reflection
knobs, nothing records the trace into the session file, and tracking has not
been moved off the GUI thread. Steps 5–8 of
[docs/EYELOOP-INTEGRATION.md](docs/EYELOOP-INTEGRATION.md) — read that first.

**The other open question — one 30 s recording answers it.** Full-frame
bin 1 now keeps 100 % of its frames in the bench (2026-08-25); nothing has run
it with the camera in the loop, and `WRITER_MBPS = 1800` is a derate, not a
measurement. Record 30 s at bin 1, count the frames off the closed file,
replace the number. §6 item 1. **Nothing about this actuates** — it is the
Record button.

**Experiment routines are built** (2026-08-26) and are the newest thing here.
`routines/` is the protocol + the engine, both **Qt-free and callable-driven**,
`adapters/routines.py` the only part that touches a real device. Read
`routines/engine.py`'s docstring before changing any of it — the two decisions
the plan had left open are settled in there: an interrupted step's data is
**kept and marked**, and **Resume repeats that step** as a fresh attempt.
**Nothing about it has actuated anything**: it is mock-verified only, and its
first real run is a rig trip. One piece is deliberately unbuilt — `per_step`
save mode is modelled and validated but does not roll files yet (§6).

**Calibration also works and has run at the rig.** DMD tab → Photostimulation
ROIs → **Calibrate…** → one button, 19 exposures, ~9 s. It steps a narrow
stripe across each axis, fits a line to where each lands, and writes an affine.
Then draw an ROI and check optically where the light falls — no residual
replaces that.

**The rig's measured optical facts** — the first ones this project has:

| | |
|---|---|
| DMD centre in camera | **~(2040, 1370)** of a 4432x2368 frame (bin 1) |
| scale | **5.87 px/mirror in x, 5.91 in y** — near isotropic |
| rotation | **DMD-x at ~+89°** — the panel is mounted turned ~90° to the camera, so an axis-aligned ROI maps to an axis-aligned mask |
| field | the DMD **overfills the camera**; it sees mirrors x≈235–730, y≈0–735 of 1024x768, so ~6 of 18 stripes run off the frame each run |
| **fine patterns do not survive** | a solid bar images cleanly; a 280 px checkerboard modulates **13 %** of the frame and a 70 px stripe pattern **9 %** |

An earlier table here quoted 4.36/5.45 px/mirror, 20 % anisotropy and +1.1°.
**Those were wrong** — from a probe that grew centred bars, whose centroids the
frame clipped on one side and vignetting ate on the other (§7).

**That last row is the important one and it is why the code looks the way it
does.** It is scattering in the sample and relay, not defocus — it does not
improve with a coarser code. Gray coding, checkerboards, homography fitting and
the decode were all built, run at the rig, failed, and **deleted** (§7 (al)).
Do not rebuild them for this rig without new evidence that fine patterns survive.
`calibration.py` is 488 lines and has one entry point, `calibrate()`.

The rest of this section is context; nothing below is blocking.

**The camera numbers. Nothing in the path is the constraint any more.** The
grab path runs at 92 % of the link (105.92 fps / 2223 MB/s at full frame,
2026-08-17). The writer used to cap recording near 48 fps — a bin-1 session
stored 52.9 % of its frames — and **that is fixed as of 2026-08-25**: the
direct-chunk write took it 1304 → 2696 MB/s and the ring 512 MB → 2 GB, so
**full-frame bin 1 records complete in the bench** (100 % of 106 fps offered,
2464 MB/s saturated). 2×2 binning is no longer a requirement, only a way to
use a quarter of the disk.

**But this has not run against the camera.** `WRITER_MBPS` is 1800, the bench
derated by a guessed 0.77 for the ORCA grab thread. That number is the one
thing here that is not measured — see §6.

**The disk was never the constraint, and a note in DECISIONS.md said it was
"already at a hardware limit" for a week.** D: writes 2700 MB/s from a plain
file. The 1004 was one line of Python. Measure the floor before calling
something floor-bound.

**Two standing instructions from the operator:**
- **Write comments terser than the surrounding style** — the non-obvious *why*
  in a line, then stop. §6 item 5 has the rule in full.
- **Commit freely, but batch the pushes** — one at the end of a chunk of work,
  not after every commit.

**The layout, since it is the thing newcomers ask about.** An instrument appears
in two places and they are not duplicates:

    adapters/wheel.py   the ADAPTER — how it plugs into THIS window
    devices/wheel/      the DEVICE  — driver, worker, model, widgets

`main.py` → `adapters/` → `devices/`, and nothing imports back. Inside a device
package, `settings.py` is the model (**no Qt**) and `panel.py` its widgets.
`closed_loop/` and `saving/` follow the same shape. The root is the shell —
`main`, `config`, `console`, `dialogs`, `probe`, `style` — with the acquisition
core (clock, recorder, ring, worker, writer, `sync`, `devices` protocols) in
`acq/`. README has the long version.

**`main.py` is the operator's active file.** The settings-window work is theirs
and ongoing: **ask before touching its dock/settings code**, and don't
restructure it (that is why §5b A5 split `modules.py` and deliberately left
`main.py` alone; it is 889 lines).

**A warning that cost most of a session.** An editing pass aimed at "the
settings" rewrote every file with *settings* in its name, reducing
`devices/stage/settings.py` to stubs — `load_settings()` returned hardcoded
defaults instead of reading `stage_control/config.json`, and
`save_axis_updates()` became a `pass`. It also deleted `SettingsDialog._PAD` and
most of `DmdModule.metadata()`. Only three of those five broke a test. **If the
stage or DMD starts behaving oddly, diff against `HEAD` before debugging** — a
green suite did not catch the one that mattered most.

## 1. Goal

A single PyQt6 app that runs and records six rig subsystems (voltage cam, pupil
cam, wheel, puffer, XY stage, DMD) against **one shared session clock**, into
**one HDF5 file per session**, so every stream is analysable on a common
timebase, plus a closed loop that fires an output from one of those streams.
All of that is built and mock-verified. The work now is making it correct and
trustworthy **on real hardware**, which is where all of it is still unproven.

## 2. Ground rules

These are invariants, not preferences. Breaking one has cost real time before.

- **Installs go ONLY into `acqApp/.venv`.** The bootstrap in `main.py` enforces
  this; never pip-install into another interpreter.
- **Mock first.** Every change must pass `tests/run_all.py` in Emulate mode
  before it goes near the rig. Real-hardware-only claims get flagged as such.
- **This machine IS the rig computer** (operator, 2026-08-17), which resolves
  three sessions of confusion — the devices are here because *here is the rig*.
  Two consequences outrank the rest of this section. **The actuation rule is
  live, not theoretical**: an animal may genuinely be under the objective while
  a session runs. And **"measure it at the rig" and "measure it here" are the
  same errand**, so anything §6 defers to a rig trip should be re-read as doable
  now — the pupil clip on `E:` is the standing example.
  *If work ever resumes on a different machine, re-check this line first.*
- **Probe before concluding a device is absent.** This rule twice read "the
  laptop has no hardware" and was twice wrong. `probe_all` here reports the ORCA,
  `Dev3 PCIe-6363` for wheel *and* puffer, COM54 for the stage, and the DMD —
  everything but the Basler pupil camera. Two caveats: a probe is *enumeration*,
  so COM54 "present" is not a working serial link (`devices/stage/driver.py`
  still fails to open it here); and presence is not permission — the stage and
  puffer are actuators. Anything genuinely uncheckable goes in §6 "Needs the
  rig".
- **Ask before actuating anything physical.** Opening, configuring and
  uploading to a device are safe and reversible; **emitting light, firing the
  puffer, or driving the stage are not** — this is an in-vivo rig and there may
  be an animal under the objective. The pattern that worked for the DMD: verify
  the whole path *short of* the actuating call (open → render → upload →
  release, which projects nothing), report that, and ask before the last step.
- **Follow SOLID.** New code and refactors are judged against it: one
  responsibility per class, extend rather than modify (a new instrument is a
  subclass plus a registry line, not an edit to the window), subtypes
  substitutable for their base, interfaces split rather than fat, and
  high-level code depending on the `Protocol`s in `acq/devices.py` rather than
  on concrete drivers. §5b is the standing review against these — read its
  "What is strong" list before changing that shape.
- **Commit before restructuring.** See the warning in §3 — this is currently
  the single biggest risk to the project.
- **An exception escaping a `QThread.run()` aborts the process** (PyQt6
  `qFatal`). All worker bodies stay inside the `PullWorker.run()` guard.
- **Every runnable entry point calls `enable_safe_console()`** before its first
  print. `tests/test_console_safety.py` enforces this.

## 3. Backup status

`acqApp/` is a git repo — `turboSwagDonkey/acqApp` (private), branch `master`.
The laptop writes and pushes; the rig pulls, runs, fixes and pushes back.
**As of 2026-08-17 that is one machine, not two** — this box is the rig (§2), so
a commit made here is already on the machine that runs it. Push anyway: the
remote is still the backup.

**Commit before restructuring, and push before leaving the machine.**
`.gitignore` excludes `sessions/`, `*.h5`, `*.csv`, `*_local.json` and `.venv/`,
so committing cannot push experiment data. This once drifted six weeks and 68
files without a commit, with a 950-line refactor done on top; don't repeat it.

## 4. Stages

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Hardware de-risk (encoder, camera throughput) | ✅ **both closed** — encoder 4.912 V/rev; camera **105.9 fps, 2223 MB/s** through the app's loop (re-measured 2026-08-17; the earlier 46.17 / 969 was an artefact, §7 (t)) |
| 1 | `acq/` skeleton: clock, ring buffer, recorder, writer | ✅ |
| 2 | Camera streaming + preview + HDF5 | ✅ mock-verified |
| 3 | Encoder streaming + plot | ✅ mock-verified |
| 4 | Unified session start/stop, shared clock, metadata | ✅ mock-verified |
| **4.5** | Audit remediation + test net (§5) | ✅ all 22 closed, 454 checks |
| **5** | Closed-loop: trigger DMD/puffer from encoder state | ✅ mock-verified — `closed_loop.py`, armed from the Closed loop tab; **never run on the rig** |
| 6 | Hardware sync: `DaqClock` on the PCIe-6363, triggered ORCA | future |

## 5. Checklist — audit remediation

**All 22 items closed** — the 2026-08-10 full-app audit, finished 2026-08-12.
The checklist itself moved to **[docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md)**
once it was complete: it is 190 lines of closed work, and a fresh session should
not have to read it to start. Item numbers (#1–#20, C1–C3, B1–B2) are referenced
from §5b, the session log and `tests/README.md` — look them up there.

## 5b. Architecture review — SOLID (2026-08-12)

A separate pass from the §5 audit: that one was about wrong data, this one is
about the shape of the code. Numbered `A*` so it can't be confused with the
audit items. **None of these are breaking anything today** — they are listed
because two of them are how a future wrong-data bug gets in.

### What is strong — do not "improve" these

- **Open/Closed is real, not aspirational.** Adding an instrument is a
  `ModuleAdapter` subclass plus a registry line. Verified: the only module
  names in all 833 lines of `main.py` are two colour lookups for button
  styling. Nothing in the window knows what a pupil camera is. This is #17
  having actually worked, and it is worth defending in review.
- **Dependency inversion in `acq/`.** `Recorder` depends on `Writer` and
  `AbstractClock`, never on h5py or `perf_counter` — which is what keeps
  `DaqClock` (phase 6) a drop-in. Vendor SDKs are imported *inside* methods,
  not at module scope, which is what made C3's driver blocking possible at all.
- **The per-instrument adapter owning panel+plot+worker+sink+metadata is a
  deliberate trade**, not an SRP failure to be fixed. It is SRP by *instrument*
  instead of by *concern*, and it is what killed the six-way repetition in the
  old `MainWindow`. Splitting it back up would undo #17.

### Open

- [x] **A1 The mock/real device pairs have no declared interface.** ✅ Nine
      `getattr`/`hasattr` probes stood in for a type, and
      `getattr(c, "device_name", "none")` files a session that really
      projected as one that didn't. Now the `Protocol`s in `acq/devices.py`,
      asserted by `tests/test_device_contracts.py` — **the Protocol alone
      catches nothing, since this project ships no type checker.**
- [x] **A2 A new module needs three registrations, not the two
      documented.** ✅ `adapters.ADAPTERS`, `config.MODULES` **and** a
      `style.HEX` colour. Now stated at `ADAPTERS`.
- [ ] **A3 Mock/real selection is hard-wired inside each adapter.** ⬜ Every
      `build_session`/`build_controller` imports both concrete classes and picks
      with `if emulate:`, so a test cannot inject a device — it has to
      monkeypatch `sys.modules`, which is exactly why `block_real_devices()`
      exists rather than a one-line fake injection (C3).
      **Triggered 2026-08-18**, and not from the direction this item predicted:
      not a seventh *module* but a **third variant of one device** —
      `VideoFileCameraWorker` replays a clip as the pupil camera, so
      `PupilCamModule.build_session` is an `if` chain over three concrete classes
      it imports itself. The cost was paid immediately: testing the tracker on
      real footage needed exactly the `sys.modules` monkeypatch this item names.
      A `build_session(source_factory)` seam would have made it a one-line
      injection. Still not urgent, but the condition is met.
- [x] **A4 The adapter's "narrow surface" onto the window is a docstring
      promise.** ✅ Now `acq.devices.ModuleHost`, and `test_device_contracts`
      checks **both** directions — that the window still provides the
      surface, and that no adapter reaches past it. Widening it is a
      deliberate line in `acq/devices.py`.
- [x] **A5 `main.py` and `modules.py` are large.** ✅ `modules.py` became
      `adapters/` (one file per *instrument*, not per layer); **`main.py`
      was deliberately left alone** — it is the operator's file.
      Reasoning in [docs/DECISIONS.md](docs/DECISIONS.md).

## 6. Next actions

**THE NEXT THREE THINGS**, per §8's own rule. Everything after them is reference
kept for its reasoning, not a queue.

1. **Re-measure the writer with the camera running.** Full-frame bin 1 now
   records complete *in the bench* — 2464 MB/s saturated against 2223 offered,
   100 % kept over 60 s / 133 GB — but no ORCA was in the loop. The one number
   that is a guess is `WRITER_MBPS = 1800`: the bench derated by 0.77, which is
   the camera contention the 2026-08-17 run showed against its own bench
   (1004/1305). **Run a 30 s full-frame bin-1 recording, count the frames off
   the closed file, and replace 1800 with what it says.**
   - If it sheds frames, the next lever is DCAM's own recorder (`.dcimg`) —
     the cost is the one-file/one-clock invariant and hand-written ctypes,
     since pylablib binds no `dcamrec_*`. Details in DECISIONS.md item 7.
   - If it does not, say so and the throughput work is finished.
   - Nothing to configure: it is the ordinary Record button at bin 1.

2. **Save a calibration and check it optically.** The sweep runs; what has not
   happened is anyone confirming where the light actually lands. Run
   Calibrate…, save the JSON (the panel adopts it at once), then draw one ROI on
   a landmark, project the mask, and look. **An affine has no keystone term**,
   so expect it best near the centre and worst at the edges — that is the
   measurement worth making, and the residual cannot substitute for it.
   - **Preconditions**: voltage camera *running* (Free run or Record),
     `dmdGUI_project` **closed** (one process owns the USB), illumination on.
   - **Read the residual first.** It is the scatter of stripe centroids about a
     straight line, in camera px. Single digits is good; tens means an affine
     does not describe this relay and the log will name the odd stripe.
   - The ALP refused to open once and opened on an identical retry. **A single
     "not found or not ready" is not proof the DMD is absent.**

   - **Then judge the fit from its hold-out.** `holdout_px` (refit without a
     stripe, then predict it) is the honest number; the residual is optimistic
     by construction. If it is poor, the known cause is that ~6 of 18 stripes
     run off the frame and the survivors bunch to one side — a two-pass sweep
     (3 coarse stripes to find the visible range, then 9 across it) would fix
     it for ~6 extra exposures and ~20 lines. Not built: re-run first.

3. **Measure the wheel diameter** — the last unmeasured constant, and a ruler
   answers it. Until it is set the app reports rev/s and rev instead of mm/s and
   mm, and the closed loop's threshold has to be set in revolutions.
   `volts_per_rev` is a measured 4.912 and the sign is settled, so this is the
   only thing between the wheel and fully physical units.

**EXPERIMENT ROUTINES ARE BUILT** (2026-08-26) — `routines/` +
`adapters/routines.py`, 88 checks, mock-verified. What is left of them is one
piece and one rig trip:

- **`per_step` save mode does not roll files yet.** It is modelled, validated
  and carried into `StepRun.attrs()` — every step file names the session origin
  and its own t0 on the shared clock, which is the invariant that mattered —
  but both modes currently write one session file with `/routine` boundaries in
  it. Rolling means re-entering `MainWindow._start_recording` mid-session, and
  **main.py is the operator's file: ask first.**
- **`RoutineHooks.moving` is a seam nothing fills.** Arrival is `settle_s`
  today, per the operator's answer (3). A cheap arrival signal would let a step
  wait for the stage instead of for a guessed time; the MCM6101 answers only
  over the serial link the 4 Hz poller already shares, so it is not free.
- **Its first real run is a rig trip**, and it comes after item 2 above: a
  routine that places ROIs inherits the calibration nobody has checked
  optically.

The four design questions the operator answered on 2026-08-26 are now **in the
code**, not the plan — `routines/engine.py`'s docstring carries them and the two
they implied, which are also decided: an interrupted step's data is **kept and
marked**, and **resume repeats the step** as a fresh attempt rather than
continuing it. **Confirm that second one with the operator on the first real
run** — it is the only one of the six they did not state themselves.

**EYELOOP WAS TRIED AND IT WORKS** (2026-08-26) — **151/151 frames on both rig
clips**, the operator's hard one included, at 1.2–1.8 ms/frame. Nothing is
integrated: no acqApp module imports it. Full measurements, traps and open
decisions in **[docs/EYELOOP.md](docs/EYELOOP.md)**; the clone is `../eyeloop/`
(upstream cd22fb7, GPL-3.0) and is **untracked**, so
[docs/eyeloop-3.14-patches.diff](docs/eyeloop-3.14-patches.diff) is the only
durable copy of the patches. The five things worth carrying:

- **Four patches, not the artifact's two.** `engine_constants.py` dies at
  import under NumPy 2 NEP 50 (`np.int8 * 360` overflows); the stock GUI then
  dies on `putText` against a float64 buffer under OpenCV 5. Neither is in the
  artifact. **The stock GUI runs on a clip** — command in docs/EYELOOP.md.
- **cv2 on 3.14 is settled: it installs** — opencv-python 5.0.0.93 ships a
  `cp37-abi3` wheel. It and PyYAML are in `.venv` and **the suite is still
  912/912 green**. §0's "no cv2 wheels exist for 3.14" is retired.
- **Fit rate is not a quality metric.** `fit()` never resets `params` on
  failure, so a dead frame returns the last good answer. Worse, threshold sets
  the radius (**60 % swing**, thr 25→60) and a seed 100 px off silently halves
  it — both at a reported 151/151. The artifact's threshold 35 is wrong for
  this rig; ~60 matches the archived tracker's measured 53.6 px.
- **The crop is mandatory**: full frame is **0/151** at 73 ms/frame; any crop
  200–900 px gives the same answer. **The eye region is that crop** — kept in
  the live app, persisted, and currently consumed by nothing. It gets a
  consumer back.
- **It does not beat the archived tracker on fit rate or steadiness** (151/151
  at sd 0.87 px). **The win is that ellipse mode works on real footage**, where
  acqApp's own was broken at 8/151.
- **cv2 is needed, but shallowly** — the ellipse maths and the ray walk are
  pure numpy; only the per-frame threshold (3 ops, scipy has them) and the
  failure path use it. **That failure path opens a modal `waitKey(0)` and
  blocks forever** (`processor.py:135`), on exactly the frames where tracking
  failed. It must be neutered whichever way the cv2 question goes; the probes
  could not find it because the good clips never failed.

**A bench app exists: `../eyeloopGUI/`** (2026-08-26). Open a clip, click the
pupil to set the eye crop *and* the seed, tune the threshold, sweep. It does
the one thing the stock EyeLoop GUI cannot — set the crop — and reproduces the
probe numbers with both controls green (noise → None 20/20, uncropped → 0/20).
`tracker.py` is the only file touching EyeLoop and is written to move into
`devices/pupil_cam/` unchanged; `source.py`'s `FrameSource` is where
`acquisition.py` slots in. **It is a sibling, like `wheelApp/`, and like
`wheelApp/` it is not under version control** — the only copy is on this disk.

**Corneal-reflection removal is in the bench app** (2026-08-26). EyeLoop's own
is dead code — disabled in three places and writing to a buffer `engine.py`
never creates — so `tracker.py` does it, with live controls and a red mask
overlay. **A real but clip-dependent gain**: on pAce it tightens radius sd
1.86 → 1.42 for +0.9 px; on the *operator's* State clip the reflection sits at
0.84 r and nothing safe reaches it. Two traps found and fixed: masking the rim
erases the boundary and inflates the radius (+3.5 px), and the search area must
follow the fitted **ellipse** — a circle at 0.85 × mean-radius is already
outside an 0.78-ratio pupil on the minor axis. Numbers in docs/EYELOOP.md.
**Fit rate stayed 151/151 through every one of these failures.** The operator
is tuning it.

**INTEGRATION STARTED 2026-08-26, on master, at the operator's call.** Done:
`devices/pupil_cam/eyeloop_tracker.py` (the tracker, moved unchanged from the
bench app), `tracking.py` (the seam), tracking + corneal-reflection knobs on
`PupilSettings` with `crop_box()` deriving the crop from the **circular eye
region** already there, and `test_pupil_eyeloop` — 22 checks, every one with a
control, both rig clips 151/151 through the app path. Suite 934/26 green.

**Not done — this is where to resume**, steps 5–8 of the handoff:
1. **The panel.** Nothing draws the fit, and no control sets any of the knobs.
2. **Persistence.** The fields exist on `PupilSettings` but nothing saves them,
   so the operator's tuning is lost on close — they already lost one set.
3. **Off the GUI thread.** 2.4 ms fits in a 33 ms tick, but that was offline
   and single-threaded; the branch's `track_worker.py` solved this before.
4. **Record it.** The session file needs the ellipse *and* the settings behind
   it — a pupil trace without its threshold is not reproducible.

**THE WRAP-UP IS [docs/EYELOOP-INTEGRATION.md](docs/EYELOOP-INTEGRATION.md)** —
read that before writing any integration code. It carries the two gates, the
eight steps in dependency order, the seven silent traps, and what not to
rebuild. EYELOOP.md is the measurements; that file is what to *do* with them.

**Still blocked on the operator:** which tree (§0 forbids restoring a tracker on
master; `pupil-tracking` already has a *working* one), and **GPL-3.0** —
vendoring EyeLoop into `devices/` makes acqApp a derived work, which is why
both the clone and the bench app were kept outside.

~~Pupil tracking.~~ **Retired 2026-08-24** — `archive/pupil_tracking/`. The
eye region stayed. Do not restore without being asked; the README there has the
measurements and the restore steps. The EyeLoop note above is the live thread.

~~Settle `_SIGN`.~~ **Done 2026-08-19** — the operator answered it; see §0 and
§7 (ad).

~~Finish the trim/optimise pass.~~ **Recommended closed 2026-08-19 (ae)** —
nine more files done and the remainder measured as *not* where the prose is.
Item 5 keeps the reasoning and the numbers.

**Older items** — closed ones and the long-tail open ones (full-frame writer
throughput, closing the loop on the rig, which wheel speed a rule watches) moved
to [docs/DECISIONS.md](docs/DECISIONS.md) on 2026-08-24 with their reasoning
intact. Two are still worth doing and are listed there under "Still open".

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what's next.

### 2026-08-26 (ay) — EyeLoop integration started, on master

Operator chose **master** and asked to begin. Moved `tracker.py` in unchanged
as `devices/pupil_cam/eyeloop_tracker.py`, added `tracking.py` as the seam, and
put the tracking + corneal-reflection knobs on `PupilSettings`. Suite **934
checks / 26 files**, green.

- **The circular eye region is the crop**, via `PupilSettings.crop_box()`. It
  had been persisted and drawn but consumed by nothing since the 2026-08-24
  retirement; it now does the job that makes tracking work at all.
- **`test_pupil_eyeloop`, 22 checks, every one with a control** — the stale-fit
  check backed by asserting `params` really was nulled, the reflection mask by
  requiring nothing outside 0.95 r is touched, a pin's reach by the automatic
  pass failing to reach the same reflection. Both rig clips 151/151 through the
  app path.
- **No EyeLoop source is in this repo, and the repo is public.** Vendoring
  would be distribution and would make acqApp a derived work; that decision is
  still open. `EyeLoopUnavailable` leaves the pupil camera untouched, tested.
- §0's "do not restore the tracker on master" is **superseded** and rewritten;
  the *hand-rolled* tracker stays retired.

**Next: the panel, persistence, the worker thread, recording** — steps 5-8 of
docs/EYELOOP-INTEGRATION.md.

### 2026-08-26 (ax) — reflection removal, pinning, and the handoff

Corneal-reflection removal in the bench app, plus operator-pinned reflections,
then wrote the integration handoff. **EyeLoop's own removal is dead code** —
disabled in three places and writing to a buffer `engine.py` never creates.

- **Pins are exempt from both guards** (`reach`, `max_area`), because those
  guard against bright things the automatic pass cannot identify and a pin is
  an identification. Stored in *frame* coordinates so moving the eye region
  does not walk them off target.
- **Removal is a real but clip-dependent gain, and near the rim it costs.**
  pAce: radius sd 1.86 → 1.42 for +0.9 px. Pinning State's 0.84 r reflection
  removes it properly and costs **+6.6 px of radius** — blanking something that
  overlaps the boundary erases the boundary. Pins pay off *inside* the pupil.
- Two of my own mistakes, both caught by measuring: a circular search area ate
  the eyelash line (fixed by following the fitted ellipse), and the two-pass
  rewrite went 1.7 → 8.5 ms/frame before per-blob boxes put it back to 2.4.
- **The operator is tuning it themselves** and has settings saved outside the
  app — it persists nothing, which is step 5 of the integration.

**Next: [docs/EYELOOP-INTEGRATION.md](docs/EYELOOP-INTEGRATION.md).** Two gates
first — which tree, and GPL-3.0 — and both are the operator's.

### 2026-08-26 (aw) — the EyeLoop bench app

Built `../eyeloopGUI/`, a sibling that drives EyeLoop over a rig clip: click
the pupil to place the eye crop and seed, tune the threshold, sweep. Four
files, split so `tracker.py` (the only EyeLoop contact, Qt-free) can move into
`devices/pupil_cam/` unchanged and `source.py`'s `FrameSource` takes the live
camera later without `window.py` changing.

- **It reproduces the probe numbers exactly**, which is the point of it —
  pAce 151/151 r 45.18 ± 1.86 at thr 45, State 151/151 r 59.66 ± 1.13 at
  thr 60. Controls green: noise → None 20/20, uncropped → 0/20.
- **One trap, an hour.** `Shape.min_radius/max_radius` bound the ray walk and
  are clipped *into an int array*; floats there make `np.clip(out=…)` raise
  inside the bare except, so **every frame fails silently**. Split into an int
  `walk_radius` and a float `accept_radius`.
- `waitKey(0)` is bound to a no-op in the wrapper, and a failed frame now
  returns None instead of the last good fit.
- Registered in the root `CLAUDE.md` beside the other siblings. **Not under
  version control** — nor is `wheelApp/`; that is the existing arrangement,
  worth a decision rather than a surprise.

**Next: unchanged** — §6's three rig measurements, and the operator's two
EyeLoop decisions before any of it moves into `devices/`.

### 2026-08-26 (av) — EyeLoop, tried

Cloned EyeLoop (`../eyeloop/`, GPL-3.0, upstream cd22fb7), patched it and drove
it headless over both rig clips. **It fits 151/151 on each, the operator's hard
clip included, at 1.2–1.8 ms/frame.** Nothing integrated — no acqApp module
imports it. [docs/EYELOOP.md](docs/EYELOOP.md) has the numbers.

- **The artifact's two patches are not enough.** A third, `engine_constants.py`
  under NumPy 2 NEP 50, stops it at *import*. Saved all three as
  `docs/eyeloop-3.14-patches.diff` — the clone is untracked, so that diff is
  the only durable copy.
- **cv2 installs on 3.14** (`cp37-abi3` wheel). Retires a §0 claim that shaped
  two subsystems. cv2 + PyYAML now in `.venv`; **suite re-run, 912/912 green**.
- **The 151/151 nearly fooled me.** `fit()` never resets `params` on failure,
  so failures return the last good fit. A noise control (0/151 genuine, 151
  stale) is what makes the real number trustworthy. Then two silent
  degradations *at* 151/151: threshold swings radius 60 %, and a seed 100 px
  off halves it. **The artifact's threshold 35 is wrong for this rig.**
- **Full frame is 0/151 at 73 ms.** The crop is required, and acqApp's eye
  region — persisted, recorded, consumed by nothing since the retirement — is
  exactly it.

**Next: the operator decides** which tree it lands on and what GPL-3.0 implies
before any code moves into the repo. §6's three rig measurements are unchanged.

### 2026-08-26 (au) — a note, not code

Recorded the operator's **EyeLoop integration request** in §6 from the
*EyeLoop Integration Handoff* artifact. **Nothing was built** — §6's three next
actions are all rig measurements, and this is the fourth thing, not a fourth
action.

- The artifact is **the laptop's tree, not this one**: it targets an empty
  `pupil_cam/tracking.py` stub, and here that tracker exists, works, and was
  *retired* 2026-08-24. The note says translate before following it.
- Two facts worth having anyway: EyeLoop's per-frame failure was NumPy 2
  removing `np.mat` swallowed by a bare `except`, and its blink detector is a
  whole-frame brightness test — **62 % false positives on the wide rig FOV,
  0 % on an eye crop**. Frame tight regardless of tracker.
- Checked what the artifact assumes against this machine: **no cv2 in
  `acqApp/.venv`** (numpy 2.4.6, Python 3.14.3), and **no eyeloop sibling
  folder** — the two patches live only on the laptop.

**Next: unchanged.** §6 items 1–3, then ask the operator whether EyeLoop
replaces the branch's hand-rolled tracker or nothing.

### 2026-08-26 (at) — the same sweep, tree-wide

Ran §6 item 5 over everything the pass had never covered. **The prose half was
again not where the value was** (tree 24.5 %, and worst-first points at
interface files where the docstring IS the contract). Everything below came
from a scan or a profile.

- **The per-tick restyle was in two more places** — `closed_loop/panel`
  (269 `setStyleSheet` calls in 6 s, identical string) and `adapters/wheel`'s
  plot title, through pyqtgraph's `setHtml`. Both guarded on an actual change.
- **Three docstrings claimed what the code does not do**, the class the
  2026-08-18 survey flagged. `PullWorker.set_sink` said "Thread-safe.", which
  reads as "detaching stops delivery" — it does not, and `Recorder.late_count`
  exists precisely to count what lands after.
- **`StageTarget.stop_motion` was declared "must not raise" and did.** It runs
  *on a fault*, so a dead serial link is exactly when it is reached. Guarded,
  with the raw call as the test's control.
- Eight unused imports, two dead names. A dead-name scan over 109 files found
  nothing else — the tree is clean.

**One measured thing left deliberately alone, and it needs the operator: the
two camera LUT bars are ~45 % of the display tick** (4.43 ms → 2.45 ms with
their histograms disconnected; 13.3 % of a 30 Hz budget either way). Fixing it
means recomputing the histogram twice a second instead of 30×, which **changes
what you see on the preview you drag for contrast**. Number and reasoning in
DECISIONS §6 item 5. Nothing is hurting today.

**Two traps from getting that number**, both worth keeping: cProfile
*under*-attributed it, and my first control was **vacuous** — it detached
`getattr(m, "_hist", None)` and the adapters keep the LUT bar in a local, so it
detached nothing and reported "no effect". It now counts what it detached.

Suite **910 → 912 checks**, 25 files, green.

### 2026-08-26 (as) — prose/optimisation sweep over the routines code

The §6 item 5 sweep, same two jobs, run over what (ar) added. **The prose half
found almost nothing** and the measurement said so up front: the new files sit
at 20.8 % comment+docstring against a tree at 24.5 %, so worst-first by ratio
pointed at interface files where the docstring *is* the contract. That is the
2026-08-19 finding again — the remainder is not where the prose is.

**Both real findings came from profiling**, and one of them refuted a
micro-benchmark:
- **`Recorder.offered()` took the enqueue gate to read one int** — read ~70×/s
  from the GUI thread while a routine runs, against every worker's `put()`.
  **6.1 ms mean / 28.7 ms worst → 1.4 µs / 4.2 µs.** The lock bought a count no
  caller can distinguish; the increment still happens under the gate the
  enqueue already holds, at 68 ns/sample.
- **The routines panel restyled every display tick** — `setStyleSheet`
  repolishes against the window's whole cascade, **53 % of the shared 30 Hz
  tick**, re-applying an identical string. A bench on a detached panel had said
  2 µs and "not worth it"; the profile of the *built window* said 26 µs. Tick
  **0.05 → 0.02 ms**.

Also: two dead names deleted, and `StepRun.attrs()` was promising the file
something nothing wrote — `single` mode now files `routine_runs`, so **which**
execution faulted is recoverable (`/routine` carries only a signed index).
Numbers and reasoning in [docs/DECISIONS.md](docs/DECISIONS.md) §6 item 5, which
is that pass's ledger. Suite **899 → 910 checks**, 25 files, green.

### 2026-08-26 (ar) — experiment routines: built, wired, and mock-verified

The next big one from (aq)'s entry, to the four answers the operator gave the
same morning. Two commits: the Qt-free core, then the wiring.

- **`routines/`** — `settings.py` (Step / Routine / `validate`) and `engine.py`
  (the executor). Both Qt-free; **every actuation reaches the engine as a
  callable**, as `calibration.py` does, which is what let the whole feature be
  verified before anything moved. `tick()` state machine over `now()` and
  `frames()`, not a loop with sleeps — so pause/resume/abort are transitions and
  a test steps a whole routine on a fake clock in 0.1 s.
- **The two questions the plan left open are decided, in the code**: an
  interrupted step's data is **kept and marked** (never discarded — with an
  animal on the rig, recorded frames are not ours to throw away), and **resume
  repeats the step** as a fresh `attempt`. Both attempts stay in the file.
  **The operator has not confirmed the second**; ask on the first real run.
- **The seam is two new Protocols**, `StageTarget` / `PatternTarget`, pooled by
  the window as `stage_target()` / `pattern_target()` — the `signal_sources()`
  shape. `adapters/routines.py` names no instrument; declaring a target is the
  whole cost of making one routine-drivable. Deliberately no "is it moving?":
  the MCM6101 answers only over the link the 4 Hz poller shares.
- **`Recorder.offered(stream)`** is what a "100 frames" step counts — frames
  that reached the FILE, not frames the camera produced. Those differ exactly
  when the write path is what is falling behind, which is the failure this rig
  had.
- **Three things that cost the most thought, all of them "what does the file
  say afterwards"**: `/routine` carries a signed entry per boundary on the
  shared clock; `routine_steps` carries the protocol as JSON because "which
  position was step 4" is recoverable from nothing else; and `StepRun.attrs()`
  names the session origin + per-step t0 once, so the two save modes cannot
  disagree.
- **Not built, on purpose**: `per_step` file rolling. It means re-entering
  `MainWindow._start_recording` mid-session and **main.py is the operator's
  file**. Both modes write one session file today.
- Suite **802 → 899 checks, 24 → 25 files**. `test_routines` is 88 of them, a
  fake-rig half and a real-window half. Nothing here has touched hardware.

Entries before 2026-08-26 (ar) are in
**[docs/SESSIONLOG.md](docs/SESSIONLOG.md)** — moved there to keep this file small
enough to read at the start of every session.

## 8. How to keep this file useful

**At the end of every session, before the context runs out:**

1. Tick what actually got done — and only what was *verified*, with the test or
   the command that verified it. (§4's table, §5b, or §5's one remaining item.)
2. Rewrite §6 "Next actions" (3 items max, ordered).
3. Add one dated entry to §7, newest first.
4. Update the **Last updated** date and the **Progress** figure in the header.
5. Note anything discovered that contradicts §2 or the README. §2 is a list of
   invariants, and an invariant that has quietly stopped being true is worse
   than no rule — "the laptop has no hardware" was wrong for a while before
   anyone checked.
6. Refresh **§0** if what a fresh session needs has changed: the check count,
   a new gotcha that cost you time, and above all anything left **uncommitted**.
   §0 is the only part of this file written for someone who has never seen the
   project.

7. If anything **moved, was renamed or was added**, update
   [docs/STRUCTURE.md](docs/STRUCTURE.md) in the same commit — the tree *and*
   the diagram. `tests/test_structure.py` will fail the suite if you don't,
   which is the only reason a structure doc survives contact with a refactor.

Do this as a *small* edit to this file, never a rewrite — the reasoning in the
session log is the part that's expensive to reconstruct.

**Keeping it short.** This file is read in full at the start of every session,
so its length is a running cost. It went 676 → 417 lines on 2026-08-12 by
moving *finished* work out, not by deleting it: the closed audit checklist to
[docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md) and older session entries to
[docs/SESSIONLOG.md](docs/SESSIONLOG.md). Keep doing that rather than trimming
detail — the detail is the value. **Rule of thumb: §7 holds the newest ~3
sessions; when a checklist reaches all-closed, archive it and leave a stub with
whatever is still open.** Aim to stay under ~400 lines.

**Numbering:** sections are referenced from both `CLAUDE.md` files (§0, §8) and
from inside §5b. Add a "§5b"-style suffix rather than renumbering.
