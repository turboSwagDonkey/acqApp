"""The broad net: the real MainWindow, all modules, Emulate — Live view ->
Record -> puff -> DMD -> stop -> close, then the HDF5 must hold every stream on
one timebase with the metadata that makes it interpretable later.
"""
from __future__ import annotations

import shutil
import sys

from _harness import MemorySettings, Report, isolate_user_state, make_window, pump, qt_app

EXPECTED_STREAMS = [
    "voltage_cam", "voltage_cam_index", "pupil_cam",
    # Written even without EyeLoop (NaN rows): a gap must show in the file.
    "pupil_x", "pupil_y", "pupil_major", "pupil_minor", "pupil_angle",
    "wheel_voltage", "wheel_speed", "wheel_distance",
    "stage_x_um", "stage_y_um", "puffer", "dmd",
]
PUPIL_FIT_STREAMS = ["pupil_x", "pupil_y", "pupil_major", "pupil_minor",
                     "pupil_angle"]

PUPIL_THRESHOLD = 57                    # not the default, so a stuck one shows

CONFIG_ATTRS = ["created", "emulated", "modules", "mouse_id", "cam_exposure_us",
                "wheel_rate_hz", "pupil_rate_hz", "stage_port", "dmd_on_time_ms",
                "puffer_channel", "puffer_duration_s",
                # The threshold SETS the radius; without it no reproduction.
                "pupil_track_threshold", "pupil_track_model"]

# Written at close: what the run did, not how it was configured.
FINAL_ATTRS = ["cam_timestamp_source", "cam_dropped_frames",
               "wheel_timestamp_source", "wheel_rate_actual_hz",
               "recorder_dropped_samples", "recorder_late_samples",
               "recorder_unstamped_samples",
               # A fit slower than the camera drops frames; say by how much.
               "pupil_frames_tracked", "pupil_fits"]

# Not the defaults, and not a line the puffer or either LED already claims.
TEST_CHANNEL = "Dev3/port0/line3"
TEST_DURATION = 0.250


