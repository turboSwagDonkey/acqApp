# acqApp

Multi-instrument acquisition app for the ICN rig: DMD pattern control +
Hamamatsu Orca Fire camera + rotary wheel encoder (NI PCIe-6363 "Dev3").

Built by extending the existing PyQt6 DMD control app (`dmdGUI_project`) into a
unified, dockable acquisition interface with synchronized recording.

## Status: Phase 0 — hardware de-risk

Standalone scripts in `scratch/` that prove each device talks to Python before
any app code is written. **These get deleted once Phase 0 is validated.**

| Script | Purpose |
|--------|---------|
| `scratch/cam_grab.py` | Grab N frames off the Orca, measure achieved FPS + MB/s |
| `scratch/encoder_read.py` | Live position/speed read as analog voltage off Dev3/ai2 |

### Running Phase 0 (on the rig machine, with hardware)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# camera throughput test
.venv\Scripts\python scratch\cam_grab.py --frames 200 --exposure 0.005 --save

# encoder live read (analog voltage on Dev3/ai2; pass --volts-per-rev to scale)
.venv\Scripts\python scratch\encoder_read.py --chan Dev3/ai2 --rate 50
```

Vendor runtime drivers must be installed (see `requirements.txt`):
NI-DAQmx (encoder) and Hamamatsu DCAM-API (camera).

## Roadmap

- **Phase 0** — hardware de-risk (current)
- **Phase 1** — skeleton: `SessionClock` interface, `Recorder`/`Writer`, dockable
  `QMainWindow`, copy & adapt DMD control
- **Phase 2** — camera streaming + live preview + HDF5 disk writer
- **Phase 3** — encoder streaming + live plot
- **Phase 4** — unified session Start/Stop, software-timestamp sync, metadata
- **Phase 5** — closed-loop: trigger DMD from encoder state
- **Phase 6** — hardware sync upgrade (DaqClock on the PCIe-6363, triggered Orca)

## Sync design note

All devices timestamp data via a central `SessionClock`. v1 uses software
timestamps (`perf_counter`); the PCIe-6363 makes a future hardware-clocked
`DaqClock` a drop-in swap without touching device code.
