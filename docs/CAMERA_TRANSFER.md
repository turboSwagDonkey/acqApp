# Camera Module — Transfer Document

Handoff for a session focused on the **voltage-imaging camera** (Hamamatsu
ORCA-Fire, `C16240-20UP`) in `acqApp`. Everything you need to pick up the camera
work in isolation.

---

## 1. What the camera module is

The voltage camera is the primary imaging device. It captures 16-bit frames from
a Hamamatsu ORCA-Fire via **pylablib's DCAM wrapper** (`pylablib==1.4.5`,
DCAM-API installed on this machine). It runs in two contexts:

- **Bring the camera up alone** — start the app, tick only this module in the
  startup picker, and press **Live view**: live image + ΔF/F trace + settings,
  with nothing else loaded. **This is where camera work should happen first.**
  It replaced `voltage_cam/_toy.py`, which duplicated the panel and had
  already drifted from it.
- **Main app** — the full multi-instrument suite; the camera is the central
  image and records into the shared-clock HDF5 session.

Both are the same worker (`OrcaFireWorker`), presets and settings panel — which
is the point: there is no longer a second copy to keep in step.

---

## 2. How to run

Use the **project venv's Python** (`acqApp/.venv/Scripts/python.exe`) — it has
pylablib, PyQt6, pyqtgraph, h5py. Run on a real display (not headless).

```powershell
# from C:\Users\User\Desktop\python
acqApp\.venv\Scripts\python.exe acqApp\main.py                      # tick Voltage cam only
acqApp\.venv\Scripts\python.exe acqApp\main.py --mock               # synthetic frames
```

Headless smoke tests (mock only) use `QT_QPA_PLATFORM=offscreen` and
`PYTHONPATH=C:/Users/User/Desktop/python`.

The venv is created/repaired by `main.py`'s bootstrap on first run. Vendor
**DCAM-API** must be installed for real hardware (it is, on this machine).

---

## 3. Relevant files

| File | Role |
|------|------|
| [devices/voltage_cam/acquisition.py](../devices/voltage_cam/acquisition.py) | `OrcaFireWorker` (real, DCAM) + `MockCameraWorker`. The capture thread. **Most camera logic lives here.** |
| [devices/voltage_cam/presets.py](../devices/voltage_cam/presets.py) | `ResolutionPreset`, `AcqConfig`, the datasheet-derived `PRESETS`, binning/trigger options. |
| [devices/voltage_cam/settings.py](../devices/voltage_cam/settings.py) | `SettingsPanel` — resolution/binning/exposure/trigger UI; `get_config()` → `AcqConfig`. |
| Live view, one module ticked | Brings the camera up alone — what `voltage_cam/_toy.py` used to do. |
| ~~voltage_cam/recording.py~~ | Deleted 2026-08-13 with the toy it served. The app records through `acq/`. |
| [acq/worker.py](../acq/worker.py) | `PullWorker` base class: `get_latest`/`set_sink`/`stop` scaffolding all workers share. |
| [acq/recorder.py](../acq/recorder.py) | `Recorder` — main-app writer thread draining the ring buffer (stamps the shared clock). |
| [acq/ring_buffer.py](../acq/ring_buffer.py) | Bounded ring buffer (count + **byte** cap; sheds image frames, never scalar events). |
| [acq/writer.py](../acq/writer.py) | `HDF5Writer` — main-app HDF5 sink (block-grown datasets, NaN-tail crash-safety). |
| [acq/clock.py](../acq/clock.py) | `SessionClock` (software `perf_counter`); the shared timebase. |
| [main.py](../main.py) | Full app: builds workers per session, wires sinks, records. Camera wiring in `_start_session`/`_start_recording`/`_pull_frames`. |

---

## 4. Architecture

### Capture worker (`OrcaFireWorker`)
- Subclasses `PullWorker` (a `QThread`). `run()` opens/configures the camera,
  `start_acquisition`, then loops: `wait_for_frame` → `read_multiple_images` →
  `self._emit_sink(img)` for every frame (gap-free recording) and
  `self._set_latest(imgs[-1])` for the ~30 Hz preview.
- **GUI pulls** the newest frame with `get_latest()` (returns once, else None).
- **Recording sink** attached via `set_sink(fn)`; each frame is stamped on the
  shared clock at acquisition time by the `Recorder`.
- **Exposure** is hot-changeable via `set_exposure()` (queued, applied next loop).
  Resolution/binning/trigger are structural (locked while running).
