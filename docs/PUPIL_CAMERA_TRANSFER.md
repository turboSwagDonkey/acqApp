# Pupil Camera Module — Transfer Document

Handoff for a session focused on the **pupil-tracking camera** (Basler, via
`pypylon`) in `acqApp`. Everything you need to pick up the pupil-cam work in
isolation. Companion to [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md) (the
voltage/ORCA camera).

---

## 1. What the pupil camera module is

A secondary camera that images the mouse's eye to track **pupil diameter**. It
grabs **Mono8** (8-bit grayscale) frames from a **Basler** camera via `pypylon`,
runs a per-frame pupil detector, and reports/records the radius. Two contexts:

- **Standalone toy** — `pupil_cam/_toy.py`: single-window GUI (live image +
  detected-pupil circle + radius trace + LED + record). **Real Basler path works
  here** — this is where pupil-cam work should happen first.
- **Main app** — `main.py`: the pupil cam has its own dockable video box (frame +
  pupil outline) and a radius trace; records frames into the shared-clock HDF5.
  ⚠️ In the main app the *camera* is still **mock-only** (real Basler not wired
  in yet — see §6.3), though the real tracker is wired in.

Contrast with the ORCA voltage camera: pupil frames are small **uint8**, the
device is Basler/pypylon (not DCAM), and the interesting output is the **tracked
radius**, not the raw image.

---

## 2. How to run

Use the **project venv's Python** (`acqApp/.venv/Scripts/python.exe`). Real
display, not headless.

```powershell
# from C:\Users\User\Desktop\python
acqApp\.venv\Scripts\python.exe acqApp\pupil_cam\_toy.py           # real Basler
acqApp\.venv\Scripts\python.exe acqApp\pupil_cam\_toy.py --mock    # synthetic pupil
acqApp\.venv\Scripts\python.exe acqApp\pupil_cam\_test_tracking.py # tracker vs ground truth
```

The toy falls back to the mock automatically if the camera can't be opened, and
prints why. In the toy: **click the image** to place the annulus by hand,
**Re-seed** to drop the lock and re-detect from scratch. Green edge points were
used in the fit, red ones were rejected as outliers, and the dashed rings are
the annulus band the search lines sweep — that overlay is the tuning instrument.

Real hardware needs the **Basler pylon runtime + pypylon** (for the camera, on a
**USB 3.0 port** — see §6.1) and **NI-DAQmx** (for the eye-tracking LED).
`--mock` needs neither. `_test_tracking.py` needs no hardware and no Qt. Headless
smoke tests use `QT_QPA_PLATFORM=offscreen`,
`PYTHONPATH=C:/Users/User/Desktop/python`.

---

## 3. Relevant files

| File | Role |
|------|------|
| [pupil_cam/acquisition.py](../pupil_cam/acquisition.py) | `open_camera()` + `PupilCameraWorker` (real Basler) + `MockPupilCameraWorker`. The capture thread. |
| [pupil_cam/tracking.py](../pupil_cam/tracking.py) | `detect(...)`, `PupilTracker`, `find_circular_edge(...)`. **The pupil algorithm — an IMAQ Find Circular Edge port.** |
| [pupil_cam/_test_tracking.py](../pupil_cam/_test_tracking.py) | Ground-truth validation of the tracker on synthetic eyes. No hardware. |
| [pupil_cam/control.py](../pupil_cam/control.py) | `LedController` / `MockLedController` — eye-tracking LED on NI `Dev3/port0/line1`. |
| [pupil_cam/settings.py](../pupil_cam/settings.py) | `PupilSettings` + `SettingsPanel` — exposure/fps, tracking params (threshold, min/max radius), LED toggle. |
| [pupil_cam/_toy.py](../pupil_cam/_toy.py) | Standalone bring-up GUI. Opens the Basler, drives the worker, overlays the pupil circle, records. |
| [pupil_cam/recording.py](../pupil_cam/recording.py) | `FrameWriter` (frames → HDF5) + `TrackingLog` (per-frame detection → CSV). **Toy only.** |
| [acq/worker.py](../acq/worker.py) | `PullWorker` base: `get_latest`/`set_sink`/`stop` scaffolding both workers share. |
| [acq/recorder.py](../acq/recorder.py), [acq/ring_buffer.py](../acq/ring_buffer.py), [acq/writer.py](../acq/writer.py), [acq/clock.py](../acq/clock.py) | Main-app shared-clock recording pipeline. |
| [main.py](../main.py) | Full app: pupil wiring in `_start_session` (mock worker), `_start_recording` (frame sink), `_pull_frames` (detect + overlay + radius), `_on_pupil_exposure`, `_on_pupil_led`. |

