# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-26 (ba) |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done; audit remediation 100 % (22 of 22). The pupil tracker was retired 2026-08-24 into `archive/pupil_tracking/`, eye region kept **2026-08-24: DMD calibration is built, wired and has run at the rig** — a narrow stripe stepped across each axis, two line fits, an affine. Gray coding was tried and deleted; this relay scatters enough to erase any periodic pattern (§7). **2026-08-25: full-frame bin 1 records complete** — the writer's direct-chunk path (1304 → 2696 MB/s) and a 2 GB ring ended the 53 %-of-frames loss, in the bench; it has not run against the camera, which is §6 item 1. Also 2026-08-25: **instruments load and unload without restarting** (sidebar → Modules), and the sidebar carries one item per settings page. **2026-08-26: experiment routines are built and wired** — a protocol of stage positions and DMD patterns executed step by step, engine Qt-free over callables, loadable as a module. Mock-verified only; the per-step *file rolling* is the one piece deferred (§6). **2026-08-26: EyeLoop pupil tracking is integrated and complete in the app** — the panel, persistence, its own thread, and the ellipse recorded into the session file. Mock-verified only; step 8 (the live Basler) is a rig trip. **2026-08-26: the routines panel starts its own recording**, and its step list is edited through drop-downs and spin boxes rather than typed words. Suite **1044 checks, 27 files — all green**. §5b **A3 triggered** and is the one open architecture item. Next: §6 — the three rig measurements, none of which can be done from a keyboard. |

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
the contract: **1044 checks, 27 files, ~65 s**, and it is ALL GREEN. Run it
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

**The EyeLoop integration is DONE in the app** (2026-08-26, steps 2–7 of
[docs/EYELOOP-INTEGRATION.md](docs/EYELOOP-INTEGRATION.md)): the tracker, the
seam, the panel controls, persistence through a restart, tracking on its own
thread, and the ellipse in the session file as five NaN-padded streams.
**Step 8 is what is left and it is a rig trip** — nothing here has met the live
Basler, and the two questions it answers are whether one threshold holds for a
whole session and whether a fit keeps up at full resolution.

**The trap to know before touching any of it: fit rate is not accuracy.**
Threshold *sets* the reported radius — a 60 % swing over 25–60 on the rig clips
— at a clean 151/151 the whole way. Nothing in the app can tell the operator
their threshold is wrong, which is why the file records it beside the trace.

**The other open question — one 30 s recording answers it.** Full-frame
bin 1 now keeps 100 % of its frames in the bench (2026-08-25); nothing has run
it with the camera in the loop, and `WRITER_MBPS = 1800` is a derate, not a
measurement. Record 30 s at bin 1, count the frames off the closed file,
replace the number. §6 item 1. **Nothing about this actuates** — it is the
Record button.

