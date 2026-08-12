# acqApp

Multi-instrument in-vivo acquisition suite for the ICN rig. One PyQt6 app that
runs and records six subsystems against a single shared session clock:

| Subsystem    | Package        | Device                                            |
|--------------|----------------|---------------------------------------------------|
| Voltage cam  | `voltage_cam/` | Hamamatsu ORCA-Fire C16240-20UP via `pylablib` DCAM (CoaXPress) |
| Pupil cam    | `pupil_cam/`   | Basler acA1920-40umMED via `pypylon` (USB3)       |
| Wheel        | `wheel/`       | Rotary encoder as analog voltage on NI `Dev3/ai2` |
| Puffer       | `puffer/`      | Air-puff TTL on NI `Dev3/port0/line0`             |
| XY stage     | `stage/`       | Thorlabs MCM6101 (serial): position logging **and** motion |
| DMD          | `dmd/`         | pattern-stimulus output — **still a stub**, see below |

> **What has actually run.** Every subsystem has a real driver path and a mock
> twin, and the whole suite is verified against the mocks (`tests/`). Apart from
> the wheel encoder, **none of it has been run against the rig hardware yet** —
> treat rates, timings and device quirks as unconfirmed until it has. The DMD is
> the one exception to "real driver path": `dmd/control.py` still prints where
> the vendor calls belong, so with Emulate off, Display does nothing.

Panels are **dockable** — drag any settings/plot/video panel to re-dock, float,
or tab it with another; drag the tabs to reorder. The layout is remembered
across runs. The voltage camera has the central image; the **pupil camera has
its own dockable video box** (live frame + detected-pupil outline), alongside
its radius trace in the Signals panel.

The **settings** for every loaded subsystem live in one tabbed dock that is
**collapsed by default** — click the **⚙ Settings** tab on the left edge to pop
it open, click again (or close the dock) to tuck it away, keeping the workspace
clear for the images and plots. Each subsystem's tab (and its group boxes, plot,
and dock accent) is coloured by that subsystem. The pupil camera's tab exposes
camera exposure/frame-rate, the pupil-tracking parameters (threshold, min/max
radius, search lines, edge polarity, minimum edge strength, circle-or-ellipse
fit), and the eye-tracking LED toggle.

**Every panel's settings persist** to `acqapp_local.json` and come back on the
next launch — camera preset and exposure, wheel V/rev and diameter, pupil
tracking parameters, puffer line and duration, stage port, DMD pattern and
timing, and the save destination. Runtime state deliberately does not persist:
the eye-tracking LED always starts off. The stage's axis calibration is not
kept here either — it belongs to the config shared with the standalone
`stage_control` app.

The **Puffer** tab can **schedule puffs** to fire at fixed session times
(`Puff at t = N s`). Scheduled puffs are armed on the shared trigger bus, so they
fire (on the next 100 ms tick past their time) and are logged on the session
clock alongside every other stream — the basis for timed-stimulus experiments.

The **XY stage** tab both logs and moves. It polls the MCM6101 for X/Y position
(µm) and records it to `/stage_x_um`, `/stage_y_um`, and it drives the stage:
jog, absolute go-to, stop, a session-scoped "home" bookmark, and a calibration
that **drives both axes into their reverse hard limits** to re-measure the
command→encoder map. Soft limits clamp every target, and absolute go-to is
disabled until the frame is known, so a stale origin cannot silently send the
stage to the wrong place.

Calibration, soft limits and the origin live in the config **shared with the
standalone `stage_control` app** (`../stage_control/config.json`, falling back
to `stage/stage_config.json`), so both programs agree on where 0,0 is. Only one
program can hold the serial port at a time. Every write to that file leaves the
previous contents as `.bak`.

## Running

No manual setup needed — just launch it **any** of these ways. A bootstrap in
`main.py` creates `acqApp/.venv` on first run if it's missing, re-execs into it,
fixes the import path, and installs `requirements.txt`:

```
python acqApp\main.py                              # any interpreter → uses/creates .venv
acqApp\.venv\Scripts\python acqApp\main.py         # run the file directly
python -m acqApp.main --mock                         # as a module (--mock = no hardware)
```

Installs happen **only inside `acqApp/.venv`** — the bootstrap will never
pip-install into some other interpreter. If you run with a non-venv Python and
disable the re-exec, it refuses to install rather than touch that environment.

Escape hatches: `ACQAPP_NO_REEXEC=1` skips the venv re-exec/creation (use the
current interpreter as-is), `ACQAPP_NO_INSTALL=1` skips auto-installing.

