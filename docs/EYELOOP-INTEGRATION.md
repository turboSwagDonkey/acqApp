# Integrating EyeLoop into acqApp — the handoff

**2026-08-26.** EyeLoop tracks this rig's pupils, headless, at 151/151 frames on
both clips. A bench app (`../eyeloopGUI/`) proves the shape the integration
should take. **Nothing has touched the live Basler**, and nothing in acqApp
imports EyeLoop yet.

This file is the plan for closing that gap. Measurements live in
[EYELOOP.md](EYELOOP.md); this is what to *do* with them.

## Where it stands

| | |
|---|---|
| tracking | **151/151** on pAce and State, ellipsoid and circular |
| cost | 1.2–1.8 ms/frame; 2.4 ms with reflection removal on |
| patches to EyeLoop | **four** — `eyeloop-3.14-patches.diff` |
| environment | opencv-python 5.0.0.93 + PyYAML in `.venv`; suite still 912/912 |
| bench app | `../eyeloopGUI/` — crop, threshold, reflection removal, pins, sweep |
| on the rig | **nothing** |

## Two decisions gate the first line of code

Both are the operator's, and neither is technical.

**1. Which tree.** PLAN §0 forbids restoring a tracker on master without being
asked. Tracker work goes on **`pupil-tracking`**, which already carries a
*working* hand-rolled tracker (151/151 at centre sd 0.87 px). So this is a
replacement, or a second `fit` mode beside it — not a fill-in. EyeLoop does not
win on fit rate or steadiness; **it wins because its ellipse works on real
footage**, where acqApp's own was broken at 8/151.

**2. GPL-3.0.** Vendoring EyeLoop into `devices/` makes acqApp a derived work.
A port does not escape it either — copyright follows the algorithm's
expression, and `Shape` is what would be ported. The sibling-clone arrangement
used so far avoids the question rather than answering it; it cannot survive
into `devices/`.

## The move, in dependency order

**1. Settle the two decisions above.** Everything below assumes the answer is
"the branch", and that GPL has been squared.

**2. `eyeloopGUI/tracker.py` → `devices/pupil_cam/eyeloop_tracker.py`.** It is
written to move unchanged: Qt-free, app-state-free, no window, no file paths
beyond locating the clone. It carries `PupilFit`, `GlintRemoval`, `Pin`,
`remove_glints`, `measure_reflection`, `seed_from_darkest`. **Keep every
EyeLoop import inside this one file** — that is what makes the licence
boundary and the upgrade path legible.

**3. Widen the result contract.** `PupilResult` (centre + one radius +
confidence) becomes `PupilFit` (centre + both semi-axes + angle), with
`radius` staying the mean of the semi-axes so nothing downstream breaks. Set
`confidence` from something real — axis ratio is already computed and is the
cheapest honest signal — not a hardcoded 0.8.

**4. Feed it the eye region.** The crop is **mandatory**: full frame fits
nothing at all (0/151) at 73 ms/frame, and every crop from 200 to 900 px gives
the same answer. acqApp's **eye region** is exactly this crop — operator-set,
drawn, persisted, recorded, and per `archive/pupil_tracking/README.md`
currently consumed by nothing. It gets its consumer back. `source.py`'s
`Region` shows the mapping both ways; `FrameSource` is the seam
`acquisition.py` slots into.

**5. Settings and persistence.** The bench app **does not persist anything** —
close it and the tuning is gone. In acqApp these belong on `PupilSettings`,
with panel controls and the usual save path: pupil threshold, blur, model,
and the reflection-removal set (`enabled`, `threshold`, `pad`, `ring`,
`search_scale`, `pins`). **Pins are rig geometry, not a preference** — they
describe where the fixed reflections land, so they persist with the eye region
and should be cleared when the optics move.

**6. Off the GUI thread.** 2.4 ms fits inside a 33 ms tick, but that was
offline, single-threaded, with no Qt. The branch's `track_worker.py` already
solved this for the old tracker; reuse its shape rather than re-deriving it.

**7. Record what produced the number.** The session file should carry the
ellipse *and* the settings that generated it — threshold above all, since
threshold sets the radius. A pupil trace without its threshold is not
reproducible.

**8. Then the live Basler.** Everything to this point is offline clips.

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

- **The operator's tuned settings.** They exist outside the app, since it
  persists nothing. They should become the shipped defaults and be recorded
  here.
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
