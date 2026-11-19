"""Render one settings panel offscreen to a PNG, for layout checks.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\snap.py <module_key|save|review|roi>
      [--width 380] [--out file.png] [--settings JSON]

module_key is a config.MODULES key (voltage_cam, pupil_cam, wheel, ...); `main`
is the whole window, every module loaded (try --width 1600); `save` is the
Save section, `review` the standalone pupil review, `roi` the DMD ROI
dialog (demo frame, live band; try --width 1000), `stage_etl` the stage page
with the ETL's path-2 plane on the Z gauge (try --width 700), `modules` /
`modules_probe` the startup / mid-session module picker (stand-in probes),
`cameras` / `overlay` both paths live, side by side / merged (--width 1600). `--settings` seeds the
module's stored settings first, e.g. vis_stim --settings
"{\\"trial_type\\": \\"contrast\\"}". Mock window, isolated user state:
nothing on the rig is touched or rewritten. Not part of run_all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# Before the first Qt import: windowless, with real fonts to measure text by.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from _harness import isolate_user_state, make_window, qt_app  # noqa: E402


def _roi_dialog():
    """The FOV -> ROI dialog on a demo frame, its live view a middle band."""
    from acqApp.devices.dmd._roi_editor import (DMD_H, DMD_W, demo_frame,
                                                stand_in_calibration)
    from acqApp.devices.dmd.pair_dialog import PairRoiDialog
    from acqApp.devices.dmd.roi import CircleRoi, RectRoi
    from acqApp.devices.dmd.roi_panel import RoiEditor

    f = demo_frame()
    h, w = f.shape
    ed = RoiEditor(stand_in_calibration((w, h), (DMD_W, DMD_H)))
    ed.set_image(f)
    ed._add(RectRoi(x=0.42 * w, y=0.45 * h, w=0.12 * w, h=0.15 * h))
    ed._add(CircleRoi(x=0.58 * w, y=0.25 * h, r=0.06 * h))
    band = f[h // 3: 2 * h // 3]
    return PairRoiDialog(ed, "demo_roi",
                         live_source=lambda: (band, (0.0, h / 3), 1.0),
                         illuminate=lambda _r: None)


def _panel(key: str):
    """-> (widget to grab, the window holding it or None)."""
    if key == "review":
        from acqApp.devices.pupil_cam.review_dialog import ReviewWidget
        return ReviewWidget(), None
    if key == "roi":
        return _roi_dialog(), None
    if key in ("reticle", "bend"):
        from acqApp.devices.dmd._roi_editor import DMD_H, DMD_W, demo_frame
        from acqApp.devices.dmd.reticle_align import ReticleAlignDialog
        dlg = ReticleAlignDialog(lambda _f: None, (DMD_W, DMD_H), demo_frame())
        if key == "bend":           # step 4 with one corner dragged
            dlg._steps.button(3).setChecked(True)
            k = len(dlg.warp.xs) + 2
            node = dlg._nodes[k]
            node.setPos(node.pos().x() + 120, node.pos().y() - 80)
        return dlg, None
    from acqApp import config
    if key in ("modules", "modules_probe"):     # the startup / Modules picker
        from acqApp import probe
        from acqApp.dialogs import ModuleSelectDialog
        if key == "modules":
            return ModuleSelectDialog(["voltage_cam", "dmd", "wheel", "stage"]), None
        # Stand-in results: a snapshot must not enumerate the rig.
        demo = {"voltage_cam": ("ok", "ORCA-Fusion BT, S/N 001234"),
                "dmd": ("missing", "no ALP found (is the standalone app open?)"),
                "wheel": ("ok", "Dev3 (USB-6343)"), "stage": ("error", "COM5 busy")}
        probe.probe = lambda k, **_kw: probe.ProbeResult(
            *demo.get(k, ("stub", "no device of its own")))
        return ModuleSelectDialog(["voltage_cam", "dmd", "wheel", "stage", "etl"],
                                  probe_kwargs=dict), None
    if key == "main":
        win = make_window(set(config.MODULES))
        return win, win
    if key in ("cameras", "overlay"):   # both paths live; overlay: merged in one
        from _harness import pump
        win = make_window({"voltage_cam", "voltage_cam_2", "dmd", "dmd_2"})
        win._module("voltage_cam")._chk_overlay.setChecked(key == "overlay")
        win.show()          # first: a hidden LUT bar skips its histogram refresh
        win._btn_run.setChecked(True)
        for _ in range(20):
            pump(qt_app(), 0.05)
            win._display_tick()
        return win, win
    if key == "stage_etl":          # the stage page with path 2's plane on the Z gauge
        win = make_window({"stage", "etl"})
        etl, stage = win._module("etl"), win._module("stage")
        etl.panel._spn_gain.setValue(40.0)      # µm/V, a stand-in calibration
        etl.panel._spn_cofocal.setValue(2.0)
        etl.set_offset_um(60.0)
        stage.update_display()
        return stage.panel.view, win
    win = make_window({key} if key in config.MODULES else set())
    if key == "save":
        return win._save_panel, win
    m = win._module(key)
    if m is None or m.panel is None:
        sys.exit(f"no panel for {key!r}; known: save, review, "
                 + ", ".join(config.MODULES))
    return m.panel, win


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("key", help="a config.MODULES key, 'main', 'save', 'review', "
                                "'roi' or 'reticle'")
    ap.add_argument("--width", type=int, default=380)
    ap.add_argument("--out", default="",
                    help="default: snap_<key>.png in the temp dir")
    ap.add_argument("--settings", default="",
                    help="JSON stored as the module's settings before it builds")
    a = ap.parse_args()

    isolate_user_state()
    if a.settings:
        from acqApp import config
        config.save_settings(a.key, json.loads(a.settings))
    app = qt_app()      # held: a collected QApplication takes the process down
    w, win = _panel(a.key)
    if w is win:        # the whole window, at 16:9
        w.resize(a.width, a.width * 9 // 16)
        w.show()
        app.processEvents()
    else:
        w.setParent(None)   # out of its scroll area, so the width is ours to set
        w.resize(a.width, 100)
        w.show()
        app.processEvents()
        h = w.heightForWidth(a.width) if w.hasHeightForWidth() else -1
        w.resize(a.width, max(h, w.sizeHint().height(), w.minimumSizeHint().height()))
        app.processEvents()
    out = a.out or os.path.join(tempfile.gettempdir(), f"snap_{a.key}.png")
    if not w.grab().save(out):
        print(f"could not write {out}")
        return 1
    print(f"{out}: {w.width()}x{w.height()}")
    w.close()
    if win is not None:
        win.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    sys.exit(main())
