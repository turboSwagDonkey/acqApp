# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-21 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done; audit remediation 100 % (22 of 22). **2026-08-18/19** were a correctness-and-speed sweep, all of it found by driving real workloads rather than reading code (§0). The newest of those: the pupil tracker learned **where it may look** — a user-set eye region and measured lid sectors — taking the rig's own clip from **0/151 frames to 151/151** with no click, and its fit confidence from 0.26 to 0.34. Suite **776 checks, 23 files — all green**. §5b **A3 triggered** and is the one open architecture item. Next: §6's top three, two of which need the operator (§6). |

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
the contract: **776 checks, 23 files, ~52 s**, and it is ALL GREEN. Run it
before and after anything. For pupil work also run
`devices/pupil_cam/_test_tracking.py` (15 synthetic ground-truth checks): the
suite and that script cover different failures, and 2026-08-18 showed the
script passing 15/15 while the tracker could not follow a real eye at all.
**Better still, replay the rig's own clip** — it is on this machine at
`E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi`, and 2026-08-19 (ae)
shows what it catches that neither of the other two can.

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py
```

Use the **absolute** path to that interpreter. The shell usually starts in
`Desktop\python` (the *parent* of this repo), where `.venv\Scripts\python.exe`
resolves to nothing and Python reports a baffling "the module '.venv' could not
be loaded". Python here is **3.14** — no cv2 wheels exist for it, which is why
`devices/pupil_cam/tracking.py` is hand-rolled numpy.

**Sibling projects are proven code, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` are standalone apps that already work on this hardware, and
acqApp *shares their config files* rather than duplicating their state — the
stage's calibration in `stage_control/config.json`, the DMD's ALP path and
optical alignment in `dmdGUI_project/dmd_config.json`. Look next door before
writing a device path from scratch: `devices/dmd/alp.py` is a port of
`dmdCommandLine.py`, and it is why #5 took one session instead of several.

**Practical gotchas that have each cost real time:**
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

**The pupil tracker now has two controls for where it looks** (§7 (ae), (af)),
and on the rig's own clip they take it from **0/151 frames to 151/151** with no
click anywhere:

- **The eye region** — a circle in camera px, set on the preview. It bounds the
  seed and refuses a fit centred outside. Without it `coarse_seed` bails at its
  >50 %-dark guard, because the frame is 53 % below threshold. **Draw it
  generously**: that same guard now applies *inside* the circle.
- **Ignored directions** (`exclude_deg`) — where a lid crosses the pupil, the
  rays find the *lid's* edge and those points go into the fit like any other.
  `find_circular_edge` had always taken this argument; nothing exposed it.
  "Find lids" measures the sectors (`tracking.lid_sectors`) instead of asking
  the operator to know the angles. Worth **conf 0.26 → 0.34, rms 1.16 → 0.98,
  and half the worst-case frame-to-frame radius jump.**

Two claims this file used to make, both corrected by measurement: auto-seeding
*can* work unlimited, but only over thresholds 30–45 — a 15-level window with
the shipped 60 outside it, against 25–80 with a region. And the dark region is
**not** elliptical: measured directly it sits at 50–57 px over ~200° of arc, and
the low points are lid occlusion, not shape. Fitting a circle is right.

**PICK UP HERE — §6's "THE NEXT THREE THINGS".** Two of the three are questions
for the operator, not code: the DMD ROI work needs someone at the rig (it
projects light) and has three open design calls named in §6 item 4, and the
wheel diameter needs a ruler. Everything is committed and pushed; the working
tree is clean.

**The method that earned its keep over 2026-08-18/19, and the one to reuse.**
Every real defect in those sessions was in code that had only ever run at *toy*
scale, and every one was found by **driving the real workload and measuring**,
not by reading. Profiling the tracker on the rig clip found a 2× win and an AVI
bug; sizing the DMD calibration at the real camera found 11.4 GB where 1.6 was
needed; **rendering the UI offscreen and looking at it** found the Save tab
calling a frame-dropping setup healthy; replaying the clip found the search
limit taking tracking from 0/151 to 149/151. Do that to the next feature before
trusting it. Rendering recipe: `QT_QPA_PLATFORM=offscreen` plus
`QT_QPA_FONTDIR=C:/Windows/Fonts` — without the second, all text draws as boxes.

**§6 item 1: DMD ROI photostimulation** — image with the DMD all-on, draw ROIs
on that frame, project the mask back. **The blocker is registration, not
drawing:** an ROI is in camera pixels, a mask is in DMD mirrors, and acqApp has
no measured transform between them, because the DMD runs with `fit=True`.

