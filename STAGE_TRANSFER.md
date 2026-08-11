# Stage Transfer — XY stage subsystem in acqApp

Continuation notes for the **XY stage** device in `acqApp`. Companion to
[SESSION_HANDOFF.md](acqApp/SESSION_HANDOFF.md) and the other device transfers
(camera, wheel, pupil). Read this before touching stage code or moving the stage.

The hardware is a **Thorlabs PLS-XY** stage on an **MCM6101** controller. There are
**two** codebases for it — keep them straight:

- **`stage_control/`** (sibling folder, NOT in acqApp) — the standalone, **proven**
  Tkinter app. Has the XY map and **"Set 0,0 = center"**.
- **`acqApp/stage/`** — the integrated device: position + jog/go-to/stop inside the
  acquisition app, and (**since 2026-08-07**) origin/frame calibration of its own.

The driver is a byte-for-byte **copy**: [stage/driver.py](acqApp/stage/driver.py)
== `stage_control/mcm6101.py`. If you fix the protocol in one, port it to the other.

**The calibration JSON is no longer duplicated.** Both apps now read *and write*
`stage_control/config.json`; [stage/stage_config.json](acqApp/stage/stage_config.json)
is a **fallback only**, used if the sibling folder is missing, and is not kept
current. `stage/settings.py` `config_path()` resolves it; the panel shows the
path in use.

---

## Files

| File | Role |
|------|------|
| [stage/driver.py](acqApp/stage/driver.py) | `MCM6101` APT/serial driver. All motion methods flagged. Copy of the standalone `mcm6101.py`. |
| [stage/control.py](acqApp/stage/control.py) | `StageController` (owns the serial connection) + `MockStageController`. Motion API in **microns**; soft limits clamp every target. |
| [stage/acquisition.py](acqApp/stage/acquisition.py) | `StagePollWorker` — read-only position poller (a `PullWorker`). Never issues motion. |
| [stage/settings.py](acqApp/stage/settings.py) | `StageAxis`/`StageSettings` dataclasses + Qt panel (port, poll rate, live X/Y readout, travel map, jog/go-to/stop, session home, **Calibrate…**) + `CalibrationDialog`. `config_path()`, `load_settings()`, `save_axis_updates()`. |
| [stage/map_widget.py](acqApp/stage/map_widget.py) | `StageMap` — read-only picture of the stage inside its travel. Nothing here commands motion. |
| [stage/stage_config.json](acqApp/stage/stage_config.json) | **Fallback copy only** — the live calibration is `stage_control/config.json`. |

---

## Hardware facts (verified — see [[mcm6101-stage-setup]])

- Controller **MCM61010**, fw **7.0.2**, **COM54** (USB-CDC, VID_1313/PID_201E).
  Serial **115200 8N1, DTR+RTS asserted** or the CDC device stays silent.
- APT addressing: controller/info at dest **0x11**; axis N at dest **0x21+N**,
  channel-ident = N. **3 axes** (0=X, 1=Y, 2=Z/Focus). **Z is disabled**
  (`"active": false`).
- **ThorImage holds an exclusive lock on COM54** — close it before connecting here,
  and vice-versa. Only one program owns the port at a time.
- Motion quirks baked into the driver:
  - Firmware **ignores APT relative-move (0x0448)**. Use absolute moves — jog is
    implemented as "absolute move to current+delta" (`move_to_readout`).
  - **Command units are ~17.78× coarser** than the status-position readout. The
    per-axis linear map `enc = slope·cmd + offset` (slope ≈ 17.78) converts them.
    Landing accuracy ~±30 counts.
  - Read position via **`get_status` (0x0480)**; `REQ_POSCOUNTER` returned 0.
  - Encoder counter **wraps at ±2^21**; full ~1-inch travel is near that range.
- Scale: **X ≈ 62.0, Y ≈ 61.9 counts/µm** (~16 nm/count), from 1-inch travel.

---

## THE critical model — the coordinate frame

Absolute positioning only works inside an established **frame**. The controller
**re-references its command origin (and the enc↔cmd offset) whenever a HARD LIMIT is
hit.** So the calibration splits in two:

- **Frame-independent** (stable forever): `counts_per_um`, `span_counts`.
- **Frame-specific** (valid only until a limit is next hit): `slope`, `offset`,
  `true_center`, `travel_min/max`, `soft_min/max`.

