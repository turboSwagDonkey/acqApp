# Closed decisions — the reasoning, archived

Blocks moved out of `PLAN.md` because the work is finished and the *reasoning*
is what is worth keeping. PLAN.md is read in full every session; this file is
opened only when chasing a specific decision. Sections are named by the PLAN.md
section they came from.

Its two siblings: [AUDIT-2026-08.md](AUDIT-2026-08.md) (the closed audit
checklist) and [SESSIONLOG.md](SESSIONLOG.md) (older §7 entries).

## §0 — what was committed on 2026-08-12, and what went on 2026-08-13/14

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


## §5 — the 2026-08-10 audit, and B2

Nothing from that list is open. **B2 closed 2026-08-12**: `SESSION_HANDOFF.md`
folded into [docs/HANDOFF.md](docs/HANDOFF.md) and removed, its inbound links
repaired. Its session narrative and file list went (that wiring has since moved
into `modules.py`); its three durable facts stayed — the two camera-crash causes,
the measured encoder signal, and the still-open CoaXPress-vs-USB3 fps question.
Folding it also **answered two of HANDOFF's own open items**: `volts_per_rev` is
a measured 4.912, and the encoder voltage does wrap.


## §5b A1 and A2 — declared interfaces, and the third registration (closed 2026-08-12)

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


## §5b A4 — the adapter's narrow surface onto the window (closed 2026-08-12)

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


## §5b A5 — `main.py` and `modules.py` are large (closed 2026-08-12)

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


## §6 items 0 and 1 — the root regroup, and "raise the capture rate" (closed)

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


## §6 item 5 — the trim pass: file lists, optimisations, and the survey findings

   **Done 2026-08-19** (`3777e98`): `voltage_cam/_check_link.py`,
   `acq/devices.py`, `adapters/{wheel,stage}.py`, `devices/stage/settings.py`,
   `probe.py`, `saving/config.py`, `config.py`, `dialogs.py`.

   **Done** (`9131017`, `7b40bb9`, `383c787`, and the last of that session):
   `main.py` `wheel/acquisition.py`
   `pupil_cam/settings.py` `console.py` `closed_loop/{__init__,settings}.py`
   `adapters/{__init__,base,dmd,closed_loop,voltage_cam}.py`
   `pupil_cam/{track_worker,rays,tracking,fits,video}.py`
   `acq/{worker,ring_buffer,sync,writer}.py`
   `voltage_cam/{presets,acquisition}.py` `dmd/{alp,calibration}.py`
   `tests/_harness.py`

   **Not done, if it is resumed anyway** — but read the "why close it" note
   above first: `tests/test_encoder_derive.py`, `tests/test_device_contracts.py`
   (test prose carries the "what this defends" reasoning `tests/README.md`
   leans on, so trim these last if at all), `acq/recorder.py`,
   `stage/{control,panel}.py`, `pupil_cam/{acquisition,avi}.py`,
   `closed_loop/worker.py`, `dmd/{control,roi,roi_panel}.py`,
   `puffer/control.py`, `adapters/pupil_cam.py`.

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

   **From the 2026-08-26 sweep over `routines/` (the same two jobs, worst-first
   by ratio — which found nothing, so both findings came from profiling):**
   - **`Recorder.offered()` took the enqueue gate to read one int.** It is read
     ~70×/s from the GUI thread while a routine runs (the routine's own tick
     plus the display tick), and every device worker enqueues through that same
     lock. **6.1 ms mean / 28.7 ms worst → 1.4 µs / 4.2 µs** against a
     saturating producer. The lock bought a count that never leads the buffer,
     which no caller can tell apart: the one comparison is
     `n - frame0 >= length`. One dict lookup of an int is atomic under the GIL;
     the *increment* still happens under the gate the enqueue already holds,
     for 68 ns/sample. `test_recording_losses` holds the gate from another
     thread and requires the read to return, with the locked read as the
     control that blocks.
   - **The routines panel restyled itself every display tick.** `setStyleSheet`
     repolishes against the window's whole cascade — 26 µs a call, and **53 %
     of the shared 30 Hz tick** with eight modules loaded, to re-apply an
     identical string. Guarded on the phase actually changing: tick
     **0.05 → 0.02 ms**, and `set_state` leaves the profile entirely. Honest
     framing: the tick was never in trouble (0.1 % of budget), it was simply the
     largest thing in it and free to remove. Counted, not timed, in the test.
   - **A micro-benchmark said this last one cost 2 µs and was not worth it.**
     Profiling it *in the built window* said 26 µs. The 2026-08-18 lesson holds:
     one profile of the real path beats guessing, and beats a bench on a
     detached widget.
   - Two dead names deleted (`settings.with_step`, `Phase.ALL` — nothing ever
     called either), and `StepRun.attrs()` was **promising the file something
     nothing wrote**: it had no caller outside the test. Now `single` mode files
     the list as `routine_runs`, so which execution faulted, and when each
     started on the session clock, is recoverable — `/routine` carries only a
     signed index.

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
   - ~~The **camera read path and the writer are already at hardware
     limits** (92 % of the link, 1004 MB/s measured), so neither is an
     optimisation target.~~ **Half of that was wrong, and it cost a week of
     dropped frames.** The read path really is at 92 % of the link. The writer
     was nowhere near a hardware limit: the same disk writes 2700 MB/s from a
     plain file, and the 1004 was one line of Python — `dset[i] = frame`
     (2026-08-25, item 7). *"Already at a hardware limit" was inferred from a
     single end-to-end number, with nothing measuring the hardware underneath
     it.* Measure the floor before calling something floor-bound.


