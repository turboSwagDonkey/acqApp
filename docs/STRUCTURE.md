# STRUCTURE.md — what is where, and what may import what

The map of the tree. **[tests/test_structure.py](../tests/test_structure.py)
checks this file against the code**, both halves: every file below must exist
and every file must be listed, and the arrows in the diagram must be exactly the
imports the AST finds. So this cannot quietly rot — but it *can* fail the suite,
which is the point. Update it in the same commit as any move, rename or new
module.

## The dependency flow

One direction only: the window knows about adapters, adapters know about
instruments, and **nothing imports back**. `acq/` is the sink — it depends on
nothing in the app, which is what keeps `DaqClock` (phase 6) a drop-in.

```mermaid
flowchart TD
    main["main.py<br/><i>window · session · docks</i>"]
    adapters["adapters/<br/><i>one file per instrument</i>"]
    devices["devices/<br/><i>the six instruments</i>"]
    routines["routines/<br/><i>run a protocol step by step</i>"]
    saving["saving/<br/><i>where the file goes</i>"]
    acq["acq/<br/><i>clock · recorder · ring · writer · protocols</i>"]
    dialogs["dialogs.py<br/><i>module picker · device monitor</i>"]
    probe["probe.py<br/><i>is a device present?</i>"]
    config["config.py"]
    style["style.py"]
    console["console.py"]
    widgets["widgets.py<br/><i>shared panel widgets</i>"]
    run_pupil_review["run_pupil_review.py<br/><i>pupil review tool, alone</i>"]

    main --> adapters
    main --> saving
    main --> dialogs
    main --> acq
    main --> config
    main --> style
    main --> console
    main --> devices
    adapters --> devices
    adapters --> routines
    adapters --> saving
    adapters --> acq
    adapters --> config
    adapters --> style
    devices --> acq
    devices --> console
    devices --> style
    devices --> config
    devices --> widgets
    routines --> style
    routines --> devices
    routines --> widgets
    saving --> console
    dialogs --> config
    dialogs --> probe
    dialogs --> style
    dialogs --> widgets
    probe --> devices
    probe --> config
    run_pupil_review --> console
    run_pupil_review --> devices
```

