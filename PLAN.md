# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-19 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done; audit remediation 100 % (22 of 22). **2026-08-18/19** were a correctness-and-speed sweep, all of it found by driving real workloads rather than reading code (§0): the pupil tracker now works on real footage and is **3.5× faster**, the DMD registration and ROI editor exist offline and no longer need **11.4 GB** to run at camera scale, **boot 13.4 s → 8.1 s**, the config file survives a crash mid-write, and the UI stopped calling a frame-dropping setup healthy. Suite **688 checks, 22 files — all green** (`_SIGN` settled 2026-08-19). §5b **A3 triggered** and is the one open architecture item. Next: §6's top three. |

---

## 0. Start here (fresh session orientation)

Read this section, then §6 (next actions). That is the whole of what a fresh
session needs. §5b is reference — consult the item you're working on rather than
reading it through — and the two archives it points at
([docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md),
[docs/SESSIONLOG.md](docs/SESSIONLOG.md)) are closed work: **open them only when
chasing a specific item number or an old decision.** They were split out of this
file precisely so nobody reads 300 lines of finished work to start.

**Where the project stands.** Phases 0–5 are built and mock-verified and the
2026-08-10 audit is closed. **Phase 0 closed 2026-08-17** with the camera
throughput number, so the roadmap is clear through phase 5. The test suite is
the contract: **688 checks, 22 files, ~48 s**, and as of 2026-08-19 it is
ALL GREEN. Run it before and after anything. For pupil work also run
`devices/pupil_cam/_test_tracking.py` (15 synthetic ground-truth checks): the
suite and that script cover different failures, and 2026-08-18 showed the
script passing 15/15 while the tracker could not follow a real eye at all.

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py
```

Use the **absolute** path to that interpreter. The shell usually starts in
`Desktop\python` (the *parent* of this repo), where `.venv\Scripts\python.exe`
resolves to nothing and Python reports a baffling "the module '.venv' could not
be loaded". Python here is **3.14** — no cv2 wheels exist for it, which is why
`devices/pupil_cam/tracking.py` is hand-rolled numpy.

**Sibling projects are proven code, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` are standalone apps for the stage and the DMD that already
work on this hardware. acqApp deliberately *shares their config files* rather
than duplicating their state — the stage's calibration lives in
`stage_control/config.json`, the DMD's ALP path and optical alignment in
`dmdGUI_project/dmd_config.json`. Before writing a device path from scratch,
look next door: `devices/dmd/alp.py` is a port of `dmdCommandLine.py`, and it is the
reason #5 took one session instead of several.

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

**`_SIGN` is settled and the suite is fully green — 688 checks, 22/22.** The
operator confirmed 2026-08-19: **a mouse running forward reads positive, and the
encoder voltage ramps UP as it does.** So `_SIGN = +1.0` was right and three
*fixtures* were wrong — they generated a falling ramp and asserted in a docstring
that "the rig's forward direction is the falling one". Fixed in
`test_encoder_derive.sim()`, `test_encoder_timing`'s fake task and
`MockEncoderWorker`. **The rig fact to remember: forward = rising voltage =
positive speed and distance.** Everything is committed and pushed; the working
tree is clean.

**PICK UP HERE — §6's "THE NEXT THREE THINGS".** In order: settle `_SIGN` (one
question for the operator, three red tests), the DMD ROI work at the rig (asks
first — it projects light), and finish the trim/optimise pass (§6 item 5 names
the file it stopped at).

**A method that earned its keep over 2026-08-18/19 and is worth reusing.** Every
real defect found in those sessions was in code that had only ever run at *toy*
scale, and every one was found by **driving the real workload and measuring**,
not by reading. Profiling the tracker against the rig clip found a 2× win and
pointed at an AVI bug; sizing the DMD calibration at the actual camera found
11.4 GB where 1.6 was needed; **rendering the UI offscreen and looking at it**
found the Save tab calling a frame-dropping setup healthy. Do that to the next
feature before trusting it. Rendering recipe: `QT_QPA_PLATFORM=offscreen` plus
`QT_QPA_FONTDIR=C:/Windows/Fonts` — without the second, all text draws as boxes.

**§6 item 1: DMD ROI photostimulation.** Image with the DMD
all-on, draw ROIs on that frame, project the mask back. Read that item first:
the headline is that **the blocker is registration, not drawing** — an ROI is in
camera pixels, a mask is in DMD mirrors, and acqApp has no measured transform
between them because the DMD runs with `fit=True`, which ignores scale, rotation
and offset outright. The deferred optical-alignment question is now on the
critical path.

The rest of this section is context for a project that is otherwise in a good
state; nothing below is blocking.

**The camera work that filled 2026-08-17 is finished and closed.**
The grab path was measured through `OrcaFireWorker` itself on 2026-08-17:
**105.92 fps / 2223 MB/s** at full frame against a camera offering 115.26 — 92 %
of the ceiling, not the 40 % that 46.17 fps implied. **Phase 0's 46.17 / 969 was
a measurement artefact and is withdrawn** (§7 (t)). Nothing above the hardware
is eating 13 ms per frame, because there is no 13 ms.

What this re-points: acquisition is not the constraint, **the writer is**.
Full frame produces ~2223 MB/s and the write path sustains a **measured
1004 MB/s end to end**, so **recording caps near 48 fps** whatever the buffers
do — a bin-1 session stores 52.9 % of its frames. **2×2 binning stores 100 %**
at the full 114.9 fps and is the standing recommendation. `_check_link.py` was
right in kind all along ("recording will cap near 58 fps") where this file
called it "superseded"; it was only optimistic by the benchmark-vs-path gap.

**Two standing instructions from the operator:**
- **Write comments terser than the surrounding style.** This codebase's prose is
  considered too long; state the non-obvious *why* in a line and stop. Two trim
  passes have taken it 25 % → 23 % (§7, 2026-08-13 (p), (q)) → **22.5 %**
  (2026-08-18 (v)). The second is **unfinished** — §6 item 2 names where.
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

**Committed 2026-08-12** in `69c657c` (28 files): the operator's settings-window
work — the settings tabs moved from a dock into a `SettingsDialog` pop-up
(`main.py`), a measured `default_size()`, a DMD panel that drops the timing
controls in favour of `all_on` and a permanent static hold — together with
phase 5 (`closed_loop.py` and its wiring) and §5b A1 (`devices.py`). Both
strands are named separately in that commit message. Mock-verified only; none
of it has run on the rig.

**Two things that changed on 2026-08-13 and are not obvious from the code.**

- **The `_toy.py` harnesses are gone** (all five, plus the four per-package
  `recording.py` modules that only served them). To bring one device up alone:
  start the app, tick that module in the startup picker, and press **Free run**
  — devices and previews with the shared clock never started, so nothing can be
  recorded. The pupil tracker's tuning overlay (annulus, per-ray edge points,
  click-to-seed) moved into the app as **Show search overlay** in its tab.
- **`scratch/` and `toy_output/` are gone (2026-08-14).** `cam_grab.py` was kept
  only to take phase 0's camera number; that was taken (46.17 fps, 969 MB/s) and
  the script deleted with phase 0. The five raw captures beside it were **moved,
  not destroyed** — `../rig_captures/`, outside the repo, with a README naming
  what each one established. They were gitignored, so that folder is the only
  copy.

**`main.py` is the operator's active file.** The settings-window work is theirs
and ongoing: **ask before touching its dock/settings code**, and don't
restructure it (that is why §5b A5 split `modules.py` and deliberately left
`main.py` alone; it is 787 lines).

**A warning that cost most of a session.** An editing pass aimed at "the
settings" rewrote every file with *settings* in its name, including
`devices/stage/settings.py` — which it reduced to stubs, so `load_settings()` returned
hardcoded defaults instead of reading `stage_control/config.json` and
`save_axis_updates()` became a `pass`. It also deleted `SettingsDialog._PAD` and
most of `DmdModule.metadata()`. Only three of those five broke a test; the
metadata deletion was silent. **If the stage or DMD starts behaving oddly, diff
against `HEAD` before debugging** — and note that a green suite would not have
caught the one that mattered most.

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
- **This machine IS the rig computer** — confirmed by the operator 2026-08-17,
  and it resolves three sessions of confusion. Every "surprise" below was the
  same fact seen from the wrong end: the devices are here because *here is the
  rig*. Two consequences that outrank the rest of this section. **First, the
  actuation rule is live, not theoretical** — an animal may genuinely be under
  the objective while this session runs, so the "ask before actuating" rule
  below is the operating rule, not a precaution for later. **Second, "measure it
  at the rig" and "measure it here" are the same errand**, so anything §6 defers
  to a rig trip should be re-read as doable now.
  *If work ever resumes on a different machine, this line is the first thing to
  re-check.* The older wording is kept below because it is how the mistake was
  made, and the shape of it is worth recognising again:
- **This machine has *some* hardware — check, don't assume.** This rule used to
  read "the laptop has no hardware", and on 2026-08-12 that was wrong: the
  **DMD is attached to this machine** and opens from `acqApp/.venv`. **It was
  wrong a second time on 2026-08-14:** `probe_all` here reports the ORCA
  (1 DCAM camera), `Dev3 PCIe-6363` for wheel *and* puffer, and COM54 for the
  stage — everything but the Basler pupil camera. Treat "rig-only" as a claim
  to re-check, not a fact. Two caveats: a probe is *enumeration*, so COM54
  "present" is not a working serial link (`devices/stage/driver.py` still fails
  to open it here), and presence is not permission — the stage and puffer are
  actuators. So: write + commit + push
  here, pull + run + fix on the rig, but *probe before concluding* a device is
  absent — `devices/dmd/alp.py`'s `AlpDevice.open()` answers in 0.14 s. Anything that
  genuinely can't be checked here goes in §6 "Needs the rig".
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