On startup a **module picker** pops up — tick which instruments to load this
session (defaults to your last-used selection, stored in `acqapp_local.json`).

Vendor **runtime drivers** must be installed for real hardware (not
pip-installable): NI-DAQmx (wheel + puffer) and Hamamatsu DCAM-API (camera).

> **Qt binding:** the project is standardized on **PyQt6**. If both PyQt5 and
> PyQt6 are present in the venv, `main` forces `PYQTGRAPH_QT_LIB=PyQt6` so
> pyqtgraph binds to the same one.

## The shared clock (why one timebase)

Every device timestamps its data against a single `SessionClock`
(`acq/clock.py`, `time.perf_counter`-backed). `SyncController` owns that clock,
starts it once at **Start** (t = 0), drives the periodic tick, and schedules
named triggers (e.g. fire the puffer at t+5 s). The `Recorder` holds the *same*
clock instance and stamps each sample at acquisition time, so all streams in a
session share one origin. The NI PCIe-6363 can later back a hardware `DaqClock`
that swaps in behind the same `AbstractClock` interface without touching device
code.

## Recording format

**Start** begins live acquisition + preview. **Record** streams every sample to
one HDF5 file per session. The **Save** tab picks the destination drive and
folder, names the file from a token template (`{subject}`, `{session}`,
`{date}`, `{time}`), and shows how many minutes the free space is worth at the
current data rate. It also shows the exact path the next recording will get:

- A template that resolves to an existing file is **auto-numbered** (`_001`,
  `_002`) rather than overwriting it, and the numbered name is what the preview
  and the status line show.
- The writer opens with HDF5 mode `"x"`, so even a path that reached it by some
  other route raises instead of truncating a session.

```
<folder>/<name>/<name>.h5          # default: one subfolder per recording

/voltage_cam/frames        (N, H, W) uint16   /voltage_cam/timestamps  (N,) float64
/voltage_cam_index/values  (N,) float64       …/timestamps             (N,) float64
/pupil_cam/frames          (N, H, W) uint8    /pupil_cam/timestamps    (N,) float64
/wheel_voltage /wheel_speed /wheel_distance    (M,) float64  + timestamps
/puffer/values             (K,) float64 (dur) /puffer/timestamps       (K,) float64
/stage_x_um  /stage_y_um   (P,) float64       …/timestamps             (P,) float64
/dmd/values                (Q,) float64 (idx) /dmd/timestamps          (Q,) float64
```

### Frame timing

`/voltage_cam/timestamps` are the times the **camera** says each frame was
exposed, not the times the app read them. That distinction matters: frames come
off the camera in batches, so stamping them on arrival would give every frame in
a batch the same time and quantise the stream to the read cadence instead of the
frame rate. The camera's clock has its own epoch, so it is anchored to the
session clock on the first frame — intervals are exact, and the stream carries
one constant offset (the read latency of that first frame, well under a frame
period). The `cam_timestamp_source` attribute records whether this worked
(`camera`) or the app had to fall back to arrival times (`arrival`).

`/voltage_cam_index` is the camera's own frame counter, on the same timestamps.
A frame the driver dropped shows up as a **jump in the index**, so a gap caused
by data loss is distinguishable from one caused by a slow period.

The **wheel** is timed the same way, and for a sharper reason: speed is a slope,
so the samples are clocked by the DAQ's own timing engine
(`cfg_samp_clk_timing` + continuous block reads) rather than by a Python sleep
loop competing with the GUI. Those blocks are anchored to the session clock on
the first read and spaced by the board's rate, so the recorded intervals are the
board's and not the reader's. `wheel_timestamp_source` says which timebase the
run actually got (`hardware`, or `software` if the board refused the timing
configuration and acquisition fell back to pacing), and `wheel_rate_actual_hz`
records the rate the divider settled on rather than the one requested.

The **DMD** is a stimulus *output*, not an input: its settings tab loads a pattern
and starts/stops display. While a session is recording, each displayed pattern
index is logged on the shared clock (`/dmd`) so the stimulus aligns with the
imaging and behavioural streams.

Each device worker pushes into a bounded ring buffer; a single writer thread
drains it to disk (acquisition threads never touch disk I/O). The buffer is
bounded by both item count **and** payload bytes, so full-frame images can't
balloon RAM. Under either bound it sheds the oldest **image frames** first —
they are redundant with the live preview and plentiful — and only drops a
sparse scalar/stimulus **event (puffer, DMD, wheel, stage) if nothing else is
left to shed**, so the experiment's event record survives a disk stall.
`Recorder.stop()` drains the backlog and reports anything it couldn't flush.

