# Session Handoff — camera crash, wheel speed/distance, camera link label

Continuation notes for `acqApp`. Covers three threads of work done this session:
a hard camera crash, the wheel encoder outputting speed/distance, and the camera
frame-rate label. Companion to [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md),
[WHEEL_TRANSFER.md](WHEEL_TRANSFER.md),
[PUPIL_CAMERA_TRANSFER.md](PUPIL_CAMERA_TRANSFER.md),
[STAGE_TRANSFER.md](STAGE_TRANSFER.md).

All work was verified in **mock/headless** unless noted. Real-hardware paths are
flagged. Env rule still in force: installs go ONLY into `acqApp/.venv`.

---

## 1. Camera crash on Start — FIXED

**Symptom:** real ORCA-Fire opened fine (printed device info), then the app died
"very quickly" with **no traceback**.

**Two root causes, both fixed:**

1. **Python exception escaping a `QThread.run()` → PyQt6 `qFatal()`/abort.**
   Any unwrapped DCAM call in the worker took the whole process down.
   Fix: [acq/worker.py](../acq/worker.py) — `PullWorker.run()` is now a guard
   that calls `self._run()` in try/except, prints the traceback, and emits a new
   `error = pyqtSignal(str)`. **Every worker's loop was renamed `run` → `_run`**
   (camera, encoder, pupil, stage). Don't override `run()` in subclasses again.
   [main.py](../main.py) `_start_session` connects each worker's `error` to
   `_on_worker_error` (status bar).

2. **Native DCAM crash from a double-open** (no Python traceback → not catchable).
   Startup probed the camera by open→read-info→**close**, then the worker
   **re-opened** it; re-opening a just-closed DCAM device segfaults.
   Fix: [main.py](../main.py) pre-init now opens the camera **once** and keeps
   the handle (`_cam_handle`); the worker **reuses** it via
   `OrcaFireWorker(0, cfg, cam=self._cam_handle)` (never re-opens). Handle closed
   in `closeEvent`. Also removes the ~7 s re-open per Start.

3. **`faulthandler.enable()`** added at the very top of [main.py](../main.py)
   so any *future* native crash dumps a C+Python stack instead of vanishing.

**Status:** user confirmed **"worked ok"** on real hardware.

---

## 2. Wheel encoder → speed + distance — DONE (needs on-rig calibration)

**Goal:** the `Dev3/ai2` voltage is a **single-turn POSITION** signal; user wants
live **speed** and **distance**, and all three recorded.

**Signal confirmed by capture** (see tools below): a clean **0–5 V sawtooth**,
~4.9 V per revolution, no glitches at 1 kHz. Sensor + acquisition are fine; the
conversion was the problem.

**What was built:**
- Derivation moved **into the worker** so it runs per-sample at the full 50 Hz
  (not the decimated GUI): [wheel/acquisition.py](../wheel/acquisition.py)
  `_EncoderBase._derive()` — sawtooth unwrap → speed = d(position)/dt, distance =
  running total. Shared by `EncoderWorker` (real) and `MockEncoderWorker`.
  - `get_latest()` now returns **`(voltage, speed, distance, elapsed)`** (4-tuple;
    was 2-tuple — consumers updated).
  - Robustness: **glitch guard** drops jumps > 0.5 rev/sample (wrong V/rev or
    glitch → the "random spikes"); **velocity-floor deadband** `_DEADBAND_REV_S =
    0.05` rev/s stops noise creep while stationary (a per-sample threshold is
    rate-dependent — do NOT go back to that).
  - `set_scaling(volts_per_rev, wheel_dia_mm)` lets V/rev + diameter change live;
    [main.py](../main.py) `_on_wheel_settings` pushes edits to a running worker.
- **Recording writes three streams**: [main.py](../main.py) `_record_wheel` →
  `wheel_voltage`, `wheel_speed`, `wheel_distance` (raw voltage is lossless ground
  truth; speed/distance derived). Metadata adds `wheel_volts_per_rev`,
  `wheel_dia_mm`, `wheel_speed_units`, `wheel_distance_units`.
- **UI:** Signals plot relabels to speed (mm/s or rev/s); wheel settings tab shows
  live `speed … / distance …` readout ([wheel/settings.py](../wheel/settings.py)
  `set_readout`; [main.py](../main.py) `_wheel_show`).
- Units: **mm/s + mm** when wheel diameter set, else **rev/s + rev**.
- Toy updated for the 4-tuple: [wheel/_toy.py](../wheel/_toy.py).

### Update 2026-08-06 — derivation reworked from a real capture (analysis-only, not yet on rig)