---

## 4. Architecture

### Capture worker (`PupilCameraWorker` / `MockPupilCameraWorker`)
- Both subclass `PullWorker` (a `QThread`). `run()` grabs frames and calls
  `self._publish(frame)` (updates the newest-frame snapshot **and** feeds the
  recording sink). GUI pulls with `get_latest()`.
- `open_camera(index=0)` does the enumerate + open and **never raises** — it
  returns `None` and prints why, so the caller falls back to the mock. Call it
  before importing Qt (see the pre-init block in `_toy.py`).
- **Real worker normally takes an already-open camera**: `PupilCameraWorker(cam,
  exposure_us, fps)`, and then never closes it — the owner does. Pass
  `cam=None` and it opens/closes its own, mirroring the ORCA's `own_cam` path.
- `_configure()` forces **Mono8** (the camera can persist Mono12 from an earlier
  session, which would hand us uint16 and silently break both the tracker's
  threshold units and the `(0, 255)` display levels), turns off
  `ExposureAuto`/`GainAuto`/`TriggerMode`, and clamps exposure/fps to each
  node's own `GetMin()`/`GetMax()`. Node names are looked up across SFNC
  variants (`ExposureTime` / `ExposureTimeAbs`), and a missing node is a printed
  warning, not a crash. It logs `ResultingFrameRate` so a bandwidth- or
  exposure-limited rate is visible rather than mysterious.
- Grabs with `TimeoutHandling_Return` on a 500 ms timeout, so a missed frame
  spins the loop and `stop()` stays responsive instead of raising out of it.
- The first frame's `shape`/`dtype`/range is printed, with a warning if it isn't
  uint8 — that is the §6.1 orientation/format check, automated.
- `set_exposure(us)` queues the change and applies it on the next grab tick
  (the camera is opened on the GUI thread and grabbed on the worker thread).
- **Mock** honors `fps` (constructor arg) and renders a grey frame with a dark
  disc whose radius oscillates (0.1 Hz) — 240×320 uint8 — plus a bright
  specular dot standing in for the IR corneal glint, since that is the artefact
  the tracker's outlier rejection exists to handle.

### Pupil detection (`tracking.py`) — a port of **IMAQ Find Circular Edge**

Mirrors the rig's LabVIEW pipeline: an annular ROI around the estimated pupil
centre, a fan of search lines through it, an intensity edge per line, and a
least-squares circle through the resulting points.

1. **Sample the annulus** — `n_rays` radial lines from `r_inner` to `r_outer`,
   bilinearly sampled at ~0.5 px steps (the unwrap `cv2.linearPolar` would give,
   restricted to the band the way IMAQ actually searches).
2. **Edge per ray** — Gaussian-smooth along the radius, take the strongest
   gradient, refine to sub-pixel with a parabola on the gradient peak.
   `polarity` is the IMAQ edge-polarity control: `rising` (dark pupil → bright
   iris, the usual IR setup), `falling` (bright pupil), `any`.
3. **Robust circle fit** — RANSAC consensus over 3-point circumcircles, then
   Taubin least squares with MAD outlier rejection. Taubin rather than Kåsa
   because eyelids leave a partial arc, which biases Kåsa's radius badly.
4. **Optional ellipse** — Fitzgibbon direct least squares (Halir–Flusser) on the
   surviving inliers, for an off-axis eye. `radius` is then the mean semi-axis.

Steps 1–3 repeat `refine_iters` times, each pass re-centring the annulus on the
previous fit, so a coarse seed converges.

**Pure numpy + scipy — cv2 is not used at all** (it has no Python 3.14 wheels,
and nothing here needed it).

Three entry points:

| | |
|---|---|
| `detect(frame, threshold, min_r, max_r, ...)` | Stateless, per-frame. Same signature as the old stub. Takes an optional `seed=(cx, cy, r)`. |
| `PupilTracker` | **Stateful — prefer this for live video.** Seeds each annulus from the previous fit; falls back to `coarse_seed` after `max_lost` failures. `.seed(cx, cy, r)` places the annulus by hand. |
| `find_circular_edge(...)` | The IMAQ primitive itself, if you want to drive the annulus directly. |

`PupilResult` gained `axes`, `angle`, `edge_x`/`edge_y`, `inliers`, `rms` and
`n_rays` — all defaulted, so the old 4-field construction still works. The toy
draws the edge points and the annulus band, which is what you tune against.