Whatever was lost is written **into the file** when it closes, not just shown in
the status bar. A session file always says how complete it is:

| Attribute | What it counts |
|---|---|
| `recorder_dropped_samples` | shed by the ring buffer — the disk fell behind |
| `recorder_late_samples` | arrived after the file closed (a worker still mid-callback when recording stopped) |
| `recorder_unstamped_samples` | offered before the session clock started, so there was no timebase to record them against |
| `cam_dropped_frames` | discarded by the *camera* because we read too slowly; visible as a jump in `voltage_cam_index` |

(The wheel has no loss counter: on the hardware clock a read that falls far
enough behind overflows the board's buffer, which raises rather than silently
skipping — the error reaches the status bar and the worker stops.)

All four are written after the drain and before the close, which is the only
moment they are both final and still writable. Zero across all four means
nothing was lost.

Metadata attributes keep their **own types** — `f.attrs["wheel_volts_per_rev"]`
is a float, `f.attrs["emulated"]` is a bool — so nothing needs parsing on the
way back out. A value that was never set reads as an empty string.

## Architecture

- `acq/` — device-agnostic infrastructure: `SessionClock`, `RingBuffer`,
  `Recorder`, `Writer`/`HDF5Writer`.
- `sync.py` — `SyncController`: shared clock + tick + trigger bus.
- one package per subsystem, each with `acquisition.py` (a `QThread` worker with
  a mock twin), `settings.py`/`control.py` (a Qt panel), `recording.py`, and a
  `_toy.py` standalone harness for bringing that device up in isolation.
- `pupil_cam/track_worker.py` — pupil **tracking** gets a thread of its own, on
  top of the camera's. Tracking is unbounded work (a degenerate mask costs
  100–200 ms in `coarse_seed`, and a lost pupil re-seeds every frame), so in the
  display tick it froze the whole window, voltage-camera preview included. It is
  the sole consumer of the pupil camera's frames and republishes each frame
  *with* the fit made from it, so the outline always matches the image under it.
- `modules.py` — one `ModuleAdapter` per subsystem, holding everything that is
  specific to that instrument: its settings tab, its plot, its worker, its
  ~30 Hz display tick, its recording sink, and the metadata it writes into the
  session file. See the lifecycle table in that file's docstring.
- `main.py` — session-wide shell only: the shared clock, the trigger bus, the
  recorder, the save destination, the docks and the theme. It iterates over the
  adapters and holds no per-instrument logic.
- `console.py` — `enable_safe_console()`, called by **every** runnable entry
  point before its first print. The diagnostic messages use characters ("→",
  "≤", "⚠") that a non-UTF-8 console cannot encode, and those prints sit inside
  the acquisition loops — unguarded, the resulting `UnicodeEncodeError` escapes
  into `PullWorker.run()` and looks exactly like a device failure. Add the call
  to any new entry point.

**Adding an instrument** is a new `ModuleAdapter` subclass plus one line each in
`modules.ADAPTERS` and `config.MODULES` — the window itself does not change.

## Tests

```
acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py
```

Runs in Emulate mode against fakes — no rig hardware, no windows, ~30 s. Covers
the session/recording path end to end (including the written HDF5), every
module-subset combination, camera frame timing, settings surviving a restart,
save-path collisions, stage state, the ways a sample can be lost, the pupil fits
and tracking thread, the encoder's hardware timing and its position→distance
derivation, the readout-rate table, and the console-encoding guard. See
[tests/README.md](tests/README.md) for what each one defends and the two
conventions to follow when adding one.

## Roadmap

See [PLAN.md](PLAN.md) for the live version — stages, checklist and next
actions. In short:

- ✅ Unified session Start/Stop, shared software clock, single-file HDF5 recording
- ✅ Six-subsystem module architecture, settings persistence, recording-loss accounting
- ✅ Pupil tracking moved off the GUI thread
- Encoder scaling measured on the rig (`volts_per_rev`, wheel diameter)
- Camera throughput measured on the rig — the number that sizes the ring buffer
- DMD: replace the stub with the real ALP path (`alp4lib`)
- Closed-loop: trigger DMD / puffer from encoder state
- Hardware sync upgrade: `DaqClock` on the PCIe-6363, hardware-triggered ORCA
