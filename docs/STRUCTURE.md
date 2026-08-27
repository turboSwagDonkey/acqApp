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
    closed_loop["closed_loop/<br/><i>fire an output from a signal</i>"]
    routines["routines/<br/><i>run a protocol step by step</i>"]
    saving["saving/<br/><i>where the file goes</i>"]
    acq["acq/<br/><i>clock · recorder · ring · writer · protocols</i>"]
    dialogs["dialogs.py<br/><i>module picker · device monitor</i>"]
    probe["probe.py<br/><i>is a device present?</i>"]
    config["config.py"]
    style["style.py"]
    console["console.py"]
    widgets["widgets.py<br/><i>shared panel widgets</i>"]

    main --> adapters
    main --> saving
    main --> dialogs
    main --> acq
    main --> config
    main --> style
    main --> console
    adapters --> devices
    adapters --> closed_loop
    adapters --> routines
    adapters --> acq
    adapters --> config
    adapters --> style
    devices --> acq
    devices --> console
    devices --> style
    closed_loop --> acq
    closed_loop --> style
    routines --> style
    dialogs --> config
    dialogs --> probe
    dialogs --> style
    dialogs --> widgets
    probe --> devices
```

Two edges surprise people, so they are drawn rather than explained away:
`probe.py → devices` (the DMD probe resolves the ALP path through
`devices/dmd/alp.py`), and `adapters → closed_loop` (the loop is a module like
any other, and its adapter is what arms it).

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
  pupil_tracking/       the pupil tracker, retired 2026-08-24 (PLAN §7 (ai))
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
adapters/               one ModuleAdapter per subsystem — tab, plot, worker,
                        display tick, recording sink, metadata
  __init__.py           the registry (ADAPTERS) and the lifecycle table
  base.py               ModuleAdapter itself + the two shared widget builders
  closed_loop.py
  dmd.py
  puffer.py
  pupil_cam.py
  routines.py           the ONLY routine code that touches a real device
  stage.py
  voltage_cam.py
  wheel.py
closed_loop/            phase 5: watch one module's signal, fire another's output
  panel.py
  settings.py           LoopRule / LoopSettings — no Qt
  worker.py
devices/                one package per instrument
  dmd/
    _roi_editor.py      script: open roi_panel's editor alone, no rig, no light
    alp.py              all Vialux ALP knowledge, Qt-free; build_frame is the
                        one a mispositioned stimulus would come from
    calibration.py      DMD↔camera registration: stripes in, affine out. Pure,
                        so it is testable before any light is emitted
    control.py          panel-facing controller + mock twin
    panel.py
    roi.py              stimulation ROIs in camera px (no Qt); rect and circle
    roi_panel.py        draw and edit ROIs over a snapshot
    sweep.py            runs calibration.py against the rig: the fresh-frame
                        grabber and the dialog that asks before emitting light
  puffer/
    control.py
  pupil_cam/
    acquisition.py      Basler worker + mock twin
    avi.py              uncompressed-AVI reader (no Qt); there is no decoder here
    control.py          eye-tracking LED
    eyeloop_tracker.py  the ONLY file that touches EyeLoop (GPL-3.0, not vendored)
    panel.py
    settings.py         camera, eye region, tracking + corneal-reflection knobs
    track_worker.py     tracking on its own thread; sole consumer of the frames
    tracking.py         settings + a frame in, a PupilFit out; no Qt, no EyeLoop
    video.py            third frame source: replay recorded footage
  stage/
    acquisition.py      read-only position poller; never issues motion
    control.py          StageController + mock; microns, soft-limit clamped
    driver.py           MCM6101 APT/serial driver; copy of stage_control's
    map_widget.py
    panel.py
    settings.py         calibration, SHARED with ../../stage_control/config.json
    stage_config.json   fallback copy only — the live calibration is the shared one
  voltage_cam/
    _check_link.py      script: CoaXPress or USB3? run after any cabling change
    acquisition.py      ORCA worker + mock twin
    panel.py
    presets.py          AcqConfig + the datasheet-derived resolution presets
  wheel/
    acquisition.py      NI encoder worker + mock; the derivation lives here
    analyze_raw.py      script: V/rev from a raw capture
    capture_raw.py      script: hardware-clocked 1 kHz raw capture
    panel.py
    settings.py
routines/               experiment routines: a protocol executed in order
  panel.py              the protocol, the run controls, and one Start button
  table.py              the step list: every cell edits through a widget that
                        can only produce a legal value
  engine.py             the executor — every actuation arrives as a callable,
                        so the whole of it is testable before light is emitted
  estimate.py           how long a routine takes — the one place frames become
                        seconds, and it says so; no Qt
  settings.py           Step / Routine / validate() — no Qt
  templates.py          the saved-protocol library, one JSON file each — no Qt
saving/                 where the session file goes
  config.py             SaveConfig + path building — no Qt
  panel.py
docs/
  AUDIT-2026-08.md      closed audit — archive
  CAMERA_TRANSFER.md
  DECISIONS.md          closed items, kept for their reasoning — archive
  EYELOOP-INTEGRATION.md  the plan for moving it into devices/ — start here
  EYELOOP.md            EyeLoop tried 2026-08-26 — and eyeloop-3.14-patches.diff
                        beside it, the only durable copy of the 4 patches
  HANDOFF.md
  PUPIL_CAMERA_TRANSFER.md
  README.md
  SESSIONLOG.md         older session entries — archive
  STAGE_TRANSFER.md
  STRUCTURE.md          this file
  WHEEL_TRANSFER.md
tests/                  plain scripts, not pytest; each runs in its own process
  _harness.py           Report, qt_app(), isolate_user_state()
  README.md             the two conventions: isolate user state, include a control
  run_all.py            the suite: run this
  test_camera_timestamps.py
  test_closed_loop.py
  test_console_safety.py
  test_device_contracts.py    the Protocols in acq/devices.py, both directions
  test_dmd.py
  test_dmd_calibration.py
  test_dmd_roi.py
  test_dmd_sweep.py           the wiring: fresh grabs, untransformed patterns
  test_encoder_derive.py
  test_encoder_timing.py
  test_module_hotload.py       loading instruments without restarting
  test_module_subsets.py
  test_pupil_eyeloop.py       EyeLoop through the app path; every check has a control
  test_pupil_track.py         tracking through the app: thread, trace, overlay, file
  test_pupil_limit.py         the eye region: panel, preview, persistence
  test_pupil_video.py
  test_readout_fps.py
  test_routines.py            the routine engine, on a fake rig and a fake clock
  test_recording_losses.py
  test_save_paths.py
  test_session_recording.py
  test_settings_persistence.py
  test_stage_panel.py
  test_stage_state.py
  test_structure.py     this file vs the code
  test_undefined_names.py     every name resolves; catches the moved-code defect
  test_writer_chunks.py       the direct-chunk write, and the guard on it
main.py                 the shell: window chrome, docks, theme, session start/stop,
                        the venv bootstrap. Holds no per-instrument logic.
config.py               settings persistence + the MODULES table
console.py              enable_safe_console() — every entry point calls it first
dialogs.py              module picker (startup + sidebar), device monitor,
                        settings dialog
probe.py                presence checks; enumeration only, never opens a device
style.py                the theme and the per-module HEX colours
widgets.py              shared panel widgets — the collapsible group box
CLAUDE.md               how to work in here
PLAN.md                 the living plan — read first
README.md               the authoritative description
requirements.txt
.gitignore
acqapp_local.json       local settings — gitignored
__init__.py
```

Not listed and deliberately so: `.venv/`, `__pycache__/`, `sessions/` (recordings),
`routine_templates/` (the operator's saved protocols, written by `routines/templates.py`),
anything else gitignored, and the per-package `__init__.py` — every package has
one, and only the two carrying logic are called out above (the adapter registry,
and the lazy PEP 562 re-exports in `closed_loop/`, `routines/` and `saving/`). Raw rig captures live **outside** the repo in
`../../rig_captures/`.

## Adding a module

Three registrations, not the two that used to be documented:
`adapters.ADAPTERS`, `config.MODULES`, and a `style.HEX` colour — the third only
showed up as a `KeyError` at build time. Then a new file in `adapters/`, a
package in `devices/`, and an entry in the tree above.