Ran `analyze_raw.py` on the first real rig capture (`wheel_raw.csv`, a hand-spin).
It exposed three bugs in the old per-sample derivation and drove a rewrite. **All
findings/validation are offline against that one capture — untested on the rig.**

**What the capture proved:** the wheel oscillated *within a single turn* (no wraps;
position 0.02–1.01 rev; ended where it started → true net ≈ 0, real path ≈ 11 rev).
Against that ground truth the OLD code (50 Hz) gave: net **+3.9 rev** (should be ~0),
distance **17.5 rev** (should be ~11), stationary speed **±0.46 rev/s** jitter.

**Root causes:** (1) it differentiated the *raw* voltage, so per-sample ADC noise
(±0.045 V ≈ 0.009 rev over a 20 ms step = 0.46 rev/s) read as real speed — the
velocity deadband can't tell that from motion. (2) distance rectified that noise
(`+= |Δ|`) so it accumulated. (3) at 50 Hz, fast hand-flicks (>0.5 rev/sample)
aliased through the wrap-correction, flipping a real −0.54 rev into +0.46 rev
(phantom net). A mouse (~1–2 rev/s) never hits that, but it's latent.

**Decisions (user, this session):**
- **Distance = net_forward** (signed; back-spin subtracts). NOT path. Metadata key
  `wheel_distance_mode: "net_forward"`.
- **Sample rate → 120 Hz** (user asked; "120 khz" read as 120 Hz — a per-sample
  software-timed `task.read()` loop can't do kHz, and 120 Hz is ~60× a mouse's
  speed so aliasing is impossible). If they ever really mean kHz that's a
  hardware-buffered rearchitecture — flag it.

**Fix in [wheel/acquisition.py](../wheel/acquisition.py) `_EncoderBase._derive`:**
accumulate a wrap-corrected **net signed position** `_pos`; distance = `_pos·circ`
(net — zero-mean noise cancels, no deadband needed); speed = d/dt of an EMA
**low-pass** of position (`_TAU_S = 0.15 s`), with the 0.05 rev/s floor only zeroing
the *display*. Glitch guard (>0.5 rev/sample) unchanged. `_dist` accumulator removed.
Defaults changed: `rate 50→120`, `volts_per_rev 5.0→4.912` (measured), in both
[settings.py](../wheel/settings.py) and the workers.

**Validated (real `_derive`, capture decimated to ~120 Hz):** net **−0.106 rev /
−0.050 m** (≈ ground truth), stationary speed rms **0.025 rev/s** (was 0.46). Same
result at 1 kHz and 120 Hz → rate-independent, aliasing gone.