The rest of this section is context; nothing below is blocking.

**The camera numbers, settled 2026-08-17 (§7 (t), table in §6 item 7).**
Acquisition is **not** the constraint — the grab path runs at 92 % of the link
(105.92 fps / 2223 MB/s at full frame). **The writer is:** a measured 1004 MB/s
end to end caps recording near **48 fps**, so a bin-1 session stores 52.9 % of
its frames. **2×2 binning stores 100 % at the full 114.9 fps** and is the
standing recommendation.

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
`main.py` alone; it is 787 lines).

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

**Both of the first two are asks, not code.** They have been the top of this
list for two sessions and neither can advance without the operator, so they are
stated here as the questions they actually are.

1. **DMD ROI photostimulation, the rig half** (item 4 below). The offline half
   is built, verified and sized for a real camera. What is left **projects
   light** at an in-vivo rig, so it is an ask — and it carries three design
   calls only the operator can make, which is why "wire `RoiEditor` in" has not
   simply been done:
   - **The snapshot has to cross modules.** An ROI is drawn on an *imaging
     camera* frame while the *DMD* is all-on, so the DMD adapter needs a frame
     from `voltage_cam`. Nothing in `ModuleHost` provides one, and §5b A4 says
     widening that surface is a deliberate line in `acq/devices.py`, not
     something to help yourself to. Which module owns the snapshot decides the
     shape of the wiring.
   - **Single frame or an average?** (item 4 (c), still open.)
   - **Do ROIs persist across sessions, and is a mask one static pattern or a
     sequence?** (item 4, still open.) The second decides whether
     `alp.project()` is enough or `project_sequence()` is needed.
2. **Measure the wheel diameter** — the last unmeasured constant, and a ruler
   answers it. Until it is set the app reports rev/s and rev instead of mm/s and
   mm, and the closed loop's threshold has to be set in revolutions.
   `volts_per_rev` is a measured 4.912 and the sign is settled, so this is the
   only thing between the wheel and fully physical units.
3. **Set a search limit on the pupil camera at the rig and check it holds** —
   the one new thing that can be done without either of the above. §7 (ae)
   validates it on a 151-frame clip from one prep; a live animal moves, blinks
   and is re-mounted. Draw the circle, watch whether the tracker keeps the eye
   across a session, and record whether the limit needs redrawing between
   animals (which is what decides if it should live in the config or in the
   session setup).

~~Settle `_SIGN`.~~ **Done 2026-08-19** — the operator answered it; see §0 and
§7 (ad).

~~Finish the trim/optimise pass.~~ **Recommended closed 2026-08-19 (ae)** —
nine more files done and the remainder measured as *not* where the prose is.
Item 5 keeps the reasoning and the numbers.

**Closed — kept for the reasoning, not the tick:**

0. ~~Group the root modules.~~ **Done 2026-08-14** (§7 (r)).
1. ~~Raise the capture rate.~~ **Closed 2026-08-17 — the premise was
   wrong**; the grab path was already at 92 % of the camera (§7 (t)). It
   left three *diagnostic* fixes, because a loss reported with the wrong
   cause sends the next session after the wrong fix — see
   [docs/DECISIONS.md](docs/DECISIONS.md).

**Open, in order:**