Nothing from that list is open. **B2 closed 2026-08-12**: `SESSION_HANDOFF.md`
folded into [docs/HANDOFF.md](docs/HANDOFF.md) and removed, its inbound links
repaired. Its session narrative and file list went (that wiring has since moved
into `modules.py`); its three durable facts stayed — the two camera-crash causes,
the measured encoder signal, and the still-open CoaXPress-vs-USB3 fps question.
Folding it also **answered two of HANDOFF's own open items**: `volts_per_rev` is
a measured 4.912, and the encoder voltage does wrap.

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

- [x] **A1 The mock/real device pairs have no declared interface.** ✅
      **Was the one worth doing.** Nine `getattr`/`hasattr` probes stand in for a
      type: `modules.py:363,371,375,451,666,668,907,919` and `:200`. Some are
      honest (the LED controller really has no `set_sink`); `modules.py:451` is
      pure hedging, since `MockPupilCameraWorker.set_exposure` exists and is
      documented as "kept for API parity". The cost is not tidiness — it is
      that `getattr(c, "device_name", "none")` writes a *plausible wrong value*
      into the session file when a mock drifts from its real twin, which is the
      exact failure class §5 spent twenty items removing. Near-miss on
      2026-08-12: adding `device_name`/`resolution`/`on_pixels` to
      `DmdController` meant remembering all three on `MockDmdController`, with
      nothing to catch the omission.
      **Done 2026-08-12.** New `acq/devices.py` holds seven small `Protocol`s
      (`DeviceWorker`, `TimestampedWorker`, `CameraWorker`, `ClockedWorker`,
      `ExposureControl`, `OutputController`, `RecordingOutput`,
      `ProjectorController`), split rather than fat: the eye-tracking LED is
      deliberately *not* a `RecordingOutput`, which is why `detach_sink` now
      asks `isinstance(c, RecordingOutput)` instead of
      `hasattr(c, "set_sink")` — same answer, but the question has a name.
      Adapters annotate `worker`/`controller` and read metadata directly.
      **The plan above was wrong about one thing:** structural typing alone
      changes nothing at runtime *and this project ships no type checker* (the
      rig installs `requirements.txt` and nothing else), so a Protocol nobody
      asserts catches exactly nothing. `tests/test_device_contracts.py`
      (33 checks) is what makes it bite — conformance per twin plus a
      real/mock API-parity check, both with controls. It immediately found a
      live instance: `skipped_frames` existed on `OrcaFireWorker` only, so
      `cam_dropped_frames` read a `getattr(..., 0)` default and a lossy real
      run and a mock filed the same 0.
- [x] **A2 A new module needs three registrations, not the two documented.** ✅
      `adapters.ADAPTERS`, `config.MODULES` **and** a `style.HEX` colour; the
      third was undocumented and only showed up as a `KeyError` at build time.
      Now stated at `ADAPTERS`, which is where someone adding a module is
      looking. Confirmed the hard way: `closed_loop` needed exactly those three.
- [ ] **A3 Mock/real selection is hard-wired inside each adapter.** Every
      `build_session`/`build_controller` imports both concrete classes and
      picks with `if emulate:`, so a test cannot inject a device — it has to
      monkeypatch `sys.modules`. That is precisely why `block_real_devices()`
      exists rather than a one-line fake injection (C3). Defensible for six
      known instruments; revisit if a seventh needs a third variant. ⬜
      **Reviewed 2026-08-12 and deliberately left open.** The seventh module
      arrived (`closed_loop`) and did *not* trigger this: it has no mock/real
      pair at all — its device is another module's signal. So the condition
      this item set for itself has still not been met. Leaving it.
      **Triggered 2026-08-18, from the direction this item did not predict.**
      Not a seventh *module* but a **third variant of one device**:
      `VideoFileCameraWorker` replays a clip as the pupil camera, so
      `PupilCamModule.build_session` is now an `if` chain over three concrete
      classes it imports itself. The cost was paid immediately — testing the
      tracker on real footage first needed a `sys.modules` monkeypatch of
      exactly the kind this item names, before the source was made a real
      setting. A `build_session(source_factory)` seam would have made that a
      one-line injection. Still not urgent, but the condition is now met.
- [x] **A4 The adapter's "narrow surface" onto the window is a docstring
      promise.** ✅ Adapters get the whole `win` object; the seven-method
      contract in the header was enforced by nothing. A `Protocol` here too
      would make it real, and pairs naturally with A1.
      **Done 2026-08-12.** `acq.devices.ModuleHost` — the same idea pointed up
      instead of down. The docstring was already wrong when this was written:
      it named seven members while the code used nine, because the closed loop
      added `module_keys` and `signal_sources` with nothing to notice.
      `test_device_contracts` checks **both** directions, which matters — a
      Protocol alone only proves the window still *provides* the surface, and
      the drift that actually costs something is an adapter reaching *past* it
      into `win._save_panel`. So the second half scans the adapters' source and
      fails on any `self.win.X` that `ModuleHost` doesn't declare. Widening the
      surface is now a deliberate line in `acq/devices.py`.
- [x] **A5 `main.py` (899) and `modules.py` (1204) are large.** ✅ `main.py`
      still carries window chrome, docks, theme, session start/stop and
      recording wiring; `modules.py` was seven cohesive adapters in one file.
      Low priority and partly the A-side of the trade named above — but if
      `modules.py` grows again, split it per instrument rather than per layer.
      **`modules.py` done 2026-08-12; `main.py` deliberately not.** The trigger
      this item set fired: the closed loop took the file from 939 to 1204 lines
      (+28 %). It is now `adapters/` — `base.py` (the adapter, the two shared
      widget builders, the plot constants), one file per instrument, and
      `__init__.py` holding the registry and the lifecycle table. Per
      instrument, not per layer, as instructed: a session at the rig is spent on
      *the wheel* or *the DMD*, and the adapters were already independent of
      each other (they import only `base`). Bodies moved verbatim — verified
      line-by-line against the pre-split file, the only differences being
      imports, section banners and the two lines A4 changed. Callers see
      nothing: `build_adapters`, `.ADAPTERS` and `.ModuleAdapter` are where they
      were (the package itself was renamed `modules/` → `adapters/` on
      2026-08-14). **`main.py` was left alone on purpose** — it is the
      operator's active file this week (the settings dialog work), and
      restructuring under someone's in-progress edits is how the collateral
      damage earlier this session happened.

## 6. Next actions

**THE NEXT THREE THINGS**, per §8's own rule. Everything after them is reference
kept for its reasoning, not a queue.

1. **DMD ROI photostimulation, the rig half** (item 4 below). The offline half
   is built, verified and now sized for a real camera. What is left needs
   someone in front of the hardware and **projects light**, so it is an ask:
   run the sweep, check the fields overlap, wire `RoiEditor` into the adapter.
2. **Finish the trim/optimise pass** (item 5 below) — it carries its own ordered
   file list and stops where it stops.
3. **Measure the wheel diameter** — the last unmeasured constant. Until it is
   set the app reports rev/s and rev instead of mm/s and mm, and the closed
   loop's threshold has to be set in revolutions. `volts_per_rev` is already
   measured (4.912) and the sign is now settled, so this is the only thing
   between the wheel and fully physical units.

~~Settle `_SIGN`.~~ **Done 2026-08-19** — the operator answered it; see §0 and
§7 (ad). The suite went 19/22 → **22/22**.

**Closed — kept for the reasoning, not the tick:**

0. ~~Group the root modules.~~ **Done 2026-08-14** — option (a), plus the
   operator's call to gather the six instrument packages under `devices/`. See
   §7 (r).

