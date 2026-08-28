"""
The DMD: what gets projected, and whether it was projected at all.

Two halves.

**Geometry** (`alp.build_frame`) is pure numpy/PIL, and it is where a
mispositioned stimulus actually comes from. Its conventions are inherited from
the standalone `dmdGUI_project` app — the one the optics are aligned with — so
each is pinned here rather than left to be rediscovered: the >127 threshold
applied twice, clockwise-positive rotation, and an offset measured from the
panel's centre.

**The controller** is driven against a fake ALP that records every call. The
bug being closed (#5) is that `DmdController` used to `print("stub")` and emit
`pattern_started`, so a session could record a stimulus stream, write DMD
settings into the file, and project nothing at all — with the panel looking
exactly as it does when it works. So the checks are: the right calls in the
right order, and the mock and real controllers agreeing about what reaches the
`/dmd` stream.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd.py
"""
from __future__ import annotations

import sys
import types

import numpy as np

from _harness import Report, block_real_devices, qt_app

W, H = 1024, 768                # this rig's ALP panel


# ── a fake ALP ────────────────────────────────────────────────────────────────

class FakeALP4:
    """Records the call sequence a projection makes. No device, ever."""

    instances: list["FakeALP4"] = []
    fail_init = False

    def __init__(self, version="4.2", libDir=None):
        self.version, self.libDir = version, libDir
        self.calls: list[tuple] = []
        self.nSizeX, self.nSizeY = W, H
        FakeALP4.instances.append(self)

    def _log(self, *c): self.calls.append(c)

    def Initialize(self, DeviceNum=None):
        if FakeALP4.fail_init:
            raise RuntimeError("The specified ALP is already in use")
        self._log("Initialize")

    def SeqAlloc(self, nbImg=1, bitDepth=1): self._log("SeqAlloc", nbImg, bitDepth)
    def SeqPut(self, imgData=None, **kw): self._log("SeqPut", imgData)
    def SeqControl(self, ctl, val, SequenceId=None): self._log("SeqControl", ctl, val)
    def SetTiming(self, **kw): self._log("SetTiming", kw.get("illuminationTime"))
    def Run(self, SequenceId=None, loop=True): self._log("Run", loop)
    def Halt(self): self._log("Halt")
    def FreeSeq(self, SequenceId=None): self._log("FreeSeq")
    def Free(self): self._log("Free")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def last(self, name: str):
        for c in reversed(self.calls):
            if c[0] == name:
                return c
        return None


def install_fake_alp() -> None:
    mod = types.ModuleType("ALP4")
    mod.ALP4 = FakeALP4
    mod.ALP_BIN_MODE = 2104
    mod.ALP_BIN_UNINTERRUPTED = 2106
    mod.ALP_SEQ_REPEAT = 2100
    sys.modules["ALP4"] = mod


