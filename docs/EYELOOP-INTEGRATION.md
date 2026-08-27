# Integrating EyeLoop into acqApp — the handoff

**2026-08-26.** EyeLoop tracks this rig's pupils, headless, at 151/151 frames on
both clips. A bench app (`../eyeloopGUI/`) proved the shape the integration
should take, and **that integration is now built on master** — the panel, the
persistence, its own thread and the trace in the session file. **Nothing has
touched the live Basler**: everything is offline clips and the mock.

This file was the plan for closing that gap and is now the record of how it was
closed, step by step, with what each step actually became. Measurements live in
[EYELOOP.md](EYELOOP.md); this is what was *done* with them.

## Where it stands

| | |
|---|---|
| tracking | **151/151** on pAce and State, ellipsoid and circular |
| cost | 1.2–1.8 ms/frame; 2.4 ms with reflection removal on |
| patches to EyeLoop | **four** — `eyeloop-3.14-patches.diff` |
| environment | opencv-python 5.0.0.93 + PyYAML in `.venv`; suite still 912/912 |
| bench app | `../eyeloopGUI/` — crop, threshold, reflection removal, pins, sweep |
| in acqApp | **steps 2-7 are done** (2026-08-26): tracker, seam, settings, panel, persistence, its own thread, the trace in the file |
| on the rig | **nothing** |

## Two decisions gated the first line of code

Both are the operator's, and neither is technical. **The first is answered.**

**1. Which tree — ANSWERED 2026-08-26: master.** The operator asked for it
there. (The reasoning that pointed at the branch is kept because it still
describes what EyeLoop is and is not: `pupil-tracking` carries a *working*
hand-rolled tracker at 151/151 and centre sd 0.87 px, so this was a replacement
rather than a fill-in. EyeLoop does not win on fit rate or steadiness; **it wins
because its ellipse works on real footage**, where acqApp's own was broken at
8/151.)

**2. GPL-3.0 — STILL OPEN.** Vendoring EyeLoop into `devices/` makes acqApp a derived work.
A port does not escape it either — copyright follows the algorithm's
expression, and `Shape` is what would be ported. The sibling-clone arrangement
avoids the question rather than answering it — and it did survive into
`devices/`, because `eyeloop_tracker.py` *imports* the clone and vendors none of
it. **This repo is public**, so the answer still has to be given before anything
is copied in.

## The move, in dependency order

**Steps 2-7 landed on master on 2026-08-26** — 39536ba (the tracker and the
seam) and the commit after it (the panel, persistence, the thread, the trace).
Each is marked below with what it actually became, since the plan and the code
now differ in places. **Step 8 is the whole of what is left**, and it is a rig
trip.

**1. Settle the two decisions above.** ~~Everything below assumes the answer is
"the branch"~~ — the operator chose **master**, on 2026-08-26. The GPL question
is **still open**: nothing is vendored, EyeLoop is imported from `../eyeloop`,
and that arrangement is what makes the answer deferrable rather than answered.

