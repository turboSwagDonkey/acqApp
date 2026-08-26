# EyeLoop as acqApp's pupil tracker — what was tried, and what it measured

**2026-08-26.** EyeLoop was cloned, patched and driven headless over both rig
clips. **It works**, at 151/151 on each, including the operator's hard one.
Nothing has been integrated: no acqApp module imports it, and this file plus
[eyeloop-3.14-patches.diff](eyeloop-3.14-patches.diff) are the whole result.

Source of the request: the operator's *EyeLoop Integration Handoff* artifact,
written on the laptop against a tree that is **not** this one — it targets a
`pupil_cam/tracking.py` stub, and here that tracker was built, measured and
*retired* on 2026-08-24. Translate its file map before following any step.

## Where it is

    ../eyeloop/            clone, upstream cd22fb7, v0.35, GPL-3.0

The clone is **untracked by any repo here**, so
[eyeloop-3.14-patches.diff](eyeloop-3.14-patches.diff) is the only durable copy
of the patches. `git clone` + `git apply` reproduces the working state.

## The four patches — the artifact names two

| File | Fix | Needed for |
|---|---|---|
| `engine/models/ellipsoid.py` | `np.mat(` → `np.asmatrix(`, 3 sites | everything |
| `constants/engine_constants.py` | `int(i) * 360` | everything |
| `guis/minimum/minimum_gui.py` | seed `self.cursor` | the GUI |
| `guis/minimum/minimum_gui.py` | `putText` via uint8 | the GUI |

**The middle two are why the artifact's pair is necessary and not sufficient.**
`engine_constants.py` dies *at import* under NumPy 2's NEP 50: `angular_range`
is `dtype=np.int8`, and `i * 360` no longer promotes, so it overflows int8 on
the first non-zero element. Nothing runs until it is fixed.

The fourth is what stops the **stock GUI** from starting under OpenCV 5:
`arm()` builds its label strips with `np.zeros(...)` (float64) and `cv2.putText`
now asserts `CV_8U`. Those buffers are composited as float downstream, so the
patch draws on a uint8 scratch and converts back rather than changing their
dtype — same values, same dtype, no change to the display stack.

The `np.mat` one is the blocker the artifact describes, and it verifies: fitting
a known ellipse (centre 300,200, semi-axes 60×35, 25°) recovers it to
**1.6e-11**. Its `fit(r)` takes an **(N,2)** array of points — the docstring
says `[[x…],[y…]]` and is wrong.

## Environment — cv2 on Python 3.14 is settled

`opencv-python 5.0.0.93` **installs and imports on 3.14.3**: it ships a
`cp37-abi3` wheel, so the version-specific-wheel problem does not apply. It and
`PyYAML` are now in `acqApp/.venv`, and **the suite is still 912/912 green**
with them present.

This retires the claim in PLAN §0 that no cv2 wheels exist for 3.14. `avi.py`'s
hand-rolled RIFF reader is no longer *forced* — it still works and is what the
probes below used to read the clips.

EyeLoop's own `requirements.txt` pins `numpy==1.19` and `pymba` (an Allied
Vision camera lib). **Do not install it.** Only PyYAML is needed, and only
because `eyeloop/__init__.py` eagerly imports `run_eyeloop` — the same eager
re-export trap PLAN §0 warns about in acqApp's own packages.

## What it measures on the rig clips

400×400 crop centred on the eye, `Shape(type=1)`, blur (3,3), ellipsoid.

