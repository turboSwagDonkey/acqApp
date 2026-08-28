# PLAN.md — the living plan for acqApp

**The single file that carries the project across sessions.** There is exactly
one; update it in place, don't fork it.

| | |
|---|---|
| **Last updated** | 2026-08-27 (bi) |
| **What the app is** | [README.md](README.md) is the authoritative *description*; this file is the *plan*. |
| **Progress** | Phases 0–5 done, audit 22/22 closed, **suite 1173 checks / 27 files green**. `closed-loop` is a reconfirmed occasional flake — a cross-thread race in the test, not the app (`docs/SESSIONLOG.md` (ba)); rerun alone before trusting a red run. §6 item 1 **root-caused and fixed**: the save drive (was E:, SATA — now D:) plus an under-sized DCAM ring buffer, neither the writer nor the GIL. Needs one confirmation run with the GUI. §5b **A3** is the one open architecture item. |

---

## 0. Start here (fresh session orientation)

Read this, then §6; §5b is reference, consult only the item in hand. Finished
work lives in three archives, opened only to chase something specific:
[docs/DECISIONS.md](docs/DECISIONS.md) (closed items, reasoning kept),
[docs/AUDIT-2026-08.md](docs/AUDIT-2026-08.md),
[docs/SESSIONLOG.md](docs/SESSIONLOG.md).

**Run the suite before and after anything:**

```
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe c:\Users\User\Desktop\python\acqApp\tests\run_all.py
```

~65 s, no hardware, no windows. **1158 checks / 27 files is a whole run** —
fewer means something didn't run. Use absolute paths — the shell usually starts
in this repo's parent, where the relative one fails obscurely. For one test, run
it directly with `-q`; `run_all` selects by short name (`routines`), not filename.

**This machine IS the rig computer** (operator, 2026-08-17): §2's actuation rule
is live, not theoretical — an animal may be under the objective — and "measure
at the rig" and "measure here" are the same errand.

**Probe before concluding a device is absent** — this line twice claimed "no
hardware" and was twice wrong. `probe_all` reports the ORCA, `Dev3 PCIe-6363`
(wheel + puffer), COM54 (stage) and the DMD — everything but the Basler. A probe
is *enumeration*: COM54 present is not a working link.

**Sibling projects are proven apps, not scratch work.** `../stage_control/` and
`../dmdGUI_project/` already run on this hardware; acqApp **shares their
config** rather than duplicating it (`stage_control/config.json`,
`dmdGUI_project/dmd_config.json`). Look next door before writing a device path.

**The layout.** An instrument lives in two places, not duplicated:
`adapters/wheel.py` plugs it into THIS window, `devices/wheel/` is the driver,
worker, model, widgets. `main.py` → `adapters/` → `devices/`, nothing imports
back; `settings.py` inside a device package is the model (**no Qt**). Root is
the shell, `acq/` the acquisition core. README has the long version,
[docs/STRUCTURE.md](docs/STRUCTURE.md) the tree.

**`main.py` is the operator's file — ask before editing it.** Settings-window
work is theirs, ongoing; don't restructure it (§5b A5 split `modules.py` out
and left `main.py` alone on purpose).

**A warning that cost most of a session:** an edit pass aimed at "the settings"
rewrote every file with *settings* in its name, reducing
`devices/stage/settings.py` to stubs and gutting `DmdModule.metadata()` — only
3 of 5 breakages failed a test. **If the stage or DMD misbehaves, diff against
`HEAD` before debugging** — a green suite missed the one that mattered.

**Two standing instructions:** comments **terser than the surrounding style**
— the non-obvious *why*, one line, stop; **commit freely, batch the pushes**.

**Gotchas, each cost real time:**

- **Read `acqapp_local.json` first** for any "it doesn't work" — it's what the
  app loads, it's gitignored (never in a diff), and once held the whole answer
  to four sessions of work.
- **PowerShell 5.1 mangles quotes** to native exes — write the commit message
  to a file, `git commit -F <file>`.
