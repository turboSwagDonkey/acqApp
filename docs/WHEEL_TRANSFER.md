# Wheel Encoder Module — Transfer Document

Handoff for a session focused on the **running-wheel encoder** in `acqApp`.
Companion to [CAMERA_TRANSFER.md](CAMERA_TRANSFER.md) and
[PUPIL_CAMERA_TRANSFER.md](PUPIL_CAMERA_TRANSFER.md).

---

## 1. What the wheel module is

Measures the mouse's **locomotion** on a running wheel, read as an **analog
voltage** on the NI DAQ (`Dev3/ai2`). It's the simplest module — a single scalar
stream, no image, no device SDK (just `nidaqmx`). Two contexts:

- **Bring the encoder up alone** — start the app, tick only this module in the
  startup picker, and press **Live view**: voltage and speed traces with
  nothing else loaded. Where wheel work should happen first. It replaced
  `wheel/_toy.py`, which duplicated the panel and had drifted from it.
- **Main app** — `main.py`: recorded as the `/wheel` scalar stream on the shared
  clock; shown as the "Wheel velocity" trace in the Signals dock.

The rig NI board is a **PCIe-6363 ("Dev3")**, shared with the puffer (`port0/
line0`) and the eye-tracking LED (`port0/line1`); the wheel uses analog-in `ai2`.

---

## 2. How to run

Project venv Python, real display:

```powershell
# from C:\Users\User\Desktop\python
acqApp\.venv\Scripts\python.exe acqApp\wheel\_toy.py           # real NI DAQ
acqApp\.venv\Scripts\python.exe acqApp\wheel\_toy.py --mock    # synthetic sine
```

Real hardware needs **NI-DAQmx + `nidaqmx`** and the 6363 present as `Dev3`
(check with the app's 🔌 Devices monitor — it currently reports
`Dev3 PCIe-6363` connected). `--mock` needs neither.

---

## 3. Relevant files

| File | Role |
|------|------|
| [devices/wheel/acquisition.py](../devices/wheel/acquisition.py) | `EncoderWorker` (real NI ai) + `MockEncoderWorker`. The poll thread. |
| [devices/wheel/settings.py](../devices/wheel/settings.py) | `EncoderSettings` + `SettingsPanel` — channel, sample rate, and (unused) scaling fields. |
| Live view, one module ticked | Brings the encoder up alone — what `wheel/_toy.py` used to do. (That toy never wrote CSV; this table said it did.) |
| ~~wheel/recording.py~~ | Deleted 2026-08-13 with the toy it served. The app records via the shared pipeline. |
| [acq/worker.py](../acq/worker.py) | `PullWorker` base both workers subclass. |
| [main.py](../main.py) | Builds the worker in `_start_session` (real vs mock by the Emulate toggle), records `/wheel`, plots it in `_pull_frames`. |

---

## 4. Architecture

### Worker (`EncoderWorker` / `MockEncoderWorker`)
- Subclass `PullWorker` (a `QThread`). `run()` opens an NI `Task`, adds an
  analog-voltage channel (`Dev3/ai2`, **RSE**, ±10 V), then loops at the
  configured rate: `task.read()` → one sample → `self._publish((voltage,
  elapsed), record=voltage)`.
- `_publish` updates the newest-sample snapshot **and** feeds the recording sink
  the raw **voltage**. GUI pulls `get_latest()` → `(voltage, elapsed_s)`.
- `fps_update = pyqtSignal(float)` reports samples/s.
- **Software-timed**: it's a Python loop with `time.sleep` to pace to `rate` Hz
  (default 50 Hz), one `task.read()` per iteration — *not* a hardware-clocked
  acquisition. Fine for behaviour at ≤ a few hundred Hz; see §6.
- Recording timestamps come from the **shared SessionClock** (stamped in the
  sink at acquisition time), so wheel samples align with the camera/pupil/etc.
- **Mock**: a 0.2 Hz sine over ±2.5 V at 50 Hz — no hardware.