Six edges surprise people, so they are drawn rather than explained away:
`devices → widgets`, and `routines → widgets` with it
(`widgets.spin()` builds the configured spin boxes every settings panel wants
— range-before-value, suffix, keyboard tracking; that is what `widgets.py` is
for, and the alternative was the same six lines a hundred times over),
`probe.py → devices` (the DMD probe resolves the ALP path through
`devices/dmd/alp.py`), `probe.py → config` (which NI device to look for is
the active rig's, from `rigs.json` — imported inside the function, not at
module scope, so `probe.py` stays runnable as a plain script and the test
harness can re-point config's file first), `devices → config`
(`devices/dmd/sweep.py`'s Calibration dialog seeds its Model/cross-length
controls from the active rig's `dmd_calibration` profile — a steeply tilted
camera needs the same fit every run, not a re-pick), and
`routines → devices` (a
step's pattern picker opens `devices/dmd/roi_picker.py` to choose a saved ROI
set, and its FOV picker opens `devices/stage/fov_picker.py` the same way —
routines still touches no device directly, `adapters/routines.py` still is).
**`main → devices`** is the odd one out — not a deliberate exception like the
other three, but drift: `main.py` reaches into
`devices/voltage_cam/presets.py` and `devices/dmd/control.py` directly for
preset/mode-recipe lookups (SESSIONLOG.md (bm), the Scan-mode work), bypassing
`adapters/` the way REFERENCE.md §5b calls out as the thing *not* to do. Drawn so
the diagram stays true, not endorsed — flagged in REFERENCE.md §5b for the
operator to decide whether it gets routed back through an adapter.

**An instrument appears in two places and they are not duplicates:**

    adapters/wheel.py   the ADAPTER — how it plugs into THIS window
    devices/wheel/      the DEVICE  — driver, worker, model, widgets;
                        knows nothing about acqApp's window

**Inside a device package:** `settings.py` is the model (**no Qt** — measured at
0 PyQt6 modules, so config/tests/analysis can read it without a QApplication),
`panel.py` its widgets, and `acquisition.py`/`control.py`/`driver.py` the device.

## The tree

```text
acq/                    acquisition core — no Qt widgets, no vendor SDKs
  clock.py              SessionClock: the one timebase every stream shares
  devices.py            the Protocols: what an adapter may assume of a worker,
                        and (ModuleHost) what it may ask of the window
  recorder.py           fan-in from every worker thread; owns the ring buffers
  ring_buffer.py        bounded per-stream buffer; drops oldest, counts losses
  sync.py               SyncController: shared clock + tick + trigger bus
  worker.py             PullWorker: the QThread guard every device worker uses
  writer.py             Writer / HDF5Writer: one file per session
archive/                removed-but-kept code; nothing here is imported
  pupil_tracking/       the pupil tracker, retired 2026-08-24 (SESSIONLOG (ai))
    README.md           why it went, what was kept, how to restore it
    _mark_truth.py      script: mark the pupil edge by hand, then score against it
    _test_tracking.py   script: tracker vs synthetic ground truth
    fits.py             circle/ellipse fitting
    rays.py             radial edge search
    track_worker.py     the tracker's own thread
    tracking.py         the algorithm (IMAQ Find Circular Edge port)
    tests/
      test_pupil_fits.py
      test_pupil_tracking_thread.py
  closed_loop/          fire an output from a live signal, retired 2026-10-01
    README.md           why it went, how to restore it
    adapter.py          was adapters/closed_loop.py
    panel.py
    settings.py         LoopRule / LoopSettings — no Qt
    worker.py
    tests/
      test_closed_loop.py
adapters/               one ModuleAdapter per subsystem — tab, plot, worker,
                        display tick, recording sink, metadata
  __init__.py           the registry (ADAPTERS) and the lifecycle table
  base.py               ModuleAdapter itself + the two shared widget builders
  dmd.py
  mirror.py
  puffer.py
  pupil_cam.py
  routines.py           the ONLY routine code that touches a real device
  stage.py
  vis_stim.py
  voltage_cam.py
  wheel.py
devices/                one package per instrument
  dmd/
    _roi_editor.py      script: open roi_panel's editor alone, no rig, no light
    alp.py              all Vialux ALP knowledge, Qt-free; build_frame is the
                        one a mispositioned stimulus would come from
    calibration.py      DMD↔camera registration: stripes in, transform out —
                        affine by default, or a full projective homography
                        (model="homography") for a rig whose camera is tilted
                        enough that affine measurably mis-registers it. Pure,
                        so it is testable before any light is emitted
    control.py          panel-facing controller + mock twin
    corner_editor.py    drag the auto fit's 4 corners onto an all-on frame;
                        Apply refits an exact homography through them, the
                        same way with_corners does — sweep.py's post-fit knob
    panel.py
    roi.py              stimulation ROIs in camera px (no Qt); rect and circle
    roi_panel.py        draw and edit ROIs over a snapshot
    roi_picker.py        dialog to choose a saved ROI set: session list + Browse
    roi_store.py         save/load named ROI sets; session/archive rotation (no Qt)
    sweep.py            runs calibration.py against the rig: the fresh-frame
                        grabber and the dialog that asks before emitting light
  mirror/
    _probe_mirror_state.py  script: one-shot read of chip 7's GR/CAMERA
                        channels (axis 6 — chip N = axis N-1 on this rig)
                        via the real mirror-state protocol (REQ/GET_MIRROR_
                        STATE) — ThorImage holds the port while open, so
                        this can't watch it live, only a before/after read
                        across a ThorImage close/flip/close cycle
    _scan_chips.py      script: which axes 0-9 on the MCM6101 answer a
                        status request at all — axis 6 alone didn't
    _toggle_mirror_state.py  script: manual flip-and-restore of chip 7 —
                        confirms SET_MIRROR_STATE physically moves the
                        hardware (audible), always ends on the CAMERA/epi
                        default
    panel.py            manual Camera/PMT toggle — the operator asserts what
                        they set in ThorImage, since acqApp can't read it
    settings.py         MirrorSettings — no Qt
    startup.py          launch-time check: confirm chip 7's GR/CAMERA
                        channels are the CAMERA/epi default, silently
                        correct if not (no Qt, driver injectable)
  puffer/
    control.py
  pupil_cam/
    acquisition.py      Basler worker + mock twin
    autotune.py         Auto: suggest threshold/blur/reflection (+ eye region)
                        from a few frames; own segmentation, no EyeLoop, no Qt
    avi.py              uncompressed-AVI reader (no Qt); there is no decoder here
    clip.py             review's readers: AVI, a session .h5's pupil stream, TIFF
    control.py          eye-tracking LED
    eyeloop_tracker.py  the ONLY file that touches EyeLoop (GPL-3.0, not vendored)
    panel.py
    review.py           offline review model: track a whole clip, hand edits kept
                        apart from auto fits, sidecar save/load (no Qt)
    review_app.py       the review window as its own program (QApplication + main)
    review_dialog.py    ReviewWidget: the clip review (Review mode of the Pupil tab,
                        or its own window via review_app); scrub, fix, apply
    settings.py         camera, eye region, tracking + corneal-reflection knobs
    track_worker.py     tracking on its own thread; sole consumer of the frames
    tracking_panel.py   the tracking controls (region, fit, blinks, reflections),
                        one widget shared by panel.py and review_dialog.py
    tracking.py         settings + a frame in, a PupilFit out; no Qt, no EyeLoop
    video.py            third frame source: replay recorded footage
  stage/
    acquisition.py      read-only position poller; never issues motion
    backend.py          which driver is plugged in; probes the port and picks
    control.py          StageController + mock; microns, soft-limit clamped
    driver.py           MCM6101 APT/serial driver; copy of stage_control's
    fov_picker.py       dialog to choose a saved FOV bookmark: session list +
                        Browse, thumbnails — devices/dmd/roi_picker.py's shape
    fov_store.py        save/load named FOV bookmarks (position + snapshot);
                        session/archive rotation (no Qt)
    map_widget.py
    mcm301_driver.py    MCM301 driver (the current rig); wraps the vendor DLL
    mcm301_sdk/
      README.md         the vendored MCM301Lib_x64.dll (+ Thorlabs EULA): why
                        it is here, and how to update it
    panel.py
    settings.py         calibration, SHARED with ../../stage_control/config.json
    stage_config.json   fallback copy only — the live calibration is the shared one
  vis_stim/
    circle.py           the circle-in-a-region aperture geometry (regions.py
                        grid + region width -> diameter) tuning/contrast/size
                        all share, no Qt
    contrast.py         contrast-tuning trial: the 6 contrast levels swept +
                        pretrial count, no Qt
    control.py          VisStimController: priming -> per-trial trigger
                        gating, counted off the shared session clock's own
                        tick (no DAQ line, unlike the .m code) and replacing
                        its blocking while loop with one step per painted frame
    grating.py          gamma-corrected drifting sinusoid + aperture geometry,
                        no Qt — ported from visStimCode's genGratingTex
    panel.py
    regions.py          the 3x3 region grid the map/tuning/contrast/size
                        trial types share, no Qt
    settings.py         StimParams/LoopVar/VisStimSettings — no Qt
    size.py             size-tuning trial: fractions of the region's own
                        width swept + pretrial count, no Qt
    trials.py           loop variables -> full-factorial trial list, no Qt
    tuning.py           orientation-tuning trial: the 8 orientations swept +
                        pretrial count, no Qt
    window.py           the full-screen QWidget the grating paints into
  voltage_cam/
    _check_link.py      script: CoaXPress or USB3? run after any cabling change
    _probe_burst.py     script: MASTER PULSE BURST — N frames per edge, and
                        does it re-arm itself? Camera only; acqApp closed
    acquisition.py      ORCA worker + mock twin
    dcimg.py            DCAM's own recorder (dcamrec_*) by hand-written ctypes
                        — pylablib has the structs but binds no functions.
                        The driver writes the .dcimg, so those frames never
                        reach a Writer: preview survives, per-frame access
                        doesn't
    led.py              primary illumination LED on Dev3/port0/line2 + mock —
                        same shape as pupil_cam/control.py's LED controller
    panel.py
    presets.py          AcqConfig + the datasheet-derived resolution presets
  wheel/
    acquisition.py      NI encoder worker + mock; the derivation lives here
    analyze_raw.py      script: V/rev from a raw capture
    capture_raw.py      script: hardware-clocked 1 kHz raw capture
    panel.py
    settings.py
routines/               experiment routines: atomic steps (move/display/wait/
                        puff) executed in order, with a Recording sticker on
                        the one step the camera captures for (click its row
                        number to toggle)
  panel.py              the protocol, the run controls, and one Start button
  banner.py             large colour-coded run-state popup, driven by the
                        panel's state_shown signal; click to dismiss
  table.py              the step list: every cell edits through a widget that
                        can only produce a legal value; Kind picks what a row
                        does, unused columns render "—"
  engine.py             the executor — every actuation arrives as a callable,
                        so the whole of it is testable before light is emitted
  estimate.py           how long a routine takes — the one place frames become
                        seconds, and it says so; no Qt
  settings.py           Step / Group / Recording / Routine / validate() — no Qt
  templates.py          the saved-protocol library, one JSON file each — no Qt
  timeline.py           one cycle drawn to scale: a Group bracket, step
                        blocks by kind/duration, and recordings as SEPARATE
                        bars (one per repeat, never merged) — view-only
saving/                 where the session file goes
  bpod_dialog.py        the Save tab's "Match to Bpod…": Check, then Apply
  bpod_match.py         after a session: align the routine's edge log to Bpod's
                        trials, renumber folders, VOID missed trials (CLI too)
  config.py             SaveConfig + path building — no Qt
  panel.py
docs/
  AUDIT-2026-08.md      closed audit — archive — gitignored, private repo
  CAMERA_TRANSFER.md
  DECISIONS.md          closed items, kept for their reasoning — archive — gitignored, private repo
  EYELOOP-INTEGRATION.md  the plan for moving it into devices/ — start here
  EYELOOP.md            EyeLoop tried 2026-08-26 — and eyeloop-3.14-patches.diff
                        beside it, the only durable copy of the 4 patches
  HANDOFF.md
  PUPIL_CAMERA_TRANSFER.md
  README.md
  REFERENCE.md          layout, gotchas, ground rules (§2), architecture (§5b) — private repo
  RIG.md                this rig's hardware facts — read before touching a device — private repo
  SESSIONLOG.md         older session entries — archive — gitignored, private repo
  STAGE_TRANSFER.md
  STRUCTURE.md          this file
  USER_GUIDE.md         operator quick-start, screenshots in images/guide/
  WHEEL_TRANSFER.md
tests/                  plain scripts, not pytest; each file (and each part of a
                        multi-part file, `--part NAME`) runs in its own process
  _harness.py           Report, qt_app(), isolate_user_state(), run_parts()
  README.md             the two conventions: isolate user state, include a control
  run_all.py            the suite: run this
  test_camera.py              readout table, frame timestamps, .dcimg, recording losses
  test_config.py              rigs.json, modes.json, settings persistence, mirror default
  test_console_safety.py
  test_device_contracts.py    the Protocols in acq/devices.py, both directions
  test_dmd.py                 frames/controllers, calibration fit, sweep wiring, ROIs
  test_encoder.py             position -> speed/distance, hardware-timed reads
  test_modules.py             every module subset; loading/unloading in place
  test_pickers.py             the saved-ROI and saved-FOV pickers
  test_pupil.py               EyeLoop seam, tracking, eye region, clip replay, offline review, Auto, review/live mirror
  test_routines.py            routine engine/adapter/panel, and the timeline
  test_saving.py              save paths, split writer, direct-chunk HDF5 write
  test_session_recording.py
  test_stage.py               stage calibration persistence, the Z axis
  test_stage_panel.py
  test_stage_focus_ui.py      Z widgets and the calibration dialog's two-warning gate
  test_structure.py     this file vs the code
  test_undefined_names.py     every name resolves; catches the moved-code defect
  test_vis_stim.py            trial expansion, geometry, the tick-driven state machine
main.py                 the shell: window chrome, docks, theme, session start/stop,
                        the venv bootstrap. Holds no per-instrument logic.
config.py               settings persistence + the MODULES table
console.py              enable_safe_console() — every entry point calls it first
dialogs.py              module picker (startup + sidebar), device monitor,
                        settings dialog
run_pupil_review.py     launcher for the pupil review tool alone: makes its own venv,
                        fetches EyeLoop, runs from this checkout (stdlib only)
requirements-pupil.txt  the pupil tool's few dependencies (no DAQ/camera/DMD)
modes.json              named mode recipes (camera preset/exposure, DMD
                        all-on) main.py applies on selection
rigs.json               per-rig hardware profiles (NI device, DAQ channels,
                        what is fitted) — tracked; acqapp_local.json's "rig"
                        key names which one THIS machine is
probe.py                presence checks; enumeration only, never opens a device
style.py                the theme and the per-module HEX colours
widgets.py              shared panel widgets — the collapsible group box
CLAUDE.md               how to work in here — gitignored, private repo
PLAN.md                 the short plan: orientation + next three — private repo
notes.ps1               commit the private files, push both repos — private repo
README.md               the authoritative description
requirements.txt
.gitignore
acqapp_local.json       local settings — gitignored
newCal.json             the rig's active DMD calibration (dmd_calibration setting)
__init__.py
```

Not listed and deliberately so: `.venv/`, `__pycache__/`, `sessions/` (recordings),
`routine_templates/` (the operator's saved protocols, written by `routines/templates.py`),
anything else gitignored, and the per-package `__init__.py` — every package has
one, and only the two carrying logic are called out above (the adapter registry,
and the lazy PEP 562 re-exports in `routines/` and `saving/`). Raw rig captures live **outside** the repo in
`../../rig_captures/`.

## Adding a module

Three registrations, not the two that used to be documented:
`adapters.ADAPTERS`, `config.MODULES`, and a `style.HEX` colour — the third only
showed up as a `KeyError` at build time. Then a new file in `adapters/`, a
package in `devices/`, and an entry in the tree above.

Two optional declarations on the adapter, both read by the shell and neither
naming a module in `main.py`: **`own_window`** puts its panel in a window of
its own instead of a settings page, and **`config.ALWAYS_ON`** keeps it out of
the module picker. `routines` sets both.
