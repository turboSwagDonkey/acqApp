# Archived: the pupil tracker

Removed from the live app on **2026-08-24** at the operator's request, after the
2026-08-22 measurements showed the complaint ("jitter and dropouts") was two
mis-set knobs rather than a bad algorithm — see PLAN.md §7 (ah) and (ai).

**Nothing here is imported by the app.** It is kept because the algorithm works
when configured correctly (151/151 frames on the rig's pAce clip) and because
the measurements behind it were expensive.

## What is here

| file | what it is |
|---|---|
| `tracking.py` | `PupilTracker`, `find_circular_edge`, `coarse_seed`, `lid_sectors` — the IMAQ *Find Circular Edge* port |
| `rays.py` | ray casting and per-ray sub-pixel edge detection |
| `fits.py` | robust circle and ellipse fits |
| `track_worker.py` | `PupilTrackWorker` — ran the tracker off the GUI thread |
| `_test_tracking.py` | 15 synthetic ground-truth checks |
| `_mark_truth.py` | hand-mark the pupil edge, then score the tracker against it |
| `tests/` | `test_pupil_fits.py`, `test_pupil_tracking_thread.py` |

## What was kept in the live app

The **eye region** (`limit_x/limit_y/limit_r`), because it is operator-set
geometry drawn on the preview. It no longer bounds a search — nothing consumes
it — but it persists, draws on the preview and is recorded in the session
metadata.

## Restoring it

1. `git mv` the modules back to `devices/pupil_cam/` and the tests to `tests/`.
2. Put these fields back on `PupilSettings` (they were removed with the panel
   controls that set them):

   ```
   threshold 60,  min_r 10,  max_r 80,  n_rays 64,  polarity "rising",
   min_strength 4.0,  fit "circle",  edge_select "first",  smooth_sigma 1.5,
   min_confidence 0.10,  smooth_median 3,  smooth_ema 0.5,  reseed_after 30,
   exclude_deg (),  show_search False
   ```

3. Re-add `PupilSettings.risky()`, which warned that `edge_select="strongest"`
   takes 151/151 frames to 44/151. **That warning is why it is written down
   here**: the value has a tooltip describing the failure and it still cost a
   session.
4. Re-register `("pupil-fits", …)` and `("pupil-thread", …)` in
   `tests/run_all.py`.

## The numbers worth not re-deriving

- Six rig clips on `E:`, all 1928×1208 IYUV / 151 frames / 15 fps. They are
  **not** equivalent — `pAce/…/FOV1_T1` is easy (frame median 49),
  `State/VF182.6B/…/FOV1_T1` is the hard one (median 37, 61 % below grey 60).
- `edge_select="strongest"` → 44/151 frames, centre sd 28.7 px. `"first"` →
  151/151, centre sd 0.87 px.
- `smooth_sigma` has **no single safe value**: best is 0.5 on the State clip,
  1.5–3.0 on pAce, and from 4.0 the fitted radius inflates 53.6 → 70 px
  silently, with the frame count still high.
- `reseed_after` is an amplifier, not a cause: at 100 one bad frame costs 100
  blind ones.
- A hand-placed seed is worth ~5 frames once `edge_select` is right, and nothing
  while it is wrong (a perfect seed still gives 18/151).
- Ellipse mode is broken on real footage (8/151) — `_BAND_ELLIPSE` sweeps
  r×0.35–2.9, far out into fur. It passes the synthetic suite, which is the
  blind spot that hid it.