**Follow-up same day — final derivation (reset-rejection) + sign flip + distance plot.**
The `np.unwrap` look-ahead attempt still **sawtoothed** on real hardware: the
encoder reset is **smeared over 2–3 samples** (confirmed in a real toy capture:
`0.02 → 3.21 → 4.60 V`), so each sub-step is under unwrap's half-turn threshold and
the reset reads as +1 rev of motion, cancelling the turn. Final `_derive` (in
[wheel/acquisition.py](../wheel/acquisition.py)) instead:
- integrates per-sample `frac = v/vpr` with wrap-correction into a cleaned absolute
  `_pos`; any step implying **> `_MAX_REV_S` (10 rev/s)** is a reset/dead-zone
  artifact → dropped and **coasted** at the current velocity `_vel` (wheel keeps
  spinning through the sensor's blind spot). This is what stops the sawtooth.
- reports (in `_report`, for a sample `_LAG_S`=1 s in the past) **speed = LSQ slope
  over a ±`_SLOPE_WIN_S`=0.25 s window** (real SNR — a per-sample diff on this
  0.045 V-noisy signal reads ~0.5 rev/s of phantom speed), and **distance =
  deadband-gated integral of that speed** (so stationary noise can neither
  random-walk nor over-count it).
- `_SIGN = -1.0` (forward → positive; flip to +1 if wrong). Metadata:
  `wheel_distance_mode=net_forward`, `wheel_speed_lag_s`, `wheel_sign`.

**Validated on the REAL toy capture** (`acqApp/toy_output/wheel.csv`, 7 resets):
**0 reset leaks**, distance tracks the actual spins (net +3.19 m ≈ 6.8 rev), holds
flat when stopped, 0.000 m drift over 60 s stationary (synthetic), slow 0.1 rev/s
within ~5%. Old delayed-`np.unwrap` / `_dist_rev`-frame-diff path is gone.

**Ported to the main app** ([main.py](../main.py)): the wheel plot now shows
**Distance** (title "Wheel distance"), with the **live current speed as a number in
the plot title** and in the settings-panel readout (`speed … / net …`). Toy
([wheel/_toy.py](../wheel/_toy.py)) plots Distance too and its mock is now a
realistic descending sawtooth. NOTE: the toy's Record-CSV saves at the **GUI rate
(~51 Hz)**, not the worker's 120 Hz.

**ACTION FOR USER (on rig):** V/rev now defaults to the measured **4.912**; just set
**`Wheel dia`** to the real diameter. Re-capture a *known* spin (e.g. exactly 10
forward turns) and confirm the app's **net** reads +10 rev and speed is smooth.

---

_(original section 2 notes below; the derivation details above supersede the path/deadband parts)_

---

## 3. Diagnostic tools (new, standalone, NI + numpy only, ASCII output)

- [wheel/capture_raw.py](../wheel/capture_raw.py) — hardware-clocked **1 kHz**
  raw-voltage capture while spinning; live min/max, saves `wheel_raw.csv`, prints
  a summary. `python wheel\capture_raw.py --seconds 30`.
- [wheel/analyze_raw.py](../wheel/analyze_raw.py) — unwraps the sawtooth,
  reports measured **volts_per_rev** (peak-to-peak), **net rotation**, and **path
  (deadband)** at full rate and decimated to 50 Hz (the app's rate).
  `python wheel\analyze_raw.py wheel_raw.csv --dia 150`. Validated on a synthetic
  3-rev spin: net +3.00 rev / 1.41 m at 50 Hz.

Real capture from the rig: voltage 0.10–5.02 V, span 4.91 V, 0 glitches — a clean
single-turn sawtooth.

---

## 4. Camera frame-rate label said "USB3" — FIXED

The label was a **hardcoded default** (`DEFAULT_LINK = USB`), not link detection.
Rig is **CoaXPress-only**.

- [voltage_cam/presets.py](../voltage_cam/presets.py) — `DEFAULT_LINK = CXP`;
  measurement note updated (old 15.8 fps / 316 MB/s reading was USB-enumerated and
  doesn't apply to CXP-only).
- [voltage_cam/settings.py](../voltage_cam/settings.py) — panel now shows the
  camera's **measured** rate once running (`set_measured_rate`, driven by the
  worker's `timing_update`); reverts to the datasheet estimate on stop.
- [main.py](../main.py) `_start_session` connects `timing_update` →
  `set_measured_rate`; `_stop_session` clears it.

**OPEN — watch the measured fps at full frame:** ~**115 fps** = CoaXPress live;
~**15.8 fps** = still bandwidth-capped like USB3, which would be a DCAM/grabber
enumeration issue *below* this app (camera's USB interface also enumerated, or the
FireBird grabber not the enumerated path). The app can't choose the link, but the
measured label now tells you which one is live.

---

## Quick state summary

| Area | State |
|------|-------|
| Camera crash | Fixed (double-open + QThread guard + faulthandler); user confirmed working |
| Wheel speed/distance | **Reworked 2026-08-06**: net_forward + low-pass, 120 Hz, V/rev=4.912. Validated offline vs real capture; needs rig re-test + real wheel dia |
| Wheel signal | Confirmed 0–5 V single-turn sawtooth, **4.912 V/rev** (measured); noise ±0.045 V drove the derivation rewrite |
| Camera link label | Defaults to CoaXPress; shows measured rate when running |
| Untested on hardware | wheel derivation end-to-end, camera full-frame fps (is it really CXP?) |

## Files touched this session
- [acq/worker.py](../acq/worker.py) — crash guard + `error` signal, `_run` contract
- [voltage_cam/acquisition.py](../voltage_cam/acquisition.py) — `run`→`_run`
- [pupil_cam/acquisition.py](../pupil_cam/acquisition.py) — `run`→`_run`
- [stage/acquisition.py](../stage/acquisition.py) — `run`→`_run`, dropped dup `error`
- [wheel/acquisition.py](../wheel/acquisition.py) — `_EncoderBase`, derivation, guards, `set_scaling`
- [wheel/settings.py](../wheel/settings.py) — live readout, field docs
- [wheel/_toy.py](../wheel/_toy.py) — 4-tuple
- [wheel/capture_raw.py](../wheel/capture_raw.py), [wheel/analyze_raw.py](../wheel/analyze_raw.py) — NEW
- [voltage_cam/presets.py](../voltage_cam/presets.py), [voltage_cam/settings.py](../voltage_cam/settings.py) — link default + measured rate
- [main.py](../main.py) — faulthandler, cam handle reuse, worker error/timing/drops wiring, wheel derive/record/readout