## §6 item 8 — projecting through the full app (half closed 2026-08-12)

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


## Moved from PLAN.md §6 on 2026-08-24

Closed items kept for their reasoning, plus the long-tail open ones. The
plan's §6 holds the next three actions only; anything here that is still
open says so.

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
     the other needs a measured camera↔DMD transform. **Closed 2026-08-24 (aj)
     as far as it can be closed off the rig**: the sweep is built, wired to a
     Calibrate… button and tested end to end, and it emits light, so items 1
     and 2 above are the rest of it. The hand alignment in
     `acqapp_local.json` (104 %, −7 px, `fit` off) is not a substitute — it was
     set by eye, and nothing measured where it lands.
   - **Actuation applies at two points** (§2): the calibration projection *and*
     every stimulation. Verify open → render → upload → release first, which
     projects nothing, then ask.
   - **The offline half is built and verified (2026-08-18), and nothing in it
     projects or grabs** — the point being that the pipeline is held to a
     transform *we chose* before any light is emitted.
     `devices/dmd/calibration.py` (`calibrate(project, grab, …)` takes the two
     hardware operations **as callables**; signed stripe offsets, so a **mirror
     flip cannot pass as a valid registration**) and `devices/dmd/roi.py` + `roi_panel.py` (rect and circle ROIs
     in camera px, the reachable field outlined, ROIs outside it named rather
     than silently clipped). Measured on the simulated rig: decode median
     **0.40 px**, homography **rms 0.41 px** over 3169 points, ROI round trip
     **100 %** on target with 0 % spill. 51 checks, each with a control.
   - **What is left needs the rig**: (a) run the probe, then the sweep — items
     1 and 2 above; (c) single frame or an average for the ROI snapshot.
     ~~(b) wire `RoiEditor` into the adapter~~ done (`720a1b6`).
   - **Two traps, both now handled in code — do not re-introduce them.**
     Calibration patterns **must bypass `build_frame`**: hence
     `project_frame()` and the `RawProjector` protocol, with a test whose
     control shows `build_frame` really would move the pattern. And
     `alp.project()` uploads **one** frame (`SeqAlloc(nbImg=1)`), so the sweep
     is software-timed, project→grab per plane — which is why `FreshGrabber`
     exists; a hardware-timed sweep would need `project_sequence()`.

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

