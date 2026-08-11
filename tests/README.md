# acqApp tests

```
acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py          # everything
acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py -v       # full output
acqApp\.venv\Scripts\python.exe acqApp\tests\run_all.py session  # one test
```

Everything runs in **Emulate mode against fakes** — no rig hardware, no windows
on screen, ~17 s for the set. Each test is also runnable on its own.

Plain scripts, not pytest: pytest is not in `requirements.txt` and the rig
machine installs only what's there. `run_all.py` gives each test its own process
(three of them build a `QApplication`, and tearing one down and rebuilding it in
a single process is not reliable) and prints a summary.

## What each one defends

| test | guards |
|---|---|
| `test_console_safety` | A diagnostic print containing `→` / `≤` / `⚠` raises `UnicodeEncodeError` on a non-UTF-8 console. Those prints sit inside the acquisition loops, so it surfaces as a *device failure* — the camera just doesn't start. Checks every entry point calls `enable_safe_console()`, and that the real path survives a forced cp1252 console. |
| `test_camera_timestamps` | The camera reads frames in batches. Stamping them on arrival gave every frame in a batch the same time, quantising the recorded timebase to the read cadence instead of the frame rate. Drives the real worker against a fake batching camera and checks intervals track the frame rate, and that a dropped frame shows up in `voltage_cam_index`. |
| `test_module_subsets` | Any combination of instruments can be loaded, so each `ModuleAdapter` has to cope with its neighbours being absent — including the voltage camera, which owns the central view. Builds a window per subset, runs a session, toggles Emulate. |
| `test_session_recording` | The broad net: full session → record → stop → verify the HDF5. Every expected stream present, timestamps monotonic and trimmed, metadata written (including the close-time attributes), and the puffer panel's channel/duration actually reaching the hardware. |

## Two things to know before adding a test

**Isolate user state.** The GUI tests drive the *real* `MainWindow`, which
persists things as a side effect of ordinary use: the Save tab rewrites
`acqapp_local.json` on every field edit, and closing the window writes the dock
layout to `QSettings`. Any test that builds a window must call
`_harness.isolate_user_state()` first, or it will silently overwrite the
operator's save folder, subject ID and panel arrangement. `test_session_recording`
asserts this redirection actually happened, so a regression in the harness is
caught rather than assumed.

**Include a control where the test could be vacuous.** `test_camera_timestamps`
re-runs the identical fake camera through the old arrival-stamping behaviour and
asserts it *fails* the same checks; `test_console_safety` asserts the unhardened
path still dies on cp1252. Without those, both tests would keep passing if the
condition they detect quietly stopped being reachable.

## Not covered

These test logic against fakes. They cannot tell you that DCAM populates
`timestamp_us` on the real ORCA-Fire, that the NI lines are wired to what the
config says, or that the stage's serial framing matches the firmware. Those need
the rig — see the `_toy.py` harness in each package for bringing one device up
on its own.
