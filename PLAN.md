# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-13 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done (**phase 5, closed-loop, built and mock-verified**); **audit remediation 100 %** (22 of 22 closed). Everything left in phases 0–5 needs the rig. A separate architecture review (§5b) has **1 open item** (A3), reviewed and deliberately left open. |

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
2026-08-10 audit is closed, so **everything still open needs the rig** — see
§6's "Needs the rig". The test suite is the contract: **502 checks, 17 files,
~43 s, all passing.** Run it before and after anything.

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py
```

Use the **absolute** path to that interpreter. The shell usually starts in
`Desktop\python` (the *parent* of this repo), where `.venv\Scripts\python.exe`
resolves to nothing and Python reports a baffling "the module '.venv' could not
be loaded". Python here is **3.14** — no cv2 wheels exist for it, which is why
`pupil_cam/tracking.py` is hand-rolled numpy.

**Sibling projects are proven code, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` are standalone apps for the stage and the DMD that already
work on this hardware. acqApp deliberately *shares their config files* rather
than duplicating their state — the stage's calibration lives in
`stage_control/config.json`, the DMD's ALP path and optical alignment in
`dmdGUI_project/dmd_config.json`. Before writing a device path from scratch,
look next door: `dmd/alp.py` is a port of `dmdCommandLine.py`, and it is the
reason #5 took one session instead of several.

**Practical gotchas that have each cost real time:**
- **PowerShell 5.1 mangles quotes** passed to native executables. Write commit
  messages to a scratch file and use `git commit -F <file>` — a `-m` with an
  apostrophe or an embedded quote gets re-tokenised and git sees a bogus
  pathspec.
- The Bash tool's working directory is not always this repo. `cd` first.
- Tests are plain scripts, **not pytest**, and each runs in its own process.
- When adding a test, follow the two conventions in
  [tests/README.md](tests/README.md): isolate user state, and include a control
  wherever the test could be vacuous.

**Nothing is uncommitted and nothing is unpushed.** The 13-commit backlog that
had built up over the size pass is gone, so the rig can pull all of it.

**A comment-trimming pass is half done** — see §7's 2026-08-13 (p). The
operator's instruction is that this codebase's comments are too long, so **write
new comments terser than the surrounding style**: state the non-obvious *why* in
a line and stop. Seven files are done; the density table and the AST-equivalence
check that made it safe are in that entry.

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
- **`scratch/` is down to `cam_grab.py`**, which stays: it is how §6's open
  camera-throughput measurement gets taken. The `encoder_*.csv` beside it are
  the rig capture behind the 4.912 V/rev figure — data, not code.

**`main.py` is the operator's active file.** The settings-window work is theirs
and ongoing: **ask before touching its dock/settings code**, and don't
restructure it (that is why §5b A5 split `modules.py` and deliberately left
`main.py` alone; it is 787 lines).

**A warning that cost most of a session.** An editing pass aimed at "the
settings" rewrote every file with *settings* in its name, including
`stage/settings.py` — which it reduced to stubs, so `load_settings()` returned
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
- **This machine has *some* hardware — check, don't assume.** This rule used to
  read "the laptop has no hardware", and on 2026-08-12 that was wrong: the
  **DMD is attached to this machine** and opens from `acqApp/.venv`. The
  camera, NI board and stage are still rig-only. So: write + commit + push
  here, pull + run + fix on the rig, but *probe before concluding* a device is
  absent — `dmd/alp.py`'s `AlpDevice.open()` answers in 0.14 s. Anything that
  genuinely can't be checked here goes in §6 "Needs the rig".
