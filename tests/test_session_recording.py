"""
End-to-end test of the session + recording path, in Emulate mode.

Drives the real MainWindow through: build UI (all modules) -> Live view ->
Record -> fire a puff -> run the DMD -> stop -> close, then opens the resulting
HDF5 and verifies it contains every stream it should, on one timebase, with the
metadata that makes the file interpretable later.

This is the broad regression net: it touches the module adapters, the shared
clock, the ring buffer, the writer and the save-path logic in one go.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_session_recording.py
"""
from __future__ import annotations

import shutil
import sys

from _harness import MemorySettings, Report, isolate_user_state, pump, qt_app

EXPECTED_STREAMS = [
    "voltage_cam", "voltage_cam_index", "pupil_cam",
    # The pupil trace. Written whether or not EyeLoop is installed: with no
    # clone every frame is a NaN row, which is the contract — a gap in the
    # trace has to be visible in the file.
    "pupil_x", "pupil_y", "pupil_major", "pupil_minor", "pupil_angle",
    "wheel_voltage", "wheel_speed", "wheel_distance",
    "stage_x_um", "stage_y_um", "puffer", "dmd",
]
PUPIL_FIT_STREAMS = ["pupil_x", "pupil_y", "pupil_major", "pupil_minor",
                     "pupil_angle"]

PUPIL_THRESHOLD = 57                    # not the default, so a stuck one shows

CONFIG_ATTRS = ["created", "emulated", "modules", "subject", "cam_exposure_us",
                "wheel_rate_hz", "pupil_fps", "stage_port", "dmd_on_time_ms",
                "puffer_channel", "puffer_duration_s",
                # A pupil trace without the threshold that produced it is not
                # reproducible: threshold SETS the radius.
                "pupil_track_threshold", "pupil_track_model"]

# Written when the file closes, not when it opens — they describe what the run
# actually did rather than how it was configured.
FINAL_ATTRS = ["cam_timestamp_source", "cam_dropped_frames",
               "wheel_timestamp_source", "wheel_rate_actual_hz",
               "recorder_dropped_samples", "recorder_late_samples",
               "recorder_unstamped_samples",
               # Frames are dropped when a fit is slower than the camera, so
               # the trace is sparser than the frames and the file has to say
               # by how much.
               "pupil_frames_tracked", "pupil_fits"]

TEST_CHANNEL = "Dev3/port0/line2"       # not the default, so a stuck default shows
TEST_DURATION = 0.250                   # likewise


def main() -> int:
    r = Report("session")
    tmp = isolate_user_state()
    out = tmp / "recordings"

    # --mock before importing main: its module-level pre-init probes DCAM.
    sys.argv = ["main.py", "--mock"]
    app = qt_app()
    from acqApp import config
    import acqApp.main as M

    enabled = set(config.MODULES)
    r.note(f"modules: {sorted(enabled)}")
    win = M.MainWindow(cam_info=None, mock=True, enabled=enabled, cam_handle=None)
    mod = {m.key: m for m in win._modules}

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_subject.setText("smoke")
    win._save_panel._ed_template.setText("{subject}_{date}_{time}")
    win._save_panel._on_edited()
    r.check(win._save_panel.writable_error() is None, "save target is writable")

    # Editing the Save tab persists to disk as a side effect. Prove that landed
    # in the temp config and not in the operator's real one — otherwise this
    # test would quietly repoint their save folder at a temp directory.
    tmp_cfg = tmp / "acqapp_local.json"
    r.check(tmp_cfg.is_file() and "smoke" in tmp_cfg.read_text(encoding="utf-8"),
            "panel edits persisted to the isolated config, not the user's")

    # ── Live view ────────────────────────────────────────────────────────────
    win._btn_run.setChecked(True)
    r.check(win._sync.running, "session clock running after Live view")
    pump(app, 1.0)
    for _ in range(5):
        win._display_tick()
        pump(app, 0.05)
    # Tracking on, with the eye region the mock frame needs (240x320): the
    # crop is what EyeLoop needs to fit anything at all, and without a region
    # nothing is tracked. Set before Record so the trace covers the file.
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
    # Ask the window where it recorded rather than re-resolving: the template
    # is second-granular, so a resolve() on either side of a second boundary
    # names a different file.
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
    r.check(all(m.worker is None for m in win._modules), "workers released")

    win.close()
    pump(app, 0.2)
    # closeEvent saves the dock layout via QSettings — same argument as above:
    # it must land in the substitute, not in the operator's real registry key.
    # (Asserting it was *written* also proves the substitution is on the path
    # actually used, rather than silently bypassed.)
    r.check("dockState" in MemorySettings.store,
            "dock layout written to the substituted QSettings, not the user's")
    import PyQt6.QtCore
    r.check(PyQt6.QtCore.QSettings is MemorySettings,
            "QSettings substitution is in place")
    # The settings window writes its geometry through its OWN module-level
    # QSettings, so when it moved out of main.py the substitution had to follow
    # it. A module that imported the name before the patch keeps the real class
    # and writes the operator's registry — which is not visible from any
    # assertion about `main`, so assert it about every module that writes.
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

        # Attributes keep their own type. Everything used to be str()-ed, so
        # `emulated` read back as "False" — which is truthy — and every number
        # had to be parsed by whoever opened the file.
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
        # The placeholder written at open must have been overwritten at close.
        r.check(attrs.get("cam_timestamp_source") == "camera",
                f"cam_timestamp_source resolved "
                f"(got {attrs.get('cam_timestamp_source')!r})")
        # The mock encoder is paced by a sleep loop, and the file has to say so
        # rather than implying these came off the board's sample clock.
        r.check(attrs.get("wheel_timestamp_source") == "software",
                f"the mock wheel admits a software timebase "
                f"(got {attrs.get('wheel_timestamp_source')!r})")

        # Frame times must come from the worker's acquisition instants, not the
        # writer thread's arrival times.
        fts = f["voltage_cam"]["timestamps"][:]
        fdt = np.diff(fts)
        r.check(bool(np.all(fdt > 0)), "no two camera frames share a timestamp")
        r.info(f"camera frame interval: mean {fdt.mean()*1e3:.1f} ms "
               f"std {fdt.std()*1e3:.2f} ms")
        # The pupil streams are one per TRACKED frame, and there is one of
        # each per frame — a trace missing a semi-axis cannot be read as an
        # ellipse.
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