| threshold | pAce (easy, median 49) | State (operator's, median 37) |
|---|---|---|
| 25 | 151/151, r 28.8 ± 3.2 | 151/151, r 37.3 ± 3.3 |
| 35 | 151/151, r 37.8 ± 1.2 | 151/151, r 51.4 ± 1.8 |
| 45 | 151/151, r 45.2 ± 1.9 | 151/151, r 59.6 ± 1.9 |
| 60 | 151/151, r 50.7 ± 2.3 | 151/151, r 59.7 ± 1.1 |

Centre sd 1.4–8.2 px (pAce), 2.1–11.2 px (State). **1.2–1.8 ms/frame** —
circular is ~25 % cheaper than ellipsoid (1.25 vs 1.57 ms) and on these clips
no less steady.

Compare the archived hand-rolled tracker: **151/151 at centre sd 0.87 px** on
pAce, circle mode. EyeLoop does not beat that on fit rate or steadiness.
**The win is that ellipse mode works on real footage** — acqApp's own ellipse
mode was broken, 8/151 (`archive/pupil_tracking/README.md`).

## The traps, all of which report success

### `params` is never reset on failure

`processor.py:157` — `fit()` catches every exception, logs at INFO and calls
`center_adj()`. It **never sets `fit_model.params = None`**, so a failed frame
silently returns the previous frame's fit. The artifact's "None on failure" is
wrong, and its "150/150" was measured the same way.

Count failures at the logger instead (`eyeloop.engine.processor`, messages
containing `fit`). **Control: 151 frames of uniform noise give 0/151 genuine
and 151 stale**, logging `fit index error`. That control is the only reason the
151/151 above can be believed — on the real clips, stale count is **0**.

### Fit rate never falls; only the answer goes wrong

Both of these report a clean 151/151:

- **Threshold sets the radius.** State clip, r 37.3 → 51.4 → 59.6 → 59.7 across
  thr 25→60: a **60 % swing** in reported pupil size from a tuning knob.
  The artifact's **threshold 35 is wrong for this rig** — the archived tracker
  measured 53.6 px on pAce, and only thr 60 (r 50.7) lands near it. Threshold
  must be a per-session control with a truth check behind it.
- **Seed tolerance is ~50 px**, about one pupil radius. Offset the seed and
  r degrades 59.6 → 41.5 (+100 px) → 3.6 (+150 px), silently.

This is the same silent-degradation shape the archived README documents for
`smooth_sigma` (53.6 → 70 px "with the frame count still high"). **Frame count
is not a quality metric for either tracker.**

## The crop is mandatory — and acqApp already has it

| framing | result | cost |
|---|---|---|
| full frame 1928×1208 | **0/151** | 73 ms/frame |
| crop 200×200 … 900×900 | 151/151, r 59.4–59.6 | 0.97 – 4.4 ms/frame |

Full frame does not merely cost more, it **fails outright**, and at over twice
the 33 ms display tick. Every crop from 200 to 900 px gives the same answer, so
the crop's job is to exclude, not to resolve.

**The eye region is exactly this crop.** It is operator-set geometry that the
live app already draws, persists and records, and which per
`archive/pupil_tracking/README.md` "no longer bounds a search — nothing
consumes it". Integrating EyeLoop gives it a consumer again.

The artifact's blink-framing argument reaches the same conclusion by another
route (62 % false positives wide, 0 % cropped). That path is moot headless —
the blink detector is bypassed — but the crop is required regardless.

## Auto-seeding does not work the artifact's way

"Darkest point of a heavily blurred frame" returns the **frame corner** on both
clips — these are wide-FOV shots with dark backgrounds and vignetting, so the
darkest large-scale region is not the eye. It gave (0, 825) and (0, 0). A seed
finder has to be scale-aware (a dark blob of pupil radius) or the eye region
has to supply the seed, which it can: its centre is within tolerance by
construction.

## The bench app — `eyeloopGUI/`

Built 2026-08-26. A sibling app, not part of acqApp:

```
acqApp\.venv\Scripts\python.exe eyeloopGUI\main.py
```

Open a clip, **click the pupil** — one click sets both the eye crop and the
seed — tune the threshold, then **Sweep whole clip** for fit rate, mean radius
and ms/frame. It does the one thing the stock EyeLoop GUI cannot: set the crop.

    tracker.py   the ONLY file touching EyeLoop. No Qt.
    source.py    FrameSource protocol + the eye Region. No Qt, no EyeLoop.
    window.py    Qt only.
    main.py      bootstrap into acqApp/.venv (the wheelApp pattern)

`tracker.py` is written to move into `devices/pupil_cam/` unchanged, and
`source.py`'s `FrameSource` is where `acquisition.py` slots in with no change
to `window.py`. It sits outside acqApp because of the GPL question, not because
it is throwaway.

**It reproduces the probe numbers exactly** — pAce 151/151 r 45.18 ± 1.86 at
thr 45, State 151/151 r 59.66 ± 1.13 at thr 60 — with two controls green: pure
noise returns None 20/20 (no stale fits leaking), and the uncropped frame
returns 0/20. Circular runs 0.63 ms/frame against ellipsoid's 1.73.

### One trap it cost an hour to find

`Shape.min_radius`/`max_radius` bound the ray walk and are clipped **into an
int array** (`clip_`). Passing floats makes `np.clip(..., out=…)` raise inside
`fit()`'s bare except — **every frame fails, silently, and nothing is logged
that looks like a type error**. The wrapper keeps them int (`walk_radius`) and
holds its own float acceptance band (`accept_radius`) separately. They were one
parameter until this happened.

## Corneal-reflection removal

**EyeLoop's own is dead code.** `Shape.artefact_` paints a filled circle over
the CR, but it is disabled in three places at once: `fit()`'s call to it is
commented out, `Shape.__init__` binds `artefact` to a no-op for *both* types,
and `artefact_` writes into `config.engine.pup_source` — which `engine.py`
never creates. Upstream also tracks the pupil *before* the CR (`engine.py:183`
then `:185`), so even a working version could not clean the frame the pupil fit
sees. It has never run.

So `eyeloopGUI/tracker.py` does it: `GlintRemoval` + `remove_glints()`, with
live controls in the bench app and a red mask overlay showing what is blanked.

### What is actually there

Frame 0, pupil interior vs the reflections:

| | pAce | State |
|---|---|---|
| pupil interior | median 23, p95 43 | median 22, p95 40 |
| reflections | saturated 235, ~1.3 % of pupil px | same |
| where they sit | area 61 at **0.54 r**, area 55 at 0.89 r | area 156 at **0.84 r** |

The gap between p95 ≈ 42 and 235 is why `bright ≥` is not delicate: anything in
100–180 selects the same pixels.

### Two things that cost the measurements, both now fixed in the code

**Mask the rim and the radius runs away.** Blanking bright pixels near the
boundary erases the boundary, the walk runs further out, the radius grows, the
search area grows with it. Measured at **+3.5 px on pAce** before it was
constrained.

**The search area must follow the fitted ellipse, not a circle.** A circle of
0.85 × mean-radius is already *outside* an 0.78-ratio pupil along the minor
axis — and the State clip's lash line is covered in bright specks at 0.80–0.91
r. Switching the test to the ellipse cut State's masked pixels at reach 0.85
from 253 to 76, and its radius drift from +1.65 px to +0.40.

Each blob is also filled from **its own surrounding annulus** rather than from
the pupil's interior level, so a reflection near the edge is not replaced with
pupil-dark.

### What it buys, measured

Sweeps of both clips, ellipsoid, `bright ≥ 120`, `pad 4`:

| | pAce thr 45 | State thr 60 |
|---|---|---|
| CR off | r 45.18 ± **1.86**, centre sd (2.83, 1.42) | r 59.66 ± **1.13**, sd (2.19, 2.56) |
| reach 0.70 | r 46.05 ± **1.42**, sd (2.70, 1.20) | r 59.71 ± 1.49, sd (2.64, 2.59), 12 px |
| reach 0.85 | r 47.04 ± **1.29**, sd (2.25, 1.00) | r 60.02 ± 2.33, sd (3.98, 2.57), 76 px |

**It is a real but clip-dependent gain, and it is not free.** On pAce it
tightens the radius (sd 1.86 → 1.42) and the centre, at +0.9 px of radius. On
State — the operator's clip — the reflection sits at 0.84 r, so nothing safe
reaches it: at 0.70 it masks 12 px and changes nothing, and at 0.85 it starts
costing steadiness. **Fit rate stays 151/151 throughout and tells you nothing**,
which is the standing lesson of this file.

### Pinned reflections

Large reflections are stationary, so the bench app lets the operator mark them:
pin mode, left-click to add (sized to the blob hit), right-click to remove.
Stored in *frame* coordinates so the eye region can move without dragging them
off target, and **exempt from both `reach` and `max_area`** — those guards
protect the automatic pass from bright things it cannot identify, and a pin is
an identification. Pinning is therefore the only way to reach a reflection the
automatic pass has to be too timid to touch.

It works, and it is not free: pinning the State clip's 0.84 r reflection
removes it properly (447 px masked against 27) and costs **+6.6 px of radius**,
because blanking something that overlaps the boundary erases the boundary.
**Pins earn their keep on reflections inside the pupil, not on the rim.**

`max_area` turned out inert — every blob is small once the frame-spanning
background component is excluded — so it is not exposed in the GUI.

**Defaults are `reach 0.70`, `bright ≥ 120`, `pad 4`, `fill ring 6`**, chosen to
not inflate rather than to maximise anything. Tuning is the operator's.

## Do we need OpenCV?

The dependency is real but shallow — and one part of it is a liability that has
to go regardless.

| layer | needs cv2? | what it calls |
|---|---|---|
| `Ellipse.fit` — the ellipse maths | **no** | pure numpy |
| `pupil_walkout` — the ray walk | **no** | pure numpy |
| `Shape.pupil_thresh` — every frame | **yes**, 3 ops | `erode`, `GaussianBlur`, `threshold` |
| `Shape.center_adj_` — on failure | **yes** | `HoughCircles`, **and a blocking `imshow` + `waitKey(0)`** |

### The failure path must be neutered whether or not cv2 stays

For `type=1` — the pupil, the one we use — `center_adj` is bound to
`center_adj_` (`processor.py:53`) and is called on **every** fit failure
(`:173`, `:177`). If `HoughCircles` finds any circle, it opens a modal debug
window named `kk` and calls **`cv2.waitKey(0)`, which blocks forever**, once
per circle found.

In an integrated acqApp that is a hang on exactly the frames where tracking
failed. It never fired in the probes because the good clips had **zero**
failures and the noise control returned `circles is None` — so this is a
landmine the measurements above could not have found. Patch it out or bind
`center_adj` to a no-op the way the CR branch already does (`:61`).

### Dropping cv2 would be a small job

`scipy.ndimage` is **already in `.venv`** (1.17.1) and gives `grey_erosion` and
`gaussian_filter`; the threshold is one numpy comparison. That would leave
EyeLoop's pupil path pure numpy — which matters mainly because it makes the
algorithm easy to port, though **GPL follows a port just as it follows a copy**.

### Recommendation: keep it, for now

opencv-python installs on 3.14, the suite is green with it, and **acqApp
imports cv2 nowhere else** — so it is additive, not a change to anything that
already works. The reason acqApp avoided it (no 3.14 wheels) is gone. Revisit
only if the 44 MB wheel or a numpy-only pupil path is wanted for its own sake.
**The `waitKey(0)` is not part of that trade** — it goes either way.

## Open decisions — the operator's

1. **Which tree.** PLAN §0: do not restore a tracker on master without being
   asked. Tracker work goes on **`pupil-tracking`**, where a *working*
   hand-rolled tracker already sits. EyeLoop replaces it, or sits beside it as
   a second `fit` mode.
2. **GPL-3.0.** Vendoring EyeLoop into `devices/` makes acqApp a derived work.
   The sibling-clone-plus-diff shape used here avoids that, which is why it was
   used. Decide before any of its code moves inside the repo.
3. **Where tracking runs.** 1–2 ms fits inside the 33 ms tick, but that was
   offline, single-threaded, no Qt. `track_worker.py` on the branch already
   solved this for the old tracker.
4. **Ellipse or circular**, given circular is cheaper and equally steady here.
   Ellipsoid earns its cost only if the ellipse itself is wanted — which was
   the point of trying EyeLoop.

## Running the stock GUI on a clip

Verified end-to-end on 2026-08-26 — it opens, tracks and exits clean:

```
cd c:\Users\User\Desktop\python\eyeloop
c:\Users\User\Desktop\python\acqApp\.venv\Scripts\python.exe -m eyeloop.run_eyeloop ^
    --video "E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi" ^
    --scale 0.4 --framerate 15 --save 0
```

`cd` into the clone: EyeLoop is **not pip-installed** into `.venv`, so it is
found only via the current directory.

- `--scale 0.4` — the clips are 1928×1208 and the stock GUI does not fit on
  screen at 1:1.
- `--save 0` — **the default is 1, which writes every frame as a JPG**: ~27 MB
  per run of a 151-frame clip, into `eyeloop/data/trial_<timestamp>/`.
- `--framerate 15` matches the clips. It rate-limits playback.

In the **CONFIGURATION** window: hover the pupil and press **1** to seed it,
**R/F** threshold, **T/G** blur, then **z** and **y** to start tracking, **q**
to quit. Pressing 1 before moving the mouse is what the cursor patch fixes.

**The blink detector is live in this path** and these are the wide-FOV clips,
so expect it to misfire — 62 % per the artifact, and `--scale` scales rather
than crops, so the CLI cannot give it the tight framing it wants. The stock GUI
is for *looking at* the tracker, not for measuring it. The headless path is
where the numbers above came from.

## Reproducing

Probes are in the session scratchpad, not the repo (they read `E:` and take a
few minutes). Each is standalone: `sys.path.insert` the clone and
`c:\Users\User\Desktop\python`, stub `config.arguments` / `config.engine`,
`Shape(type=1)`, `reset(seed)`, then `track(frame)` per frame. `config` is a
**process-wide global** — two tracked cameras in one process would collide.