- **Ask before actuating anything physical.** Opening, configuring and
  uploading to a device are safe and reversible; **emitting light, firing the
  puffer, or driving the stage are not** — this is an in-vivo rig and there may
  be an animal under the objective. The pattern that worked for the DMD: verify
  the whole path *short of* the actuating call (open → render → upload →
  release, which projects nothing), report that, and ask before the last step.
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
      **Done 2026-08-12.** New `devices.py` holds seven small `Protocol`s
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
      `modules.ADAPTERS`, `config.MODULES` **and** a `style.HEX` colour; the
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
- [x] **A4 The adapter's "narrow surface" onto the window is a docstring
      promise.** ✅ Adapters get the whole `win` object; the seven-method
      contract in the header was enforced by nothing. A `Protocol` here too
      would make it real, and pairs naturally with A1.
      **Done 2026-08-12.** `devices.ModuleHost` — the same idea pointed up
      instead of down. The docstring was already wrong when this was written:
      it named seven members while the code used nine, because the closed loop
      added `module_keys` and `signal_sources` with nothing to notice.
      `test_device_contracts` checks **both** directions, which matters — a
      Protocol alone only proves the window still *provides* the surface, and
      the drift that actually costs something is an adapter reaching *past* it
      into `win._save_panel`. So the second half scans the adapters' source and
      fails on any `self.win.X` that `ModuleHost` doesn't declare. Widening the
      surface is now a deliberate line in `devices.py`.
- [x] **A5 `main.py` (899) and `modules.py` (1204) are large.** ✅ `main.py`
      still carries window chrome, docks, theme, session start/stop and
      recording wiring; `modules.py` was seven cohesive adapters in one file.
      Low priority and partly the A-side of the trade named above — but if
      `modules.py` grows again, split it per instrument rather than per layer.
      **`modules.py` done 2026-08-12; `main.py` deliberately not.** The trigger
      this item set fired: the closed loop took the file from 939 to 1204 lines
      (+28 %). It is now `modules/` — `base.py` (the adapter, the two shared
      widget builders, the plot constants), one file per instrument, and
      `__init__.py` holding the registry and the lifecycle table. Per
      instrument, not per layer, as instructed: a session at the rig is spent on
      *the wheel* or *the DMD*, and the adapters were already independent of
      each other (they import only `base`). Bodies moved verbatim — verified
      line-by-line against the pre-split file, the only differences being
      imports, section banners and the two lines A4 changed. Callers see
      nothing: `modules.build_adapters`, `.ADAPTERS` and `.ModuleAdapter` are
      where they were. **`main.py` was left alone on purpose** — it is the
      operator's active file this week (the settings dialog work), and
      restructuring under someone's in-progress edits is how the collateral
      damage earlier this session happened.

## 6. Next actions

1. **Project through the full app.** *Half of this closed on 2026-08-12, on
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
2. **Close the loop on the rig.** Phase 5 is built and mock-verified but has
   never seen an animal, and the one number it needs cannot be guessed here:
   **what wheel speed counts as "running"** for this rig's V/rev and diameter.
   The tab is designed for finding it — disarmed, the rule still evaluates and
   the readout shows whether the condition is met, so the threshold can be set
   against a live animal without actuating anything. Arm only after that reads
   sensibly. Start with the puffer (a puff is recoverable; a stimulus train
   mid-experiment is not), `retrigger` off, and a `max_fires` ceiling.
3. **Decide which wheel speed a rule should watch.** The panel offers both and
   the file records the choice, but the default is `wheel_speed_live` on the
   grounds that a closed loop should act while the animal runs. Measured this
   session: the recorded speed crosses the same threshold **1.15 s** after the
   live one. If the paradigm wants the rule to agree exactly with the recorded
   trace, switch it — this is a scientific call, not a default worth inheriting.