- Bash's working directory isn't always this repo — `cd` first.
- **Assign `qt_app()`.** An unreferenced `QApplication` is garbage-collected;
  widget construction then aborts natively — exit code, no traceback.
- Tests are plain scripts, **not pytest**, one process each — see
  [tests/README.md](tests/README.md): isolate user state, include a control
  wherever the test could be vacuous.
- Python is **3.14**; **cv2 installs fine** (opencv-python 5.0.0.93, `cp37-abi3`).
- Four diagnostic tools run directly, never imported — they look dead and
  aren't. Check the docs before deleting anything "nothing imports".
- **EyeLoop bakes the fit model into the `Shape` it builds at `arm()`** — not a
  live knob like threshold/blur. A model switch alone (region untouched) needs
  its own re-arm check, or it silently keeps fitting the old shape
  (`devices/pupil_cam/tracking.py`).

**Experiment routines actuate — the one feature whose purpose is to.**
`routines/` (protocol + engine) is **Qt-free, callable-driven**;
`adapters/routines.py` is the only part touching a device. **Always loaded**
(`config.ALWAYS_ON`), panel is **its own window** (`ModuleAdapter.own_window`)
— declarations the shell reads; `main.py` still names no module.
`routines/estimate.py` is the **only** place frames become seconds, naming the
rate used. Read `engine.py`'s docstring first: an interrupted step's data is
**kept and marked**, **Resume repeats that step**.

## 1. Goal

One PyQt6 app, six rig subsystems on **one shared session clock**, **one HDF5
per session** — every stream analysable on a common timebase — plus a closed
loop firing an output from one of those streams. Built and mock-verified; the
work now is trustworthiness **on real hardware**, where almost none of it is
proven.

## 2. Ground rules

Invariants, not preferences — each has cost real time. The everyday list is in
[CLAUDE.md](CLAUDE.md); these two need their reasoning kept here, and are what
the rest of the repo cites "§2" for.

- **Ask before actuating anything physical.** Opening, configuring and
  uploading are safe and reversible; **emitting light, firing the puffer,
  driving the stage are not** — in-vivo rig, an animal may be under the
  objective. The DMD's pattern: verify the whole path *short of* the actuating
  call (open → render → upload → release, which projects nothing), report
  that, ask before the last step.
- **Commit before restructuring** — see §3, the single biggest risk here.

The rest, one line each: installs go only into `acqApp/.venv`; mock-first, and
say plainly when a claim is mock-only; SOLID, judged against §5b's "what is
strong"; worker bodies stay inside the `PullWorker.run()` guard, because an
exception escaping `QThread.run()` aborts the process (PyQt6 `qFatal`); every
runnable entry point calls `enable_safe_console()` before its first print
(`tests/test_console_safety.py` enforces it); never commit experiment data.

## 3. Backup status

`turboSwagDonkey/acqApp` (private), branch `master`. This box is the rig, so a
commit here is already on the machine that runs it — push anyway, the remote
is the backup, and `.gitignore` keeps experiment data out of any commit.
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

- **Open/Closed is real.** A new instrument is a `ModuleAdapter` subclass plus
  a registry line; the only module names in `main.py` are two colour lookups.
  Defend it in review.
- **Dependency inversion in `acq/`.** `Recorder` depends on `Writer` and
  `AbstractClock`, never h5py or `perf_counter` — keeps `DaqClock` (phase 6) a
  drop-in. Vendor SDKs import inside methods, not at module scope.
- **One adapter owning panel+plot+worker+sink+metadata is a deliberate
  trade**, not an SRP failure: SRP by *instrument*, not *concern* — killed the
  six-way repetition in the old `MainWindow`.
- **The DMD's coarse-stripe calibration.** Gray coding, checkerboards,
  homography fitting, the decode — all built, run at the rig, **failed and
  deleted**: scattering in the sample and relay, so a coarser code doesn't fix
  it. **Do not rebuild without new evidence fine patterns survive.** Numbers
  in `devices/dmd/calibration.py`'s docstring.