### Settings (`EncoderSettings` / `SettingsPanel`)
- Fields: `channel`, `rate` (Hz), `volts_per_rev`, `wheel_dia_mm`.
- **`channel` and `rate` are applied** (passed into `EncoderWorker`).
- **`volts_per_rev` and `wheel_dia_mm` are NOT used anywhere yet** — the panel
  collects them but nothing converts voltage to real units (see §6.1).

### Recording
- **Toy**: `CSVWriter` → `toy_output/wheel.csv` (`elapsed_s, voltage`).
- **Main app**: worker sink → shared `Recorder` → `/wheel/values` (float64
  voltage) + `/wheel/timestamps` on the session clock.

---

## 5. Current state

**Verified (mock/headless):** worker on `PullWorker`, `/wheel` records on the
shared clock, plots live, Emulate toggle picks real vs mock.

**Real hardware:** the 6363 is present and the app's monitor sees it, but a real
`ai2` read has **not been verified through this code** — the toy is the place to
confirm the wheel actually produces the expected voltage swing.

---

## 6. Open questions / next steps

1. **Record real units, not raw volts (the big one).** `volts_per_rev` and
   `wheel_dia_mm` exist in settings but are dead. Decide the physical meaning of
   the `ai2` voltage first (see #3), then convert in the worker/sink to a
   meaningful stream — angular velocity (rev/s), or linear speed
   (`mm/s = rev/s · π · wheel_dia_mm`) / cumulative distance. Consider recording
   both raw voltage *and* the derived unit so nothing is lost. This is the
   roadmap's "encoder scaling to real units" item.
2. **What is the sensor / what does the voltage mean?** This reads an *analog*
   voltage, so it isn't a quadrature encoder on counter inputs — likely an
   analog velocity/tacho output or a potentiometer angle. The main-app plot is
   labelled "Wheel velocity" but the recorded value is voltage, and the toy
   derives speed by differentiating voltage — clarify whether the voltage is
   **velocity** (use directly) or **position/angle** (differentiate) and make
   the code + labels consistent.
3. ~~**Hardware-clocked acquisition.**~~ **Done (#13, 2026-08-12.)**
   `EncoderWorker` now uses `cfg_samp_clk_timing` + continuous block reads,
   anchors the first block to the session clock and spaces the rest by the
   board's rate; `wheel_timestamp_source` records whether that worked. The
   software-paced loop survives only as the fallback. Sections 2–5 below and §7
   describe the *pre-#13* worker and have not been rewritten — the code wins.
4. **Direction / range.** RSE ±10 V is configured; confirm the wheel signal is
   bipolar (bidirectional running) or unipolar, and set `min_val`/`max_val`
   to the real range for best ADC resolution.
5. **Settings persistence.** The wheel panel isn't wired to
   `config.load_settings`/`save_settings` yet — only `voltage_cam` is (it's the
   template). Wire channel/rate/scaling so they stick across runs.
6. **Plot semantics.** Once units are decided, relabel the Signals-dock trace
   accordingly (velocity vs voltage) and set sensible axis units.

---

## 7. Handy facts

- Channel `Dev3/ai2`, **RSE**, ±10 V. *(As of #13 the rate is the board's own
  sample clock, default 120 Hz, and `get_latest()` → `(voltage, speed,
  distance, elapsed_s)` while the sink gets `(voltage, speed, distance,
  acquired_at)` into `/wheel_voltage`, `/wheel_speed`, `/wheel_distance`. The
  two lines below are the original wiring, kept for the history.)*
- `get_latest()` → `(voltage, elapsed_s)`; the recording sink gets **voltage**
  only. Recorded as `/wheel/values` (float64).
- Mock: 0.2 Hz sine, ±2.5 V, 50 Hz — no hardware, matches the real API.
- The NI board is shared: wheel `ai2`, puffer `port0/line0`, LED `port0/line1` —
  different subsystems, no line conflict.
- Emulate toggle (status bar) selects `MockEncoderWorker` vs `EncoderWorker` at
  session start.