def square(size: int, box: int, at: tuple[int, int] | None = None) -> np.ndarray:
    """A `size`x`size` black image with a white `box` square (default centred)."""
    img = np.zeros((size, size), dtype=np.uint8)
    y, x = at if at is not None else ((size - box) // 2, (size - box) // 2)
    img[y:y + box, x:x + box] = 255
    return img


def bbox(frame: np.ndarray):
    """(top, left, bottom, right) of the on-pixels, or None."""
    ys, xs = np.nonzero(frame)
    if ys.size == 0:
        return None
    return int(ys.min()), int(xs.min()), int(ys.max()), int(xs.max())


def centre(frame: np.ndarray):
    b = bbox(frame)
    return None if b is None else ((b[1] + b[3]) / 2.0, (b[0] + b[2]) / 2.0)


def main() -> int:
    r = Report("dmd")
    block_real_devices("ALP4")          # nothing here may reach the real DMD
    install_fake_alp()                  # …and then a fake that records instead
    app = qt_app()

    from acqApp.devices.dmd import alp
    from acqApp.devices.dmd.control import (DmdController, DmdSettings, FRAME_START,
                                    FRAME_STOP, MockDmdController)

    # ══ geometry ══════════════════════════════════════════════════════════════
    src = square(200, 100)              # 100 px white square in a 200 px image

    f = alp.build_frame(src, W, H, scale_pct=100.0)
    r.check(f.shape == (H, W) and f.dtype == np.uint8,
            f"frame is the panel's shape and dtype (got {f.shape}, {f.dtype})")
    r.check(set(np.unique(f)) <= {0, 255},
            f"frame is binary — the mirrors have no grey (values {np.unique(f)})")
    r.check(f.flags["C_CONTIGUOUS"],
            "frame is C-contiguous: SeqPut hands its raw buffer to the driver")
    r.check(int((f > 0).sum()) == 100 * 100,
            f"at 100 % the square keeps its size ({int((f > 0).sum())} px)")
    cx, cy = centre(f)
    r.check(abs(cx - W / 2) <= 0.5 and abs(cy - H / 2) <= 0.5,
            f"and lands centred on the panel (centre {cx:.1f}, {cy:.1f})")

    f = alp.build_frame(src, W, H, scale_pct=50.0)
    r.check(int((f > 0).sum()) == 50 * 50,
            f"scale 50 % quarters the area ({int((f > 0).sum())} px)")

    f = alp.build_frame(src, W, H, offset_x=100.0, offset_y=-60.0)
    cx, cy = centre(f)
    r.check(abs(cx - (W / 2 + 100)) <= 0.5 and abs(cy - (H / 2 - 60)) <= 0.5,
            f"offset moves the pattern from the panel centre by exactly that "
            f"many device px (centre {cx:.1f}, {cy:.1f})")

    # Fit: scale to the largest that fits, centre, and ignore the rest.
    # The source image is 200 px square with a 100 px mark in it, so fitting it
    # to a 768-tall panel scales by 3.84 and the mark ends up exactly half the
    # panel's height.
    f = alp.build_frame(src, W, H, scale_pct=25.0, offset_x=300.0,
                        rotation_deg=45.0, fit=True)
    b = bbox(f)
    r.check(b is not None and abs((b[2] - b[0] + 1) - H // 2) <= 1,
            f"fit scales the image to the short axis (the half-width mark is "
            f"{b[2] - b[0] + 1} px of the panel's {H})")
    cx, cy = centre(f)
    r.check(abs(cx - W / 2) <= 1.0 and abs(cy - H / 2) <= 1.0,
            "fit re-centres, overriding scale, rotation and offset")

    # Rotation direction. A mark in the TOP-LEFT must go to the TOP-RIGHT under
    # a +90 degrees rotation if — and only if — positive is clockwise. Getting
    # this backwards would still look plausible on any symmetric pattern.
    mark = square(200, 40, at=(20, 20))
    f = alp.build_frame(mark, W, H, scale_pct=100.0, rotation_deg=90.0)
    cx, cy = centre(f)
    r.check(cx > W / 2 and cy < H / 2,
            f"rotation is clockwise-positive, like the standalone GUI: a "
            f"top-left mark moves top-right (centre {cx:.0f}, {cy:.0f})")
    f0 = alp.build_frame(mark, W, H, scale_pct=100.0)
    c0 = centre(f0)
    r.check(c0[0] < W / 2 and c0[1] < H / 2,
            f"control: unrotated, the same mark is top-left "
            f"({c0[0]:.0f}, {c0[1]:.0f})")

    # Thresholds, both of them.
    r.check(int((alp.build_frame(np.full((10, 10), 128, np.uint8), W, H) > 0).sum())
            == 100, "grey 128 is on (>127)")
    r.check(int((alp.build_frame(np.full((10, 10), 127, np.uint8), W, H) > 0).sum())
            == 0, "grey 127 is off")

    f = alp.build_frame(src, W, H, invert=True)
    r.check(int((f > 0).sum()) == 200 * 200 - 100 * 100,
            f"invert swaps the mirrors inside the pattern's own bounds "
            f"({int((f > 0).sum())} px on)")

    f = alp.build_frame(src, W, H, scale_pct=2000.0)
    r.check(int((f > 0).sum()) == W * H,
            "a pattern larger than the panel is cropped, not an error")
    try:
        alp.build_frame(np.zeros((4, 4, 3), np.uint8), W, H)
        ok = False
    except ValueError:
        ok = True
    r.check(ok, "a colour (3-D) array is rejected rather than silently reshaped")

    # ══ the controller ════════════════════════════════════════════════════════
    import tempfile
    from pathlib import Path
    from PIL import Image
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dmd_"))
    pat = tmp / "square.png"
    Image.fromarray(src, mode="L").save(pat)

    FakeALP4.instances.clear()
    s = DmdSettings(pattern_path=pat, on_time_ms=250.0, static_hold=False,
                    n_repeats=0, scale_pct=100.0)
    c = DmdController(s)
    dev = FakeALP4.instances[0]
    r.check(dev.names() == ["Initialize"], f"opening initialises the ALP and "
                                           f"nothing else (got {dev.names()})")
    r.check(c.resolution == (W, H) and "1024x768" in c.device_name,
            f"the device reports itself: {c.device_name}")
    r.check(c.on_pixels == 100 * 100,
            f"the pattern named in the settings was rendered on open "
            f"({c.on_pixels} mirrors)")

    events: list[int] = []
    c.set_sink(events.append)
    c.display()
    seq = dev.names()[1:]
    r.check(seq[:5] == ["SeqAlloc", "SeqPut", "SeqControl", "SetTiming", "Run"],
            f"display runs the vendor sequence in order (got {seq})")
    put = dev.last("SeqPut")[1]
    r.check(isinstance(put, np.ndarray) and put.shape == (H, W)
            and put.dtype == np.uint8 and put.flags["C_CONTIGUOUS"],
            f"the frame handed to SeqPut is the panel-sized binary buffer "
            f"(got {getattr(put, 'shape', None)}, {getattr(put, 'dtype', None)})")
    r.check(int((put > 0).sum()) == 100 * 100,
            "…and it is the rendered pattern, not the raw image")
    r.check(dev.last("SeqControl")[1:] == (2104, 2106),
            f"binary uninterrupted mode is set, so a held pattern does not "
            f"blank between pictures (got {dev.last('SeqControl')[1:]})")
    r.check(dev.last("SetTiming")[1] == 250_000,
            f"on-time reaches the device in microseconds "
            f"(got {dev.last('SetTiming')[1]})")
    r.check(dev.last("Run")[1] is True, "0 repeats means loop")
    r.check(events == [FRAME_START],
            f"display logs one event to /dmd (got {events})")

    c.stop()
    r.check(dev.names()[-2:] == ["Halt", "FreeSeq"],
            f"stop halts and releases the sequence (got {dev.names()[-2:]})")
    r.check(events == [FRAME_START, FRAME_STOP],
            f"…and closes the projection window in the log (got {events})")
    events.clear()
    c.stop()
    r.check(events == [], "a second stop is a no-op, not a second event")

    # Static hold: device-default timing, looped — not a 250 ms flicker.
    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=True))
    c.display()
    r.check(dev.last("SetTiming")[1] is None and dev.last("Run")[1] is True,
            f"static hold leaves the timing at the device default and loops "
            f"(got illumination={dev.last('SetTiming')[1]}, "
            f"loop={dev.last('Run')[1]})")
    c.stop()

    # The two checks below drive the CYCLING path, which the panel no longer
    # offers — it hardcodes static_hold=True so the DMD innately holds one
    # image. So they say static_hold=False explicitly rather than leaning on
    # the dataclass default, which now follows the panel.

    # Repeats: a finite burst rather than a loop.
    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=False,
                                 on_time_ms=20.0, n_repeats=3))
    c.display()
    r.check(dev.last("Run")[1] is False, "a repeat count stops looping")
    r.check(("SeqControl", 2100, 3) in dev.calls,
            f"…and is sent as ALP_SEQ_REPEAT (calls {dev.calls[-4:]})")
    c.stop()

    # The ALP cannot hold a picture longer than 10 s.
    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=False,
                                 on_time_ms=30_000.0))
    c.display()
    r.check(dev.last("SetTiming")[1] == alp.MAX_PICTURE_US,
            f"an on-time past the ALP's limit is clamped, not passed through "
            f"(got {dev.last('SetTiming')[1]})")
    c.stop()

    # Geometry edited after loading must rebuild the frame, or Display projects
    # the old alignment while the panel shows the new one.
    c.apply_settings(DmdSettings(pattern_path=pat, scale_pct=50.0))
    r.check(c.on_pixels == 50 * 50,
            f"a geometry change re-renders the pattern ({c.on_pixels} mirrors)")

    # No pattern: say so and touch nothing.
    n_before = len(dev.calls)
    c2 = DmdController(DmdSettings())
    dev2 = FakeALP4.instances[-1]
    events2: list[int] = []
    c2.set_sink(events2.append)
    c2.display()
    r.check(dev2.names() == ["Initialize"] and events2 == [],
            f"display with no pattern projects nothing and logs nothing "
            f"(got {dev2.names()}, {events2})")
    c2.close()
    r.check(dev2.names()[-1] == "Free", "close releases the device")
    r.check(len(dev.calls) == n_before,
            "the second controller did not touch the first device")

    c.close()

    # ══ the mock agrees about the /dmd stream ═════════════════════════════════
    m = MockDmdController(DmdSettings(pattern_path=pat, static_hold=True))
    m.load_pattern(pat)
    mev: list[int] = []
    m.set_sink(mev.append)
    m.display()
    m.stop()
    r.check(mev == [FRAME_START, FRAME_STOP],
            f"the mock brackets a projection the same way (got {mev})")
    r.check(m.resolution == (W, H) and m.on_pixels == 100 * 100,
            f"and renders through the same builder at the same panel size "
            f"({m.resolution}, {m.on_pixels} mirrors)")
    r.check("mock" in m.device_name,
            f"the mock names itself as such: {m.device_name!r}")

    # ══ a busy ALP must fall back, not crash ══════════════════════════════════
    FakeALP4.fail_init = True
    try:
        DmdController(DmdSettings())
        raised = False
    except Exception:
        raised = True
    FakeALP4.fail_init = False
    r.check(raised, "a device already in use raises out of the constructor, "
                    "so the adapter can substitute the mock and say so")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    check_roi_wiring(r)
    check_mode_switch_and_cache(r)
    return r.finish()


