# closed_loop — retired 2026-10-01

Watched one module's live signal (e.g. wheel speed) and fired another's output
(puffer, DMD) when a rule held — PLAN's "phase 5". Retired at the operator's
request: never used on the rig. Nothing here is imported; it will not run in
place (its imports still point at `acqApp.closed_loop`).

What stayed in the app: `SignalSource`, now in `acq/devices.py`, and
`ModuleAdapter.signal_sources()` / `MainWindow.signal_sources()` — vis-stim's
visuomotor mode reads the wheel through them.

To restore: `git revert` the commit that moved these files here, or move them
back (`adapter.py` → `adapters/closed_loop.py`, the rest → `closed_loop/`,
`tests/test_closed_loop.py` → `tests/`), re-register `"closed_loop"` in
`config.MODULES`, `adapters.ADAPTERS`, `style.HEX`, `probe.py` and
`tests/run_all.py`, and point `SignalSource` imports at `acq/devices.py`.