4. **DMD ROI photostimulation: image → draw ROIs → project the mask.** The
   operator's stated next big addition (2026-08-18). The workflow is: put the
   DMD in **all-on** (full mirror) mode, grab a frame with the imaging camera,
   **draw ROIs on that frame**, turn them into a DMD mask, and re-image with
   only those mirrors on.
   - **The blocker is registration, not drawing.** An ROI is drawn in *camera*
     pixels; a mask is in *DMD mirror* coordinates (1024×768). Converting one to
     the other needs a measured camera↔DMD transform, and **acqApp has none**:
     the DMD runs with `fit = True`, which computes its own scale to fill the
     panel and **ignores scale, rotation and offset entirely** (decided
     2026-08-12, see "Needs the rig" below). So the optical-alignment question
     this file has deferred twice is now on the critical path — you cannot aim
     at an ROI with a projector you have not registered to the camera.
   - **Actuation applies at two points** (§2): the calibration projection *and*
     every stimulation. Verify open → render → upload → release first, which
     projects nothing, then ask.
   - **The offline half is built and verified (2026-08-18), and nothing in it
     projects or grabs** — the point being that the pipeline is held to a
     transform *we chose* before any light is emitted.
     `devices/dmd/calibration.py` (complementary checkerboards with 1/2/3/4-dot
     corner marks, so a **mirror flip cannot pass as a valid registration**;
     Gray-coded planes; affine/homography fit with outlier rejection;
     `run_calibration(project, grab, …)` takes the two hardware operations **as
     callables**) and `devices/dmd/roi.py` + `roi_panel.py` (rect and circle ROIs
     in camera px, the reachable field outlined, ROIs outside it named rather
     than silently clipped). Measured on the simulated rig: decode median
     **0.40 px**, homography **rms 0.41 px** over 3169 points, ROI round trip
     **100 %** on target with 0 % spill. 51 checks, each with a control.
   - **What is left needs the rig**, in order: (a) run the sweep and see whether
     the fields overlap — the operator expects "about the same size", and the
     first complementary pair answers it; (b) wire `RoiEditor` into the adapter;
     (c) single frame or an average.
   - **Two live traps.** Calibration patterns **must bypass `build_frame`**:
     they are already at the device's size, and its scale/rotation/offset — plus
     `fit`, which overrides all three and is the current default — would
     transform the very geometry being measured. And `alp.project()` uploads
     **one** frame (`SeqAlloc(nbImg=1)`), so the sweep is software-timed,
     project→grab per plane; a hardware-timed one needs `project_sequence()`.

5. **Comment-verbosity trim + optimisation pass** (operator, 2026-08-18).
   Two jobs in one sweep over the tree, worst-first by comment+docstring ratio.
   **Recommend closing it.** Tree total **23.2 % → 22.5 %** (4107 → 3951 of
   ~17.6k lines) with no fact lost — only prose.

   **Why close it (2026-08-19).** Nine more files moved the tree **4008 → 3981
   comment lines, 25.3 % → 25.2 %**. The done half was genuinely verbose; what
   is left is dense fact — `acq/devices.py` reads 47 % only because it is a file
   of `Protocol`s whose docstrings *are* the content. **`stage/driver.py` is
   deliberately excluded**: a verbatim copy of `stage_control`'s, which editing
   would make diverge. The pass was worth more for the **stale facts** it found
   than the lines (three of them, §7 (ae)).

   **The rule, which still applies to new code:** state the non-obvious *why* in
   a line and stop. Keep every measured number, every "this cost a session"
   note, every why-not-the-obvious-thing. Drop restatements of the code, second
   explanations of the same point, and adjectives.

   **File lists, the measured optimisations (the 1633 ms → 7 ms homography, the
   tracker's 9.27 → 1.78 ms/frame, `np.nanmedian`'s masked array, the `avi.py`
   stride bug) and what was deliberately NOT optimised are in**
   [docs/DECISIONS.md](docs/DECISIONS.md).

6. ~~Test the pupil tracker on a sample video.~~ **Done 2026-08-18** (§7 (u)) —
   it found a real bug, not a tuning problem. Two durable findings:
   - **`_test_tracking.py` could not have caught it**, and that is the lesson.
     It validates against *synthetic* eyes, where the pupil edge is the
     highest-contrast thing in frame; real IR footage inverts that — the
     orbit→fur margin is a ~200 grey-level step against the pupil's ~30. A
     synthetic suite passing 15/15 said nothing about it. **Re-run both** after
     any `tracking.py` change: the script for accuracy, a clip for realism.
   - **The rig's clip needs no decoder** — it is uncompressed `IYUV`, where the
     Y plane *is* the grayscale frame. `devices/pupil_cam/avi.py` reads
     IYUV/I420/YV12, Y800 and BI_RGB and **refuses anything compressed by
     name**, since cv2/imageio/av are all absent on 3.14.
     (`../rig_captures/` holds encoder CSVs only — no pupil footage there.)
   - ~~**Auto-seeding cannot work on this rig's framing.**~~ **Overstated,
     corrected 2026-08-19 (ae)** — the observation was right (`coarse_seed`
     returns `None` on every frame, the dark mask being 53 % of the sensor at
     threshold 60), the conclusion was not. See §0's search-limit paragraph.