1. ~~Raise the capture rate.~~ **Closed 2026-08-17 — the premise was wrong.**
   The grab path was already at 92 % of the camera. See §7 (t) for the numbers
   and for why the old figure was low. **The three findings it left behind are
   all closed 2026-08-17** — as *diagnostics*, not as tuning. A loss reported
   with the wrong cause costs as much as a silent one, because it sends the next
   session after the wrong fix:
   - **The drop message blamed the writer for a driver-buffer overflow.** It
     cannot be the writer: the sink only enqueues (`Recorder.put` → ring, no
     disk I/O), so a slow writer sheds in the *ring* and is counted there. A
     camera skip means *this loop* did not drain in time. Now
     `_skip_report()`, which names the read loop and says the writer is a
     separate count. The status bar had said this correctly all along.
   - **`_buffer_frames` announces when the byte cap beats the time target.**
     Still 0.33 s at full frame, and **still not retuned** — `_BUFFER_BYTES` is
     a memory call, and the arithmetic in §7 (t) shows a bigger buffer cannot
     fix a *sustained* deficit anyway. What changed is that it now says so, and
     prints what the full 2 s would cost (3.9 GiB).
   - **`_maximise_readout_speed` reports absence** instead of silently doing
     nothing. Returns `absent`/`set`/`already`/`error`;
     `get_all_readout_speeds()` is `[]` on this model.

   Twelve checks in `test_recording_losses`, each with a control — the warning
   must stay quiet on a small frame, and a camera that *does* offer `fast` must
   still be switched to it.

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
   - **How to get it:** project a sparse **asymmetric** pattern (≥3
     non-collinear spots, or a grid) with the DMD, image it on the ORCA, and
     least-squares fit an affine — projective if the throw is oblique — from DMD
     mirrors to camera pixels. Invert it to turn ROIs into masks. Asymmetric is
     not optional: item 4 below records that a **checkerboard cannot show a
     geometry error**, being symmetric under every transform being measured.
     Store the fit next to the existing alignment rather than inside `fit`.
   - **Actuation applies at two points** (§2): the calibration projection *and*
     every stimulation. Verify open → render → upload → release first, which
     projects nothing, then ask.
   - **The offline half is built and verified (2026-08-18).** Nothing in it
     projects or grabs, which is the point — the whole pipeline is held to a
     transform *we chose* before any light is emitted.
     `devices/dmd/calibration.py`: complementary checkerboards with corner marks
     that carry 1/2/3/4 dots (mutually distinguishable, so a **mirror flip
     cannot pass as a valid registration** — four identical marks could not
     reveal one), Gray-coded bit planes, the decode, and an affine/homography
     fit with outlier rejection. `run_calibration(project, grab, …)` takes the
     two hardware operations **as callables**, so the sweep is repeatable, and
     `DmdCalibration` saves to JSON with its rms and point count — a transform
     with no provenance cannot be judged later.
     `devices/dmd/roi.py` + `roi_panel.py`: rectangular (rotatable) and circular
     ROIs in camera px, drawn and edited over a snapshot, with the DMD's
     reachable field outlined and ROIs outside it named rather than silently
     clipped.
     Measured on the simulated rig: decode median **0.40 px**, homography
     **rms 0.41 px** over 3169 points, and the ROI round trip lands **100 %** on
     target with 0 % spill. 51 checks in `test_dmd_calibration.py` and
     `test_dmd_roi.py`, each paired with a control — including that an affine
     model *must* show the keystone in its residual, and that a 40 px
     registration error *must* miss.
   - **What is left needs the rig**, in this order: (a) run the sweep and see
     whether the fields overlap — the operator expects "about the same size" and
     the first complementary pair answers it; (b) wire `RoiEditor` into the DMD
     adapter (grab a snapshot with `all_on`, edit, project the mask); (c) decide
     whether the ORCA snapshot is a single frame or an average.
   - **What already exists**, so none of it gets rewritten: `devices/dmd/alp.py`
     (open/upload/display, a port of `dmdCommandLine.py`), the DMD panel's
     `all_on` and permanent static hold. Note `alp.project()` uploads **one**
     frame (`SeqAlloc(nbImg=1)`), so the sweep is software-timed, project→grab
     per plane; a hardware-timed version would need `project_sequence()`.
   - **Calibration patterns must bypass `build_frame`.** They are already at the
     device's size, and its scale/rotation/offset — and `fit`, which overrides
     all three — would transform the very geometry being measured. `fit=True` is
     the current default, so this is a live trap for the projection step too.
   - Still open for the operator: whether ROIs persist across sessions, and
     whether a mask is one static pattern or a sequence.

5. **Comment-verbosity trim + optimisation pass** (operator, 2026-08-18).
   Two jobs in one sweep over the tree, worst-first by comment+docstring ratio.
   **Started; resume from the list below.** Tree total **23.2 % → 22.5 %**
   (4107 → 3951 of ~17.6k lines) with no fact lost — only prose.

   **Rule for the trim:** state the non-obvious *why* in a line and stop. Keep
   every measured number, every "this cost a session" note, every
   why-not-the-obvious-thing. Drop restatements of the code, second explanations
   of the same point, and adjectives.

   **Done** (`9131017`, `7b40bb9`, `383c787`, and the last of the session):
   `main.py` `wheel/acquisition.py`
   `pupil_cam/settings.py` `console.py` `closed_loop/{__init__,settings}.py`
   `adapters/{__init__,base,dmd,closed_loop,voltage_cam}.py`
   `pupil_cam/{track_worker,rays,tracking,fits,video}.py`
   `acq/{worker,ring_buffer,sync,writer}.py`
   `voltage_cam/{presets,acquisition}.py` `dmd/{alp,calibration}.py`
   `tests/_harness.py`

   **Not done, in order** — highest ratio first, which is where the prose is:
   `voltage_cam/_check_link.py`, `acq/{devices,recorder}.py`,
   `adapters/{wheel,pupil_cam,stage}.py`, `tests/test_encoder_derive.py`,
   `tests/test_device_contracts.py`, `stage/{settings,control,driver}.py`,
   `dialogs.py`, `saving/config.py`, `probe.py`, `config.py`,
   `pupil_cam/{acquisition,avi}.py`, `closed_loop/worker.py`,
   `dmd/{control,roi,roi_panel}.py`, `puffer/control.py`, the remaining tests.

   **The two files that were excluded are now done** (operator authorised them,
   2026-08-18). `devices/wheel/acquisition.py` was unblocked by committing the
   `_SIGN` flip on its own first (`25ed583`), so it stays findable instead of
   buried in a trim; `main.py` was trimmed without restructuring — no code
   moved, only comments.

   **Optimisations found so far, all behaviour-identical and all measured:**
   - `calibration._homography` called `np.linalg.svd(A)` with the default
     `full_matrices=True` on a (2n, 9) matrix, building and discarding a
     (2n, 2n) U. **1633 ms → 7 ms** over 3169 points, rms identical. This is in
     the path the rig calibration will run.
   - `rays._bilinear` cast the **whole frame** to float32 per call to read a few
     thousand annulus points. Gather first, cast after.
   - `find_circular_edge` hoists cos/sin out of the refinement loop;
     `coarse_seed` uses `argpartition` for its top-4 blobs; `fits._ransac_circle`
     does all 48 circumcircles and their residuals in one broadcast.
     Together with the above: tracker **9.27 → 4.00 ms/frame** (108 → 250 fps)
     on a 1600×1200 synthetic eye, same radius to 2 dp.
   - `presets.readout_fps` sorted its datasheet table on every call.

   - `_EncoderBase._report` ran `np.polyfit` **per sample** — an SVD behind a
     Vandermonde, 120×/s, to fit a straight line. Now the closed-form slope
     `cov(t,p)/var(t)`, plus `searchsorted` for the window (the buffer is
     time-ordered, so it is a contiguous slice, not a mask). **49.3 → 31.0 µs
     per sample**, speed and distance identical to 6 dp. Honest caveat: the
     wheel was never a bottleneck at 120 Hz — the value is that a straight-line
     fit no longer goes through an SVD. `np.fromiter` over the deque is now the
     cost, and fixing *that* needs a preallocated ring, which is not worth the
     complexity in load-bearing code.

   **How to verify anything in this item:** `tests/run_all.py` **and**
   `devices/pupil_cam/_test_tracking.py` — the second is what covers the pupil
   maths against ground truth, and an optimisation that quietly changed a fit
   would show there first.

   **A survey pass followed (2026-08-18, §7 (w)), and it is where the real
   findings came from.** Guessing at hot spots was worth less than one profile
   of the tracker against the actual rig clip. What it turned up:
   - **`np.nanmedian` was ~30 % of the tracker** — numpy falls back to
     `_nanmedian_small`, which builds a **masked array per call**, for rows
     shorter than ~600. `rays._nanmedian_rows` sorts instead (NaN sorts last,
     the valid count indexes the middle). Real clip **3.49 → 1.78 ms/frame**;
     checked against `np.nanmedian` on 3000 random NaN patterns.
   - **`avi.py` ignored the DIB row stride** — a real latent bug, not a
     slowdown. Scanlines are padded to a 4-byte boundary, so any BI_RGB clip
     whose `width × bytes-per-pixel` is not 4-aligned decoded **progressively
     sheared**. Invisible in the old test because it used W=96, where 96×3
     already is aligned. Fixed, with a W=97 case that fails without the fix.
   - **Two docstrings asserted safety properties the code does not have**, which
     is worse than no comment: `saving/config.resolve` said the writer "opens
     for truncation" (it is mode `"x"` and refuses), and
     `ClosedLoopWorker.recorded_fires` promised it always equals
     `len(/closed_loop)` when a ring-buffer drop or a late put can break it.
     Both now say what is true.
   - **A silently swallowed exposure change** in the camera loop
     (`except Exception: pass`): the operator drags the slider, nothing happens,
     nothing is said. Now printed once per distinct reason — not per tick, which
     would put console I/O in the capture path.

   **Known and deliberately NOT fixed, with the reasoning:**
   - `_smooth_rows` loops over its ~11 Gaussian taps in Python, and
     `fit_circle_taubin` makes ~13 separate `.mean()` passes. Both are now small
     next to everything else, and the tracker is at **1.78 ms/frame on a
     1928×1208 clip — under 3 % of a core at the rig's 15 fps.** Optimising
     further buys nothing real.
   - `_EncoderBase._report` still builds two arrays per sample with
     `np.fromiter`. Removing that needs a preallocated ring in place of the
     deque; not worth the complexity in load-bearing code at 120 Hz.
   - The **camera read path and the writer are already at hardware limits**
     (92 % of the link, 1004 MB/s measured), so neither is an optimisation
     target — §6 item 4 is about fitting *within* the writer, not speeding it.

6. ~~Test the pupil tracker on a sample video.~~ **Done 2026-08-18 — and it
   found a real bug, not a tuning problem.** See §7 (u). Kept because the three
   findings underneath it are the durable part:
   - **No decoder was needed after all.** The rig's clip
     (`E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi`) is **uncompressed
     `IYUV`**, where the Y plane *is* the grayscale frame — a reshape, not a
     decode. 2026-08-17's "an avi needs an install" was true of compressed avi
     only. `devices/pupil_cam/avi.py` reads IYUV/I420/YV12, Y800 and BI_RGB and
     **refuses anything compressed by name**, since cv2/imageio/av are all still
     absent on 3.14.
   - **`_test_tracking.py` could not have caught this**, and that is the lesson.
     It validates against *synthetic* eyes, where the pupil edge is the
     highest-contrast thing in frame. Real IR footage inverts that: the
     orbit→fur margin is a ~200 grey-level step against the pupil's ~30. A
     synthetic suite that passes 15/15 said nothing about it. **Re-run both**
     after any `tracking.py` change — the script for accuracy, a clip for
     realism.
   - **Auto-seeding cannot work on this rig's framing.** `coarse_seed` returns
     `None` on every frame: at threshold 60 the dark mask exceeds half the
     sensor and it bails at its own cheap guard. Click-to-seed (**Show search
     overlay** → click) is the operating procedure, not a fallback.
   - `../rig_captures/` holds **encoder CSVs only** — no pupil footage there.