**Seeding is the weak point, not the fit.** The auto-seed takes the largest
thresholded blob, fills its holes (the IR glint punches one), and uses the
inscribed-circle centre rather than a centroid. That still loses when a dark
eyelid margin merges with the pupil into one larger blob — it then reports *no
detection* rather than a wrong radius. Fix it by seeding manually (click the
image in the toy) or by excluding the lid sectors with `exclude_deg`.

### Eye-tracking LED (`LedController`)
- NI DAQ digital out on `Dev3/port0/line1` — deliberately distinct from the
  puffer's `line0` (two tasks can't own the same physical line). `on/off/set/
  close`. `MockLedController` tracks state only.

### Settings (`PupilSettings` / `SettingsPanel`)
- Fields: `exposure_us`, `fps`, `threshold`, `min_r`, `max_r`, plus the annulus
  controls `n_rays`, `polarity`, `min_strength`, `fit`.
- Signals: `exposure_changed(float)` (hot-applied), `led_toggled(bool)`.
- `settings_changed(PupilSettings)` — every parameter edit. `track_params(s)` in
  `track_worker.py` picks out the tracker's share of it; the app queues that onto
  the tracking thread, which applies it between frames via
  `PupilTracker.configure(**...)` (that re-seeds only when a change invalidates
  the current lock: `threshold`, `min_r`, `max_r`, `polarity`, `fit`).
- `track_params` → `(threshold, min_r, max_r)` and `track_kwargs` → dict of the
  annulus options are the per-tick reads `_toy.py` still uses, where tracking
  runs on the GUI timer. **The app does not**: see `pupil_cam/track_worker.py`.

### Recording (two separate paths)
- **Toy**: `FrameWriter` streams frames to `toy_output/pupil_frames.h5`;
  `TrackingLog` appends `frame,center_x,center_y,radius,confidence` to
  `toy_output/pupil_tracking.csv`.
- **Main app**: worker sink → shared `Recorder` → `/pupil_cam` frames on the
  session clock. **Note:** the main app records only the raw **frames** — the
  tracked radius is computed in `_pull_frames` for *display* and is **not** a
  recorded stream. If you want recorded radius/center, add a tracking stream
  (see §6).

---

## 5. Current state

**Verified (mock, headless):** worker → `PullWorker` refactor, mock honors `fps`,
settings tab (exposure/fps/threshold/min-r/max-r + rays/polarity/strength/fit +
LED), tracking params flow into the tracker, LED toggle drives the (mock)
controller, frames record to `/pupil_cam` on the shared clock, dockable video box
+ radius trace in the main app. The toy was driven headlessly through start →
tick → click-to-seed → record → close in both `circle` and `ellipse` modes.

**Tracker verified against ground truth** — `_test_tracking.py`, 15 checks, all
sub-pixel:

| case | centre err | radius err |
|---|---|---|
| clean disc, r=12..72 | 0.01 px | 0.22 px |
| random pose | 0.15 px | 0.06 px |
| eyelid 20/40/60 % occlusion, seeded | 0.25 px | 0.21 px |
| noise σ=5..20 | 0.16 px | 0.06 px |
| ellipse, off-axis | 0.18 px | 0.13 px |
| tracker over dilation + blink | 0.19 px | 0.12 px |