### Open

**A1, A2, A4, A5 closed** — reasoning in [docs/DECISIONS.md](docs/DECISIONS.md).
**A3 still open:**

- [ ] **A3 Mock/real selection is hard-wired inside each adapter.** Every
      `build_session`/`build_controller` imports both concrete classes and
      picks with `if emulate:` — a test can't inject a device, it has to
      monkeypatch `sys.modules` (why `block_real_devices()` exists instead of
      a one-line fake injection, C3). **Triggered 2026-08-18**, not as
      predicted: not a seventh *module* but a **third variant of one
      device**, when `VideoFileCameraWorker` made `PupilCamModule.build_session`
      an `if` chain over three concrete classes it imports itself. A
      `build_session(source_factory)` seam would make it a one-line
      injection. Not urgent, but the condition is met.

## 6. Next actions

**THE NEXT THREE THINGS**, per §8's own rule. Everything after them is reference
kept for its reasoning, not a queue.

1. **Confirm the fix with the GUI.** Root-caused and fixed (docs/SESSIONLOG.md (bf)): the
   ~510 MB/s ceiling was never the writer or GIL — the save drive (E:, SATA,
   ~550 MB/s cap; moved to D:, NVMe, measured 1533 MB/s) plus an under-sized
   DCAM ring buffer (`_BUFFER_BYTES` 768 MB → 6 GiB, fixed the last ~6%
   camera-side loss too). `WRITER_MBPS` raised 510 → 1300, conservatively
   below 1533. **All measured through a zero-Qt harness**
   (`.../scratchpad/profile_writer_no_preview.py`, session temp dir, not in
   the repo) — left: one real recording through the GUI (live preview,
   saving to D:) to confirm 1300 before raising toward 1533. **Scan drives**
   (Save tab) now catches a wrong save drive from the UI, so this bug class
   doesn't need a session to find again.

2. **Save a calibration and check it optically.** The sweep runs; nobody's
   confirmed where the light actually lands. Run Calibrate…, save the JSON,
   draw one ROI on a landmark, project the mask, look. **An affine has no
   keystone term** — best near centre, worst at edges — no residual replaces
   looking.
   - **Preconditions**: voltage camera *running*, `dmdGUI_project` **closed**
     (one process owns the USB), illumination on.
   - **Read the residual first** (stripe-centroid scatter about a line,
     camera px; single digits good), then `holdout_px` — refit without a
     stripe, predict it — the honest number. If poor: ~6 of 18 stripes run
     off-frame, a two-pass sweep fixes it for ~6 exposures/~20 lines.
     Re-run first.
   - The ALP once refused to open, opened on an identical retry. **One "not
     found or not ready" is not proof the DMD is absent.**

3. **Measure the wheel diameter** — last unmeasured constant, a ruler answers
   it. Until set, the app reports rev/s and rev instead of mm/s and mm, and
   the closed loop's threshold is in revolutions. `volts_per_rev` measured
   4.912, sign settled: **forward = rising voltage = positive speed/distance**.

**Below: the open tail, one line each** — reasoning in the doc named beside
each, read before touching any of them.

**Experiment routines** (`routines/` + `adapters/routines.py`, 185 checks,
mock-verified):

- **`per_step` save mode doesn't roll files.** Both modes write one session
  file with `/routine` boundaries. Rolling means re-entering
  `MainWindow._start_recording` mid-session — **main.py is the operator's
  file, ask first.**