7. **Make full-frame recording fit the writer.** This is the real constraint and
   it is now the only one: ~2223 MB/s acquired against ~1165–1200 written, so
   about half the frames cannot be stored. The levers, and the one measurement
   that picks between them:
   - **Binning does not cost frame rate on this camera.** The frame period stays
     **8.68 ms at bin 1, 2 and 4** — binning cuts bytes, not time. So 2×2 gives
     ¼ the data at the *same* 115 fps (measured: 114.26 fps, 600 MB/s), which
     fits the writer with room to spare. If the science tolerates 2216×1184,
     this is the whole answer and needs no code.
   - Otherwise cap the rate (the panel already warns and names the exposure), or
     take a smaller ROI.
   - ~~Not yet measured: the writer's real sustained rate through a session.~~
     **Measured 2026-08-17 through the real path** (ORCA → `OrcaFireWorker` →
     `Recorder` → `HDF5Writer` → D:), 10 s per run, frames counted **off the
     closed file**, not off a counter:

     | run | offered | on disk | kept | sustained |
     |---|---|---|---|---|
     | full frame, bin 1 | 957 | 506 | **52.9 %** | 1004 MB/s (47.9 fps) |
     | full frame, bin 2 | 1153 | 1153 | **100 %** | 603 MB/s (114.9 fps) |

     **1004 MB/s, not the 1165 benchmark** — a benchmark measured the writer,
     this measures the path. `_WRITER_MBPS` was 1200 and is now **1000**, which
     moves the advised cap from a wrong 60 fps to a right 48. The bin-1 warning
     the app already prints was accurate to within 1 % (predicted ~48 % loss,
     actual 47.1 %).
   - **Done 2026-08-17: the offered presets stop at `4432x512`.** Below that the
     sensor outruns the writer so far that the preset could only produce an
     unrecordable session. `MIN_PRESET_ROWS` trims the *dropdown*; the datasheet
     table keeps all nine rows, because `readout_fps()` interpolates them for
     binned ROIs — 512 rows at bin 4 reads out like 128. Four checks in
     `test_readout_fps` hold that line apart, one of them the control that the
     table still carries what the dropdown does not.

8. **Project through the full app.** *Half of this closed on 2026-08-12, on
   this machine* — everything short of emitting light now runs through the
   **app's** path (the adapter and panel, not just the standalone script of
   §5 #5): the real ALP opens as `ALP-4.2 1024x768` with the API resolved from
   `dmdGUI_project/dmd_config.json`, the live controller satisfies
   `ProjectorController`, all 16 metadata keys populate, and the geometry was
   swept against the real 1024×768 panel — scale, offset and clockwise-positive
   rotation each land within the half-pixel the integer paste allows.
   **What is left needs someone in front of the hardware:** that Display
   projects and Stop halts, and that a recorded session's `/dmd` carries 0 and
   −1 around it. **Close `dmdGUI_project` first** — one process owns the USB.
   Two traps found doing this half, both worth knowing before repeating it: a
   **checkerboard cannot show a geometry error** (it is symmetric under every
   transform being tested — use an off-centre asymmetric mark), and **`fit`
   overrides scale, rotation and offset** by design, so a sweep with `fit` on
   measures nothing. The panel is honest about the second: it greys those
   spinboxes out.
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
  **Decided 2026-08-12: acqApp stays on `fit`, deliberately.** Worth writing
  down because it looks like a discrepancy every time someone checks —
  `dmdGUI_project` is aligned at **132.4 %**, acqApp's saved DMD settings are
  `scale_pct = 100.0` with `fit = True`, and with `fit` on the scale is ignored
  entirely (`build_frame` computes its own to fill the panel). So acqApp is
  *not* projecting at the standalone app's registration, and that is the
  operator's call, not a bug. The 132.4 % seeding in `DmdModule._settings()`
  still works — it only applies to a **fresh install** with no saved value, and
  this machine has one. Revisit only if the projected field needs to be
  registered to the optics rather than filling the panel.