Blink recovery is 1 frame. A blank frame yields no detection. Cost is **6.7 ms
per frame at 1920×1200** for `PupilTracker` once locked (~150 fps ceiling,
comfortably above the camera's 41.6); the stateless `detect()` is ~33 ms because
it re-runs the coarse seed's connected-component pass every frame — another
reason to prefer the tracker live. A frame with no findable pupil (lens cap,
illumination off, blink) costs **1.7 ms**, not the ~190 ms it did before the
seed grew its early-out and distance-transform decimation.

**Verified on the real Basler:** SuperSpeed link, 8 s sustained grab at 41.57 fps
/ 95.8 MB/s with zero failed grabs or timeouts, `PupilCameraWorker` holding
19.95 fps against a requested 20, frames arriving as `(1200, 1920) uint8`, and
the NI DAQ LED line opening. See §6.1 for the numbers and the `ExposureAuto`
trap. **The tracker has not yet run on a real eye** — the camera isn't mounted
or focused, so test frames are featureless.

**cv2 is not installed and is no longer needed** — the tracker is pure
numpy + scipy.

---

## 6. Open questions / next steps for the pupil cam

1. ~~Bring up the real Basler~~ — **done, connections verified.** The camera
   was initially on the USB 2.0 side of a VIA Labs VL813 hub and `Open()` was
   hard-refused ("The device cannot be operated on an USB 2.0 port"); re-plugging
   fixed it. It now enumerates through two chained **SuperSpeed** hubs to the
   Intel USB 3.20 xHCI controller. Verified end to end:

   | check | result |
   |---|---|
   | `BslUSBSpeedMode` | `SuperSpeed`, `DeviceLinkSpeed` 500 MB/s |
   | sustained grab, 8 s | 41.57 fps, 95.8 MB/s, **0 failed, 0 timeouts** |
   | frame format | `shape=(1200, 1920) dtype=uint8`, not transposed ✓ |
   | `PupilCameraWorker` | 19.95 fps against a requested 20 |
   | NI DAQ LED, `Dev3/port0/line1` | opens, left off ✓ |

   41.58 fps is the hardware ceiling at full frame — `SensorReadoutTime` is
   24.05 ms, and the measured per-frame period matches it exactly. It is
   **sensor-readout limited, not USB limited**: a 640×480 ROI reaches 98 fps
   / 227 MB/s on the same link.

   ⚠️ **`ExposureAuto` ships as `Continuous`, and that will silently cost you
   the frame rate.** On a dark scene it ramps exposure to 500 ms, which caps
   acquisition at **2 fps** while `AcquisitionFrameRate` still reads 1000000 and
   nothing looks wrong. `_configure()` now forces `ExposureAuto=Off`, so the app
   path is immune — but if you drive the camera from your own script or the
   pylon Viewer, set it yourself. This is the single most confusing failure mode
   on this camera.

   Still to do on hardware: the camera **is not mounted or focused yet** — test
   frames are featureless, so the tracker has never run on a real eye. Tuning
   (§6.2) is blocked on that, not on code.
2. ~~Replace the tracking stub~~ — **done**, see §4. Remaining tuning is against
   *real eye frames*: `threshold` (seed only), `min_r`/`max_r`, and
   `min_strength`. Watch the toy's red (rejected) edge points — a ring of them
   on one side means the annulus is off-centre or an eyelid needs `exclude_deg`.
3. **Wire the real pupil cam into the main app.** Today `main.py` still builds
   `MockPupilCameraWorker` and `MockLedController`. `open_camera()` +
   `PupilCameraWorker(cam, exposure, fps)` + the real `LedController` is the
   whole change, mirroring the voltage-cam pattern — but do §6.1 first so it can
   actually be tested. The tracker side is already wired (`PupilTracker` in
   `_pull_frames`, panel kwargs included).
4. **Record the tracked radius**, not just frames. In the main app, add a scalar
   stream (e.g. `/pupil_radius`) alongside `/pupil_cam`, or record center+radius,
   so analysis doesn't have to re-run detection. The toy already logs this to CSV
   via `TrackingLog` — decide on the canonical format. Worth recording
   `confidence` too, so NaN gaps can be told from low-confidence frames.
5. ~~Close the Basler on exit~~ — **done**; the toy's `closeEvent` now closes
   `_cam` as well as the LED.
6. **Cross-thread camera** — the Basler is opened on the main thread (pre-init)
   and grabbed on the worker `QThread` (same pattern as the ORCA). `set_exposure`
   now queues the change and applies it on the grab thread rather than writing
   the node cross-thread. If pypylon still objects, pass `cam=None` so the
   worker opens its own handle inside `run()`.
7. ~~Display cost~~ — **done**; the toy re-scans levels every 30th tick instead
   of every frame, and no longer copies to float32.
8. **Auto-seeding on real frames.** The seed is the one part that synthetic data
   can't validate honestly (see §4). If the mouse's lid margin is dark enough to
   merge with the pupil, seed by clicking. If that turns out to be the common
   case, consider a persistent operator-drawn ROI instead of a per-frame seed.

---

## 7. Handy facts

- Frames are **Mono8 (uint8)**, `(H, W)`. Mock is **240×320**. The rig camera is
  a **Basler acA1920-40umMED** → 1920×1200 at up to ~40 fps, **USB3 Vision**
  (not GigE, despite what the old docstring said).
- Device stack: **Basler + pypylon** (camera, **USB 3.0 port required**),
  **NI-DAQmx** on `Dev3/port0/line1` (LED). Neither needed with `--mock`.
- Angle convention in `tracking.py`: 0 rad = +x (image right), increasing toward
  +y (image **down**, row-major). So 90° is the bottom of the image and 270° the
  top — that is what `exclude_deg` takes, e.g. eyelids at `[(60, 120), (240, 300)]`.
- `PupilResult.radius is None` means "no confident detection" → recorded/plotted
  as NaN.
- The worker never owns the camera handle — the caller opens/closes it and passes
  it in (`PupilCameraWorker(cam, …)`).
- Mock path needs no hardware and matches the real worker's API — good for logic
  and tracking-algorithm work (feed it synthetic or recorded frames).
