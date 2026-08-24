# Pupil tracking — the working branch

**You are on branch `pupil-tracking`. This branch exists so you can change the
pupil tracker without touching anyone else's work.** `master` does not have the
tracker in it: it was retired on 2026-08-24 into `archive/pupil_tracking/`,
because the operator chose to stop tuning it. Here it is restored, live, and
running in the app.

Nothing you do on this branch reaches `master` until someone opens a pull
request and merges it. Push here freely.

```
git clone https://github.com/turboSwagDonkey/acqApp.git
cd acqApp
git checkout pupil-tracking
```

## Getting it running in ten minutes, with no rig

You do not need the microscope, the DMD, the stage or the DAQ. Everything below
runs on a laptop.

**1. The interpreter.** Python **3.14**. Installs go into this repo's own venv
and nowhere else — that rule is in `CLAUDE.md` and it is not negotiable, because
the rig machine's other interpreters run other experiments.

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

There is **no OpenCV** here and there will not be: no cv2 wheels exist for 3.14.
That is why `avi.py` reads RIFF by hand and every fit in `fits.py` is numpy.
Do not add a dependency to work around it without saying so.

**2. Run the suite. It must be green before you change anything.**

```
.venv\Scripts\python.exe tests\run_all.py
```

Use the **absolute** path to that interpreter if your shell is anywhere but the
repo root — a relative `.venv\Scripts\python.exe` from the parent folder
resolves to nothing and Python reports a baffling "the module '.venv' could not
be loaded".

Tests are **plain scripts, not pytest**, and each runs in its own process. To
run one: `tests\run_all.py fits`.

**3. Start the app in Emulate mode** — synthetic devices, no hardware:

```
.venv\Scripts\python.exe main.py --mock
```

Pick **Pupil camera** in the startup dialog. You get a synthesised eye, a
preview, and the tracking controls.

## The three ways to exercise the tracker

| | what it proves | cost |
|---|---|---|
| `devices/pupil_cam/_test_tracking.py` | the algorithm against **synthetic** ground truth (15 checks) | seconds |
| the app in `--mock` | the whole GUI path — panel, worker, overlay, recording | seconds |
| **replaying real footage** | the only thing that has ever found a real bug | needs a clip |

**Use all three, and treat the first as necessary but not sufficient.** This is
the single most expensive lesson on this file: `_test_tracking.py` passed 15/15
while the tracker scored **0/151** on real footage. Synthetic eyes put the
highest-contrast edge at the pupil boundary; real IR footage inverts that — the
orbit-to-fur margin is a ~200 grey-level step against the pupil's ~30. A green
synthetic suite says nothing about a real eye.

### Replaying footage

`devices/pupil_cam/video.py` is a third frame source: it replays a recorded clip
as if it were the camera, so the whole pipeline runs against real data. Set the
clip path in the Pupil camera tab.

The reader takes **uncompressed** AVI only — IYUV/I420/YV12, Y800 and BI_RGB —
and refuses anything compressed **by name** rather than failing obscurely. The
rig's own clips are uncompressed IYUV, where the Y plane *is* the grayscale
frame, so no decoder is needed.

**Where the clips are:** they are not in this repo. They are ~500 MB each, they
are experiment data, and `CLAUDE.md` forbids committing recordings. Ask the
operator for a copy, or point `video_path` at your own footage. See "Test
footage" at the bottom.

## What is already known — do not re-derive this

All of it was measured, most of it expensively. `docs/SESSIONLOG.md` has the
sessions in full.

**There are six rig clips and they are NOT equivalent.** Four sessions measured
the first one and generalised from it, which is how the tracker got a reputation
it half deserved.

| clip | frame median | character |
|---|---|---|
| `pAce/VF203.2R/…/FOV1_T1` | 49 | the easy one. Every number before 2026-08-22 came from here |
| `State/VF182.6B/…/FOV1_T1` | 37 | **the operator's own.** 61 % of it is below threshold 60; the eye is a low-contrast almond and the pupil barely separates from the iris |

**Measure any change on both.** A change that helps `pAce` and breaks `State` is
a change that breaks the rig.

**No clip contains a blink.** Every lost frame in the montages is a wide-open,
clearly visible eye — so the dropouts were never the animal, and the
simulated-occlusion work has no real footage behind it.

### The knobs, and which ones are dangerous

- **`edge_select`** is the one that matters. `"strongest"` locks onto the eyelid
  margin, which out-contrasts the pupil on real IR footage: 151/151 → **44/151**,
  centre sd 0.87 → 28.7 px. `"first"` is right. This value has a tooltip saying
  exactly that and it *still* cost a session, which is why `PupilSettings.risky()`
  exists and why the panel shows a warning outside the collapsible section.