- ~~Phase 0's camera throughput number.~~ **Closed, and re-measured 2026-08-17
  through `OrcaFireWorker` itself: 105.92 fps, 2223 MB/s** at full frame
  (4432×2368, 20.99 MB/frame, 5 ms), against a camera offering 115.26. The
  three numbers now agree instead of disagreeing: link ~115 fps ≈ 2419 MB/s,
  achieved 2223 MB/s (92 %), writer ~1165–1200 MB/s. **The read path is not the
  limit; the writer is.** Size the ring buffer (#14) from 2223 MB/s, not 969.
  The **2026-08-14 figure of 46.17 fps / 969 MB/s is withdrawn** — see §7 (t).
- Encoder **wheel diameter** — still unmeasured, and until it is set the app
  reports rev/s and rev rather than mm/s and mm. The panel now keeps whatever is
  typed in, so measure once and it stays measured. (`volts_per_rev` is *not*
  open: it was measured at **4.912** from a rig capture — see
  [docs/HANDOFF.md](docs/HANDOFF.md) "Hardware facts". This item used to claim
  both were unmeasured.)
- **#13 on a real board:** the first session should report
  `wheel_timestamp_source = "hardware"` and a `wheel_rate_actual_hz` at or very
  near the requested rate. `"software"` means the 6363 refused
  `cfg_samp_clk_timing` on that channel — the run is still valid, but the speed
  carries scheduler jitter and the printed reason is in the console.
- Real-hardware validation of *everything* in phases 2–4: no rig hardware has
  ever run this code.

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what's next.

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

### 2026-08-18 (ab) — looked at the UI, and it was telling the operator wrong
- **Rendered the real window and every settings tab to PNG and read them.**
  `QT_QPA_PLATFORM=offscreen` plus `QT_QPA_FONTDIR=C:/Windows/Fonts` (without the
  second, text draws as boxes). Worth repeating before any UI change — none of
  what follows was visible from the source.
- **The Save tab called a frame-dropping configuration healthy.** It read
  "936 GB free — about 8.0 min at 2002 MB/s" **in green**, for a full-frame bin-1
  setup that can only write half its frames. Two errors in one line: the disk
  fills at what is *written* (~1000 MB/s), so the real figure is 16 min, and the
  colour said "fine". Now amber, with the shortfall named and a pointer to the
  camera tab.
- **The camera tab said nothing about it at all.** The single most consequential
  fact about a configuration — that recording it loses ~50 % of frames — existed
  only in a console print (`_warn_data_rate`). There is now a **Recording** row
  beside Frame rate, red when the rate exceeds the writer, naming the remedy
  (2×2 binning, smaller ROI, or the fps to cap at).
- **`WRITER_MBPS` moved to `presets.py`** so the worker and the panel cannot
  disagree, and reaches the Save tab through a widened
  `ModuleHost.set_expected_rate(mbps, writer_mbps)` — `saving/` must not import
  `devices/`, and `test_structure` enforces that.
- **`devices/pupil_cam/panel.py` was doubly-encoded**, so the operator saw
  `8000 Ãµs` and `Sample videoâ€¦` in the pupil tab. Residue of §7 (u)'s patch
  script: 26 damaged runs, one file, and the whole file's em-dashes and box
  characters with them. The repair is exact and self-checking —
  `encode('cp1252').decode('utf-8')` inverts it, and correct text cannot survive
  that round trip, so only damaged runs were touched.
  **`test_console_safety` now scans every source file for it**, with a control.
  That test already owned the other half of this problem (a *print* dying on
  cp1252); this is the same accident committed to disk instead.
- **Click-to-seed is now named in the checkbox label**, not just its tooltip. On
  this rig the auto-seed *cannot* work — `coarse_seed` bails when the dark mask
  exceeds half the sensor — so seeding by hand is the operating procedure, and
  "Show search overlay" was the only route to it.
- Suite **661 → 663**.

### 2026-08-18 (aa) — boot: 13.4 s → 8.1 s, and the Devices window 8.6 s → 0.3 s
- **Imports are not the problem and never were: 350 ms for all of
  `acqApp.main`** (pyqtgraph 256 of it), and a full mock start to a visible
  window is **848 ms**. Don't go looking there again.
- **`DCAM.get_cameras_number()` costs ~6.5 s and does so on EVERY call.** It
  re-enumerates; it is not one-time DLL init — three consecutive calls measured
  6.5 / 5.3 / 5.3 s. It was being called *before* the open, purely to decide
  whether to try, which added ~5.3 s to every launch. Now the open is
  **optimistic** and the enumeration happens only if it fails, where a few
  seconds is free and the answer ("no camera" vs "something else holds it") is
  what the operator needs. The failure message names HCImage, which is what it
  actually is. **13.36 → 8.11 s**, measured on the real camera.
- **The same call was freezing the Devices window for ~8.6 s**, on the GUI
  thread, every refresh. The app already holds the camera open, so the answer is
  known — and truer than an enumeration, since it is the handle a session will
  use. Passed through the existing `probe_kwargs` seam (`cam_open`), so
  `probe.py` still imports nothing upward. **8.6 s → 277 ms.**
- **`ConnectionMonitor.refresh` probed everything in one blocking call**, so its
  "…" placeholders could never paint. Now one module at a time with the row
  updated as it lands, Refresh disabled meanwhile — these touch hardware and
  re-entering through a second click is not a thing to discover on a rig.
- **The remaining ~8 s is the open itself, now on a thread** so it overlaps the
  Qt import and the module picker. Verified on the real camera end to end:
  opened on a worker, driven from the GUI thread (`get_detector_size`,
  `set_exposure`), window built, closed cleanly. With 3 s spent in the picker
  the wait after it drops to 4.6 s. **The load-bearing rule is untouched and is
  about lifetime, not threads** — open ONCE and keep the handle, because
  re-opening a just-closed device segfaults (docs/HANDOFF.md). Cancelling the
  picker now joins and closes rather than leaving a half-open camera behind.
- **`COM54 open` succeeded in 205 ms**, which contradicts §6's standing note
  that the stage "still fails to open here". Left as a flag for whoever next
  works on the stage; nothing else was changed on the strength of one open.
- Nothing was projected: `project()` is reachable only from `display()`, and the
  DMD lines in these runs are `load_pattern` composing a frame in memory.

### 2026-08-18 (z) — `acqapp_local.json` could be lost by a crash mid-write
- **Looked for GUI-thread disk I/O and mostly found none**, which is worth
  recording so nobody re-audits it: the Save tab uses `editingFinished`, not
  `textChanged`, so it refreshes once per committed field, and that refresh
  costs **0.09 ms** even with 60 existing recordings for `resolve(unique=True)`
  to step past. The 30 Hz display tick is 0.92 ms. Neither needs touching.
- **What the search did find is durability, not speed.** `save_config` used
  `open(path, "w")`, which **truncates before writing**, and the exposure
  spinbox is wired to `valueChanged` — so this file is rewritten on *every step*
  of a spin. It holds the operator's entire working setup (modules, theme, every
  panel's parameters), and this app can die natively mid-write: a PyQt6 qFatal
  out of a worker, or a DCAM segfault, which is exactly why `main` enables
  faulthandler. The window left a truncated file, and `load_config` read that as
  **"no settings at all"**.
- **Now written beside it and renamed** (`os.replace`, atomic on Windows and
  POSIX). Demonstrated rather than asserted: a subprocess looping on
  `save_config` was killed repeatedly and the file still parsed every time.
  **No fsync** — the threat is a process crash, not power loss, and fsync per
  spinbox step would cost more than the write it protects.
- **`load_config` no longer discards a damaged file.** Returning `{}` is right
  (the app must start), but the next save then overwrote the only copy with
  defaults, turning a recoverable corruption into a permanent loss. It is moved
  to `.corrupt.json` and said out loud.
- **Cost measured honestly: 1.32 → 1.53 ms**, +0.2 ms. An intermediate figure of
  6.94 ms was measurement noise in a scratch script and is withdrawn — the
  isolated write benchmark (0.17 → 0.43 ms) and the clean panel benchmark agree.
- Four checks in `test_settings_persistence`, verified to **fail** against the
  old truncate-write. The interesting one is deterministic rather than racy: it
  monkeypatches `json.dump` to write a partial record and then raise, and asserts
  the previous config is untouched. Suite **657 → 661**.

### 2026-08-18 (y) — the ROI editor at real camera size: 272 ms → 2.2 ms a drag
- **Driving the editor at 4432×2368 was the test that mattered**, and it still
  failed after (x)'s fixes: **272 ms per drag event**, i.e. unusable. Caching
  `accessible_mask` was necessary but nowhere near sufficient, because
  `_refresh_status` rasterised full-camera masks *per ROI* twice over —
  `outside()` once each and `clipped_mask` once more. **2.2 ms now.**
- **The fix was a design call, not a micro-optimisation.** A status line showing
  a whole-number percentage does not need a per-pixel raster of a 10.5 Mpx
  camera. So the exact call is kept for the path that needs it and two cheap
  ones stand in for the display:
  - `outside()` is now **geometric**: a projective map takes straight lines to
    straight lines, so a rectangle is inside the (convex) field exactly when its
    four corners are, and a circle's rim is sampled. `_Roi.boundary()`.
  - `RoiSet.reach_fraction()` estimates on a grid capped at 512 px a side and
    only at pixels an ROI covers — so it costs the ROI's area, not the camera's.
    Worst gap from the exact figure over 250 randomised cases: **0.5 points.**
  - `clipped_mask` is untouched and still exact. `dmd_frame`, the path that
    actually projects, never uses an estimate.
- **The geometric `outside()` is also SAFER, which was not the goal.** Across
  250 randomised cases it disagreed with the raster version 3 times and **all
  three in the safe direction** — zero cases where the raster flagged something
  geometry missed. The three are ROIs hanging off the *image* edge, where the
  raster had no pixels to judge and so called them fine. Pinned by a test.
- `_Roi.mask_at(xs, ys)` takes coordinates rather than a shape, which is what
  lets a caller ask coarsely; `mask(shape)` is a thin wrapper on it.
- Suite **652 → 657**. Also measured and **deliberately left alone**: the 30 Hz
  display tick is 0.92 ms at full frame (2.8 % of a core) and the obvious
  "improvement" of copying to a contiguous buffer makes it *worse*.
- `coarse_seed` had the same shape of bug as (x): the decimation that exists to
  bound the EDT sat *after* `binary_fill_holes`, so the fill ran unbounded —
  a third of an 87 ms call on a real 1928×1208 frame. Decimating first: **87 →
  41.8 ms**, ground truth still 15/15 and the real-clip lock rate unchanged at
  92.7 %. Seeds shift by ≤6 px at extreme thresholds, which is inside the
  decimation's own quantisation and far inside the annulus band it feeds. No
  test added: the property is a timing bound, and timing assertions in this
  suite would be fragile across machines.

### 2026-08-18 (x) — the DMD calibration would have died on the rig
- **Measuring the NEXT feature at real scale, before running it, was the whole
  value of this pass.** Every DMD number so far came from small synthetic
  frames. Driven at ORCA full frame (4432×2368) the offline half needed
  **11.4 GB** for one sweep, on a box with 11.1 GB free on C: and GB of camera
  buffers already pinned. It would have failed — or paged for minutes — the
  first time it was run for real, and the obvious suspect would have been the
  hardware. **1.60 GB now**, same registration (rms 0.40 px over 276 points).
- **Three separate causes, each the same mistake — full-size intermediates:**
  - `decode` promoted both stacks to float64 (40 frames × 10.5 Mpx) and then
    called `abs(m).min(axis=0)` on top. **7.8 GB → 0.50 GB** by streaming one
    plane at a time in float32: Gray decoding is a running XOR and validity a
    running min, so the stack never has to exist. Bit-identical to the float64
    version on 40 randomised cases.
  - `run_calibration.shot()` converted **every grab to float64** and held all
    40 — 3.4 GB where the camera's own uint16 is 0.8 GB. The copy it was
    accidentally getting *is* needed (a grab may alias a driver buffer), so it
    is now an explicit `np.array(grab())`.
  - `accessible_mask` built a full `mgrid` and transformed 10.5 M points **on
    every ROI drag** — 798 ms and 961 MB each. Now banded and cached per shape:
    **321 ms / 53 MB** first call, **0 ms** thereafter, handed out read-only.
    `RectRoi.mask`/`CircleRoi.mask` had the same `mgrid` habit: 308 ms/420 MB →
    8 ms/10 MB, and unrotated rectangles stay separable. Identical output on
    400 randomised ROIs and 60 randomised calibrations, keystone included.
- **Four new guards, each verified to FAIL against the old code**, and each
  paired with a control that stays green either way. The `accessible_mask` one
  is worth copying: rather than a magic MB budget it doubles the image height
  and asserts the peak grows by **only the extra output** — which is the
  property, and is scale-free. Suite **645 → 652 checks**.
- **A `decode` shape mismatch now names the plane** (`plane 15 shape …`) instead
  of numpy's "inhomogeneous shape after 1 dimensions". Mid-sweep that is the
  difference between a two-minute fix and re-running 40 exposures blind.
- **None of this changes what §6 item 1 still needs at the rig** — it removes a
  wall that was waiting there. Nothing has been projected.

### 2026-08-18 (w) — a survey pass: profile first, and it found a real bug
- **Asked to "find other areas of improvement", and the lesson is the method.**
  Grepping for suspicious patterns produced nothing worth doing; **one profile
  of the tracker against the actual rig clip** produced a 2× speedup and pointed
  at the file where a genuine correctness bug was hiding. Profile the real
  workload, don't reason about it.
- **`np.nanmedian` was ~30 % of the tracker**, because numpy silently falls back
  to `_nanmedian_small` — which allocates a **masked array per call** — for rows
  shorter than ~600. Ours are ~100. `rays._nanmedian_rows` sorts instead.
  **Real clip 3.49 → 1.78 ms/frame**, lock rate and radii unchanged, and the
  replacement checked against `np.nanmedian` over 3000 random NaN patterns.
  Session total on the synthetic bench: **9.27 → 2.68 ms/frame, 3.5×.**
- **`avi.py` ignored the DIB row stride — a latent correctness bug.** Scanlines
  are padded up to a 4-byte boundary; any BI_RGB clip whose row bytes are not
  4-aligned decoded **progressively sheared**. The existing test could not see
  it: W=96, and 96×3 is already aligned. Fixed, and `test_pupil_video` now has a
  **W=97** case (verified to fail without the fix) plus a control asserting that
  97 really is unaligned at both 8- and 24-bit.
- **Two docstrings claimed safety properties the code does not have.** That is
  worse than no comment, because it is what someone checks instead of the code.
  `saving/config.resolve` said the writer "opens for truncation" — it is mode
  `"x"` and refuses; `ClosedLoopWorker.recorded_fires` promised it always equals
  `len(/closed_loop)`, which a ring drop or a late put can break.
- **A swallowed exposure change** (`except Exception: pass`) in the camera's
  capture loop now reports once per distinct reason. A slider that silently does
  nothing is a support call; a print per tick would be console I/O in the
  capture path.
- **What was deliberately left**, with reasons, is in §6 item 2 — chiefly that
  the tracker is now under 3 % of a core and the camera and writer paths are
  already at hardware limits. Suite **642 → 645 checks**.

### 2026-08-18 (v) — a trim pass, and two numpy defaults that cost 200×
- **Operator's instruction: reduce comment verbosity, and optimise.** Both done
  in one sweep, worst-first by comment+docstring ratio. Tree **23.2 % → 22.5 %**
  so far and the pass is **not finished** — §6 item 2 carries the ordered list of
  what is left, so it resumes without re-deriving anything.
- **The big one: `calibration._homography` called `np.linalg.svd(A)` with the
  default `full_matrices=True`.** Only the 9×9 `Vh` is ever used, but on a
  (2n, 9) design matrix the default also builds a (2n, 2n) `U` — at 3169
  correspondences that is 6338², ~320 MB, computed and thrown away. **1633 ms →
  7 ms**, rms 0.422 px identical to 3 dp. It sits in the calibration path the
  rig sweep will run, re-fitting per rejection iteration; it would have made the
  sweep feel broken and been blamed on the hardware.
- **The same shape of mistake in `rays._bilinear`**, which cast the **whole
  frame** to float32 on every call to read a few thousand annulus points — a
  ~7 MB copy per refinement pass, 2–3 per frame. Gather first, cast after.
  With cos/sin hoisted out of the refinement loop, `argpartition` for
  `coarse_seed`'s top-4 blobs, and `_ransac_circle`'s 48 circumcircles done in
  one broadcast: **9.27 → 4.00 ms/frame, 108 → 250 fps**, same radius to 2 dp.
  **The lesson is one lesson twice: a numpy default sized for the *whole* array
  when only a corner of the result is wanted.** Worth looking for elsewhere.
- **Nothing changed behaviour.** Every optimisation was checked against both
  suites — `run_all.py` at 642 checks and `_test_tracking.py` 15/15 — and the
  two benchmarks report identical fits, not merely passing ones.
- **`main.py` and `wheel/acquisition.py` were held back, then done** on the
  operator's say-so. The wheel one was unblocked by committing their `_SIGN`
  flip **on its own first** (`25ed583`) — a hardware fact deserves its own
  commit, not a burial inside a comment trim. `main.py` was trimmed with no code
  moved, since §0 asks for no restructuring there.
- **`_EncoderBase._report` fitted a straight line with `np.polyfit` — an SVD
  behind a Vandermonde — once per SAMPLE at 120 Hz.** Closed-form slope plus a
  `searchsorted` window: **49.3 → 31.0 µs/sample**, speed and distance identical
  to 6 dp. Small in absolute terms, and said so in §6 item 2 rather than dressed
  up: the wheel was never a bottleneck.
- **The `_SIGN` failures got sharper, which is progress even though they are
  still red.** All seven encoder checks fail as *pure sign inversions* with
  exact magnitudes (−706.9 vs 706.9). That rules out a maths error on either
  side, including in the `_report` rewrite above, and reduces the open question
  to a single fact about the rig's wiring.
- Commits `9131017`, `7b40bb9`, `383c787`, `25ed583` and this session's last.
  **Nothing pushed.**

### 2026-08-18 (u) — the pupil tracker was locking the eyelid; a real bug, not a setting
- **The tracker never worked on a real eye, and no parameter could have fixed
  it.** `rays._edges_along_rays` took the **strongest** gradient on each ray
  (`argmax` over the whole ray). On IR footage the pupil→iris step is ~30 grey
  levels and the orbit→saturated-fur margin is ~200, so the eyelid won *every*
  ray. Now the default is **`edge_select="first"`** — the innermost sustained
  edge, which is right by construction: scanning outward from inside the pupil,
  its rim is what you meet first. Measured on the rig clip, click-seeded, stock
  defaults: **lock 0 % → 98.7 %**, radius 53.4 ± 3.1 px, fit rms 1.18 px.
- **Two guards had to come with it**, and both were found by regression, not by
  design. A per-ray **noise floor** (`_NOISE_K` × robust MAD of the gradient) or
  speckle inside the pupil gets the first vote and the fit collapses inward; and
  a **sustained-step test**, or the corneal glint's rising edge is taken as the
  boundary. `_NOISE_K` is sensitive: at 4.0 it killed 19 of 64 legitimate rays,
  which shrank the true circle's majority enough that RANSAC chose a spurious
  consensus and broke the 40 % eyelid case. **1.5 satisfies both the synthetic
  suite and the real clip.**
- **A cross-ray consensus vote was tried and reverted.** It did not fix the
  eyelid draw *and* it broke `_test_tracking`'s "auto-seed defeated → never
  confidently wrong" contract. A cleverer mechanism that makes the safety case
  worse is not an improvement.
- **Second compounding bug:** `find_circular_edge` re-centred the annulus each
  refinement pass but kept `half` fixed, so a pass that landed on the lid
  dragged the band outward with it and the next pass saw only the wrong edge.
  The lock was self-reinforcing. The band now contracts (`_BAND_CONTRACT`).
- **`min_confidence` 0.25 → 0.10.** Only ~half the rays ever find an edge on a
  real eye, so `(kept/cast) × exp(-rms/2)` caps a *good* real fit near 0.28 —
  0.25 was discarding fits whose rms was 1.2 px.
- **Temporal filtering added at the operator's request** (median-3 then EMA 0.5,
  both panel-adjustable). Measured: |Δcentre|/frame max **22.7 → 3.45 px**,
  |Δr| max **11.0 → 1.38 px**, lock unchanged. Built to the operator's
  constraint that **a blink must still register**: the filter runs over
  *consecutive* frames only and is dropped the moment one is lost, so a blink
  stays as lost frames instead of being interpolated across; the annulus seed is
  *retained* through it, so the eye is picked up where it left off (frame 131 of
  the clip returns at the right radius immediately); and the unfiltered fit
  survives in `PupilResult.raw_center/raw_radius`, so smoothing never destroys
  the observation. `reseed_after` re-thresholds only after a long closure, and
  keeps the pre-blink estimate if that search finds nothing.