7. ~~**Make full-frame recording fit the writer.**~~ **Closed 2026-08-25 —
   the writer was made to fit the camera instead.** The operator confirmed
   full-frame bin 1 is wanted, so the constraint was worth removing rather
   than working around.

   What it was. **Measured 2026-08-17 through the real path** (ORCA →
   `OrcaFireWorker` → `Recorder` → `HDF5Writer` → D:), 10 s per run, frames
   counted **off the closed file** rather than off a counter:

   | run | offered | on disk | kept | sustained |
   |---|---|---|---|---|
   | full frame, bin 1 | 957 | 506 | **52.9 %** | 1004 MB/s (47.9 fps) |
   | full frame, bin 2 | 1153 | 1153 | **100 %** | 603 MB/s (114.9 fps) |

   **What it was not: the disk.** A plain file on the same D: writes
   **2700 MB/s**. That measurement should have come first — see the withdrawn
   "already at hardware limits" note above.

   **The fix is one branch.** Where a frame is exactly one chunk and no filter
   is configured, hand HDF5 the frame's own buffer (`write_direct_chunk`)
   instead of assigning through the dataset. No cache copy, no type
   conversion. Full frame, on D:, 2026-08-25:

   | path | MB/s | kept |
   |---|---|---|
   | writer alone, `dset[i] = frame` | 1304 | — |
   | writer alone, direct chunk write | **2696** | — |
   | whole path, offered 106 fps | 2225 | **100 %** |
   | whole path, saturated | 2464 (117 fps) | — |
   | whole path, 60 s / 133 GB | 2220 | 99.9 % |

   Then the ring became the constraint and 512 MB → 2 GB took the last of it
   (25 → 102 frames of slack; 14–54 frames lost per 30 s run → 0, twice).

   **Measured and worthless, recorded so nobody repeats them** — all within
   3 % of 1300: chunk cache size, growth block size, preallocating the whole
   dataset, 1 MB and 4 MB file alignment, `meta_block_size`, and the Windows
   VFD. Multi-frame chunks are actively *slower* (2 frames 2001, 4 frames
   1795). No fast compressor exists in this venv — h5py has deflate and
   shuffle only, no blosc/lz4/zstd — and none is needed now.

   **The guard is the dangerous part, not the speed.** A direct write converts
   nothing, and an undersized one (a uint8 frame into a uint16 chunk) is
   accepted silently: the write returns, the file closes, and *reading it back
   kills the process with an access violation*. `_writable_chunk` checks
   shape, dtype and C-contiguity; `test_writer_chunks` proves the guard earns
   its place by running the unguarded case in a child process and requiring it
   to die.

   **Still open: this has not run against the camera.** `WRITER_MBPS` is now
   1800, which is the saturated bench derated by the 0.77 the 2026-08-17 run
   showed against its own bench. That derate is a guess. Re-measure with the
   ORCA running and replace it. If the grab thread still costs enough to shed
   frames, the next lever is DCAM's own recorder (`dcamrec_*` → `.dcimg`):
   pylablib copies the `DCAMREC_*` enums but binds none of the functions, so
   it means hand-written ctypes, and it costs the one-file/one-clock
   invariant — the camera would land in its own file on DCAM's timebase.

   - **Binning does not cost frame rate on this camera** — the frame period is
     **8.68 ms at bin 1, 2 and 4**, so binning cuts bytes, not time. 2×2 gives ¼
     the data at the same 115 fps. **If the science tolerates 2216×1184 this is
     the whole answer and needs no code**; otherwise cap the rate (the panel
     warns and names the exposure) or take a smaller ROI.
   - **1004 MB/s, not the 1165 benchmark** — a benchmark measures the writer,
     this measures the path, and the gap between them is the camera. That set
     `WRITER_MBPS` to 1000 (advised cap 48 fps); it is **1800 since
     2026-08-25**, and the same bench/path gap is what that number is derated
     by.
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
  **Superseded 2026-08-24 (aj).** The 2026-08-12 note here said acqApp stays on
  `fit = True` deliberately. **That is no longer what the app is running**: the
  live `acqapp_local.json` has `fit: false, scale_pct: 104.0, offset_x: -7`, so
  the operator hand-aligned it at some point without updating this file.
  `dmdGUI_project` is at **132.4 %**. Either way both are alignments set by eye;
  the sweep in items 1–2 is the measurement, and it is what the ROI path needs.
- ~~Phase 0's camera throughput number.~~ **Closed, re-measured 2026-08-17
  through `OrcaFireWorker`: 105.92 fps, 2223 MB/s** at full frame (4432×2368,
  20.99 MB/frame), against a camera offering 115.26 — 92 % of the link. **The
  read path is not the limit.** The writer was, until 2026-08-25 (item 7);
  now nothing in the path is, at full frame bin 1. Size the ring buffer from
  2223 MB/s — it is 2 GB. The 2026-08-14 figure of 46.17 fps / 969 MB/s is
  **withdrawn**.
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