**Needs the rig (can't be closed from the laptop):**
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
- Phase 0's camera throughput number:
  `.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save`
  → the achieved MB/s sizes the ring buffer (#14) and confirms the SSD keeps up.
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

### 2026-08-13 (p) — comment trim, batch 1 of an unfinished pass
- **The operator's call: the codebase's comments are too long.** Measured before
  starting: 15260 lines = 9014 code + **1566 comment + 2253 docstring** + 2427
  blank, i.e. **prose is 25 % of the tree**. Densest were `modules/__init__.py`
  (63 %), `devices.py` (45 %), `wheel/acquisition.py` (43 %).
- **Done so far: 15260 → 15030 (−230)** across 7 files — `devices.py`,
  `modules/__init__.py`, `modules/base.py`, `acq/recorder.py`,
  `wheel/acquisition.py`, `voltage_cam/acquisition.py`, `main.py`. Committed in
  four batches so any one is a single `git revert`.
- **Every batch verified AST-identical** against its parent: parse both, strip
  docstrings, compare `ast.dump` with positions off. That is a mechanical proof
  no code changed — the guard this pass needed, given that a blanket edit over
  "settings" files once gutted `stage/settings.py`. Suite 502/17 after each.
- **Not done, and deliberately visible:** the code-simplification half was
  approved and has not been touched — every change so far is prose, which is why
  the AST proof held. And ~3,400 prose lines remain, mostly in the files below
  `voltage_cam/acquisition.py` in the density table (`pupil_cam/tracking.py`,
  `closed_loop.py`, `dmd/alp.py`, `tests/_harness.py`, `stage/driver.py`).
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
  injection helper first picked `modules/base.py`'s `Any` and the scan stayed
  silent — correctly. The helper now drops a runtime-evaluated import.
- Verified while updating this file: **`main.py` is 787 lines**, as §0 says. A
  `Measure-Object -Line` count said 672; it undercounts. Use Python to count.
- **502 checks, 17/17, 43.4 s.**

### 2026-08-13 (n) — size and duplication: 17051 -> 14984 lines of .py
- **Deleted what the app had superseded.** Three Phase 0 scratch scripts
  (`cam_app` 799, `encoder_read` 212, `cam_live` 199), all five `_toy.py`
  harnesses, and the four `recording.py` modules that existed only for them.
  Nothing imported any of it. `cam_grab.py` and the `encoder_*.csv` captures
  were kept deliberately — see §6.
- **Two capabilities moved into the app first**, so nothing was lost with the
  toys: **Free run** (devices, no clock, no recording — `SessionClock.at()`
  raises rather than invent a timebase, so Record is disabled around it), and
  the pupil **search overlay** with click-to-seed.
- **Splits**, all verified verbatim line-by-line: `main.py` 910→787 (dialogs
  out), `pupil_cam/tracking.py` 885→491 (rays/fits out), `stage/settings.py`
  825→252, `dmd/control.py` 646→278. `stage/settings.py` now has **no Qt import
  at all** — the calibration model is readable and testable without a
  QApplication.
- **Worth knowing: splitting does not reduce line count**, it increases it. The
  −2,067 came from deletions and prose; the splits added ~150. Two different
  goals, and they pull opposite ways.
- A scratch symtable-based checker caught six names the moved code used and the
  new imports missed — `NameError`s in the calibration dialog and DMD preview
  that the mock suite would not have found.
- **454 checks, 16/16.** 13 commits unpushed on `master`.

### 2026-08-13 (m) — end-of-day scan: one real defect, one wrong comment
- **A claim committed the day before was wrong.** `_on_fired`'s docstring said
  that because `ModuleAdapter` is not a QObject, Qt calls the slot directly on
  the emitting thread. **Measured, four experiments:** a slot that is not a
  QObject's bound method runs on **the thread where `connect()` was called** —
  not the emitter's, not the sender's. Every `connect()` here is on the GUI
  thread, so these are queued and safe. Code was right; the reasoning was not.
  Keep such callbacks to one emit anyway: the guarantee lives in the caller.
- That also **cleared a suspect** the scan raised — `voltage_cam`'s
  `drops_update` lambda calling `win.status()` from inside the acquisition
  loop. Connected on the GUI thread, so it queues. Not a bug.
- **Real defect, fixed:** `StageModule.stop()` closed the serial link
  unguarded. `MainWindow._stop_session` stops every adapter in one unguarded
  loop, so one raise there leaves later modules' threads running — and through
  `closeEvent` skips the DCAM handle close, which the pre-init comment says
  crashes the driver on next open.
- **Left open, needs a `main.py` touch:** that unguarded loop itself
  (`main.py:697`). Reported, not changed — `main.py` is the operator's active
  file. One `try/except` per adapter closes it.
- Clean otherwise: no undefined names anywhere, everything compiles, no mutable
  default args. **431 checks, 16/16.**

### 2026-08-12 (l) — §6 item 1, the half that emits no light
- Ran the **app's** path to the real ALP on this machine (§5 #5 only ever proved
  the standalone script): opens as `ALP-4.2 1024x768`, live controller satisfies
  `ProjectorController`, 16 metadata keys populate, geometry swept against the
  real panel — scale, offset and clockwise-positive rotation all correct.
  Stopped immediately before `display()`; **no light was emitted**. USB released.
- **Asked and answered: not projecting from the laptop** — the Display/Stop and
  `/dmd` 0/−1 checks stay §6 item 1, for someone in front of the hardware.
- **Decided: acqApp stays on `fit`**, not `dmdGUI_project`'s 132.4 %. Recorded
  in §6's "Needs the rig" so it stops looking like a bug — see that entry for
  why the seeding in `_settings()` is not firing (it is fresh-install only).
- Two things that made the first sweep lie: a checkerboard is symmetric under
  every transform being tested, and `fit` overrides scale/rotation/offset. Both
  now in §6 item 1 so the next person doesn't repeat them. No code changed.

### 2026-08-12 (k) — §5b A4 and A5: the window surface, and `modules/`
- **A4.** `devices.ModuleHost` names what an adapter may ask of the window.
  Both directions are checked, and the second is the one that earns its keep: a
  Protocol proves the window still *provides* the surface, but the drift that
  costs something is an adapter reaching *past* it, so the test scans the
  adapters' source and fails on any undeclared `self.win.X`. The docstring it
  replaced was already wrong — seven members claimed, nine used.
- **A5.** `modules.py` (1204 lines) is now `modules/`: `base.py` plus one file
  per instrument, registry in `__init__.py`. Bodies moved verbatim and verified
  line-by-line against the pre-split file. Callers unchanged. `main.py` left
  alone deliberately — it is the operator's active file.
- Two scanners caught themselves being vacuous: the `self.win.X` search first
  matched the docstring *describing* it (so docstrings are stripped for that
  scan but not for §3's, whose needles are code containing string literals),
  and the verbatim-check first "lost" 106 lines that were only `git show`
  decoded as cp1252 against files read as UTF-8.
- **431 checks, 16/16, 39.7 s.** §5b is now down to A3 alone, reviewed and left
  open on its own terms. Everything in §6 needs the rig.

### 2026-08-12 (j) — §5b A1: the device pairs get a declared interface
- New `devices.py`: eight small `Protocol`s the adapters read their workers and
  controllers through. Split, not fat — the eye-tracking LED is deliberately not
  a `RecordingOutput`, so `detach_sink` asks `isinstance(c, RecordingOutput)`
  rather than `hasattr(c, "set_sink")`. Same answer; the question now has a name.
- **A1's own plan was wrong on one point**, worth recording: "structural typing,
  nothing happens at runtime" is true, and that is the problem — this project
  ships no type checker, so a Protocol nobody asserts catches nothing.
  `test_device_contracts` (33 checks) is what makes it bite.
- It found a live case immediately: **`skipped_frames` was on `OrcaFireWorker`
  only**, and `cam_dropped_frames` read it through `getattr(..., 0)` — so a
  lossy real run and a mock filed the identical 0. Now on both twins and read
  directly.
- The parity half compares each pair's public API with an explicit allowlist for
  deliberate asymmetries (Qt signals, the mocks' synthetic constants). That is
  the half that catches the near-miss A1 was written about: a property added to
  the real class and forgotten on the mock.
- **Found while testing phase 5: `loop_fires` disagreed with `/closed_loop`.**
  The rule runs under Live view too, so it can fire before Record is pressed —
  8 fires, 6 in the file. An attribute that disagrees with the stream beside it
  is a trap, so there are now two counters: `loop_fires` (in the file, always
  `len(/closed_loop)`) and `loop_fires_session` (everything the rule did,
  including actuations that reached the hardware but no file).
- Suite 389 → **426 checks / 16 files / 40.1 s**. §5b down to 3 open (A3–A5).

### 2026-08-12 (i) — phase 5: the loop closes, and a clobbered-file rescue
- **Phase 5 built.** New `closed_loop.py`: `LoopRule` (pure, Qt-free — the
  semantics live there), `ClosedLoopWorker` (its own thread at 200 Hz),
  `SignalSource` (what a rule may watch) and a panel with an arm switch.
  `ClosedLoopModule` is a seventh tab that owns no device.
- **It watches, it does not consume.** `_EncoderBase.snapshot()` is a new
  non-consuming read: `get_latest()` hands each sample out once and the display
  tick is already that consumer, so a second puller would take samples off the
  plot. Measured control in the test: a rival `get_latest()` thread halves what
  the display receives; `snapshot()` costs it nothing.
- **The wheel has two speeds and they are 1.15 s apart** — measured, not
  asserted. `wheel_speed` (recorded) is a slope centred `_LAG_S` in the past;
  `wheel_speed_live` is the EMA behind it. Both are offered, the file records
  which was used, and the default is the live one. See §6.2.
- **Decision on the loop's thread, actuation on the GUI's.** A `ModuleAdapter`
  is not a QObject, so `worker.fired.connect(self._on_fired)` gets no thread
  affinity and runs on the loop's thread — `_on_fired` therefore only emits, via
  `SyncController.fire()` (new), whose receiver *is* a QObject on the GUI
  thread. A rule-driven puff then takes the identical path to a scheduled one.
  The status line moved to `update_display()` for the same reason.
- **Arming is absent from `LoopSettings` entirely**, so it cannot be persisted
  by accident — the eye-tracking LED argument from #4, applied at the type
  level rather than at save time.
- `test_closed_loop` (41 checks) with an ungated control that fires **3200**
  times where the real rule fires 4. Suite 342 → **387 checks / 15 files /
  37.6 s**.
- **Rescue.** Something rewriting every file with "settings" in the name had
  gutted `stage/settings.py` to stubs (`load_settings()` returning defaults,
  `save_axis_updates()` a `pass` — so the shared `stage_control/config.json`
  was no longer read and a just-measured calibration was silently discarded),
  dropped `SettingsDialog._PAD` (settings window raised on open), flipped
  `DmdSettings.static_hold` and deleted the geometry/device keys from
  `DmdModule.metadata()`. Restored `stage/settings.py` from HEAD; repaired the
  rest in place, keeping the operator's real work (`all_on`, the pop-up
  settings window, `default_size()`). Working copies of the clobbered files are
  in the session scratchpad. **Nothing here was committed.**
- **§2 note:** `static_hold` is now the panel's fixed behaviour — the DMD holds
  one image and the on-time/repeat controls are gone. The cycling path survives
  in `dmd/control.py` and in `test_dmd` (which now says `static_hold=False`
  explicitly), reachable from code but not from the UI.

### 2026-08-12 (h) — UI: settings move out of the dock into a pop-up window
- `SettingsDialog` (modeless, in `main.py`) replaces the left settings dock. Same
  tabs, same accents, same `SavePanel`-leads-the-tabs order; what changes is that
  it is a top-level window, so it can sit beside the app or on a second screen
  while a session runs instead of competing with the camera pane for width.
- Built at startup, never destroyed: the panels inside are live objects the
  controllers are configured from, and `_build_controllers()` runs after the UI.
  Closing hides. Size/position persist under `QSettings` key `settingsGeometry`,
  next to the dock layout's `dockState`.
- The ⚙ sidebar action stays checkable and stays in sync both ways — the
  window's `finished` (title-bar ✕ *and* Esc) un-checks it, so the next click
  re-opens rather than doing nothing. `MainWindow.closeEvent` closes it, or a
  parentless-feeling top-level window would outlive the app.
- First-run size is **measured, not hard-coded**: `default_size()` takes the tab
  widget's own hint (the widest panel is the voltage cam's, 1092 px; the tallest
  the DMD's, 721), adds padding, floors it at 900×820 and clamps to 90 % of the
  screen it opens on. So it opens showing the largest panel whole, a panel that
  grows a row can't silently start opening behind a scrollbar, and it still
  can't open taller than the rig's display. Sizing happens on first show, since
  the panels are added after `__init__`; after that the operator's size wins.
- Guarded by 9 new checks in `test_settings_persistence` (top-level window, not a
  dock, starts hidden, one tab per module + Save, the ⚙ tab opens it, closing
  hides it and un-checks the tab, default size covers the widest panel and fits
  the screen). All the persistence edits below them now run against a window
  that is never shown — which is the point: it is built at startup because
  `_build_controllers()` is configured from those panels.
  Suite 335 → **344 checks / 14 files / 29.9 s**. Not eyeballed on a real
  screen — no interactive display this session, so the size numbers come from
  Qt's hints rather than from looking at it.

### 2026-08-12 (g) — #5: the DMD projects, on real hardware
- The DMD turned out to be plugged into this machine, and `dmdGUI_project/` next
  door is a proven ALP driver for it. New `dmd/alp.py` ports its pipeline: API
  lookup, `build_frame`, and the SeqAlloc → SeqPut → BIN_UNINTERRUPTED →
  SetTiming → Run lifecycle. `DmdController` is no longer a stub.
- Geometry (scale/rotation/offset/invert/fit) is now in the panel and in the
  session file. That is not a nicety: it *is* the registration to the optics,
  and the operator's alignment lives in the standalone app — so its saved
  scale/rotation seed acqApp's defaults, the same way the stage shares
  `stage_control/config.json`. Without it acqApp would project somewhere else
  while looking correctly configured.
- The other half of #5 was the UI lying. A busy ALP (the standalone app holds
  the USB — only one process can) now falls back to the mock *and the tab says
  "nothing will be projected"*. `dmd_on_pixels` in the file catches the other
  invisible failure: a frame with every mirror off looks like a projection that
  worked.
- **Verified on the hardware:** opened in 0.14 s, 70 752 mirrors on at the saved
  132.4 %, uploaded in 2 ms, projected and held while the operator looked at it,
  halted and released. `/dmd` logged 0 and −1, 23.0 s apart.
- **C3, found doing this:** `test_module_subsets` toggles Emulate off, so the
  suite was opening the real DMD — and on the rig would open a DO task on the
  puffer's line. `_harness.block_real_devices()` now blocks the vendor drivers
  for every test that isolates user state.
- Added **§5b**, a SOLID pass over the architecture, prompted by a question
  rather than a bug. The finding that matters is A1: the mock/real device pairs
  have no declared interface, so nine `getattr`/`hasattr` probes stand in for
  one — and this session came within a forgotten property of writing
  `dmd_device = "none"` into a session file that had really projected.
- **§2 corrected:** "the laptop has no hardware" was a stale invariant and is
  now "check, don't assume", plus a new rule to ask before anything physically
  actuates (light, puff, stage motion) — verify the path short of that call
  first, as was done here. Added **§0 Start here** for fresh sessions, and both
  `CLAUDE.md` files now point at it and name `dmdGUI_project/` as proven code.
- `test_dmd` (41 checks) against a fake ALP + the geometry as pure functions.
  Suite **335 checks / 14 files / 29.9 s**. Audit remediation is now complete.

Entries before 2026-08-12 (f) are in
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
