# acqApp

Multi-instrument in-vivo acquisition suite for the ICN rig. One PyQt6 app that
runs and records seven subsystems against a single shared session clock:

| Subsystem    | Package        | Device                                            |
|--------------|----------------|---------------------------------------------------|
| Voltage cam  | `voltage_cam/` | Hamamatsu ORCA-Fire C16240-20UP via `pylablib` DCAM (CoaXPress) |
| Pupil cam    | `pupil_cam/`   | Basler acA1920-40umMED via `pypylon` (USB3)       |
| Wheel        | `wheel/`       | Rotary encoder as analog voltage on NI `Dev3/ai2` |
| Puffer       | `puffer/`      | Air-puff TTL on NI `Dev3/port0/line0`             |
| XY stage     | `stage/`       | Thorlabs MCM6101 (serial): position logging **and** motion |
| DMD          | `dmd/`         | Vialux **ALP-4.2**, 1024×768, via `ALP4lib`        |
| Visual stim  | `vis_stim/`    | Drifting sinusoidal grating on a display screen, gated by the shared session clock |

…plus two that own no device: **Experiment routines** (`routines/`), which
executes a protocol of stage positions and DMD patterns step by step, and
**Closed loop** (`closed_loop/`), which watches one subsystem's live signal and
fires another's output. Both are described below. Routines is **always loaded
and opens in a window of its own**; the closed loop is a settings tab like the
instruments.

> **What has actually run.** Every subsystem has a real driver path and a mock
> twin, and the whole suite is verified against the mocks (`tests/`). Two have
> been run against the real hardware: the wheel encoder, and the **DMD** —
> opened, a pattern rendered and uploaded, projected and held, then halted and
> released, on 2026-08-12. The camera, puffer, stage and visual-stim paths have
> **not** been run on the rig; treat their rates, timings and device quirks as
> unconfirmed until they have.

Panels are **dockable** — drag any plot or video panel to re-dock, float, or tab
it with another; drag the tabs to reorder. The layout is remembered across runs.
The voltage camera has the central image; the **pupil camera has its own dockable
video box** (live frame, the fitted pupil ellipse, the eye region and any pinned
reflections), alongside its radius trace in the Signals panel.

The **settings** for every loaded subsystem live in one tabbed **pop-up
window**, reachable two ways that stay in step. The left-edge sidebar has one
item per page — **Save** first, then each loaded instrument with its accent
colour beside it — so the sidebar doubles as the list of what is loaded; and the
window keeps its **tab bar**, so every page is visible at once and the tabs drag
into your own order. Clicking a sidebar item opens the window on that page and
clicking the one you are already on tucks it away, keeping the main workspace
entirely for the images and plots. Switching tab inside the window moves the
sidebar highlight to match. Being a separate window, it can be left open beside
the app or on a second screen while a session runs, and its size and position
are remembered. The pupil camera's page exposes
camera exposure/frame-rate, the tracking parameters (threshold, blur,
ellipse-or-circle), corneal-reflection removal (threshold, pad, ring, reach) and
the eye-tracking LED toggle. The **eye region** and the **pinned reflections**
are placed on the preview rather than typed here — both are positions in the
frame.

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

The **XY stage** tab both logs and moves. It polls the stage controller for
X/Y position (µm) and records it to `/stage_x_um`, `/stage_y_um` — plus
`/stage_z_um` on a rig whose config names and enables a Z (focus) axis — and
it drives the stage:
jog, absolute go-to, stop, a session-scoped "home" bookmark, and a calibration
that **drives both axes into their reverse hard limits** to re-measure the
command→encoder map. Soft limits clamp every target, and absolute go-to is
disabled until the frame is known, so a stale origin cannot silently send the
stage to the wrong place.

Calibration, soft limits and the origin live in the config **shared with the
standalone `stage_control` app** (`../stage_control/config.json`, falling back
to `devices/stage/stage_config.json`), so both programs agree on where 0,0 is. Only one
program can hold the serial port at a time. Every write to that file leaves the
previous contents as `.bak`.

