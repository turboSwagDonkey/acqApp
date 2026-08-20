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