**2. DONE. `eyeloopGUI/tracker.py` → `devices/pupil_cam/eyeloop_tracker.py`.** It is
written to move unchanged: Qt-free, app-state-free, no window, no file paths
beyond locating the clone. It carries `PupilFit`, `GlintRemoval`, `Pin`,
`remove_glints` and `measure_reflection`. (`seed_from_darkest` came over too
and was **deleted 2026-08-26 (bb)** — nothing here seeds from a full frame, the
crop's centre is the seed, and the measured reason not to rebuild it is in "Do
not rebuild" below. The bench app keeps its own copy.) **Keep every
EyeLoop import inside this one file** — that is what makes the licence
boundary and the upgrade path legible.

**3. DONE (partly). Widen the result contract.** `PupilFit` replaced
`PupilResult` outright rather than widening it — the old tracker is archived, so
there was nothing to stay compatible with. `confidence` is **not** carried:
`axis_ratio` is a property of the fit and is computed from it on demand.
Original note follows. `PupilResult` (centre + one radius +
confidence) becomes `PupilFit` (centre + both semi-axes + angle), with
`radius` staying the mean of the semi-axes so nothing downstream breaks. Set
`confidence` from something real — axis ratio is already computed and is the
cheapest honest signal — not a hardcoded 0.8.

**4. DONE. Feed it the eye region** — `PupilSettings.crop_box()` derives the
crop from the circular region, and `PupilTracking` re-arms the tracker whenever
that box changes, because `Shape` computes its walk corners once per size. The
crop is **mandatory**: full frame fits
nothing at all (0/151) at 73 ms/frame, and every crop from 200 to 900 px gives
the same answer. acqApp's **eye region** is exactly this crop — operator-set,
drawn, persisted, recorded, and per `archive/pupil_tracking/README.md`
currently consumed by nothing. It gets its consumer back. `source.py`'s
`Region` shows the mapping both ways; `FrameSource` is the seam
`acquisition.py` slots into.

**5. DONE. Settings and persistence.** All of it is on `PupilSettings` and all
of it round-trips through a real restart (`test_settings_persistence`) — the
panel's `settings` property read six of eighteen fields before this, which is
how the operator's tuning was lost. **Pins are placed on the preview**, not
typed: click a reflection to pin it (sized off the blob under the click), click
a pin to remove it. `cr_pins` needs `__post_init__` to normalise it, because
JSON has no tuples. Original note follows. The bench app **does not persist anything** —
close it and the tuning is gone. In acqApp these belong on `PupilSettings`,
with panel controls and the usual save path: pupil threshold, blur, model,
and the reflection-removal set (`enabled`, `threshold`, `pad`, `ring`,
`search_scale`, `pins`). **Pins are rig geometry, not a preference** — they
describe where the fixed reflections land, so they persist with the eye region
and should be cleared when the optics move.

**6. DONE. Off the GUI thread** — `devices/pupil_cam/track_worker.py`, built
whether or not tracking is on so the preview has one code path. Two things the
branch's version did not have to deal with: its `error` had to stay the
`PullWorker` signal (the message is `track_error` — shadowing that signal breaks
the guard that keeps an exception in `run()` from killing the process), and the
settings are swapped wholesale rather than queued as kwargs, since
`PupilSettings` is what the seam takes. Original note follows. 2.4 ms fits inside a 33 ms tick, but that was
offline, single-threaded, with no Qt. The branch's `track_worker.py` already
solved this for the old tracker; reuse its shape rather than re-deriving it.

**7. DONE. Record what produced the number.** Five scalar streams —
`pupil_x`, `pupil_y`, `pupil_major`, `pupil_minor`, `pupil_angle` — one sample
per tracked frame, **NaN in all five where there was no fit**. The settings go
into the metadata (`pupil_track_threshold` above all) and the close writes
`pupil_frames_tracked` / `pupil_fits`, because tracking drops frames it cannot
keep up with and the trace is sparser than the frames. **The trace and the
frames are stamped independently**: these frames carry no camera timestamp, so
a fit is stamped when its frame was pulled, which can lag the frame's own stamp
by a poll interval. Original note follows. The session file should carry the
ellipse *and* the settings that generated it — threshold above all, since
threshold sets the radius. A pupil trace without its threshold is not
reproducible.

**8. NOT DONE — the live Basler.** Everything to this point is offline clips
and the mock. Nothing here has met the real camera, and the two questions it
answers are whether one threshold holds for a session and whether a fit keeps
up with the frame rate at full resolution.

## What must not be re-learned

Each of these cost real time, and every one of them reports success while
being wrong.

| trap | what happens |
|---|---|
| **`params` is never reset on failure** | `fit()` catches all, logs at INFO, leaves the last good fit in place. A dead frame returns stale data. `track()` nulls it first — keep that. |
| **`center_adj_` blocks forever** | On any fit failure it runs `HoughCircles` and, per circle, opens a modal window and calls `waitKey(0)`. Bound to a no-op in the wrapper. **Never unbind it.** |
| **walk radius vs accept radius** | `Shape.min_radius/max_radius` bound the ray walk and are clipped *into an int array*. Floats there make every frame throw inside a bare except — silent, total failure with nothing logged that looks like a type error. |
| **fit rate is not accuracy** | Threshold sets the radius: 60 % swing across thresholds 25–60, at a clean 151/151 throughout. A seed 100 px off halves the radius, also at 151/151. |
| **masking the rim inflates the radius** | Blanking bright pixels near the boundary erases the boundary; the walk runs outward and the search area grows with it. +3.5 px before it was constrained. |
| **the search area must follow the ellipse** | A circle at 0.85 × mean-radius is already outside an 0.78-ratio pupil on the minor axis. |
| **`eyeloop.config` is process-wide** | One tracker per process. Two cameras would silently share frame geometry. |

## Do not rebuild

- **EyeLoop's own reflection removal.** `artefact_` is disabled in three places
  and writes to a buffer `engine.py` never creates. It has never run, and
  upstream tracks the pupil *before* the CR anyway.
- **A cv2-free port**, unless it is wanted for its own sake. The dependency is
  three ops on the hot path (`erode`, `GaussianBlur`, `threshold`) plus the
  failure path; `scipy.ndimage` covers the three. cv2 installs on 3.14 and
  acqApp imports it nowhere else, so it is additive. **The `waitKey(0)` is not
  part of that trade** — it goes either way.
- **Auto-seeding from the darkest blurred point on a full frame.** It returns
  the frame corner: these are wide-FOV shots and vignetting beats the pupil at
  large scale. Inside a crop it works.

## Still open

- **The operator's tuned settings.** ~~They exist outside the app, since it
  persists nothing.~~ The app persists them now, in `acqapp_local.json` — what
  is still open is which numbers the operator settled on, and whether they
  should become the *shipped* defaults rather than one machine's local state.
- **Whether threshold can be set once per session or must track illumination.**
  It is the single most consequential number and nothing measures it yet.
- **Ground truth.** Everything measured so far is a proxy —
  `archive/pupil_tracking/_mark_truth.py` exists to fix that and has never been
  run. A dozen hand-marked frames would settle which threshold is *right*,
  which is the one question no amount of sweeping answers.
- **Ellipse or circular.** Circular is ~2.5× cheaper and equally steady on
  these clips. Ellipsoid earns its cost only if the ellipse is wanted for its
  own sake — which was the point of trying EyeLoop.
- **`eyeloopGUI/` is not under version control**, and neither is `wheelApp/`.