- **A third pupil frame source: `devices/pupil_cam/video.py` + `avi.py`**, so a
  recorded clip can be replayed through the real panel, track worker and overlay
  ("Sample video…" in the pupil tab). This is what **§5b A3 finally triggered
  on** — the seventh module never did, but the third variant of one device did,
  exactly as A3 said it would. `build_session` is now an `if` chain over three
  concrete classes. The clip is recorded as `pupil_video` in the session
  metadata: replayed frames must never read as rig data.
- **The clip is `IYUV` — uncompressed.** The Y plane is the grayscale frame, so
  no decoder was needed and (t)'s install question never arose.
- **Verified both ways:** `_test_tracking.py` 15/15 (synthetic ground truth) and
  the suite **568 → 598 checks, 20 files**. The new `test_pupil_video.py` pairs
  the eyelid fix with a control — on a synthetic eye with a high-contrast lid
  margin *inside* the band, `first` returns r=40.1 (truth 40) and `strongest`
  returns 61.9 (lid at 62). Without the fix that control fails.
- **Two self-inflicted traps worth not repeating.** A `python - <<EOF` patch
  script wrote `panel.py` through the console's ANSI codepage (mangling an
  em-dash to a non-UTF-8 byte) and turned `\n` escapes into literal newlines;
  `p.write_text()` then doubled every line ending to `\r\r\n`. **Edit files with
  the editor, not with a generated patch script** — and if you must, write bytes
  with an explicit `encoding="utf-8", newline=""`.
- **DMD ROI photostimulation started — the offline half only** (§6 item 1). The
  operator's calibration idea was **complementary checkerboards with corner
  symbols**, and it is better than this file's old "a checkerboard cannot show a
  geometry error" note allows: differencing a pattern with its inverse *cancels
  the sample* (structure, background, vignetting divide out), and the corner
  marks break the symmetry the note was about. Two things were added to it —
  marks carrying **1/2/3/4 dots** so a mirror flip cannot pass as valid, and a
  **Gray-code sweep**, because four corner points fit a homography exactly and
  therefore report zero residual whether the fit is right or wrong. Verified
  against a chosen transform: decode median 0.40 px, homography rms 0.41 px over
  3169 points, ROI round trip 100 % on target. **Nothing has been projected.**
- **The registration is now on the critical path**, which retires a deferral:
  "where the pattern lands on the sample" was in "Needs the rig" since
  2026-08-12 and could be ignored while the DMD only held static patterns. You
  cannot aim at an ROI with a projector you have not registered to the camera.
- **Not from this session but noticed by it:** the operator flipped
  `wheel/acquisition.py:_SIGN` −1 → +1. `test_encoder_derive.sim()` generates a
  *falling* ramp and documents "the rig's forward direction is the falling one",
  so the two now contradict each other and **encoder / enc-timing / closed-loop
  fail**. Left alone deliberately: which way the real encoder ramps is a
  hardware fact, and the sign decides whether recorded distance is positive.
  If the flip is right, `sim()`'s `(-rev_s * t)` is what needs changing.

