# Session log — archive

Older entries from `PLAN.md` §7, newest first. The three most recent sessions
stay in PLAN.md; everything before them lives here so a fresh session reads the
plan rather than the whole history.

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