7. **Make full-frame recording fit the writer** — the one remaining throughput
   constraint: ~2223 MB/s acquired against ~1004 written, so about half the
   frames cannot be stored. **Measured 2026-08-17 through the real path** (ORCA
   → `OrcaFireWorker` → `Recorder` → `HDF5Writer` → D:), 10 s per run, frames
   counted **off the closed file** rather than off a counter:

   | run | offered | on disk | kept | sustained |
   |---|---|---|---|---|
   | full frame, bin 1 | 957 | 506 | **52.9 %** | 1004 MB/s (47.9 fps) |
   | full frame, bin 2 | 1153 | 1153 | **100 %** | 603 MB/s (114.9 fps) |

   - **Binning does not cost frame rate on this camera** — the frame period is
     **8.68 ms at bin 1, 2 and 4**, so binning cuts bytes, not time. 2×2 gives ¼
     the data at the same 115 fps. **If the science tolerates 2216×1184 this is
     the whole answer and needs no code**; otherwise cap the rate (the panel
     warns and names the exposure) or take a smaller ROI.
   - **1004 MB/s, not the 1165 benchmark** — a benchmark measures the writer,
     this measures the path. `_WRITER_MBPS` is now 1000, moving the advised cap
     from a wrong 60 fps to a right 48.
   - **Done 2026-08-17: the offered presets stop at `4432x512`**, below which a
     preset could only produce an unrecordable session. `MIN_PRESET_ROWS` trims
     the *dropdown* only — the datasheet table keeps all nine rows, because
     `readout_fps()` interpolates them for binned ROIs (512 rows at bin 4 reads
     out like 128). Four checks in `test_readout_fps` hold that line apart.

8. **Project through the full app.** *Half closed 2026-08-12*: everything
   short of emitting light runs through the app's own path, and the geometry
   was swept against the real 1024×768 panel. **What is left needs someone at
   the hardware** — that Display projects, that Stop halts, and that `/dmd`
   carries 0 and −1 around it. **Close `dmdGUI_project` first**; one process
   owns the USB. Two traps, both live: a **checkerboard cannot show a
   geometry error**, and **`fit` overrides scale, rotation and offset**, so a
   sweep with `fit` on measures nothing. Detail in
   [docs/DECISIONS.md](docs/DECISIONS.md).
9. **Close the loop on the rig.** Phase 5 is built and mock-verified but has
   never seen an animal, and the one number it needs cannot be guessed here:
   **what wheel speed counts as "running"** for this rig's V/rev and diameter.
   The tab is designed for finding it — disarmed, the rule still evaluates and
   the readout shows whether the condition is met, so the threshold can be set
   against a live animal without actuating anything. Arm only after that reads
   sensibly. Start with the puffer (a puff is recoverable; a stimulus train
   mid-experiment is not), `retrigger` off, and a `max_fires` ceiling.
10. **Decide which wheel speed a rule should watch.** The panel offers both and
   the file records the choice, but the default is `wheel_speed_live` on the
   grounds that a closed loop should act while the animal runs. Measured this
   session: the recorded speed crosses the same threshold **1.15 s** after the
   live one. If the paradigm wants the rule to agree exactly with the recorded
   trace, switch it — this is a scientific call, not a default worth inheriting.

**Needs the rig — but check first, since 2026-08-14 found most of it here:**
- The DMD's *optical* alignment: the electrical path is proven and the geometry
  math is now verified against the real panel, but nothing has confirmed where
  the projected pattern lands **on the sample**. That is what `dmdGUI_project`
  is for.
  **Decided 2026-08-12: acqApp stays on `fit`, deliberately** — worth stating,
  because it reads as a discrepancy every time someone checks. `dmdGUI_project`
  is aligned at **132.4 %**; acqApp saves `scale_pct = 100.0` with `fit = True`,
  and `fit` ignores the scale entirely. So acqApp is *not* projecting at the
  standalone app's registration, and that is the operator's call, not a bug.
  Revisit when the field must be registered to the optics rather than fill the
  panel — which is exactly what §6 item 1 now needs.
