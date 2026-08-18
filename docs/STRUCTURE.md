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
    saving["saving/<br/><i>where the file goes</i>"]
    acq["acq/<br/><i>clock · recorder · ring · writer · protocols</i>"]
    dialogs["dialogs.py<br/><i>startup picker · device monitor</i>"]
    probe["probe.py<br/><i>is a device present?</i>"]
    config["config.py"]
    style["style.py"]
    console["console.py"]

    main --> adapters
    main --> saving
    main --> dialogs
    main --> acq
    main --> config
    main --> style
    main --> console
    adapters --> devices
    adapters --> closed_loop
    adapters --> acq
    adapters --> config
    adapters --> style
    devices --> acq
    devices --> console
    devices --> style
    closed_loop --> acq
    closed_loop --> style
    dialogs --> config
    dialogs --> probe
    dialogs --> style
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
adapters/               one ModuleAdapter per subsystem — tab, plot, worker,
                        display tick, recording sink, metadata
  __init__.py           the registry (ADAPTERS) and the lifecycle table
  base.py               ModuleAdapter itself + the two shared widget builders
  closed_loop.py
  dmd.py
  puffer.py
  pupil_cam.py
  stage.py
  voltage_cam.py
  wheel.py
closed_loop/            phase 5: watch one module's signal, fire another's output
  panel.py
  settings.py           LoopRule / LoopSettings — no Qt
  worker.py
devices/                one package per instrument
  dmd/
    alp.py              all Vialux ALP knowledge, Qt-free; build_frame is the
                        one a mispositioned stimulus would come from
    control.py          panel-facing controller + mock twin
    panel.py
  puffer/
    control.py
  pupil_cam/
    _test_tracking.py   script: tracker vs synthetic ground truth, no hardware
    acquisition.py      Basler worker + mock twin
    avi.py              uncompressed-AVI reader (no Qt); there is no decoder here
    control.py          eye-tracking LED
    fits.py             circle/ellipse fitting
    panel.py
    rays.py             radial edge search
    settings.py
    track_worker.py     tracking gets its own thread — it is unbounded work
    tracking.py         the pupil algorithm (IMAQ Find Circular Edge port)
    video.py            third frame source: replay footage to tune the tracker
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
saving/                 where the session file goes
  config.py             SaveConfig + path building — no Qt
  panel.py
docs/
  AUDIT-2026-08.md      closed audit — archive
  CAMERA_TRANSFER.md
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
  test_encoder_derive.py
  test_encoder_timing.py
  test_module_subsets.py
  test_pupil_fits.py
  test_pupil_tracking_thread.py
  test_pupil_video.py
  test_readout_fps.py
  test_recording_losses.py
  test_save_paths.py
  test_session_recording.py
  test_settings_persistence.py
  test_stage_panel.py
  test_stage_state.py
  test_structure.py     this file vs the code
  test_undefined_names.py     every name resolves; catches the moved-code defect
main.py                 the shell: window chrome, docks, theme, session start/stop,
                        the venv bootstrap. Holds no per-instrument logic.
config.py               settings persistence + the MODULES table
console.py              enable_safe_console() — every entry point calls it first
dialogs.py              startup module picker, device monitor, settings dialog
probe.py                presence checks; enumeration only, never opens a device
style.py                the theme and the per-module HEX colours
CLAUDE.md               how to work in here
PLAN.md                 the living plan — read first
README.md               the authoritative description
requirements.txt
.gitignore
acqapp_local.json       local settings — gitignored
__init__.py
```

Not listed and deliberately so: `.venv/`, `__pycache__/`, `sessions/` (recordings),
anything else gitignored, and the per-package `__init__.py` — every package has
one, and only the two carrying logic are called out above (the adapter registry,
and the lazy PEP 562 re-exports in `closed_loop/` and `saving/`). Raw rig captures live **outside** the repo in
`../../rig_captures/`.

## Adding a module

Three registrations, not the two that used to be documented:
`adapters.ADAPTERS`, `config.MODULES`, and a `style.HEX` colour — the third only
showed up as a `KeyError` at build time. Then a new file in `adapters/`, a
package in `devices/`, and an entry in the tree above.
