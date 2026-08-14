# Session log — archive

Older entries from `PLAN.md` §7, newest first. The three most recent sessions
stay in PLAN.md; everything before them lives here so a fresh session reads the
plan rather than the whole history.

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

### 2026-08-12 (k) — §5b A4 and A5: the window surface, and `adapters/`
- **A4.** `devices.ModuleHost` names what an adapter may ask of the window.
  Both directions are checked, and the second is the one that earns its keep: a
  Protocol proves the window still *provides* the surface, but the drift that
  costs something is an adapter reaching *past* it, so the test scans the
  adapters' source and fails on any undeclared `self.win.X`. The docstring it
  replaced was already wrong — seven members claimed, nine used.
- **A5.** `modules.py` (1204 lines) is now `adapters/`: `base.py` plus one file
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
- **B2 mostly done:** handoff/transfer docs → this folder with an index;
  83 cross-links repaired; `HANDOFF.md`'s stale status table flagged.

### 2026-08-10 — audit + first fixes
- Full read of ~10k lines → the 21 items in §5.
- Closed #17 (`modules.py` adapter architecture), #1 (camera timestamps, with a
  negative control proving the old behaviour was broken), #3 + #9 (puffer
  settings + the mid-pulse task race), C1 (console encoding, 14 entry points).
- Added `acq/clock.at()`, `Recorder/Writer.update_metadata()` so close-time
  facts (`cam_dropped_frames`, `recorder_dropped_samples`) reach the file.

---