- ~~Phase 0's camera throughput number.~~ **Closed, re-measured 2026-08-17
  through `OrcaFireWorker`: 105.92 fps, 2223 MB/s** at full frame (4432×2368,
  20.99 MB/frame), against a camera offering 115.26 — 92 % of the link. **The
  read path is not the limit; the writer is.** Size the ring buffer (#14) from
  2223 MB/s. The 2026-08-14 figure of 46.17 fps / 969 MB/s is **withdrawn**.
- Encoder **wheel diameter** — still unmeasured; until it is set the app reports
  rev/s and rev rather than mm/s and mm. The panel keeps whatever is typed in,
  so measure once. (`volts_per_rev` is *not* open: **4.912**, measured from a
  rig capture — [docs/HANDOFF.md](docs/HANDOFF.md) "Hardware facts".)
- **#13 on a real board:** the first session should report
  `wheel_timestamp_source = "hardware"` and a `wheel_rate_actual_hz` at or very
  near the requested rate. `"software"` means the 6363 refused
  `cfg_samp_clk_timing` on that channel — the run is still valid, but the speed
  carries scheduler jitter and the printed reason is in the console.
- Real-hardware validation of *everything* in phases 2–4: no rig hardware has
  ever run this code.

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what's next.

### 2026-08-21 (af) — "tracking is still awful": it was the eyelids
- **The operator was right, and the region was only half of it.** The region
  fixed *finding* the eye; it does nothing to the fit. Rendering the fit over
  the clip and reading it: only **29 of 64 rays** survived, confidence averaged
  **0.26** against a 0.10 floor, and the circle's bottom arc ran out into fur.
- **The cause, from two independent measurements that agree.** Binning ray
  survival by angle puts the failures in 60–160° and ~235–290°; sweeping the
  boundary intensity directly puts the occlusion in the same places. Where a
  lid crosses, the rays find the **lid's** edge and those points enter the fit
  like any other, for the robust rejection to fight every frame.
- **`find_circular_edge` has taken `exclude_deg` all along** — its docstring
  even gives the eyelid example — and **nothing exposed it.** Now
  `PupilSettings.exclude_deg`, with **"Find lids"** on the preview bar:
  `tracking.lid_sectors()` measures the sectors from a run of fits rather than
  asking the operator to know that the lower lid is at 70–155° (and the
  convention, 90° = image *bottom*, is a trap). Drawn on the preview in red,
  and recorded in the session file — a radius trace fitted from two thirds of
  the ring cannot be compared with one fitted from all of it.
- **Measured, auto-seed only, no click:** frames **149/151 → 151/151**,
  confidence **0.260 → 0.338**, rms **1.16 → 0.98 px**, p95 frame-to-frame
  radius jump **0.98 → 0.48 px**. The visible win is the second half of the
  clip, where the old trace sawtooths ±1.5 px and drops out and the new one
  does not. Stable across how long it watches: 30, 60 and 120 frames all give
  a sector in the same place and all beat the baseline.
- **A hypothesis of mine that the data killed, recorded so it is not retried:**
  the boundary radius varies 38→60 px by direction, which looked like an
  off-axis *ellipse*. It is not. Fitted to the directly-measured boundary a
  circle beats an ellipse (1.88 vs 3.63 px rms) — the variation is occlusion,
  and the app's r=53 already matches the unoccluded arc. Separately: **ellipse
  mode is broken on real footage** (8/151 frames), because `_BAND_ELLIPSE`
  sweeps r×0.35–2.9 = 18–154 px, far out into fur. Not fixed — this rig fits
  circles — but it passes `_test_tracking.py` on synthetic eyes, which is the
  same synthetic-suite blind spot as §6 item 6.
- Suite **756 → 776, 23/23**; `_test_tracking.py` 15/15.

### 2026-08-19/20 (ae) — a search limit for the pupil: 0/151 → 149/151 on the rig's clip
- **The operator's ask: a limiting circle, because the animal is head-fixed and
  the eye only ever occupies one part of the frame.** It is a circle in camera
  px (`limit_x/limit_y/limit_r`, r ≤ 0 = whole frame) that bounds `coarse_seed`
  and refuses a fit centred outside it. Set by dragging a `pg.CircleROI` on the
  preview or by typing three numbers; drawn on the preview whenever it is in
  force, and recorded in the session metadata — it decides which fits were
  accepted, so the trace cannot be read without it.
- **Validated on the rig's own clip, not a synthetic one.** This machine is the
  rig, so `E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi` is right here.
  Auto-seed only, no click anywhere: **coarse_seed 0/151 → 151/151, tracked
  0/151 → 149/151**, centre 853.4 ± 1.0 / 506.4 ± 1.6 px, radius 53.2 ± 2.0 px,
  2.59 ms/frame. PLAN's diagnosis was exactly right — the frame is **53.3 %**
  below threshold 60, over the >50 % guard.
- **Two things the measurement corrected, both of which this file had asserted:**
  "auto-seeding cannot work on this rig's framing" is too strong (it works over
  a 15-level threshold window, 30–45, with the shipped 60 outside it — the limit
  widens that to 25–80), and the crop is **not simply faster**: a whole-frame
  call that bails at the guard is the cheapest of all, 1.96 ms against the
  limit's 5.67 ms, because it gives up before labelling anything. The crop stops
  the limit from *costing* a sensor-sized labelling. Both now said in the code.
- **The trap the limit inherits, written into a test so it is not rediscovered:**
  drawn tight around the pupil, the same >50 %-dark guard fires *inside* the
  circle. The tooltip says to draw it generously.
- **`tests/test_pupil_limit.py`, 43 checks**, each with a control — including
  that a drag writes back as **one** settings change (three would drop the
  annulus lock three times, since `limit` is in `_RESEED_ON`) and that a click
  outside the limit is refused rather than queueing a search that can only fail.
  Suite **688 → 732, 23/23**; `_test_tracking.py` still 15/15.
- **Trim pass: nine more files, and a recommendation to close it** — see §6
  item 5. It found three stale facts, which was worth more than the 27 lines.
- **This file was 1432 lines**, against §8's target of ~400; the operator asked
  for it shorter mid-session. **1432 → 646**, in two moves and with nothing
  deleted: §7 held 16 sessions where §8 says ~3, so (ab) and older went to
  `docs/SESSIONLOG.md`; and the closed reasoning in §5b and §6 went to a new
  **[docs/DECISIONS.md](docs/DECISIONS.md)**, each block replaced by the verdict
  plus a pointer. What is left is either live or a fact still being used, so
  ~400 is not reachable without losing something — the next real reduction is
  §6 items 8–10 and "Needs the rig", once that rig session happens.

### 2026-08-19 (ad) — `_SIGN` answered; the suite is green for the first time
- **The operator settled it: a mouse running forward reads positive, and the
  voltage ramps UP as it does.** So `_SIGN = +1.0` was right all along and the
  code under test was never wrong — **three fixtures were**, each independently
  encoding a falling ramp:
  `test_encoder_derive.sim()` (`frac = (-rev_s * t) % 1.0`, with a docstring
  asserting "the rig's forward direction is the falling one"),
  `test_encoder_timing`'s fake DAQ task, and `MockEncoderWorker`.
- **A fourth thing had to move with them**: `sim()` finds each reset with
  `np.diff(frac) > 0.5`, which only detects a *falling* ramp's wrap. Flipping
  the ramp alone would have found no wraps, silently stopped smearing them, and
  turned the test that exists for smeared resets into one that never sees one.
  Now `< -0.5`.
- **19/22 → 22/22, 670 → 688 checks.** Verified beyond the suite: driving
  `_EncoderBase` with a rising ramp gives **+188.5 mm/s** forward, −188.5 mm/s
  backward and exactly 0 at rest, against an expected 0.4 × π × 150 = 188.5.
- **The durable fact, now written where §0 will be read: forward = rising
  voltage = positive speed and distance.** The old note claimed the opposite in
  a docstring, which is how it survived three sessions — a fixture that asserts
  a hardware fact is a claim, and this one was never checked against the rig.
- Nothing else changed. The wheel diameter is now the last unmeasured constant
  and has been promoted into §6's top three.

### 2026-08-19 (ac) — the three UI changes the operator asked for
- **Record is now the largest control on the status bar**, red while armed, and
  reads `● Record` / `■ Stop rec`. It was the same size as Emulate — the one
  button whose wrong state costs an experiment, sized like a dev toggle.
  `style.record_btn()`.
- **A live recording readout**: `● REC  m:ss   N.NN GB`, green, turning red with
  `⚠ N samples shed` the moment the ring or the writer loses anything. **The
  size is read off the file on disk**, not from a running total of what was
  enqueued — those two differ exactly when it matters, and the number worth
  trusting is the one that survived. It is a *permanent* status-bar widget
  because `showMessage` is transient: the tick overwrites it and any module
  calling `status()` wipes it, so a recording indicator could not live there.
- **The pupil tab is 12 rows → 4.** Threshold, both radii and the overlay stay;
  the ten tuning controls moved into a collapsible **Advanced tracking** group.
  It **auto-expands, and names what changed in its title**, whenever any of them
  differs from the shipped default — a tuned value hidden behind a closed group
  would make an unusual rig look stock. Seven checks, including that the split
  is by *parentage* (the right widgets really are inside the collapsible half)
  and that a collapsed group still reports its settings.
- Suite **663 → 670**. Verified by re-rendering the window and the status bar
  offscreen and reading them, as in (ab) — that is now the way to check UI work
  here, and it costs about a minute.

Entries before 2026-08-18 (ab) are in
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