The **Visual stim** tab drives a drifting sinusoidal grating, shown full-screen
through a circular aperture on a chosen display, gated on and off by
"trigger" pulses. Ported from a standalone MATLAB/Psychtoolbox tool
(`guiVisStimDAQ.m` and friends) that read those pulses off an external DAQ
line — this rig has none, so the pulses are the shared session clock's own
periodic tick instead (10 Hz by default), the same timing sequence every
other module already shares rather than a second one invented for this
module alone. Loop variables expand into a full-factorial trial list — name a
parameter and a value list (or a `start:step:stop` range) and every
combination runs in turn. **Priming** waits for a configurable number of
ticks before the first trial starts; each trial then alternates blank/stim
phases on further tick counts. Run puts live view on if it is not already
running (the clock only ticks while live), the same way starting a routine
opens the recording it needs. Unlike the .m tool's single blocking run loop,
a stimulus run here advances one video frame at a time, so the rest of the
app — cameras, recording, other modules — keeps running live while it plays.

A **Trial type** selector chooses the paradigm for the whole run. Alongside
the grating there is **Map**: the screen splits into 4 columns (one blacked
out) each cut into 3 rows, and one of the resulting 9 regions at a time
flips white/black while the rest stay a mid grey — a single continuous trial
that cycles through all 9. There is also **Tuning**: a circle at one of
those same 9 regions (diameter equal to the region's width) showing the
plain grating swept through 8 orientations (0°-315° in 45° steps), preceded
by 2 white "pretrial" flashes — again one continuous, tick-timed trial.
**Contrast** is the same shape, sweeping the grating's contrast through six
fixed levels (0, 0.1, 0.25, 0.5, 0.75, 1.0) instead of orientation, also
preceded by the 2 white pretrials. **Size** is the same shape again,
sweeping the circle's own diameter through five fractions of the region's
width (0.2-1.0) instead of orientation/contrast. **Visuomotor** is a normal
drifting grating like the plain one, except its drift is driven by the
wheel's live speed each frame (scaled by a Gain parameter) instead of a
fixed temporal frequency — the standard locomotion/optic-flow closed-loop
coupling; with no wheel module loaded it just stays a static grating.

## Running

> **Operating it?** [docs/USER_GUIDE.md](docs/USER_GUIDE.md) is the
> screenshot-illustrated walkthrough — launch, pick instruments, configure
> one, go live, record. This section and the rest of this file describe what
> the app *is*, not how to click through it.

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

On startup a **module picker** pops up — tick which instruments to load
(defaults to your last-used selection, stored in `acqapp_local.json`).
**Experiment routines has no checkbox**: it owns no device, so there is nothing
to unload, and unticking it would only lose the protocol you had written
(`config.ALWAYS_ON`).

The set is **not fixed for the run**: the sidebar's **🧩 Modules** button reopens
the same picker, and loading or unloading applies to the window you are looking
at. A module loaded while a session is running builds and starts its own worker
and joins the live view; an unloaded one is stopped and its tabs, docks and
plots come off. The only refusal is **while recording** — the session file names
its modules once, at the start, so a stream cannot appear or vanish part-way
through. The button greys out for the duration.

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
folder, names the file from a token template (`{mouse_id}`, `{project}`,
`{date}`, `{time}`, plus the active FOV's name appended when "Append active
FOV name" is checked and a saved FOV is currently active on the Stage tab),
and shows how many minutes the free space is worth at the current data rate.
It also shows the exact path the next recording will get:

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
/pupil_x /pupil_y /pupil_major /pupil_minor /pupil_angle   (T,) float64 + timestamps
/wheel_voltage /wheel_speed /wheel_distance    (M,) float64  + timestamps
/puffer/values             (K,) float64 (dur) /puffer/timestamps       (K,) float64
/stage_x_um  /stage_y_um   (P,) float64       …/timestamps             (P,) float64
/stage_z_um  (only on a rig with a Z stage)   …/timestamps             (P,) float64
/dmd/values                (Q,) float64 (idx) /dmd/timestamps          (Q,) float64
/routine/values            (S,) float64 (±step) /routine/timestamps   (S,) float64
```

The five **pupil** streams are the fitted ellipse — centre, semi-axes and angle
in frame pixels — one sample per *tracked* frame, and **NaN in all five** where
there was no fit, so a gap in the trace is in the file rather than being a row
nobody wrote. There are fewer of them than there are frames: tracking drops
frames it cannot keep up with, and the file's `pupil_frames_tracked` and
`pupil_fits` say by how much. `pupil_track_threshold` is written with them
because threshold *sets* the reported radius — a pupil trace without it is not
reproducible.

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

The **DMD** is a stimulus *output*, not an input: its settings tab loads a
pattern image, places it on the 1024×768 panel, and starts/stops projection.
While a session is recording, the projection's boundaries are logged on the
shared clock (`/dmd`) so the stimulus aligns with the imaging and behavioural
streams: **0** when Display starts it, **−1** when it stops. Those are the two
instants the app actually commands — once started, the ALP runs its own sequence
clock, so a cycling pattern is reconstructed from `dmd_on_time_ms` and
`dmd_repeats` between them rather than logged frame by frame. (The mock also
emits a tick per on-time, because it genuinely does produce them.)

**Where the pattern lands** is the alignment to the optics, so it is recorded
too — `dmd_scale_pct`, `dmd_rotation_deg`, `dmd_offset_x/y`, `dmd_invert`,
`dmd_all_on` (an all-mirrors-on frame ignores the pattern and the geometry, so
without it the recorded placement would describe one that was never used),
`dmd_fit`, and `dmd_on_pixels` (how many mirrors the frame switches on; **0** is
a dark panel, which is what a bad scale or offset produces and is otherwise
indistinguishable from a projection that worked). `dmd_device` names the ALP or
says `mock`, so a session that projected can be told from one that didn't.

**Sub-sampling** (`dmd_sub_sampling`, `DmdSettings.sub_sampling`) turns off 1
of every N pixels — a coarse ND filter made of missing mirrors, cutting total
light without touching exposure time or geometry. 1 (the default) is off; the
panel offers 2 ("1 out of 2", half the light) up to 10. It applies uniformly
to whatever mode is loaded — All ON, an image, or ROIs — via
`control.subsample_frame`, diagonally striped so the removed fraction is even
across the panel rather than banded along one axis. `dmd_on_pixels` already
reflects it; the field is recorded too so a dimmed session doesn't file
identically to a full-brightness one.

Rotation is clockwise-positive and the offset is measured from the panel's
centre — the same conventions as the standalone **`dmdGUI_project`** app, which
is where the optics are normally aligned; its saved scale/rotation seed this
panel's defaults so both put a pattern in the same place. **Only one process can
hold the ALP over USB**, so that app and acqApp cannot be connected at once
(like the stage's serial port). If it is open, acqApp falls back to the mock and
the DMD tab says `nothing will be projected` rather than silently doing nothing.

### Experiment routines

A window that owns no device and drives two that do. It is **always loaded**,
and its panel is **its own window** rather than a page of the settings window —
a routine is *run* from that panel while the settings window is showing the
camera it is driving, and a tab cannot be in two places. Any adapter can ask
for the same by setting `own_window` (`adapters/base.py`); the shell never
learns which module did.

A **routine** is a list of **atomic steps** executed in order — Move the
stage, start Displaying a pattern, Wait, Puff, wait for an external
Trigger — repeated for as many cycles as the operator asks. It is the first feature in the app whose whole purpose
is to **actuate**, which is why it is shaped the way it is.

A **Recording** is a sticker on one step — click that step's row number in
the table to toggle it — meaning "the camera is capturing for this step,"
independent of what the step itself does and independent of repeat groups (a
recording may sit inside, outside, or straddle a group's edge freely). This
is the one thing that decides when a `/routine` file boundary opens and when
the illumination LED comes on; a routine can move and display things outside
any Recording with neither happening. A recording covers exactly the one
step it's stuck to, not a range — it does not carry over into the step
after it — but a step inside a repeated Group still gets a fresh recording
file every time it repeats: `routines/engine.py` opens a new one per
`(cycle, serial)` automatically.

A Wait step's length is set in **frames or seconds, the operator's choice
per step**, and the two are never interconverted: at 106 Hz a rounded
conversion sheds frames at every step boundary. A frames step is measured by
what reached the **file**, not by what the camera produced — the two differ
exactly when the write path is the thing falling behind. A Move step's
`settle_s` is separate: a stage move is software-timed and not instant, so
the step doesn't end at arrival, it ends `settle_s` after it.

The panel shows **where the routine is** (a bar over the whole protocol, plus
a marker on the running step) and **what it will cost before it starts**.
`routines/estimate.py` is the one place frames are turned into seconds, and it
names the frame rate it used — with no camera loaded it reports frames as
frames rather than guessing. Steps are reordered by dragging the row, by
Ctrl+Up/Down or by the arrow buttons, and a protocol worth running twice is
saved to `routine_templates/` as a file (`routines/templates.py`).

Everything that decides lives in `routines/settings.py` (the protocol,
`Step`/`Group`/`Recording`, and validation), `routines/engine.py` (the
executor) and `routines/estimate.py`, all Qt-free; the panel is
`routines/panel.py`, the step table `routines/table.py`, and a
"Timeline…" button draws one cycle to scale in `routines/timeline.py` —
a repeat group as one bracket, recordings as separate bars, one per
repeat, never merged. **Every actuation reaches the engine as a callable**, the way the DMD calibration
takes `project`/`grab`, so a whole routine — move, display, wait, puff,
trigger, fault, resume, and when a Recording bracket opens and closes — is driven
against fakes on a fake clock in `tests/test_routines.py` before anything on
the rig moves. `adapters/routines.py` is the only part that touches a real
stage or projector, and it reaches them through
`ModuleHost.stage_target`/`pattern_target`: an instrument becomes
routine-drivable by declaring one, and the routine never imports it.

**One button starts it.** Start validates the protocol, opens the recording if
one is not already running, and runs the steps — a routine cannot record into a
file that is not open, so refusing until the operator had found the Record
button in another part of the window only moved the failure earlier. A recording
Start opened is stopped again when the routine ends; one the operator started is
left running. (`ModuleHost.set_recording`, the twin of the `set_live` the DMD
calibration uses to turn the live view on for itself.)

**Start trigger: manual, or TTL through the camera.** Manual is Start doing
the above right away. TTL opens the recording and *arms* the routine instead
— step 1 does not begin until the voltage camera reports a frame it did not
have at arm time. `adapters/routines.py` puts the camera in External edge
mode itself before arming (`ModuleHost.set_camera_trigger`, restarting live
view to apply it if needed — mirroring the DMD calibration's own stop/
reconfigure/restart dance), rather than trusting the operator to have
already set it on the Voltage cam tab, where it can silently drift back to
Internal between being set and the routine actually arming. That restart is
refused rather than interrupting a recording already in progress.
Validation refuses a TTL start trigger up front if no camera is loaded. The
panel shows **ARMED — waiting for the camera's TTL trigger** while it waits;
Abort cancels the arm cleanly, since nothing has moved or lit up yet.

**How the camera is actually gated (measured, 2026-09-14).** DCAM's
`TRIGGER SOURCE` for External edge is `MASTER PULSE`, not plain `EXTERNAL`:
`EXTERNAL` captures exactly one frame per edge, so a single start pulse gives
one static frame and then nothing. Capture runs off the camera's own
master-pulse generator instead, configured as `MASTER PULSE TRIGGER SOURCE =
EXTERNAL` and — the part that matters — **`MASTER PULSE MODE = START`**. The
camera's default `CONTINUOUS` free-runs that generator off its own interval
and never consults the line at all, which read as "the trigger is off but the
app acts like it's on". `MASTER PULSE INTERVAL` is set from the frame period,
because in `START` mode it caps the frame rate (the 0.1 s default pins a
recording to 10 Hz). Polarity is `POSITIVE` via
`setup_ext_trigger(invert=True)` — pylablib's `invert=False` default is
`NEGATIVE`, i.e. start on the trigger going *off*. All four verified against
the real camera with the line toggled by hand: zero frames while low, first
frame ~20 ms after it went high. Note these are enums that must be written as
**numeric codes** — `set_attribute_value` passes the value straight to the C
library, so a string raises `ValueError`, and `enum_as_str` is read-only.

**One recording per external edge: the `Trigger` step.** For "100 recordings
at FOV 1, each started by an edge and lasting *x* seconds", put a **Trigger**
step and a Wait in a repeat group of 100, and draw the Recording bracket over
the **Wait alone**. Each repeat re-arms the camera, holds in **WAITING** until
its edge lands, then opens the file — so with `save_mode="per_repeat"` you get
one file per edge. The bracket must not cover the Trigger step itself;
validation refuses that, because the file would otherwise be open while
waiting, and re-arming must happen underneath it.

Re-arming is necessary because `START` mode **latches**: the first edge starts
the stream and later edges do nothing on their own. A bare
`stop_acquisition()`/`start_acquisition()` is **not enough** — confirmed at the
rig (2026-09-16/17): that pair pauses reading frames, but the master-pulse
generator's own "already got my edge" latch survives it untouched, so the next
`start_acquisition()` free-runs with no edge at all (one recording worked, the
next began on its own). What actually resets the latch, also confirmed live,
is writing `MASTER PULSE MODE` away from `START` and back to it — the property
**write** is what clears it, not the acquisition state (`OrcaFireWorker.
_do_rearm`, queued via `ModuleHost.rearm_camera_trigger` onto the capture
thread since the DCAM calls belong to that thread, not the routine's).

That queueing is asynchronous, so frames already in flight keep arriving for a
while after a re-arm is requested — how long is not knowable from the engine,
so a fixed window was tried first and **also failed at the rig**: it expired
while frames were still coming, and the next one read as a trigger. Detection
is instead self-verifying: the frame count must hold completely still for
`TRIGGER_SETTLE_S` (0.75 s, past a `TRIGGER_DRAIN_S` floor of 1.5 s) before the
engine starts trusting it, and only *then* does an increase mean a real edge.
A camera that never goes quiet faults, naming the reason, rather than
fabricating a trigger. That same stillness requirement is what makes an edge
arriving while the routine is busy elsewhere get **ignored** rather than
latched — the operator's call. Pause works while WAITING, since an edge may
never come. Between edges the camera produces no frames at all, so **live view
is black while waiting** — inherent to gating capture on the line. The panel
also names which repeat is running (`RoutinesModule._repeat_suffix`) — "step
1/2" alone would read identically on repeat 1 and repeat 100.

**The step list is edited through widgets, not words.** Kind is a drop-down
(Move/Display/Wait/Puff/Trigger); Length/Unit are only editable on a Wait row and
Settle only on a Move row — every other row renders them as **"—"** rather
than a number that means nothing for that kind. A Move step's target reads
**no change** on an axis it should not move, rather than being blank, which
used to mean both "leave it alone" and "not typed yet" — Delete on the cell
clears it, or a small dialog to type numbers. Steps reorder with the arrows
or by dragging the row, and the row being executed is shown in bold while
the routine runs. A summary line under the table says how many runs, how
long at least, whether the stage moves and how many steps emit light.

**Validation is up front.** A stage target outside the soft limits, a frames
step with no camera to count them, a step that projects with no DMD loaded, a
missing pattern file — each is a refusal at the Start button with every reason
listed, not a fault at step 7 of 12 with an animal on the rig.

**A device failure pauses; it does not abort.** Stage motion is stopped and the
light blanked, capture is left alone, and the operator decides. An interrupted
recording's data is **kept and marked** (`interrupted`, plus the fault text)
rather than discarded, and **Resume repeats the interrupted step from its
start** as a new attempt — a step means "do this," and half of a Wait does
not. If that step is still inside a Recording bracket, Resume opens a
**fresh** recording run rather than reattaching to the one the pause just
closed, so the file always shows exactly where the pause happened. Skip is
the other way out.

Recording boundaries are recorded to `/routine` on the shared clock: `+n` when
bracket *n* opens, `−n` when it closes. The file also carries the protocol
itself (`routine_steps`, as JSON), because "which stage position was step 4"
cannot be recovered from anything else in it, and `routine_started` — a
routine that was configured and never run leaves the same step list as one
that ran.

Two more rules that go with it: a Move step blanks the light before travelling
and restores whatever a Display step last left lit once it has arrived and
settled, since a lit panel travelling across the sample is a stimulus nobody
asked for; and the module set cannot change while a routine runs, since the
routine holds an index into it.

> `save_mode = per_repeat`/`per_group` (2026-09-14) roll to a fresh file at a
> `RecordingRun` boundary — every repeat, or only when the covering Group
> actually changes — via `MainWindow.roll_recording()`, a plain stop-then-
> start of the recorder. Provenance (which group/repeat/cycle) travels in
> each file's own metadata (`RecordingRun.attrs()`, naming the routine's
> start on the shared clock so a folder of files reassembles onto one
> timebase), not the filename — the existing `_NNN` auto-numbering already
> gives distinct, chronologically-sortable names for free.

### Closed loop

Another tab that owns no device. It watches one live scalar and
fires one output when a condition on it holds — the behavioural counterpart to
the scheduled triggers, which fire at a *time* on the session clock rather than
at a *state* of the animal.

The rule is: *when `<signal>` goes `above`/`below` `<threshold>` and holds for
`<hold>`, fire the `<puffer|DMD>` for `<duration>`* — plus a minimum gap between
fires, an optional "one event per bout" mode, and a session-wide fire ceiling.
Each gate is there because a bare threshold on a real signal fires hundreds of
times a second; `tests/test_closed_loop.py` carries an ungated control that does
exactly that.

The wheel offers **two** speeds and they are not interchangeable. `wheel_speed`
is the recorded one — a least-squares slope centred ~1 s in the past, so it
matches the trace in the file but a rule on it acts about a second after the
animal starts running. `wheel_speed_live` is the EMA velocity behind it: noisier,
but current. The file records which was used (`loop_source`).

Evaluation runs on its own thread at 200 Hz, *watching* the wheel through a
non-consuming snapshot rather than pulling from it — the display tick is already
the consumer, and a second one would take samples away from the plot. The
decision is made on that thread but the actuation is not: a fire is handed to the
ordinary trigger bus, so a rule-driven puff takes the identical path to a
scheduled one.

**Arming is never persisted**, for the same reason the eye-tracking LED isn't:
restoring an armed rule would mean the app firing the puffer at the next launch,
in an empty rig, before anyone had looked at the threshold. Disarmed, the rule
still evaluates and the tab still shows whether the condition is met, so a
threshold can be set against a live animal without actuating anything.

Every fire is recorded to `/closed_loop` — the value that crossed, stamped at the
instant of the sample that caused it — alongside `loop_armed`, `loop_source`,
`loop_threshold`, `loop_target` and the rest of the rule, and `loop_fires` when
the file closes. `loop_armed` is what tells a rule that was never armed from one
that was armed and never met its condition; both leave `/closed_loop` empty.

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
- `acq/sync.py` — `SyncController`: shared clock + tick + trigger bus.
- `acq/devices.py` — the `Protocol`s an adapter reads its worker/controller
  through, and `ModuleHost`, the surface an adapter may ask of the window.
- `devices/` — one package per subsystem, each with `acquisition.py` (a `QThread` worker with
  a mock twin), `settings.py`/`control.py` (a Qt panel), `recording.py`.
  Tick one module at startup and press **Live view** to bring it up alone,
  isolated from the rest of the session. The pupil cam's tuning overlay — the
  pixels reflection removal blanked — is the **Show what was removed** box in
  its tab.
- `devices/dmd/alp.py` — the whole of the Vialux hardware knowledge, Qt-free: where the
  vendor API lives, `build_frame` (image → the binary panel frame, which is
  where a mispositioned stimulus would come from, so it is unit-tested), and the
  open/project/halt/close lifecycle. `devices/dmd/control.py` holds only the panel and
  the app-facing controller.
- `devices/pupil_cam/eyeloop_tracker.py` — the **only** file that touches
  EyeLoop, which is **GPL-3.0** and is imported from a clone beside the repo
  rather than vendored. With no clone the pupil camera runs exactly as before
  and says so; nothing else in the app changes. `tracking.py` is the seam that
  turns a frame plus the operator's settings into one ellipse.
- `devices/pupil_cam/track_worker.py` — pupil **tracking** gets a thread of its
  own, on top of the camera's. A fit is 1–2 ms, but it is not bounded (a lost
  pupil, a re-armed tracker), and in the display tick a slow one freezes the
  whole window, voltage-camera preview included. It is the sole consumer of the
  pupil camera's frames and republishes each frame *with* the fit made from it,
  so the outline always matches the image under it.
- `adapters/` — one `ModuleAdapter` per subsystem, **one file each**, holding
  everything specific to that instrument: its settings tab, its plot, its
  worker, its ~30 Hz display tick, its recording sink, and the metadata it
  writes into the session file. `adapters/base.py` is the adapter itself and the
  two widget builders they share; `adapters/__init__.py` holds the registry and
  the lifecycle table. The adapters never import each other.
- `main.py` — session-wide shell only: the shared clock, the trigger bus, the
  recorder, the save destination, the docks and the theme. It iterates over the
  adapters and holds no per-instrument logic.
- `console.py` — `enable_safe_console()`, called by **every** runnable entry
  point before its first print. The diagnostic messages use characters ("→",
  "≤", "⚠") that a non-UTF-8 console cannot encode, and those prints sit inside
  the acquisition loops — unguarded, the resulting `UnicodeEncodeError` escapes
  into `PullWorker.run()` and looks exactly like a device failure. Add the call
  to any new entry point.

**Adding an instrument** is a new file in `adapters/` plus one line each in
`adapters.ADAPTERS` and `config.MODULES` — the window itself does not change.

### Why an instrument appears in two places

`devices/wheel/` and `adapters/wheel.py` are not duplicates — they are the two
sides of the boundary, and the dependency only ever runs one way:

    adapters/wheel.py   the ADAPTER: how the wheel plugs into THIS window —
                        which tab, worker lifecycle, metadata keys
    devices/wheel/      the DEVICE: driver, acquisition worker, its own model
                        and widgets. Knows nothing about acqApp's window.

`main.py` imports `adapters`; `adapters/X.py` imports `devices/X/`; no device
package imports back. That is what lets an instrument be developed and tested without
the app, and the window be read without knowing what a pupil camera is.

Inside a device package the convention is:

    settings.py   the settings model — a dataclass, **no Qt**
    panel.py      its Qt widgets
    acquisition.py / control.py / driver.py   the device itself

Keeping `settings.py` Qt-free is load-bearing, not tidiness: the models import
with zero PyQt6 modules, so config, tests and analysis can read them without a
QApplication. `devices/voltage_cam/` has no `settings.py` — its model is `AcqConfig` in
`presets.py`.

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
- ✅ Pupil tracking moved off the GUI thread; encoder on the DAQ's sample clock
- ✅ DMD projecting for real (ALP-4.2), verified on the hardware
- ✅ Closed loop: trigger the DMD / puffer from live wheel speed (mock-verified)
- ✅ Experiment routines: stage + DMD protocol executed step by step (mock-verified)
- Encoder scaling measured on the rig (`volts_per_rev`, wheel diameter)
- Camera throughput measured on the rig — the number that sizes the ring buffer
- Closed loop tried on the rig, with the threshold set against a real animal
- Hardware sync upgrade: `DaqClock` on the PCIe-6363, hardware-triggered ORCA