- **`cam=` parameter**: pass an already-open `DCAMCamera` to reuse it (worker
  won't open or close it); omit to have the worker open/close its own by index.
  Backward-compatible — the main app still uses the index form.

### Presets (datasheet-accurate)
`PRESETS` are full-width (X=4432) bands at the datasheet's Y row counts, keyed by
stable id (`"4432x512"`) with a display label carrying the framerate. Frame rate
is set almost entirely by **row count**; labels show the **USB3.1 Gen1 16-bit**
rate (this app captures 16-bit). CoaXPress ≈ 7× higher. See the table in
[presets.py](../devices/voltage_cam/presets.py):

```
 Y     CoaXPress   USB 16-bit   USB 8-bit
2368     115         15.7        31.5   (full frame)
2304     118         16.2        32.4
2048     132         18.2        36.5
1024     264         36.4        72.8
 512     524         72.3       144
 256    1020        143         286
 128    1980        279         558
   8   15200       2360        5260
   4   19500       3960        7200
```

`AcqConfig.frame_shape` = `(rows//binning, cols//binning)`.

### Recording
- **Main app**: worker sink → `Recorder` (thread) → `RingBuffer` → `HDF5Writer`,
  one session file with all streams on the shared clock. Ring buffer is bounded
  by **bytes** (512 MB) so full frames can't OOM, and sheds oldest **frames**
  before scalar events.

---

## 4a. ✅ CoaXPress appears to be LIVE (2026-08-03)

A later run reports numbers only reachable over CoaXPress:

```
Voltage cam: TDeviceInfo(vendor='HAMAMATSU', model='C16240-20UP', ...)
[voltage_cam] achievable: 2042.0 fps (readout ceiling 1980.0, exposure ceiling 40000.0)
```

- **2042 fps at 4432×128.** The USB3 ceiling for 128 rows is **279 fps**; the
  datasheet CoaXPress figure is **1980**. 2042 ≈ 1980, so this is CXP.
- **1.08 MB/frame × 2042 fps = 2210 MB/s** — roughly 7× USB3 saturation, and in
  line with 4 × CXP-6 links.
- The vendor string changed from `'Hamamatsu'` (USB module) to `'HAMAMATSU'`,
  i.e. a *different DCAM interface module* is now enumerating the camera.

Confirm on demand with `acqApp\devices\voltage_cam\_check_link.py` (full frame should
show ~8.7 ms, not 63.3 ms). §4b below records the earlier USB-only state and the
commissioning steps, kept for history.

---

## 4b. THE LINK: measured USB3, not CoaXPress (2026-07-29 — superseded by §4a)

**The camera is cabled on BOTH interfaces, and DCAM is using the slow one.**

Measured directly — full frame, 1 ms exposure, `cam.get_frame_timings()`:

```
frame_period = 63.30 ms  ->  15.8 fps  ->  316 MB/s     <- USB3.1 Gen1
CoaXPress would be ~8.7 ms -> 115 fps -> 2300 MB/s      <- 7.3x faster
DCAM reports 1 camera (the USB one)
```

What is physically present and healthy:

| | |
|---|---|
| Active Silicon **FireBird 4xCXP6-2PE8** grabber | 4 CXP-6 links, all PnP status `OK` |
| `Hamamatsu C16240` on USB | `VID_0661&PID_1454\000080` (S/N 000080) |
| `DCAM-API for Active Silicon FireBird / Phoenix` | **installed** 26.2.7108 |
| `DCAM-API Drivers for USB` | installed 26.2.7108 |
| `Modules\digital\fgphnx.dll` (FireBird grabber module) | present |
| FireBird driver `oem125.inf` / `AslDma` 11.4.9.0 | provider = **Hamamatsu Photonics K.K.** |

The board was installed by Hamamatsu's own CXP setup, so it is intended for this
camera. Nothing in this app chooses the link.

### Tested 2026-07-29: unplugging USB does NOT bring CoaXPress up

```
USB unplugged + camera power-cycled:
  _check_link.py  -> "DCAM cameras: 0"
  HCImage         -> camera does not appear either
```

Both DCAM and HCImage see nothing, so **the CXP link is not being established**
— this is below the app entirely, not a pylablib or acqApp problem. Ruled out
along the way:

- `Modules\phoenix\Hamamatsu_DCAM.pcf` is a **red herring** — a legacy Camera
  Link config (`PHX_BOARD_TYPE, PHX_DIGITAL`, 12-bit, 512×512, 9600-baud serial),
  not a CoaXPress config. Vestigial.
- `CoaXPress Runtime Environment x64` is published by **Basler**, not Active
  Silicon (matching the Basler pylon GenTL paths in the environment). It is a
  different vendor's CXP stack and irrelevant to this path.
- PnP exposes no CXP link state; the System event log has nothing for
  FireBird/CXP/dcam; `aslver.exe` is a GUI app with no CLI output.

**Remaining candidates (all need physical or GUI access):**
1. **Cable/port order — most likely.** Multi-link CXP requires the camera's
   master link on the grabber's channel 0/port A, with links in order. Reaching
   115 fps needs ~2300 MB/s ≈ 4 × CXP-6 links, which is exactly what the
   4xCXP6 board provides — so all four cables must be present and in order.
2. **Camera interface selection** — a dual-interface ORCA may be pinned to USB;
   if so it must be switched back over USB (HCImage / DCAM Config) first.
3. **Camera power** — confirm the unit actually came back up after the
   power-cycle. Reconnect USB and check it enumerates: that isolates a
   camera/power fault from a CXP-link fault.
4. **Whether this SKU has the CXP option at all.** C16240-20UP: the ORCA-Fire
   *family* datasheet lists both interfaces, but confirm the CXP connectors on
   the body are populated and seated.

**Suggested order:** reconnect USB → confirm the camera returns (rules out 3) →
open `C:\Windows\System32\DCAMAPI\aslver.exe` (GUI) and check per-channel CXP
link status → then inspect cable/port order. `dcamtray.exe` in the same folder
shows DCAM's own device view.

This is now a hardware-commissioning task for Hamamatsu/Active Silicon support,
not an app change. **The app is ready for either link** — it reads the real rate
from the camera at start, so if CXP comes up it will simply report the higher
number.

### Where the recording ceiling actually is (measured 2026-08-03, on D:)

Not the disk — the **HDF5 write path**:

| | |
|---|---|
| Raw sequential write, 20 GB + `fsync` (D:, NVMe KC3000 1TB) | **1835 MB/s** |
| `HDF5Writer`, 400 × 20 MB frames, same drive | **1178 MB/s** |
| CoaXPress delivers | **~2210 MB/s** |

So ~650 MB/s is lost inside HDF5, and the drive has headroom the writer never
uses. Two plausible causes were tested and **both ruled out** — do not re-try
them:

- **Chunk-cache sizing** (`rdcc_nbytes` 0 / 1 MB / 8 MB / 4 chunks): all within
  noise, 1157–1218 MB/s. The current 4-chunk sizing is marginally best.
- **Block writes** (K frames per chunk and per write, K = 1…16): actively
  *worse* — 1175 MB/s at K=1 falling to ~960 MB/s at K≥4. One frame per chunk
  is the right configuration.

Treat ~1180 MB/s as the practical writer ceiling on this machine unless the
container itself changes (parallel writers, raw binary + sidecar index, or
striping across drives).

### ⚠ Drive layout matters more than any of this

| Drive | Type | Free | Note |
|---|---|---|---|
| **C:** | NVMe KC3000 512G | **28 GB** | OS drive — where the app currently writes |
| **D:** | NVMe KC3000 1024G | **953 GB** | empty, same speed class — **use this** |
| E: / F: | SATA MX500 1TB | 760 / 931 GB | SATA caps ~550 MB/s — not for primary capture |

At 2210 MB/s (≈133 GB/min) C:'s 28 GB is **13 seconds** of recording; D: gives
~7 minutes. Even at the sustainable 1180 MB/s, D: is ~13 minutes. Recording at
CoaXPress rates is storage-hungry — plan capacity per session, and point the
session directory at D:.

**If CXP does come up, two things in this app become wrong:**
1. ~~**The writer becomes the bottleneck.**~~ **It did, and it no longer
   does.** CXP came up, CXP full frame needs **2300 MB/s**, and the writer
   sustained 1004 — half the frames were dropped for a week. Fixed 2026-08-25
   by handing HDF5 the frame's own buffer instead of assigning through the
   dataset: **1304 -> 2696 MB/s**, 2464 through the whole path, so full frame
   bin 1 now records complete. The 1165 figure below is a superseded benchmark.
   *Live preview and the DCAM ring buffer were never affected by any of this.*
2. **Buffer sizing.** `_buffer_frames` uses the camera-reported fps, so it
   self-corrects — but the *datasheet fallback* would under-size by 7× if
   `get_frame_timings()` ever fails.

`presets.py` now carries both columns and labels every preset
`"72.3 USB / 524 CXP fps"`, so the UI can never quietly imply the wrong link.

---

## 5. Current state (as of this handoff)

**Verified on the real camera** (from the last run):
- Camera detected: `Hamamatsu C16240-20UP`, S/N 000080.
- Start now succeeds. Per-step timing printed by the worker:
  ```
  open: 7.29s   set_roi: 0.08s   start_acquisition(nframes=12): 0.32s   first frame: 0.13s
  ```
- So acquisition **start works**; the only slow step is the one-time camera open.

**Fixed recently (this is why it works now):**
1. **Trigger API** — was setting a raw `"TRIGGERMODE"` attribute that doesn't
   exist on this camera/pylablib. Now uses `cam.set_trigger_mode("int"|"ext")`,
   wrapped in try/except.
2. **Buffer allocation** — pylablib defaults `nframes=100`; at ~21 MB/frame that
   allocated ~2 GB per Start. Now sized to a 256 MB budget (~12 full frames).
3. **Open-once reuse** — the ORCA takes ~7 s to open. The **toy** now opens it
   once at launch and reuses the handle for every Start/Stop (closes on exit).
4. **Display throttle** — the toy's `_pull` no longer runs `percentile` + a
   `float32` copy every frame; it uses the strided view and refreshes contrast a
   few times/sec.
5. **Datasheet presets** — replaced made-up sizes with the exact datasheet modes.

**Frame-rate work (later session, mock-verified only):**
6. **Uncompressed image writes** — see open question 4. The single biggest win;
   gzip was costing ~88% of recorded frames at full frame.
7. **DCAM buffer sized by time, not a frame count** — was
   `clip(256 MB / frame, 8, 100)`. The hard 100-frame cap left only 0.36 s of
   slack at 4432×128 and **42 ms** at 4432×8, so any GC pause silently
   overwrote un-read frames. Now targets 2 s of acquisition, capped at 768 MB.
8. ~~**Readout speed forced to `"fast"`**~~ — **no-op on this camera.** Measured:
   `get_all_readout_speeds()` returns `[]` and the `READOUT SPEED` attribute
   raises `DCAMError`, so the property is not adjustable on the C16240-20UP over
   USB. The call is guarded and harmless (and would help on other DCAM
   cameras), but it buys nothing here. At full frame `readout time == frame
   period == 63.3 ms`, i.e. the **link**, not sensor readout, is the limit.
9. **Real timings instead of the datasheet table** — `get_frame_timings()` asks
   the camera what frame period it can actually sustain for the configured
   ROI/binning/exposure. Emitted as `timing_update`; the datasheet estimate is
   now only a fallback.
10. **Drop detection** — `get_frames_status().skipped` is polled once a second
    and emitted as `drops_update`. Nonzero = the camera overwrote frames we
    were too slow to read. Previously nothing anywhere reported this, and
    `read_multiple_images(missing_frame="skip")` actively hides gaps.
11. **Windowed fps** — was a cumulative `n/t` average since start, which
    converges too slowly to reveal a mid-run slowdown. Now taken from the
    camera's own `acquired` counter, so it is the true acquisition rate even
    when the preview deliberately skips frames.
11b. **Preview no longer reads every frame** — the capture loop called
    `read_multiple_images()` unconditionally, copying the camera's entire output
    out of the driver buffer even with no recording sink attached. At CoaXPress
    rates that is 2.2 GB/s of memcpy to display ~30 frames/s: the copy cannot
    keep up, the buffer saturates (`709/709 unread`) and the camera overwrites
    un-read frames. With no sink the loop now uses `read_newest_image()`, which
    copies ONE frame and advances the read pointer past the rest. Drop reporting
    is likewise suppressed without a sink — in preview, skipping is intentional.
12. **Toy records gap-free** — it wrote frames from the 33 ms display timer via
    `get_latest()`, so it captured ≤30 fps of non-consecutive frames despite the
    "gap-free" claim above. Now attached to the worker sink, with a background
    writer thread so the capture thread never touches disk.

> ⚠️ Changes **3–12 were made after** the last hardware run that produced the
> timing above. They pass mock/headless checks but have **not yet been confirmed
> on the real camera**. First task in the new session: re-run the toy and confirm
> open-once + the new presets behave on hardware. In particular
> `get_frame_timings`, `get_frames_status`, `get_all_readout_speeds` and
> `set_frame_format("try_chunks")` are all wrapped in try/except and fall back
> silently — check the console actually shows the timing/buffer lines rather
> than the fallback path.

---

## 6. Open questions / next steps for the camera

1. **Confirm open-once on hardware** — after launch (~7 s open), every Start
   should be ~0.5 s. Verify Start/Stop/Start reuses the handle without error.
2. **Cross-thread camera use** — the toy opens the `DCAMCamera` on the main
   thread (pre-init) and the worker uses it on its `QThread`. This mirrors the
   pupil cam but is **unverified for DCAM**. If pylablib complains about thread
   affinity, switch to a persistent single-thread camera owner (worker thread
   opens once, stays alive, toggles acquisition).
3. **Apply open-once to the main app** — `main.py` still opens the camera per
   Start (and pre-detects with an open/close probe). Give it the same
   open-once-and-reuse treatment as the toy.
4. ~~**Throughput / recording**~~ — **measured and fixed, twice.** gzip-1
   sustained only **37 MB/s (1.9 fps)** at full frame against the required
   330 MB/s — recording was discarding ~88% of frames. Image streams are now
   written **uncompressed**, which benchmarked at 1165 MB/s here and 1304 on
   the rig's D: — *not* ample headroom once CXP raised the requirement to 2300,
   which is what the direct-chunk write (2026-08-25) fixed. `hdf5plugin` is
   *not* installed, so
   lz4/blosc would need a new dependency; uncompressed is fast enough that it
   isn't worth it. `HDF5Writer(compression=…)` re-enables it per-session if disk
   space ever matters more than keeping up.
5. **Verify binning** on hardware (2×/4×) and its fps interaction. The datasheet
   estimator now models binning as an effective row reduction (full frame 2×2 →
   31.5 fps); confirm against `get_frame_timings()` on real hardware.
6. **DCAM parameter surface** — only exposure/ROI/binning/trigger are exposed.
   Others (readout speed, sensor cooling/temperature, defect correction) are set
   via pylablib methods, **not raw attributes** (see the trigger bug). Add as
   needed via `cam.set_trigger_mode`-style calls, checking the pylablib method
   exists first (`dir(DCAMCamera)`).
7. ~~**Exposure vs fps**~~ — **surfaced in the UI.** actual fps =
   min(readout max, 1/exposure), and the settings panel now shows the effective
   rate live, flagging (amber) when exposure is the binding limit and naming the
   longest exposure that still reaches the readout ceiling. The default 10 ms
   exposure caps at 100 fps, which silently throttled every preset above
   4432×256 — 4432×128 to 100 of 279 fps, 4432×4 to 100 of 3960.
8. **Getting onto CoaXPress is the big one — see §4b.** 7.3× at full frame
   (15.8 → 115 fps), and the hardware and drivers are already installed. Do this
   before any other frame-rate work; it dwarfs everything else in this list.
   **8-bit readout** is the fallback 2× lever *if you stay on USB*: the link is
   saturated at 316 MB/s so 16-bit full frame can never beat 15.8 fps, and the
   datasheet's 8-bit column is exactly 2× throughout. Not exposed in the app; it
   halves dynamic range, a real cost for ΔF/F, so make it an explicit opt-in.
   Find the property with `cam.get_all_attributes()` first (per the trigger bug
   and the readout-speed no-op: verify the name, never guess a string).
9. **Extreme presets are Python-bound, not camera-bound.** At 4432×4 (3960 fps)
   the per-frame sink chain has a ~250 µs budget. `set_frame_format("try_chunks")`
   cuts the per-frame object churn, but if those presets matter, the next step is
   batching whole chunks into the ring buffer (one `put` per read instead of one
   per frame) rather than micro-optimising the loop.

---

## 7. Handy facts

- Sensor: **4432 × 2368**, 16-bit (uint16). Full frame ≈ **21 MB**.
- ROI sizes/positions must be **multiples of 4** (DCAM requirement) — the preset
  helpers already enforce this.
- pylablib is `1.4.5`. Key `DCAMCamera` methods: `set_roi`, `set_exposure`
  (seconds), `set_trigger_mode`, `start_acquisition(nframes=…)`,
  `wait_for_frame`, `read_multiple_images`, `stop_acquisition`, `close`.
- The worker prints per-step timing on Start — use it to profile hardware.
- Mock path (`--mock` / `MockCameraWorker`) needs no hardware and matches the
  real worker's API; good for logic changes.