def check_roi_wiring(r) -> None:
    """The ROI editor reaches the DMD tab, and gets a VOLTAGE-camera frame.

    The DMD images through the voltage camera, so an ROI drawn for it is in
    ORCA pixels. Two things could silently break that: the frame arriving at
    display scale (every ROI then out by DISP_DS), or `latest_frame` consuming
    the frame and starving the camera's own preview.
    """
    import sys as _s
    from _harness import isolate_user_state, pump, qt_app
    isolate_user_state()
    app = qt_app()
    import acqApp.main as M
    _s.argv = ["main.py", "--mock"]
    win = M.MainWindow(cam_info=None, mock=True,
                       enabled={"voltage_cam", "dmd"}, cam_handle=None)
    dmd = next(m for m in win._modules if m.key == "dmd")
    cam = next(m for m in win._modules if m.key == "voltage_cam")

    r.check(win.latest_frame("voltage_cam") is None,
            "no frame before the camera has run")
    win._btn_run.setChecked(True)
    pump(app, 1.2)

    f = win.latest_frame("voltage_cam")
    if r.check(f is not None, "the DMD can reach a voltage-camera frame"):
        from acqApp.adapters.base import DISP_DS
        want = cam.panel.get_config().frame_shape
        r.check(f.shape == want,
                f"…at FULL camera resolution {f.shape} (want {want}), not the "
                f"preview's 1/{DISP_DS} — ROIs are in camera px, so a "
                f"downsampled frame would put every one of them out by {DISP_DS}x")
    # CONTROL: reading it must not consume. The camera's own preview pulls from
    # the same worker, and a stolen frame is a dropped one.
    again = win.latest_frame("voltage_cam")
    r.check(again is not None and again.shape == f.shape,
            "control: reading it twice still returns a frame (non-consuming)")
    r.check(win.latest_frame("nope") is None, "an unloaded module gives None")

    # The panel round-trips what the editor produces.
    # Keys exactly as RectRoi.to_dict() writes them: `roi_from_dict` passes
    # them straight to the constructor, so cx/cy/angle would raise.
    dmd.panel.set_rois(({"kind": "rect", "name": "r1", "enabled": True,
                         "x": 100.0, "y": 80.0, "w": 40.0, "h": 30.0,
                         "angle_deg": 0.0},))
    from acqApp.devices.dmd.roi import RoiSet
    r.check(len(RoiSet.from_list(list(dmd.panel.rois))) == 1,
            "…and they round-trip through roi_from_dict, so the ROI display "
            "mode can rebuild them")
    r.check(len(dmd.panel.settings.rois) == 1, "ROIs land in DmdSettings")
    md = dmd.metadata()
    r.check(md["dmd_n_rois"] == 1 and "r1" in md["dmd_rois"],
            f"…and into the session metadata ({md['dmd_n_rois']} rois)")
    r.check(md["dmd_calibration"] == "",
            "…recording that no calibration was in force")

    win._btn_run.setChecked(False)
    pump(app, 0.3)
    win.close()
    pump(app, 0.1)