- **`smooth_sigma` has no single safe value.** Best is 0.5 on the State clip,
  1.5–3.0 on pAce. **From 4.0 the fitted radius inflates 53.6 → 70 px silently,
  with the frame count still high** — the dangerous mode, because it looks like
  it is working.
- **`reseed_after` is an amplifier, not a cause.** At 100, one bad frame costs
  100 blind ones. It is non-monotonic against simulated occlusions, so do not
  tune it on synthetic gaps.
- **A hand-placed seed is worth ~5 frames** once `edge_select` is right, and
  nothing at all while it is wrong (a *perfect* seed still gives 18/151 with
  `"strongest"` set). Seed sensitivity is one-sided: ±20 px horizontally is
  fine, +20 px vertically gives 46/151 and −20 px gives 3/151, because the lids
  are above and below.
- **Ellipse mode is broken on real footage** (8/151). `_BAND_ELLIPSE` sweeps
  r×0.35–2.9, far out into fur. It passes the synthetic suite — the same blind
  spot as above.

### Two things that are true and counter-intuitive

- **A lighter algorithm loses.** Blob+moments and blob→boundary→robust-circle
  were both built and measured: four times the jitter, four times the centre
  wander, and no faster. The table is in `docs/SESSIONLOG.md` under
  2026-08-21 (ag).
- **Otsu inside the ROI is unusable** — it picks ~135, because the ROI's bright
  fur dominates the histogram, so the split it finds is fur-vs-rest rather than
  pupil-vs-iris. r = 94 px against a true ~53.

### The known-real defect nobody has fixed

**The fitted circle sits down-left of the pupil**, its lower-left arc running
through fur. Excluding 115° of the ring for the lids leaves a *partial arc*, and
a circle fitted to one trades centre against radius. The lids bought stability
and cost accuracy. It was not the operator's complaint so it was never fixed —
but it is real, and it is the thing to watch if the lid exclusion is widened.

## Measuring a change honestly

**Stability can be measured without ground truth; correctness cannot.** Two
attempts at an automatic accuracy metric both failed — scoring the boundary by
the first *bright* crossing fires on the corneal glint (r ≈ 10), and by the last
*dark* sample runs out into dark fur (r ≈ 80).

So there is a hand-marking tool:

```
.venv\Scripts\python.exe devices\pupil_cam\_mark_truth.py mark  <clip>
.venv\Scripts\python.exe devices\pupil_cam\_mark_truth.py score <clip>
```

`mark` clicks the pupil edge on a spread of frames, fitting a robust circle
live. `score` runs the tracker over the **whole** clip and reports centre and
radius error at each marked frame — the whole clip on purpose, so the tracker is
in the state it would really be in. A fresh tracker started at frame 120
measures something the operator never sees.

## House rules that apply to this branch too

- **Installs go only into `.venv`.**
- **Never commit experiment data** — `sessions/`, `*.h5`, `*.csv`, `*_local.json`
  are gitignored. Keep it that way.
- **Read `acqapp_local.json` before debugging any "it doesn't work".** It is
  what the app loads at launch and it is gitignored, so it never shows in a diff.
  It once held the entire answer to four sessions of pupil work: the saved
  `video_path` pointed at a different clip from the one every measurement used.
  Your settings are not the shipped defaults.
- **An exception escaping a `QThread.run()` aborts the process** (PyQt6
  `qFatal`) — no traceback, no output. Worker bodies stay inside the
  `PullWorker.run()` guard. `track_worker.py` is a QThread; respect this.
- **Every runnable entry point calls `console.enable_safe_console()`** before its
  first print. `tests/test_console_safety.py` enforces it. An unencodable
  character in a diagnostic print inside an acquisition loop reads as a device
  failure.
- **If you move, rename or add a module, update `docs/STRUCTURE.md` in the same
  commit.** `tests/test_structure.py` checks the doc against the filesystem *and*
  the diagram's arrows against the AST, so a stale map fails the suite.
- **When adding a test, include a control** — a check that would fail if the
  thing under test were broken. `tests/README.md` has the two conventions.

## Test footage

Real footage is what finds real bugs, and it is not in this repo — the clips are
~500 MB each and they are recordings. Ask the operator for:

- one **easy** clip (`pAce/…/FOV1_T1`) and one **hard** one (`State/…/FOV1_T1`),
  so a change can be measured on both;
- or a trimmed subset if bandwidth is a problem — the tracker is temporal
  (`reseed_after`, the smoothers), so a *sequence* of frames is worth far more
  than a single still.

Point the Pupil camera tab's clip path at whatever you get.

## Sending changes back

Commit on this branch and push. When something is worth merging, open a pull
request against `master` and say in it **which clips you measured on and what
the numbers were** — "151/151 on both, centre sd 0.31 px" is the currency here.
A change justified only by the synthetic suite will be asked for real numbers,
for the reason at the top of this file.