- **`RoutineHooks.moving` is a seam nothing fills.** Arrival is `settle_s`
  (operator's answer) — also why every time estimate is a floor: nothing
  times a stage move.
- **First real run is a rig trip.** A routine placing ROIs inherits a
  calibration nobody's checked optically; **confirm Resume-repeats-the-step
  is wanted** — the one of six decisions not operator-stated
  (`routines/engine.py`).

**EyeLoop pupil tracking** — complete in the app. Measurements in
[docs/EYELOOP.md](docs/EYELOOP.md), traps table in docs/EYELOOP-INTEGRATION.md.
**Read the traps first** — each reports success while being wrong.

- **Step 8, the live Basler**, is all that's left of the build. Not an
  actuation — the pupil camera only looks.
- **GPL-3.0 is still the operator's call.** Nothing vendored — a fresh
  machine needs the clone + patch diff, or `ACQAPP_EYELOOP_DIR`. Vendoring
  makes acqApp a derived work and **this repo is public**.
  `EyeLoopUnavailable` leaves the pupil camera as it was — blocks nothing.
- **The operator's tuned numbers aren't written down** — only in
  `acqapp_local.json`, this machine only. **Whether one threshold holds a
  whole session** is the most consequential unmeasured number.
- New this session (§7 (bg)): rectangle region, 3-way view mode,
  stabilization, blink detection — **none run against the live Basler yet**.

**GUI polish backlog** — Tier 2 findings from the whole-app review (§7 (bh)),
still deferred by operator's own choice; Tier 3 is done (§7 (bi)): Rate/
Exposure "Link" discoverability on both cameras, DMD's "Reset Alignment"
resetting more than its tooltip says, saving panel's capacity warning not
pointing at Scan drives, routines' estimate recompute with no throttle.

**Older items** — closed ones, and the long-tail open ones — are in
[docs/DECISIONS.md](docs/DECISIONS.md) under "Still open".

## 7. Session log

Newest first. 3–6 lines per session: what changed, what it cost, what was
learned. Older entries are in [docs/SESSIONLOG.md](docs/SESSIONLOG.md).

### 2026-08-27 (bi) — Tier 3 from the GUI review: polish, no behavior changes of substance

- **Devices monitor now lights the sidebar item while open**, the same way a
  panel window's own item does (`_on_panel_window`) — `ConnectionMonitor`
  gained the `visibility_changed` signal `PanelWindow` already had
  (dialogs.py), wired to `_devices_action.setChecked` in `_show_devices`.
- **DMD ROI shape combo got a "Shape:" label** — was a bare dropdown next to
  the Draw toggle (roi_panel.py).
- **DMD ROI docstring fixed**: claimed the reachable field was "enforced on
  drag"; no clamp exists, only `_refresh_status()`'s text naming an ROI
  outside it. Corrected, not implemented — a real clamp is Tier 1/2-sized
  work, not polish.
- **Routines' Pattern…/No pattern buttons moved to their own row**, off the
  primary step-list-operations row — both already duplicate a cell
  interaction their own tooltips document, and crowded the row that adds/
  removes/reorders steps.
- **Skipped on purpose**: wheel vs. stage readout style differences — the
  reviewing agent itself judged restructuring not worth it, so left alone.
- Suite 1170 → **1173 checks / 27 files** (new: Devices sidebar-indicator
  check in test_settings_persistence.py).

### 2026-08-27 (bh) — whole-app GUI optimization sweep (5 parallel reviews, Tier-1 fixes applied)

- **5 agents reviewed every GUI module** (main shell, both cameras, DMD,
  wheel/puffer/stage, routines/closed-loop/saving) for performance and UX;
  operator picked Tier 1 (8 items) to fix now, rest deferred.
- **Status bar was unusable during any running session**: `_on_tick` (100ms)
  routed the elapsed-time readout through the transient `status()` message,
  clobbering any real message (a worker error, "Cannot record") within
  ~100ms. Now a permanent `_lbl_time` label; `status()` is free again
  (main.py).
- **DMD mode-switch double-fired**: each radio's own `toggled` connects to
  the same handler, so a click ran the preview rebuild + settings emit
  twice (False for the outgoing button, True for the incoming). Guarded on
  `checked` (devices/dmd/panel.py).
- **Puffer "Test puff" fired with zero confirmation**, unlike the stage's
  three-layer confirm — both are physical actuation under CLAUDE.md's rule.
  Added a `QMessageBox.question` matching the stage's pattern
  (devices/puffer/control.py, operator approved).
- **Four caching/throttling fixes**: `main.py`'s recording-size `stat()`
  throttled to ~1 Hz (was every 100ms tick, blocking on a flaky share);
  DMD's pattern transform + calibration JSON now cached, so a plain resize
  or arrow-key nudge no longer re-runs `alp.build_frame`/reloads the
  calibration file; the stage travel map only repaints past a 0.5 µm
  epsilon (mirrors wheel.py's guarded-title pattern, was unconditional
  every poll tick); routines' step-table reorder now repaints only the
  rows between src/dest, not the whole table.
- Suite 1158 → **1170 checks / 27 files** (new: DMD mode/cache checks,
  stage map-guard checks, routines partial-repaint checks).
- **Deferred** (operator's call, not done): Tier 2/3 findings — Rate/Exposure
  "Link" discoverability (both cameras), DMD's "Reset Alignment" silently
  resetting Invert/Fit beyond its own tooltip, saving panel's capacity
  warning not pointing at Scan drives, routines' estimate recompute with no
  `FPS_EVERY`-style throttle, and several minor consistency/polish items.

### 2026-08-27 (bg) — pupil cam: rectangle region, view modes, stabilization, blink detection, a model-switch bug

- Eye region is now a **rectangle placed by drag** (was a circle, two
  clicks) — `PupilSettings.limit_x0/y0/x1/y1` replaces `limit_x/y/r`.
  `DragRectViewBox` (adapters/base.py) is a real `pg.ViewBox` subclass for
  it, replacing an instance-level `mouseDragEvent` monkeypatch after a
  cleanup sweep pointed at the same pattern already proven in
  `dmd/roi_panel.py`'s `_DrawViewBox`.
- Added: 3-way preview mode (full+overlay / full bare / cropped-to-region);
  Rate/Exposure link matching voltage_cam's panel; rolling-mean outline
  stabilization (raw fit still what blink detection compares); a blink
  detector (sudden radius drop vs. a rolling median baseline), shown as a
  shaded band on the radius plot and recorded as a sixth `pupil_blink`
  stream.
- **Bug fixed**: switching the tracking model (ellipse/circle) did nothing
  until something else forced a re-arm (moved region, new session) — EyeLoop
  bakes the model into the `Shape` built in `arm()`, not a live knob like
  threshold/blur. `PupilTracking` now tracks the armed model, re-arms on a
  change (`devices/pupil_cam/tracking.py`).
- **4-agent cleanup sweep** (reuse/simplification/efficiency/altitude) after
  the feature work: the ViewBox refactor above, two hand-synced parallel
  lists merged into one, `PupilSettings` cached instead of rebuilt every
  display tick, three smaller efficiency fixes. Skipped: the
  voltage_cam/pupil_cam Rate-link duplication (touches voltage_cam, outside
  this session, worth its own pass) and incremental running-sum smoothing
  (negligible gain at this app's frame rates, real float-drift risk).
- Suite 1123 → **1158 checks / 27 files**, all mock/clip-verified — none of
  it has run against the live Basler yet. Also: §0–§6 tightened for wordiness
  (facts unchanged), offsetting most of this entry's own line cost.

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

**The size budget: ~350 lines, ~4.7k tokens.** Check with `wc -l` when you
edit — it's read in full at the start of every session, so every line is paid
for again on every future one, and a soft "aim to stay short" is what let it
reach 818 lines once. Over budget: archive finished §7 entries, cut anything a
`docs/` file already says in full — **§7 holds the newest ~3 sessions**. Move
finished work out rather than trimming detail; the detail is the value. A
prose-tightening pass on §0–§6 (2026-08-27 (bg)) is a second lever — same
facts, fewer words — worth another pass once density creeps back up.

**Numbering is load-bearing.** 27 places cite these sections by number —
`devices/dmd/sweep.py`/`calibration.py` cite **§2**, six files cite §6, five
cite §7. **Add a "§5b"-style suffix rather than renumbering**, or update every
reference in the same commit.
