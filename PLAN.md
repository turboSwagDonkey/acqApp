# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-24 (an) |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done; audit remediation 100 % (22 of 22). The pupil tracker was retired 2026-08-24 into `archive/pupil_tracking/`, eye region kept **2026-08-24: DMD calibration is built, wired and has run at the rig** — a narrow stripe stepped across each axis, two line fits, an affine. Gray coding was tried and deleted; this relay scatters enough to erase any periodic pattern (§7). Suite **716 checks, 22 files — all green**. §5b **A3 triggered** and is the one open architecture item. Next: §6. |

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
the contract: **737 checks, 23 files, ~53 s**, and it is ALL GREEN. Run it
before and after anything.

**The pupil tracker is retired** (2026-08-24, operator's call). It lives in
`archive/pupil_tracking/` with a README carrying the measurements; nothing
imports it. The pupil camera still previews and records, and the **eye region**
is kept — drawn on the preview, persisted, recorded. Do not restore the tracker
**on master** without being asked.

**Tracker work happens on the `pupil-tracking` branch, not here** (2026-08-24,
operator's request: somewhere a colleague can try changes separately). It has
the tracker live and green — 830 checks, 24 files — and **`PUPIL_TRACKING.md`**
is its entry point, written for someone who has never seen the repo. Master's
decision is unchanged; the branch merges back by pull request.

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
  into every import of the package.** `closed_loop/` and `saving/` re-export
  lazily (PEP 562) so their Qt-free halves stay Qt-free.
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

**PICK UP HERE — calibration works and has run at the rig.** DMD tab →
Photostimulation ROIs → **Calibrate…** → one button, 19 exposures, ~9 s. It
steps a narrow stripe across each axis, fits a line to where each lands, and
writes an affine. Then draw an ROI and check optically where the light falls —
no residual replaces that.

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

1. **Save a calibration and check it optically.** The sweep runs; what has not
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

2. **Then judge the fit from its hold-out, and decide whether to widen the
   sweep.** `holdout_px` (refit without a stripe, then predict it) is the honest
   number; the residual is optimistic by construction. If it is poor, the known
   cause is that ~6 of 18 stripes run off the frame and the survivors bunch to
   one side — a two-pass sweep (3 coarse stripes to find the visible range, then
   9 across it) would fix that for ~6 extra exposures and ~20 lines. Not built:
   re-run first and see whether it is needed.

3. **Measure the wheel diameter** — the last unmeasured constant, and a ruler
   answers it. Until it is set the app reports rev/s and rev instead of mm/s and
   mm, and the closed loop's threshold has to be set in revolutions.
   `volts_per_rev` is a measured 4.912 and the sign is settled, so this is the
   only thing between the wheel and fully physical units.

~~Pupil tracking.~~ **Retired 2026-08-24** — `archive/pupil_tracking/`. The
eye region stayed. Do not restore without being asked; the README there has the
measurements and the restore steps.

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

### 2026-08-24 (aj–an) — DMD calibration, built and run at the rig

One day, several wrong turns; only what survived is here. The discarded designs
are in the commits (`eb5cb77` onward) if the reasoning is ever needed again.

- **`run_calibration` had never been executed by anything** — not even its own
  test, which imports the pieces. The feature's one function had zero coverage.
- **The rig cannot do Gray coding, and that is measured, not assumed.** A solid
  bar images cleanly; a 280 px checkerboard modulates **13 %** of the frame and
  a 70 px stripe pattern **9 %**. Scattering erases fine structure at *any*
  pitch — a coarsened code was tried down to 16 mirrors and still decoded
  0.0 %. So the Gray/checkerboard/homography machinery was deleted rather than
  kept as a switch nobody can use: `calibration.py` 1172 → ~500 lines.
- **What works: a narrow stripe at nine signed offsets per axis**, a line fit to
  where each lands, and the two lines are the affine. Signed offsets carry the
  direction, so a mirror flip cannot pass.
- **Three fitting bugs, each found from the rig's own numbers, not by reading:**
  growing a centred bar and reading its second moments scored rms 65.8 px
  because the frame clips one side while vignetting eats the other; fitting each
  axis with its *own* intercept made the two disagree about the panel centre by
  67 px, which shear then absorbed; and averaging the two axes' rotation
  estimates evenly dragged the well-measured axis toward the badly-measured one.
- **The residual is not the number to trust.** Least squares sits closest to the
  points it was handed, so `holdout_px` refits without a stripe and predicts it.
- **The UI, after the operator used it:** three display modes (All ON / Image /
  ROIs), drag-to-draw with a rubber band, percentile contrast (the editor was
  showing a near-black frame because `autoLevels` stretches to a hot pixel), and
  the calibration starts the camera itself instead of demanding setup.
- **`RoiSet.dmd_frame` 107 ms → 1 ms** by bounding each ROI in mirror space
  instead of rasterising a camera-sized mask per ROI.
- Suite **660 → 716 checks, 21 → 22 files**, all green.

Entries before 2026-08-24 (aj) are in
**[docs/SESSIONLOG.md](docs/SESSIONLOG.md)** — moved there to keep this file
small enough to read at the start of every session.

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