**The routines panel runs itself** (2026-08-26, operator's request): Start
validates, **opens the recording it needs**, and runs — it used to refuse until
the operator had pressed Record in another part of the window. That is
`MainWindow.set_recording`, the twin of `set_live`; it returns the previous
state, which is what lets the adapter stop a recording it started and leave the
operator's alone. The step list edits through widgets (`routines/table.py`), so
nothing parses "yes" any more.

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
`adapters/routines.py`, 130 checks, mock-verified. **Their panel was reworked
on 2026-08-26 (ba)** at the operator's request: Start opens its own recording,
and every cell of the step list edits through a drop-down or a spin box. What
is left of them is one piece and one rig trip:

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

**INTEGRATION IS DONE IN THE APP** (2026-08-26, master, steps 2–7 of the
handoff). The tracker and the seam, the panel controls, persistence, the
thread, and the trace in the file. Suite 1001/27 green, mock-verified only.
What that leaves:

- **Step 8 — the live Basler.** Everything so far is offline clips and the
  mock. It is not an actuation: the pupil camera only looks. Turn tracking on,
  place the eye region on the real eye, watch the ellipse, and record 30 s.
- **The operator's tuned numbers should become the shipped defaults.** The app
  persists them now, so they live in `acqapp_local.json` on this machine only.
  Which ones were settled on is not written down anywhere.
- **Whether one threshold holds for a session, or tracks illumination.** The
  single most consequential number, and nothing measures it yet.
- **Ground truth.** Every number so far is a proxy.
  `archive/pupil_tracking/_mark_truth.py` exists to fix that and has never been
  run; a dozen hand-marked frames would settle which threshold is *right*.

**Still blocked on the operator: GPL-3.0.** Nothing is vendored — EyeLoop is
imported from `../eyeloop`, so a fresh machine needs the clone plus the patch
diff, or `ACQAPP_EYELOOP_DIR`. Vendoring it into `devices/` makes acqApp a
derived work, and **this repo is public**. `EyeLoopUnavailable` leaves the pupil
camera working exactly as before, which is a tested contract — so the decision
can wait without blocking anything.

**[docs/EYELOOP-INTEGRATION.md](docs/EYELOOP-INTEGRATION.md)** is now the record
of how each step was closed and what it actually became; EYELOOP.md is the
measurements. Read the traps table in the first before changing any of it.

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

### 2026-08-26 (ba) — the routines panel: one Start button, and real editors

Operator's request, both halves. Suite **1001 → 1044 checks**, 27 files, green.
Still mock-verified: no routine has actuated anything.

- **Start opens its own recording.** It used to refuse until the operator had
  found the Record button in another part of the window, which only moved the
  failure earlier. `MainWindow.set_recording` is the twin of `set_live` (§5b
  A4) and is added for the same reason; it returns the PREVIOUS state, which is
  what lets the adapter stop a recording it started and leave the operator's
  running. **The operator approved the main.py edit** — it is a method beside
  `set_live`, and touches nothing in the dock or settings code.
- **`RigLimits.has_frames` changed meaning** with it: "a camera is loaded", not
  "a file is open". Validating against a recording that Start has not opened
  yet would refuse every routine measured in frames.
- **`routines/table.py`** — every cell edits through a widget that can only
  produce a legal value. The value lives in the cell's `UserRole` and the text
  is a rendering of it, so a word typed into a cell can no longer decide what a
  step does. "leave this axis" became a state on the spin box; blank used to
  mean both "leave it" and "not typed yet".
- **Also in the table**: reorder arrows (there was no way to move a step but to
  delete and retype it), "No pattern" (cancelling a file dialog means "changed
  my mind"), the running row in bold, and a summary line — runs, duration, and
  how many steps emit light, in amber.
- **`style.toggle_btn` had no `:disabled` rule**, which is the exact wart
  `solid_btn`'s own docstring warns about: a stylesheet background overrides
  the palette, so a disabled button still reads as the thing to press. Fixed in
  style.py, which also fixes **Emulate** and **Free run** — both disabled for
  the whole of a session.

**Next: unchanged** — §6's three rig measurements, plus EyeLoop step 8. Nothing
here has actuated anything either.

### 2026-08-26 (az) — the EyeLoop panel, its thread and its trace

Steps 5–8 of the handoff, so the integration is complete in the app and only
the rig is left. Suite **934 → 1001 checks, 26 → 27 files**, green. Nothing has
run against the live Basler.

- **The panel.** Tracking (enable, threshold, blur, ellipse/circle) and corneal
  reflection (threshold, pad, ring, reach, plus a red overlay of the pixels it
  blanked — the only way to see what the threshold is doing). The fit is drawn
  on the preview and **cleared on every failed frame**, which is the display
  half of EyeLoop's stale-`params` trap. Pins are placed on the preview like
  the eye region: click a reflection to pin it, click a pin to remove it.
- **Persistence was one line of a bug.** `SettingsPanel.settings` built a fresh
  `PupilSettings` from six of eighteen fields, so every tracking edit saved
  itself back as the *default* — the operator's lost tuning, exactly. It reads
  all of them now, and `__post_init__` normalises `cr_pins`, which JSON returns
  as lists. Five rows in `test_settings_persistence` check it through a real
  restart.
- **`track_worker.py`** is the sole consumer of the camera's frames and
  republishes each one *with* its fit, so the outline always matches the image.
  Built whether or not tracking is on — one code path for the preview is worth
  the thread. **Its `error` had to stay the `PullWorker` signal**: a property of
  that name shadows it and breaks the guard that keeps an exception in `run()`
  from taking the process down. The message is `track_error`.
- **The trace is five streams** (`pupil_x/_y/_major/_minor/_angle`), one sample
  per tracked frame, **NaN in all five where there was no fit** — a gap has to
  be in the file, not a row nobody wrote. `pupil_track_threshold` and the rest
  go in the metadata; the close writes `pupil_frames_tracked` / `pupil_fits`,
  because tracking drops frames it cannot keep up with.
- **`test_pupil_track`, 48 checks, each with a control**, and **none of it needs
  an EyeLoop clone**: with no clone every fit is None, which is exactly what the
  NaN contract is for. On this machine it fits 6/6 through the whole worker
  path on the mock camera.

**Next: the rig.** §6's three measurements are unchanged, and step 8 joins
them — tracking on the live Basler is looking, not actuating.

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

Entries before 2026-08-26 (av) are in
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