def check_mode_switch_and_cache(r) -> None:
    """A mode click must not double-fire, and an unchanged preview must not
    re-run the pattern transform (2026-08-27 cleanup sweep findings)."""
    import sys as _s
    import tempfile
    from pathlib import Path
    from PIL import Image
    from _harness import isolate_user_state, pump, qt_app

    isolate_user_state()
    app = qt_app()
    import acqApp.main as M
    _s.argv = ["main.py", "--mock"]
    win = M.MainWindow(cam_info=None, mock=True, enabled={"dmd"}, cam_handle=None)
    dmd = next(m for m in win._modules if m.key == "dmd")
    panel = dmd.panel

    from acqApp.devices.dmd.control import MODE_ALL_ON, MODE_PATTERN

    # ── each toggled radio in a QButtonGroup fires once — the switch is one
    # user action, and a single settings_changed/preview-rebuild per click ──
    seen: list = []
    panel.settings_changed.connect(lambda s: seen.append(s))
    panel._rb[MODE_ALL_ON].setChecked(True)
    pump(app, 0.05)
    seen.clear()
    panel._rb[MODE_PATTERN].setChecked(True)
    pump(app, 0.05)
    r.check(len(seen) == 1,
            f"a mode click emits settings_changed once, not once per radio "
            f"in the switch ({len(seen)})")

    # ── the built frame is cached: an unchanged preview reuses it ───────────
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dmdcache_"))
    pat = tmp / "square.png"
    Image.fromarray(np.full((64, 64), 255, np.uint8), mode="L").save(pat)
    panel._pattern_path = pat
    panel._emit()
    pump(app, 0.05)
    panel.resize(400, 300)
    pump(app, 0.05)
    before = panel._frame_cache[1] if panel._frame_cache else None
    r.check(before is not None, "fixture: a pattern frame was built")
    panel._update_preview()             # nothing that affects the frame changed
    after = panel._frame_cache[1] if panel._frame_cache else None
    r.check(after is before,
            "an unchanged preview reuses the built frame rather than "
            "re-running alp.build_frame")

    panel._spn_scale.setValue(panel._spn_scale.value() + 5.0)
    pump(app, 0.05)
    changed = panel._frame_cache[1] if panel._frame_cache else None
    r.check(changed is not before,
            "…but a real parameter change rebuilds it")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    win.close()
    pump(app, 0.1)


if __name__ == "__main__":
    sys.exit(main())