### 2026-08-17 (t) — the grab path was never slow; phase 0's number withdrawn
- **§6 item 1 is closed by disproving its premise.** The app's own loop —
  `OrcaFireWorker` with a counting sink, so the recording branch — sustains
  **105.92 fps / 2223 MB/s** at full frame against a camera offering 115.26.
  That is 92 % of the ceiling. The "~13 ms per frame spent above the hardware"
  this file has carried since (s) does not exist.
- **What made 46.17 fps.** `cam_grab.py` takes `t0` *before*
  `cam.start_acquisition()` and calls it with no `nframes`, so pylablib
  allocates its default 100 buffers — **2.1 GB, and 1.09 s** — inside the timed
  window. Measured directly: first `start_acquisition` 1090 ms, second 52 ms
  (buffers are reused). 1.09 s of setup + 1.85 s of grabbing at ~108 fps = 2.94 s
  for 200 frames = 68 fps, and the script rerun **verbatim today gave 69.26 fps**
  against its own recorded 46.17. So the artefact is proven and worth ~36 % of
  the figure; the rest of the original gap did not reproduce at all and is
  most likely machine state that day. **A number that cannot be reproduced by
  its own script should be withdrawn, not explained** — which is why the header,
  §4 and §6 now all carry 105.9 instead.
- **The discriminator answered a third way.** Neither "MB/s flat" (bandwidth)
  nor "fps flat" (per-frame overhead): the frame period is **8.68 ms at bin 1, 2
  and 4 alike**, so on this camera binning cuts *bytes, not time* — bin2 gave
  114.26 fps / 600 MB/s, bin4 114.35 fps / 150 MB/s, both essentially at the
  camera ceiling with the loop idle in `wait_for_frame` 66–81 % of the time.
  That makes binning the lever for **fitting the writer**, not for going faster,
  and it is now §6 item 1.
- **The residual 8 % at full frame is a copy wall, not Python.**
  `read_multiple_images` costs **9.16–10.02 ms/frame** against an 8.68 ms frame
  period (~2.3 GB/s memcpy out of the driver buffer), so the buffer fills slowly
  and overflows. Deeper buffers delay that; they cannot fix a sustained deficit.
  Retaining every frame costs ~10 % on top (97.44 vs 107.51 fps).
- **Two smaller things the runs exposed**, both in §6 item 1: `_buffer_frames`
  delivers **0.33 s of slack at full frame, not the 2.0 s `_BUFFER_SECONDS`
  advertises** (`_BUFFER_BYTES` binds first → 38 buffers), which dropped 13
  frames in 6 s *with no writer attached* — and the drop message blames the
  writer for it regardless. And `_maximise_readout_speed` is a **no-op on this
  model**: `get_all_readout_speeds()` returns `[]`.
- **§2 gains a SOLID ground rule** at the operator's request, pointing at §5b.
- **THE MACHINE IS THE RIG COMPUTER** — the operator said so, and it dissolves
  the confusion §2 has been carrying since 2026-08-12. The DMD "surprise", the
  ORCA "surprise", the 6363 and COM54: all one fact, seen from the wrong end.
  §2 and §3 are corrected. The rule that gets sharper, not looser: **an animal
  may be under the objective while a session runs here**, so actuation asks are
  live. The rule that gets looser: nothing needs deferring to "a rig trip".
- **The offered presets now stop at `4432x512`** (operator's call). The trim is
  `MIN_PRESET_ROWS` over the *dropdown* only — the nine-row datasheet table is
  untouched on purpose, since `readout_fps()` interpolates its small rows for
  binned ROIs and cutting them would silently mis-rate 512-at-bin-4 by 2×.
  Splitting "what the sensor can do" from "what we offer" is the §2 SOLID rule
  applied on the same day it was written. Two tests pinned `4432x4` for a cheap
  frame shape and now use `4432x512` at bin 4.
- **The writer's real number, and it is not the benchmark: 1004 MB/s.** Measured
  end to end rather than in isolation (table in §6 item 1). Full frame keeps
  **52.9 %** of its frames; **2×2 binning keeps 100 %** at the full 114.9 fps.
  `_WRITER_MBPS` 1200 → **1000**, which is the difference between advising a cap
  of 60 fps and of 48. Two stale figures went with it: `writer.py`'s docstring
  claimed "~330 MB/s at full frame" (the USB3-era *camera* rate, in a *writer*
  docstring) and `_check_link.py` still quoted the 1165 bench.
- **A trap worth the two minutes it cost:** importing `acqApp.main` **opens the
  camera** at module scope (`main.py:116`) and holds the handle, so a bench that
  imports it for a constant then fails its own `DCAMCamera()` open with
  `DCAMERR_FAILOPENCAMERA`. The fix is the faithful one — reuse
  `appmain._cam_handle`, which is what the worker does in the app anyway, and it
  skips the 7 s open per run. Same one-process-owns-the-device rule as the DMD.
- **The system drive is nearly full: C: has 11.1 GB.** Not where sessions land —
  `default_folder()` already picks the largest-free fixed drive, so recordings
  go to D: (945 GB free, and where these runs wrote) — but 11 GB is thin for
  temp files and the page file on a box that pins GB-scale buffers.
- **The three leftover findings are closed as diagnostics, not as tuning.** The
  drop message blamed the writer for something the writer structurally cannot
  cause (`Recorder.put` only enqueues), `_buffer_frames` let a 2 s promise
  become 0.33 s in silence, and `_maximise_readout_speed` did nothing at all on
  a camera with no selectable speeds. All three now say what is true; **no
  constant was retuned**, because §6 item 1's arithmetic shows a deeper buffer
  cannot fix a sustained deficit. 12 checks, each with a control.
- **Reconnaissance for the next task, done before clearing context:** the venv
  has **no video decoder whatsoever** — no cv2 (Python 3.14), no imageio, no av,
  no ffmpeg on PATH. `tifffile`, `Pillow`, `h5py` are present. That is §6 item 1
  and it is the whole reason that item leads with a question about file format.
- Bench scripts stayed in the scratchpad; nothing was added to the repo. Suite
  green throughout: **552 → 568 checks, 19/19, 44.7 s** (+4 preset boundary,
  +12 diagnostics, controls throughout).

### 2026-08-17 (s) — phase 0 closed, `scratch/` gone, and the tree gets a map
- **Phase 0's camera number, taken at last: 46.17 fps / 969.0 MB/s** (200 frames,
  full frame 4432×2368, 20.99 MB/frame, 5 ms exposure). Taken *here*, not at the
  rig, because §2 turned out to be wrong about the camera. Phase 0 is now ✅.
  **↑ This figure was withdrawn the next day — see (t). The real number is
  105.9 fps / 2223 MB/s; the rest of this entry's reasoning rests on the wrong
  one.**
- **The number's value is the disagreement, not the figure.** Link 115.3 fps,
  writer ~1165 MB/s, achieved 969 MB/s → neither the cable nor the disk is the
  limit; ~13 ms per frame goes somewhere above the hardware. That is §6 item 1
  and the operator's goal for next session.
- **`scratch/` and `toy_output/` deleted.** `cam_grab.py` existed only to take
  the number above, so it went with phase 0 — recover from git if the grab-path
  work needs a bench (`git show 2443a61:scratch/cam_grab.py`, verified). The five
  raw captures were **moved, not destroyed**: `../rig_captures/` with a README
  naming what each established. They were gitignored, so that is the only copy —
  worth knowing before anyone tidies that folder too.
- **`docs/STRUCTURE.md` is new, and it is checked, not trusted.**
  `tests/test_structure.py` (21 checks) validates both halves: the tree block
  against the filesystem, and the **mermaid arrows against the AST**. The second
  is the one worth having — it fails if an adapter starts importing another
  adapter, or if anything under `acq/` imports upward. It confirmed today that
  all 21 drawn edges are real and that `acq/` imports nothing in the app.
  Two edges nobody guesses: `probe.py → devices` and `adapters → closed_loop`.
- **The rule that comes with it** is in `CLAUDE.md` and §8 item 7: a move,
  rename or new module updates STRUCTURE.md *in the same commit*. The suite
  enforces it, which is the only reason such a doc survives a refactor — this
  one was written the day after a regroup broke every link in `docs/`.
- Suite is now **552 checks, 19 files**.

### 2026-08-14 (r) — the root regroup, and "rig-only" turns out to be wrong
- **§6 item 0 closed, option (a) + the operator's addition.** `sync.py` and
  `devices.py` → `acq/`; the six instrument packages → `devices/`. The root is
  now the shell (`main`, `config`, `console`, `dialogs`, `probe`, `style`) and
  four packages beside it. `git mv` throughout, so history follows the files.
- **The move's real hazard was not the imports.** Eight files walk up to
  `Desktop/python` with `Path(__file__).resolve().parents[2]`, and one level
  deeper makes that `acqApp/`. Two are load-bearing and **neither fails loudly**:
  `devices/stage/settings.py` would have silently stopped reading
  `stage_control/config.json` (falling back to defaults — the exact 2026-08-13
  failure), and `devices/dmd/alp.py` would have lost the ALP path. All eight are
  `parents[3]` now, verified by printing the resolved paths, not by inference.
- **`test_undefined_names` earned its keep again**, differently: it carries two
  hardcoded file lists and failed with *"target has moved — update this list"*
  ×3 rather than passing vacuously. That is the behaviour it was written for.
- **A dotted-path rewrite is safe where a bare-word one is not** (contrast (q)):
  `acqApp.wheel` → `acqApp.devices.wheel` cannot collide with a local variable.
  Ordering did matter — old `acqApp.devices` (the protocols) had to become
  `acqApp.acq.devices` *before* `acqApp.<instrument>` started producing
  `acqApp.devices.<instrument>`. Suite green after: 531 / 18 / 44.3 s.