def main() -> int:
    r = Report("session")
    tmp = isolate_user_state()
    out = tmp / "recordings"

    # --mock before importing main: its module-level pre-init probes DCAM.
    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from acqApp import config

    enabled = set(config.MODULES)
    r.note(f"modules: {sorted(enabled)}")
    win = make_window(enabled)
    mod = {m.key: m for m in win._modules}

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_mouse_id.setText("smoke")
    win._save_panel._ed_template.setText("{mouse_id}_{date}_{time}")
    win._save_panel._on_edited()
    r.check(win._save_panel.writable_error() is None, "save target is writable")

    # Unisolated, this would repoint the operator's save folder at a temp dir.
    tmp_cfg = tmp / "acqapp_local.json"
    r.check(tmp_cfg.is_file() and "smoke" in tmp_cfg.read_text(encoding="utf-8"),
            "panel edits persisted to the isolated config, not the user's")

    # Control: pupil_cam's override off, before Live view, proves the checkbox
    # is not ignored.
    r.check(mod["pupil_cam"].panel.settings.led_follow_live,
            "pupil_cam's Follow Live view defaults on")
    mod["pupil_cam"].panel._chk_led_follow.setChecked(False)

    # ── Live view ────────────────────────────────────────────────────────────
    win._btn_run.setChecked(True)
    r.check(win._sync.running, "session clock running after Live view")
    pump(app, 1.0)
    for _ in range(5):
        win._display_tick()
        pump(app, 0.05)
    # ── LEDs: Follow Live view ────────────────────────────────────────────
    r.check(mod["voltage_cam"].panel.get_config().led_follow_live,
            "voltage_cam's Follow Live view defaults on")
    r.check(mod["voltage_cam"].controller.is_on,
            "primary LED followed Live view on")
    r.check(not mod["pupil_cam"].controller.is_on,
            "eye-tracking LED did NOT follow Live view (explicitly overridden off)")

    # Without an eye region nothing is tracked (mock frame is 240x320). Before
    # Record, so the trace covers the file.
    pupil_panel = mod["pupil_cam"].panel
    pupil_panel.set_limit(60.0, 20.0, 260.0, 220.0)
    pupil_panel._chk_track.setChecked(True)
    pupil_panel._spn_thr.setValue(PUPIL_THRESHOLD)
    r.check(mod["pupil_cam"].panel.settings.track, "pupil tracking is on")

    r.check(len(mod["voltage_cam"]._y) > 0, "voltage-cam ΔF/F reached the plot")
    r.check(len(mod["wheel"]._y) > 0, "wheel samples reached the plot")
    r.check(mod["pupil_cam"]._img.image is not None,
            "pupil frames reached the preview")

    # ── Puffer: the panel must actually drive the controller ─────────────────
    puffer = mod["puffer"].controller
    mod["puffer"].panel._cmb_chan.setCurrentText(TEST_CHANNEL)
    mod["puffer"].panel._spn_dur.setValue(TEST_DURATION)
    r.check(puffer.settings.channel == TEST_CHANNEL,
            "puffer channel edit reached the controller")
    r.check(abs(puffer.settings.duration_s - TEST_DURATION) < 1e-9,
            "puffer duration edit reached the controller")
    fired: list[float] = []
    puffer.puff_fired.connect(lambda _t, d: fired.append(d))

    # ── Record ───────────────────────────────────────────────────────────────
    win._btn_rec.setChecked(True)
    r.check(win._recorder is not None, "recorder created")
    # Not re-resolved: the template is second-granular.
    path = win._rec_path
    if not r.check(path is not None, "window reported the recording path"):
        return r.finish()

    mod["puffer"].fire()                    # a "Test puff": no explicit duration
    r.check(fired == [TEST_DURATION],
            f"test puff used the panel duration (got {fired})")
    mod["dmd"].load(None)
    mod["dmd"].display()                    # exercise the DMD frame sink

    pump(app, 2.0)
    for _ in range(10):
        win._display_tick()
        pump(app, 0.03)
    drops = win._recorder.drop_count if win._recorder else -1

    win._btn_rec.setChecked(False)
    r.check(win._recorder is None, "recorder cleared after stop")
    mod["dmd"].stop_display()
    win._btn_run.setChecked(False)
    r.check(not win._sync.running, "session clock stopped")
    # The stage's poll worker lives with the connection, not the session, so
    # jogging works with no Live view running.
    r.check(all(m.worker is None for m in win._modules if m.key != "stage"),
            "every session-scoped worker is released")
    r.check(mod["stage"].worker is not None,
            "…but the stage's poll worker survives Live view stopping")
    r.check(not mod["voltage_cam"].controller.is_on,
            "primary LED followed Live view off again")

    win.close()
    pump(app, 0.2)
    # Written, not just substituted: proves the substitute is on the real path.
    r.check("dockState" in MemorySettings.store,
            "dock layout written to the substituted QSettings, not the user's")
    import PyQt6.QtCore
    r.check(PyQt6.QtCore.QSettings is MemorySettings,
            "QSettings substitution is in place")
    # Each writing module binds its own QSettings; one bound before the patch
    # writes the operator's registry, invisibly to any check on `main`.
    import acqApp.dialogs
    import acqApp.main
    for mod in (acqApp.main, acqApp.dialogs):
        r.check(getattr(mod, "QSettings", MemorySettings) is MemorySettings,
                f"{mod.__name__} uses the substituted QSettings")
    r.check(acqApp.dialogs.SettingsDialog._GEOM_KEY in MemorySettings.store,
            "settings-window geometry written to the substitute too")

    # ── Verify the file ──────────────────────────────────────────────────────
    r.note(f"file: {path.name}  (drops while recording: {drops})")
    if not r.check(path.is_file(), "HDF5 file was written"):
        return r.finish()

    import h5py
    import numpy as np
    with h5py.File(path, "r") as f:
        r.note(f"streams: {sorted(f.keys())}")
        for name in EXPECTED_STREAMS:
            if not r.check(name in f, f"stream '{name}' present"):
                continue
            g = f[name]
            ts = g["timestamps"][:]
            data = g["frames"][:] if "frames" in g else g["values"][:]
            ok = (len(ts) > 0 and len(ts) == len(data)
                  and np.all(np.isfinite(ts)) and np.all(np.diff(ts) >= 0))
            r.check(ok, f"stream '{name}': n={len(ts)}, monotonic and trimmed")

        attrs = dict(f.attrs)
        for key in CONFIG_ATTRS:
            r.check(key in attrs, f"metadata attr '{key}'")

        # All used to be str()-ed: `emulated` read back as truthy "False".
        NUM   = (int, float, np.integer, np.floating)
        BOOL  = (bool, np.bool_)
        for key, kind, label in (
            ("cam_exposure_us",          NUM,   "number"),
            ("cam_binning",              NUM,   "number"),
            ("wheel_rate_hz",            NUM,   "number"),
            ("wheel_volts_per_rev",      NUM,   "number"),
            ("recorder_dropped_samples", NUM,   "number"),
            ("dmd_static_hold",          BOOL,  "bool"),
            ("emulated",                 BOOL,  "bool"),
            ("cam_preset",               (str,), "string"),
        ):
            got = attrs.get(key)
            r.check(isinstance(got, kind),
                    f"attr '{key}' reads back as a {label} "
                    f"(got {type(got).__name__}: {got!r})")
        r.check(attrs.get("puffer_channel") == TEST_CHANNEL,
                f"puffer channel recorded (got {attrs.get('puffer_channel')!r})")
        for key in FINAL_ATTRS:
            r.check(key in attrs, f"close-time metadata attr '{key}'")
        # The placeholder written at open is overwritten at close.
        r.check(attrs.get("cam_timestamp_source") == "camera",
                f"cam_timestamp_source resolved "
                f"(got {attrs.get('cam_timestamp_source')!r})")
        # The mock encoder is sleep-paced; the file must not claim a clock.
        r.check(attrs.get("wheel_timestamp_source") == "software",
                f"the mock wheel admits a software timebase "
                f"(got {attrs.get('wheel_timestamp_source')!r})")

        # Acquisition instants, not the writer thread's arrival times.
        fts = f["voltage_cam"]["timestamps"][:]
        fdt = np.diff(fts)
        r.check(bool(np.all(fdt > 0)), "no two camera frames share a timestamp")
        r.info(f"camera frame interval: mean {fdt.mean()*1e3:.1f} ms "
               f"std {fdt.std()*1e3:.2f} ms")
        # One of each per tracked frame: a missing semi-axis is no ellipse.
        ns = {k: len(f[k]["values"]) for k in PUPIL_FIT_STREAMS if k in f}
        r.check(len(set(ns.values())) == 1 and min(ns.values()) > 0,
                f"the five pupil streams are the same length ({ns})")
        r.check(len(f["pupil_x"]["values"]) <= len(f["pupil_cam"]["timestamps"]),
                "…and never more samples than there were frames "
                f"({len(f['pupil_x']['values'])} vs "
                f"{len(f['pupil_cam']['timestamps'])})")
        r.check(attrs.get("pupil_track_threshold") == PUPIL_THRESHOLD,
                f"the threshold behind the trace is the one that was set "
                f"(got {attrs.get('pupil_track_threshold')})")
        r.check(attrs.get("pupil_fits") <= attrs.get("pupil_frames_tracked"),
                f"fits cannot exceed frames tracked "
                f"({attrs.get('pupil_fits')}/{attrs.get('pupil_frames_tracked')})")
        r.info(f"pupil: {attrs.get('pupil_fits')} fits of "
               f"{attrs.get('pupil_frames_tracked')} frames tracked, "
               f"{len(f['pupil_x']['values'])} recorded")

        idx = f["voltage_cam_index"]["values"][:]
        r.check(bool(np.all(np.diff(idx) == 1)),
                "voltage_cam_index is contiguous (no frames lost)")
        r.check(len(idx) == len(fts), "index stream aligns with the frame stream")

    shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
