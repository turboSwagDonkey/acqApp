# PLAN.md — the living plan for acqApp

**This is the single file that carries the project across sessions.** Start a
session with *"continue with PLAN.md"* and this becomes the working context.

There is exactly one of these files. Don't create `PLAN_v2.md`, `NEW_PLAN.md`,
or a second copy in another folder — a plan that exists twice is a plan that is
wrong once.

| | |
|---|---|
| **Last updated** | 2026-08-14 |
| **What the app is** | see [README.md](README.md) — that stays the authoritative *description*. This file holds the *plan*. |
| **Progress** | Roadmap phases 0–5 done (**phase 5, closed-loop, built and mock-verified**); **audit remediation 100 %** (22 of 22 closed). §6 item 0 (root grouping) closed 2026-08-14. What is left needs hardware — but see §2: more of it is on this machine than was thought. §5b has **1 open item** (A3), reviewed and deliberately left open. |

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
§6's "Needs the rig". The test suite is the contract: **531 checks, 18 files,
~44 s, all passing.** Run it before and after anything.

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

**Nothing is uncommitted and nothing is unpushed** (`origin/master`, 2026-08-14).

**Pick up here.** §6 item 1: **this machine has more hardware than §2 claimed**
(ORCA on CoaXPress, the 6363, COM54 — found 2026-08-14), so several "Needs the
rig" items may be closable without travelling. That is a question for the
operator before anything is run, not a licence.

**Two standing instructions from the operator:**
- **Write comments terser than the surrounding style.** This codebase's prose is
  considered too long; state the non-obvious *why* in a line and stop. A trim
  pass took it 25 % → 23 % of the tree (§7, 2026-08-13 (p) and (q)).
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
- **`scratch/` is down to `cam_grab.py`**, which stays: it is how §6's open
  camera-throughput measurement gets taken. The `encoder_*.csv` beside it are
  the rig capture behind the 4.912 V/rev figure — data, not code.

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

0. ~~Group the root modules.~~ **Done 2026-08-14** — option (a), plus the
   operator's call to gather the six instrument packages under `devices/`. See
   §7 (r).

1. **Decide what the hardware on this machine is for.** §2's "rig-only" list was
   wrong (see there): the ORCA, the 6363 and COM54 all answer here. That may
   unblock several "Needs the rig" items *without* travelling, but it also means
   an unguarded script here can drive real hardware — ask first, as §2 says.
   The cheapest next measurement is phase 0's, below.

2. **Project through the full app.** *Half of this closed on 2026-08-12, on
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
3. **Close the loop on the rig.** Phase 5 is built and mock-verified but has
   never seen an animal, and the one number it needs cannot be guessed here:
   **what wheel speed counts as "running"** for this rig's V/rev and diameter.
   The tab is designed for finding it — disarmed, the rule still evaluates and
   the readout shows whether the condition is met, so the threshold can be set
   against a live animal without actuating anything. Arm only after that reads
   sensibly. Start with the puffer (a puff is recoverable; a stimulus train
   mid-experiment is not), `retrigger` off, and a `max_fires` ceiling.
4. **Decide which wheel speed a rule should watch.** The panel offers both and
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
- Phase 0's camera throughput number:
  `.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save`
  → the achieved MB/s sizes the ring buffer (#14) and confirms the SSD keeps up.
  **The camera answers on this machine** (2026-08-14), so this is now a matter of
  asking, not travelling. The *link* half is already settled:
  `devices/voltage_cam/_check_link.py` reports **CoaXPress**, 8.68 ms full-frame
  period → 115.3 fps, 2307 MB/s — which closes the CoaXPress-vs-USB3 question
  §5 B2 left open. That is `get_frame_timings()`, i.e. what the camera says it
  can deliver; it is **not** the sustained-to-SSD number this item wants.
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