- **Docs: the relative links were broken by the move**, not just stale — every
  `[stage/driver.py](../stage/driver.py)` in `docs/*_TRANSFER.md` pointed at
  nothing. Fixed there, in README (which also gained `acq/devices.py`, never
  described), `requirements.txt`, `tests/README.md` and both `CLAUDE.md`s.
  Archives (`SESSIONLOG.md`, `AUDIT-2026-08.md`) and §7 below are **left alone**:
  they record what was true then. Paths naming files deleted *before* the move
  (`wheel/_toy.py`, the four `recording.py`) were reverted for the same reason.
- **Then the four scripts were run to prove they still work at the new depth —
  and one of them contradicted §2.** `_check_link.py` reported a real ORCA on
  **this machine**: `C16240-20UP`, **CoaXPress**, 8.68 ms full frame → 115.3 fps.
  `probe_all` then found `Dev3 PCIe-6363` (wheel + puffer) and COM54 as well;
  only the Basler is absent. §2's "the camera, NI board and stage are still
  rig-only" was wrong, and §6 is re-pointed accordingly. Nothing was actuated —
  a camera open and an enumeration are both non-actuating, and the stage was
  only enumerated.
- **One flake, unexplained:** `test_closed_loop` failed a single check on one
  full run (530/17), then passed standalone and on the two runs after. The check
  name was not captured. If it recurs, capture it — the file's timing checks are
  the suspects.

### 2026-08-14 (q) — `modules/` → `adapters/`, and a dead-code claim withdrawn
- **The operator asked why instruments live in two places.** They don't quite:
  `X/` is the device (driver, worker, its own widgets, app-agnostic) and
  `modules/X.py` was the *adapter* wiring it into this window. Verified the
  layering is one-directional — only `main.py` imports the package, and no
  device package imports it back. The confusion was the **name**, so the package
  is now **`adapters/`**. No code moved.
- **A regex rename corrupted code, and `test_undefined_names` caught it.** The
  substitution `in modules` → `in adapters` hit `probe.py`'s comprehension over
  its own `modules` *parameter*, leaving `for m in adapters` — an undefined
  name on a path no test calls. Suite went 530 → 529 with one named failure.
  That test was written for exactly this and paid for itself on its first real
  restructure. **Blind renames need the checker, not just the suite.**
- **Device packages now have one shape.** `settings.py` = the model, no Qt;
  `panel.py` = its widgets. `wheel/` and `pupil_cam/` were split (the stage
  already was); `voltage_cam/settings.py` was only ever a panel, so it is now
  `voltage_cam/panel.py` — its model is `AcqConfig` in `presets.py`.
  **Measured, not assumed:** the three models import with **0** PyQt6 modules
  loaded; importing a panel pulls in 5. README has the convention.
- **`closed_loop.py` and `saving.py` are packages now**, on the same shape:
  `closed_loop/{settings,worker,panel}.py` and `saving/{config,panel}.py`.
  Both `__init__.py` re-export **lazily (PEP 562)** — eager ones run `panel.py`
  and pull PyQt6 in through the parent, defeating the split. Measured:
  `closed_loop.settings` and `saving.config` each import with **0** PyQt6
  modules, and the old `from acqApp.closed_loop import LoopRule` still works.
- **`main.py` cannot move into a `shell/`**, and this is the reason to write
  down: its bootstrap derives the venv from its own directory
  (`here = Path(__file__).parent; venv_dir = here / ".venv"`), so under
  `shell/` it would create and install into `acqApp/shell/.venv` — breaking §2's
  first ground rule. The documented launch command and `python -m acqApp.main`
  would change too. Root grouping is therefore **not** a free rename.
- **And `core/` would add an ambiguity rather than remove one:** `acq/` already
  is the acquisition core (clock, recorder, ring buffer, worker, writer), so a
  second core-ish package invites "why is the clock in `acq/` but the trigger
  bus in `core/`?". If the root is grouped, the better move is smaller — put
  `sync.py` and `devices.py` **into `acq/`**, where their neighbours already are,
  and leave `config`/`console`/`probe`/`style`/`dialogs`/`main` as the shell.
- **Withdrew a dead-code claim before acting on it.** `_test_tracking.py`,
  `analyze_raw.py`, `capture_raw.py` and `_check_link.py` show as unreferenced
  because they are *scripts*, run directly and never imported — and the docs
  cite all four. They are the tools that measured `volts_per_rev` and the ones
  for §6's open camera-link question. An import-graph check is the wrong test
  for a script. Nothing was deleted. `toy_output/` is untracked by git (it holds
  only a gitignored capture), so there was nothing in the repo to remove.

### 2026-08-13 (p) — comment trim, batch 1 of an unfinished pass
- **The operator's call: the codebase's comments are too long.** Measured before
  starting: 15260 lines = 9014 code + **1566 comment + 2253 docstring** + 2427
  blank, i.e. **prose is 25 % of the tree**. Densest were `adapters/__init__.py`
  (63 %), `devices.py` (45 %), `wheel/acquisition.py` (43 %).
- **Done so far: 15260 → 15030 (−230)** across 7 files — `devices.py`,
  `adapters/__init__.py`, `adapters/base.py`, `acq/recorder.py`,
  `wheel/acquisition.py`, `voltage_cam/acquisition.py`, `main.py`. Committed in
  four batches so any one is a single `git revert`.
- **Every batch verified AST-identical** against its parent: parse both, strip
  docstrings, compare `ast.dump` with positions off. That is a mechanical proof
  no code changed — the guard this pass needed, given that a blanket edit over
  "settings" files once gutted `stage/settings.py`. Suite 502/17 after each.
- **Code half, one file: `stage/panel.py` 598 → 585.** Five motion handlers
  shared one shape, so `_call(what, fn)` holds it once; plus `_btn` and `_axis`.
- **Pre-existing defect it surfaced:** `_stop`, `_stop_all` and the dialog's
  `_stop_all` were unguarded — the panic path (Esc → STOP ALL, app-wide), where
  a dead serial link is exactly the case, and an escaping slot exception aborts
  the process (§2). Now guarded.
- **`test_stage_panel.py` (28 checks)** covers those paths, which nothing else
  pressed — the GUI tests build the panel but never click it. It found an
  eighth bare call the manual pass had missed (`_clear_home`) on its first run.
  Controls: the old unguarded bodies must still raise, jog stays enabled when a
  missing frame disables go-to, an unbound panel is a silent no-op.
- **Trap worth knowing:** a `QApplication` with no live Python reference is
  collected and widget construction then aborts natively — exit code, no
  traceback, no output at all. Assign `qt_app()`.
- **Second prose batch** (2026-08-14): `pupil_cam/tracking.py` −37,
  `tests/_harness.py` −19, `closed_loop.py` −18, `dmd/alp.py` −8,
  `acq/writer.py` −8, `voltage_cam/presets.py` −8. All AST-identical.
- **Third batch:** `pupil_cam/acquisition.py` −18, `stage/control.py` −10,
  `stage/settings.py` −7, `test_device_contracts.py` −5.
- **Where it stands: 15260 → 15090.** Prose 25 % → 23 % (3,819 → 3,508). The
  trim removed ~410; the tree shows −170 because `test_stage_panel.py` added
  ~240 back.
- **A refactor that did not pay, worth recording:** folding the closed-loop
  panel's four `QDoubleSpinBox` blocks into a `_spin()` helper **cost 2 lines**
  (a 15-line helper against four call sites whose long tooltips stayed either
  way). Predicted ~9 saved, measured −2, so it was reverted. Verified first that
  all five spinboxes came out identically configured — the only diff was a
  tooltip *I* had shortened. **Rule of thumb: a widget helper needs ~5+ call
  sites to break even.** `stage/panel.py`'s `_call` (8 sites) and
  `stage/driver.py`'s `_send` (7) both paid.
- **Left:** prose is now spread thin rather than pooled — `main.py` (195, mostly
  the operator's), then nothing above ~60 per file. The code half has covered
  `stage/panel.py` and `stage/driver.py`.
- `main.py` yielded only 19: it is the operator's file, so only comment blocks
  were touched and nothing in the dock/settings code.
- **The measuring script is worth rebuilding, not the trim:** density per file
  and the AST check are ~120 lines in the session scratchpad.

### 2026-08-13 (o) — pushed the backlog; the split checker becomes a test
- **13 commits pushed** (`7b7b337..9e503ac`). A session's work had been living
  on one disk; the rig can now pull the whole size pass. Suite green before and
  after: 454 checks, 16/16, 42.8 s.
- **`test_undefined_names.py`** (48 checks, 0.6 s). The symtable checker that
  caught six `NameError`s during the splits had been written and thrown away
  twice; the suite structurally cannot see that class, because the paths it
  breaks are the ones nothing calls. Now kept. Package scans clean — 78 files,
  0 unresolved, 0 star imports.
- **Controls in three layers**, the third being the one that matters: drop a
  real used import from each of the seven files split yesterday and require the
  scan to name it, paired against the unmodified file scanning clean.
- **Boundary found by the controls, now asserted:** under `from __future__
  import annotations` (66 of 78 files) annotations are strings and invisible to
  the scan, so an annotation-only import is *not* defended. Found because the
  injection helper first picked `adapters/base.py`'s `Any` and the scan stayed
  silent — correctly. The helper now drops a runtime-evaluated import.
- Verified while updating this file: **`main.py` is 787 lines**, as §0 says. A
  `Measure-Object -Line` count said 672; it undercounts. Use Python to count.
- **502 checks, 17/17, 43.4 s.**

Entries before 2026-08-13 (o) are in
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