`0,0` is the **user-set true center**, not an auto-homed point (auto-homing is
unreliable because the encoder wraps and the forward limit doesn't trip cleanly).
Positions in the app are **µm relative to `true_center`**; soft limits sit at ±0.5"
to keep the stage in the no-wrap zone.

On connect, [stage/control.py](acqApp/stage/control.py) `connect()` loads the
config's `slope`/`offset` into the driver (`set_linear_map`) so absolute go-to lands
right — **this assumes the frame in the JSON is still valid.**

## Panel layout

Top to bottom: connection + µm readout → **travel map** → Motion (jog / go-to /
stop) → **Home (this session)** → STOP ALL → frame status line → **Calibrate…**.

### Three tiers, deliberately separated

The stage has three kinds of "position reference" and they are kept apart because
confusing them is how a coordinate system gets silently destroyed:

| | What it is | Lifetime | Where |
|---|---|---|---|
| **true zero (0,0)** | The calibrated origin. All recorded µm are relative to it. | Saved to the config; survives restarts | behind **Calibrate…** |
| **session home** | A bookmark: "the spot I'm working at today." | In memory; cleared at session start/end | main panel |
| **frame** (`slope`/`offset`) | The command→encoder map that makes absolute go-to land. | Until the next hard-limit hit | behind **Calibrate…** |

**Session home** (`StageAxis.home_counts`, `set_home_here` / `go_home` /
`clear_home`) is deliberately **never loaded from or written to the config** —
`load_settings()` doesn't read it and `save_axis_updates()` never writes it. A
convenience marker that outlived the sample and got mistaken for the true zero
would be worse than no marker at all. It's stored in **encoder counts**, so
re-zeroing 0,0 moves where home *reads* in µm but not where it physically is.

### The travel map

[stage/map_widget.py](acqApp/stage/map_widget.py) draws, in µm relative to 0,0:
the hard travel rectangle, the soft-limit rectangle dashed inside it, the origin
(green cross), the session home (orange diamond), and the current position (blue
dot with guide lines to the edges). +Y is drawn screen-up when `xy_pad.invert_y`
is set, so the picture matches the rig.

- **Display only.** The standalone Tkinter app's drag-to-move map is deliberately
  *not* ported — a stray click on a settings tab must never move the stage.
- Outline markers (origin, home) are painted **over** the filled position dot;
  sitting exactly on 0,0 is the common case and the dot would otherwise hide the
  marker you're lining up with.
- With `margin_um` at 50, the soft-limit rectangle sits 0.2 % inside the travel
  edge — they look almost coincident. That's accurate, not a rendering bug.
- If the origin isn't calibrated there is no cross, just "0,0 not set".

### Calibration (behind the **Calibrate…** button)

`CalibrationDialog` holds the two actions that rewrite saved calibration. They're
rare, hard to undo, and one drives into the hard limits, so they don't sit next to
the buttons used all day.

| Button | Moves? | What it does |
|--------|--------|--------------|
| **Set 0,0 = center (here)** | **No** | Declares the current position the origin; writes `true_center` + `travel_*` + `soft_*` (±½″ around it) and saves. Centre the stage yourself first. |
| **Re-establish frame…** | **Yes — into the reverse hard limits** | `StageController.establish_frame()`: per axis, drive to the reverse limit, then two in-range probes → new `slope`/`offset`. **Never writes an origin.** Confirm dialog first. |

**Go to 0,0** stays on the main panel next to Go home — it's navigation, not
calibration.

⚠️ The dialog is **modal**, so the panel's STOP ALL can't be clicked while it's
open — and the frame re-establish drives into a limit from inside it. So the
dialog carries **its own STOP ALL**, and its `keyPressEvent` makes **Esc stop the
stage rather than close the dialog** (Qt's default Esc-on-a-QDialog is
`reject()`, which would have dismissed the window mid-move). `reject()` is also
blocked while the worker runs.

Which action you need:

- **Origin is in the wrong place, but go-to still lands accurately** → *Set 0,0*.
- **A hard limit was hit / go-to lands offset** → *Re-establish frame*, then
  centre the stage and *Set 0,0* — in that order, because re-establishing leaves
  the stage at a probe position near the reverse limit, not at your origin.

### ⚠️ `establish_frame` must never invent an origin

The driver returns a geometric centre (`reverse limit + span/2`) alongside
`slope`/`offset`. **Do not persist it.** The first version of this feature did,
and it silently overwrote a user-set 0,0 with a point nobody wanted — the same
mistake that got the standalone app's auto "Find Center" deleted back in 2026-07
(see [[mcm6101-stage-setup]]: 0,0 is USER-SET, by design).

What it does with the existing origin is decided per axis, from the freshly
measured travel `[R, R+span]`:

- origin **inside** that range → **kept** untouched.
- origin **outside** it (or never set) → **dropped** (`true_center: null`), soft
  limits re-parked on the measured travel, and the panel falls back to the red
  "no valid frame" state so absolute go-to stays blocked until you re-zero.

That second case is not hypothetical. Measured on the rig 2026-08-07: the
reverse limit read **−1,371,738**, where the pre-limit config had `travel_min`
= **501,548**. So a hard limit re-references the **encoder readout**, not just
the command origin — which means a `true_center` stored before the hit points at
nothing and cannot be trusted. Keeping it would have reported confidently wrong
microns.

Implementation notes:

- Absolute go-to (both the per-axis **Go** and **Go to 0,0**) is **disabled while
  `has_frame` is false** — a stale frame silently mislands, so the UI refuses it
  rather than doing it wrong. **Jog stays enabled** (it's relative and unaffected).
- `establish_frame` blocks for a minute or more per axis, so it runs on a
  `_FrameWorker` QThread with a progress label. The Motion and Home groups are
  disabled for the duration so no competing move can be issued. **`STOP ALL` is
  deliberately outside the Motion group box** — a Qt child of a disabled parent is
  unclickable, and the abort has to stay live exactly during that move.
- `save_axis_updates()` merges only the touched axis keys and writes via
  temp-file + `replace`, so the rest of the file (and a crash mid-write) can't
  destroy the calibration. It also drops a **`config.json.bak`** of the previous
  contents on every write — one step of undo for a hard-won origin.
- **`driver.establish_frame()` had never been called by either app** before this —
  the standalone only ever *loaded* `slope`/`offset` ([app.py:544](stage_control/app.py#L544)).
  So this path is new code, not a port, and is **unvalidated on hardware**.
- `MockStageController` implements the same three methods but **deliberately never
  writes the config** — a calibration invented with no stage attached must not
  overwrite the real one.

⚠️ Changing the origin **mid-session** changes what recorded `stage_x_um` /
`stage_y_um` mean part-way through the file. Calibrate before you start recording.

---

## How it's wired in [main.py](acqApp/main.py)

- **Enable**: "stage" in the module picker → builds `StageSettingsPanel`
  (settings tab). Stage has **no plot** — position shows as the panel's X/Y readout.
- **Session start** (`_start_session`, ~L689): builds `StageController` (real) or
  `MockStageController` (emulate mode), calls `connect()`. On failure it logs to the
  status bar and leaves the stage out (session still runs). On success it starts a
  `StagePollWorker` at `poll_hz` and `bind_controller()`s the panel so the motion
  buttons go live.
- **Recording** (`_record_stage`, ~L890): each poll sample writes two scalar streams
  **`stage_x_um` / `stage_y_um`** on the shared `SessionClock`. Metadata adds
  `stage_port`, `stage_poll_hz`.
- **Stop** (~L764): unbind controls, `close()` the connection, drop the worker.
- **Safety**: **Esc = STOP ALL** app-wide (`QShortcut`, application context).
  Go-to moves larger than `confirm_move_um` (~3.2 mm, from
  `max_unconfirmed_move_counts`) pop a confirm dialog. Soft limits clamp every target
  in `StageAxis.clamp_counts`. The poll worker **never** commands motion.

---

## Status / open items

| Item | State |
|------|-------|
| Driver / protocol | ✅ proven (standalone app, 2026-07). acqApp copy identical. |
| Read + jog/go-to/stop wired into main | ✅ code complete; **mock-verified**, needs on-rig validation *in acqApp* (standalone is the tested path). |
| Set 0,0 = center in acqApp | ✅ built 2026-08-07; used on the rig. No motion, so low risk. |
| Frame re-establish in acqApp | ✅ **run on hardware 2026-08-07** — the limit drive and the slope/offset probe both worked (X 17.7766/33.0, Y 17.7749/91.0, in line with the 2026-07 hand-measured 17.77815/17.7741). |
| Origin preservation | ✅ fixed 2026-08-07 after it clobbered a user-set 0,0 on the rig. Covered by a regression test that asserts both the kept and dropped branches. |
| Travel map | ✅ built 2026-08-10; render-tested offscreen (marker presence, +X right / +Y up, no cross when 0,0 unset, bare-axes fallback). |
| Calibration behind a gate | ✅ built 2026-08-10 — `CalibrationDialog`, with its own STOP ALL and Esc-stops-not-closes. |
| Session home | ✅ built 2026-08-10; in-memory only, verified it never reaches the config and is cleared on session start/stop. |
| Config duplication | ✅ resolved — single shared `stage_control/config.json`; acqApp copy is a stale fallback. |
| **0,0 currently on the rig** | ❌ **not set.** `slope`/`offset` are good, but `true_center` holds the bogus geometric centre the old code wrote (X −584511, Y −324391). Centre the stage and click *Set 0,0 = center (here)* — one click overwrites it. |
| Z / Focus axis | Disabled (`active:false`); not exposed. |
| Recording (`stage_x_um/y_um` + metadata) | ✅ code complete; mock-verified. |

### Before moving the stage on the rig
1. Close **ThorImage** (COM54 lock).
2. Check the panel's frame status. If it's red — or you're unsure whether a limit
   was hit — treat absolute go-to as broken.
3. Watch the stage; keep Esc (STOP ALL) reachable. Test **jog** first.
4. To rebuild a stale frame: **Re-establish frame…** (it drives to the reverse
   limits — stage must be clear), then jog to where you want the origin and
   **Set 0,0 = center (here)**.
5. Verify with a small **go-to** that the µm readout matches reality before any
   large move.

## Safety
Every motion method in [stage/driver.py](acqApp/stage/driver.py) is flagged. Nothing
moves unless `jog_um` / `move_to_um` / `home` is called — the poll worker and all
reads are motion-free. Do not send motion during dev without the user watching.
`home()` would drive to a limit and **break the frame** — don't call it.
