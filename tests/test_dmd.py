"""DMD: frame building and controllers, calibration, reticle wiring, ROIs.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd.py [-q] [--part NAME]
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import tracemalloc
import types
from pathlib import Path

import numpy as np
from _harness import (Report, block_real_devices, isolate_user_state,
                      make_window, pump, pump_until, qt_app, run_parts)
from acqApp.devices.dmd.calibration import ON, DmdCalibration, apply_transform
from acqApp.devices.dmd.roi import (CircleRoi, PolyRoi, RectRoi, RoiSet,
                                    roi_from_dict, simplify_polygon)


# ═══ dmd ════════════════════════════════════════════════════════════════

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

    def blanked(self) -> bool:
        """The mirrors now hold an all-off frame (pointed off, not just halted)."""
        put = self.last("SeqPut")
        return (self.names()[-1] == "Run" and put is not None
                and not np.asarray(put[1]).any())


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


def _part_dmd() -> int:
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

    # Fit: a 200 px source scales by 3.84 to the 768 px panel, so its 100 px
    # mark is exactly half the panel's height.
    f = alp.build_frame(src, W, H, scale_pct=25.0, offset_x=300.0,
                        rotation_deg=45.0, fit=True)
    b = bbox(f)
    r.check(b is not None and abs((b[2] - b[0] + 1) - H // 2) <= 1,
            f"fit scales the image to the short axis (the half-width mark is "
            f"{b[2] - b[0] + 1} px of the panel's {H})")
    cx, cy = centre(f)
    r.check(abs(cx - W / 2) <= 1.0 and abs(cy - H / 2) <= 1.0,
            "fit re-centres, overriding scale, rotation and offset")

    # A backwards rotation still looks plausible on any symmetric pattern.
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
    from PIL import Image
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dmd_"))
    pat = tmp / "square.png"
    Image.fromarray(src, mode="L").save(pat)

    FakeALP4.instances.clear()
    s = DmdSettings(pattern_path=pat, scale_pct=100.0)
    c = DmdController(s)
    dev = FakeALP4.instances[0]
    r.check(dev.names()[0] == "Initialize" and dev.blanked(),
            f"opening initialises the ALP and points every mirror off, so a "
            f"frame left from a last session isn't held (got {dev.names()})")
    n_open = len(dev.calls)
    r.check(c.resolution == (W, H) and "1024x768" in c.device_name,
            f"the device reports itself: {c.device_name}")
    r.check(c.on_pixels == 100 * 100,
            f"the pattern named in the settings was rendered on open "
            f"({c.on_pixels} mirrors)")

    events: list[int] = []
    c.set_sink(events.append)
    c.display()
    seq = dev.names()[n_open:]
    r.check(seq[:7] == ["Halt", "FreeSeq", "SeqAlloc", "SeqPut", "SeqControl",
                        "SetTiming", "Run"],
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
    r.check(dev.last("SetTiming")[1] is None and dev.last("Run")[1] is True,
            f"a pattern is held: device-default timing, looping "
            f"(got illumination={dev.last('SetTiming')[1]}, "
            f"loop={dev.last('Run')[1]})")
    r.check(not any(c[0] == "SeqControl" and c[1] == 2100 for c in dev.calls),
            "…with no repeat count sent")
    r.check(events == [FRAME_START],
            f"display logs one event to /dmd (got {events})")

    c.stop()
    r.check(dev.blanked(),
            f"stop points every mirror off, not just halts the frame that "
            f"was up (got {dev.names()[-7:]})")
    r.check(events == [FRAME_START, FRAME_STOP],
            f"…and closes the projection window in the log (got {events})")
    events.clear()
    c.stop()
    r.check(events == [], "a second stop is a no-op, not a second event")

    # Unrebuilt, Display would project the old alignment under the new panel.
    c.apply_settings(DmdSettings(pattern_path=pat, scale_pct=50.0))
    r.check(c.on_pixels == 50 * 50,
            f"a geometry change re-renders the pattern ({c.on_pixels} mirrors)")

    n_before = len(dev.calls)
    c2 = DmdController(DmdSettings())
    dev2 = FakeALP4.instances[-1]
    events2: list[int] = []
    c2.set_sink(events2.append)
    n_open2 = len(dev2.calls)
    c2.display()
    r.check(len(dev2.calls) == n_open2 and dev2.blanked() and events2 == [],
            f"display with no pattern projects nothing and logs nothing "
            f"(got {dev2.names()}, {events2})")
    c2.close()
    r.check(dev2.names()[-1] == "Free", "close releases the device")
    r.check(len(dev.calls) == n_before,
            "the second controller did not touch the first device")

    c.close()

    # ══ the mock agrees about the /dmd stream ═════════════════════════════════
    m = MockDmdController(DmdSettings(pattern_path=pat))
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

    shutil.rmtree(tmp, ignore_errors=True)

    check_roi_wiring(r)
    check_mode_switch_and_cache(r)
    check_light_switch(r)
    check_sub_sampling(r)
    check_controller_faults(r)
    return r.finish()


def check_controller_faults(r) -> None:
    """Display with nothing to show ends dark rather than holding the old
    frame, and a constructor that fails after opening the ALP frees it."""
    from acqApp.devices.dmd.control import (MODE_ALL_ON, MODE_ROI,
                                            DmdController, DmdSettings)

    c = DmdController(DmdSettings(display_mode=MODE_ALL_ON))
    dev = FakeALP4.instances[-1]
    c.display()
    r.check(c.projecting, "fixture: all-on is lit")
    c.apply_settings(DmdSettings(display_mode=MODE_ROI, rois=()))
    c.display()
    r.check(c.frame is None and not c.projecting and dev.blanked(),
            f"Display with nothing to show (last ROI deleted under Live update) "
            f"points the mirrors off, not leaves the old frame lit "
            f"({dev.names()[-3:]})")
    c.close()

    from acqApp.devices.dmd.control import MockDmdController
    m = MockDmdController(DmdSettings(display_mode=MODE_ALL_ON))
    m.display()
    lit = m.projecting
    m.apply_settings(DmdSettings(display_mode=MODE_ROI, rois=()))
    m.display()
    r.check(lit and not m.projecting,
            "the mock matches: nothing to show ends dark, so Emulate tests the "
            "same 'not lit' path")
    m.close()

    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dmdbad_"))
    bad = tmp / "corrupt.png"
    bad.write_bytes(b"not a png")
    try:
        DmdController(DmdSettings(pattern_path=bad))
        raised = False
    except Exception:
        raised = True
    dev = FakeALP4.instances[-1]
    r.check(raised and dev.names()[-1] == "Free",
            f"an unreadable saved pattern raises out of the constructor and the "
            f"opened ALP is freed for the next try ({dev.names()})")
    shutil.rmtree(tmp, ignore_errors=True)


def check_roi_wiring(r) -> None:
    """The ROI editor gets a full-resolution VOLTAGE-camera frame (ROIs are in
    ORCA px), without consuming it from the camera's own preview."""
    isolate_user_state()
    app = qt_app()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")
    cam = next(m for m in win._modules if m.key == "voltage_cam")

    r.check(win.latest_frame("voltage_cam") is None,
            "no frame before the camera has run")
    win._btn_run.setChecked(True)
    pump_until(app, lambda: win.latest_frame("voltage_cam") is not None)

    f = win.latest_frame("voltage_cam")
    if r.check(f is not None, "the DMD can reach a voltage-camera frame"):
        from acqApp.adapters.base import DISP_DS
        want = cam.panel.get_config().frame_shape
        r.check(f.shape == want,
                f"…at FULL camera resolution {f.shape} (want {want}), not the "
                f"preview's 1/{DISP_DS} — ROIs are in camera px, so a "
                f"downsampled frame would put every one of them out by {DISP_DS}x")
    # The preview pulls from the same worker; a stolen frame is a dropped one.
    again = win.latest_frame("voltage_cam")
    r.check(again is not None and again.shape == f.shape,
            "control: reading it twice still returns a frame (non-consuming)")
    r.check(win.latest_frame("nope") is None, "an unloaded module gives None")

    # Keys exactly as RectRoi.to_dict() writes them: `roi_from_dict` passes
    # them straight to the constructor, so cx/cy/angle would raise.
    dmd.panel.set_rois(({"kind": "rect", "name": "r1", "enabled": True,
                         "x": 100.0, "y": 80.0, "w": 40.0, "h": 30.0,
                         "angle_deg": 0.0},))
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
    re-run the pattern transform."""
    from PIL import Image

    isolate_user_state()
    app = qt_app()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")
    panel = dmd.panel

    from acqApp.devices.dmd.control import MODE_ALL_ON, MODE_PATTERN

    # A switch toggles two radios; it must still emit once.
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

    # Each trigger used to leave its own stop timer running, so a second
    # trigger inside the first's duration was cut short by the first's stop.
    stops: list = []
    dmd.controller.stop = lambda: stops.append(1)
    dmd.on_trigger(dmd.key, 0.15)
    pump(app, 0.10)
    dmd.on_trigger(dmd.key, 0.15)
    pump(app, 0.10)
    r.check(stops == [],
            "a second trigger isn't cut short by the first trigger's stop")
    pump(app, 0.15)
    r.check(stops == [1], f"…and stops once, on its own time ({stops})")

    shutil.rmtree(tmp, ignore_errors=True)
    win.close()
    pump(app, 0.1)


def check_light_switch(r) -> None:
    """The DMD card's light toggle projects the tab's selected display, all-on if
    none is selected, and never swaps a broken selection for all-on."""
    from PIL import Image

    from acqApp.devices.dmd.control import MODE_ALL_ON, MODE_PATTERN, MODE_ROI

    isolate_user_state()
    app = qt_app()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")
    panel = dmd.panel
    btn = dmd._light_btn
    lit = lambda: bool(dmd.controller._running)             # noqa: E731
    msg = lambda: win.statusBar().currentMessage()          # noqa: E731

    card = win._folds["dmd"].card
    r.check(btn is not None and card.isAncestorOf(btn)
            and "dmd" not in win._status_widgets,
            "the DMD's switch sits on its card, not the status bar")
    clipped = []
    keep = btn.text()
    for text in ("DMD OFF", "● DMD ON"):
        btn.setText(text)
        if btn.sizeHint().width() > card.minimumWidth():
            clipped.append((text, btn.sizeHint().width()))
    btn.setText(keep)
    r.check(not clipped,
            f"…and both its labels fit the card, none clipped ({clipped})")
    r.check(btn.isEnabled() and not btn.isChecked() and btn.text() == "DMD OFF"
            and not lit(), "it starts off, with the projector dark")

    # Nothing selected: Image mode, no pattern file.
    r.check(panel.mode == MODE_PATTERN and panel.settings.pattern_path is None,
            "fixture: nothing is selected")
    btn.click()
    pump(app, 0.05)
    r.check(btn.isChecked() and btn.text().startswith("●") and lit()
            and panel.mode == MODE_ALL_ON and dmd.controller.on_pixels > 0,
            "nothing selected: it lights all-on, and the DMD tab shows All on")
    r.check("all-on" in msg() and "nothing was selected" in msg(),
            f"…and the status bar says why ({msg()!r})")
    btn.click()
    pump(app, 0.05)
    r.check(not btn.isChecked() and btn.text() == "DMD OFF" and not lit(),
            "pressed again it turns the light off")

    # A pattern file is selected: that is what lights, not all-on.
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_dmdlight_"))
    pat = tmp / "bars.png"
    img = np.zeros((64, 64), np.uint8)
    img[:, ::8] = 255
    Image.fromarray(img, mode="L").save(pat)
    panel.set_pattern_path(pat)
    pump(app, 0.05)
    btn.click()
    pump(app, 0.05)
    full = dmd.controller.DEFAULT_W * dmd.controller.DEFAULT_H \
        if hasattr(dmd.controller, "DEFAULT_W") else None
    r.check(lit() and panel.mode == MODE_PATTERN
            and 0 < dmd.controller.on_pixels
            and (full is None or dmd.controller.on_pixels < full),
            f"a selected pattern is what lights, not all-on "
            f"({dmd.controller.on_pixels} mirrors, mode {panel.mode})")

    # The tab's own buttons, a trigger and a routine keep the switch honest.
    dmd.stop_display()
    r.check(not btn.isChecked() and btn.text() == "DMD OFF",
            "the tab's Stop turns the switch off")
    dmd.display()
    r.check(btn.isChecked() and lit(), "the tab's Display turns it on")
    dmd.set_light(False)
    r.check(not btn.isChecked(), "a routine's set_light(False) turns it off")
    dmd.set_light(True)
    r.check(btn.isChecked(), "…and set_light(True) on")
    dmd.build_controller(True)
    r.check(not btn.isChecked() and not lit(),
            "a rebuilt controller (Emulate toggle) starts dark, and the switch says so")

    # A selection that can't be shown is reported, never swapped for all-on.
    panel.set_pattern_path(tmp / "gone.png")
    btn.click()
    pump(app, 0.05)
    r.check(not btn.isChecked() and not lit() and panel.mode == MODE_PATTERN
            and "missing" in msg(),
            f"a missing pattern file isn't swapped for all-on ({msg()!r})")
    roi = RectRoi(x=100, y=80, w=40, h=20).to_dict()
    panel.set_roi_pattern("cells", [roi])
    pump(app, 0.05)
    btn.click()
    pump(app, 0.05)
    r.check(not btn.isChecked() and not lit() and panel.mode == MODE_ROI
            and "calibration" in msg(),
            f"ROIs without a calibration aren't swapped for all-on ({msg()!r})")
    panel.set_rois(())
    panel._rb[MODE_ROI].setChecked(True)
    pump(app, 0.05)
    btn.click()
    pump(app, 0.05)
    r.check(btn.isChecked() and lit() and panel.mode == MODE_ALL_ON,
            "ROIs mode with no ROIs drawn is 'nothing selected': all-on")
    btn.click()

    # The real controller ends dark when the selection builds no frame.
    c = dmd.controller
    c.display = lambda: None
    try:
        btn.click()
        pump(app, 0.05)
        r.check(not btn.isChecked() and btn.text() == "DMD OFF"
                and "not lit" in msg(),
                f"a Display that lit nothing leaves the switch off and says so ({msg()!r})")
    finally:
        del c.display

    # A device fault inside a slot is reported and ends dark; it must not
    # escape (no excepthook: the app would abort with the DMD maybe lit).

    def boom():
        c._running = True                   # it lit, then the driver failed
        raise RuntimeError("ALP gone")
    real_display = c.display
    c.display = boom
    try:
        btn.click()
        pump(app, 0.05)
        r.check(not btn.isChecked() and not lit() and "ALP gone" in msg()
                and "forced off" in msg(),
                f"a failing Display from the switch ends dark and says so ({msg()!r})")
        dmd.on_trigger(dmd.key, 0.0)
        r.check(not lit(), "…as does a trigger")
        try:
            dmd.display()
            raised = False
        except RuntimeError:
            raised = True
        r.check(raised, "…while display() itself still raises, for a routine to pause on")
    finally:
        c.display = real_display

    # Unloading the module takes the switch with it.
    try:
        win.set_modules({"voltage_cam"})
    except RuntimeError as e:
        r.check(False, f"fixture: module unload refused ({e})")
    else:
        pump(app, 0.05)
        r.check("dmd" not in win._folds and "dmd" not in win._docks,
                "unloading the DMD module removes its card and switch")
    shutil.rmtree(tmp, ignore_errors=True)
    win.close()
    pump(app, 0.1)


def check_sub_sampling(r) -> None:
    """1-out-of-N sub-sampling removes ~1/N of the ON pixels (n=1 is a no-op),
    and a sub_sampling-only change reloads the real controller, whose reload
    test is an explicit field tuple easy to forget a new field in."""
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, MODE_ALL_ON,
                                            DmdController, DmdSettings,
                                            MockDmdController, subsample_frame)

    # isolate_user_state() above re-blocked every vendor driver, ALP4 included.
    install_fake_alp()

    full = np.full((200, 300), 255, dtype=np.uint8)
    r.check(np.array_equal(subsample_frame(full, 1), full),
            'n=1 ("1 out of 1") is a no-op')

    for n in (2, 3, 10):
        out = subsample_frame(full, n)
        frac_off = 1.0 - int((out > 0).sum()) / full.size
        r.check(abs(frac_off - 1.0 / n) < 0.02,
                f"n={n} removes ~1/{n} of the pixels ({frac_off:.3f} off, "
                f"wanted ~{1.0 / n:.3f})")
        r.check(set(np.unique(out)) <= {0, 255}, f"…and stays binary (n={n})")

    half = full.copy()
    half[:, 150:] = 0
    masked = subsample_frame(half, 2)
    r.check(np.array_equal(masked[:, 150:], half[:, 150:]),
            "an already-off pixel is unaffected either way")

    FakeALP4.instances.clear()
    c = DmdController(DmdSettings(display_mode=MODE_ALL_ON, sub_sampling=2))
    r.check(abs(c.on_pixels / (DEFAULT_W * DEFAULT_H) - 0.5) < 0.02,
            f"All ON + 1-in-2 sub-sampling projects ~half the mirrors "
            f"({c.on_pixels} of {DEFAULT_W * DEFAULT_H})")

    before = c.on_pixels
    c.apply_settings(DmdSettings(display_mode=MODE_ALL_ON, sub_sampling=4))
    r.check(c.on_pixels != before,
            f"changing only sub_sampling reloads the frame ({before} -> "
            f"{c.on_pixels} mirrors)")
    c.close()

    # The mock reloads on any change (dataclass equality): the cross-check.
    m = MockDmdController(DmdSettings(display_mode=MODE_ALL_ON, sub_sampling=2))
    m.load_pattern()
    r.check(abs(m.on_pixels / (DEFAULT_W * DEFAULT_H) - 0.5) < 0.02,
            f"the mock agrees ({m.on_pixels} of {DEFAULT_W * DEFAULT_H})")


# ═══ calib ══════════════════════════════════════════════════════════════

DW, DH = 256, 192          # a small DMD
CW, CH = 320, 240          # the "ORCA"


def true_transform() -> np.ndarray:
    """DMD → camera: scale, a 7° rotation and an offset."""
    th = np.radians(7.0)
    s = 1.05
    return np.array([[s * np.cos(th), -s * np.sin(th), 34.0],
                     [s * np.sin(th),  s * np.cos(th), 22.0],
                     [0.0, 0.0, 1.0]])


def footprint(pattern: np.ndarray, M: np.ndarray) -> np.ndarray:
    """Which camera pixels `pattern` lights, as pure geometry — no sensor."""
    yy, xx = np.mgrid[:CH, :CW]
    d = apply_transform(np.linalg.inv(M),
                        np.column_stack((xx.ravel(), yy.ravel())))
    dx = np.rint(d[:, 0]).astype(np.int64)
    dy = np.rint(d[:, 1]).astype(np.int64)
    ok = (dx >= 0) & (dx < DW) & (dy >= 0) & (dy < DH)
    lit = np.zeros(CW * CH, bool)
    lit[ok] = pattern[dy[ok], dx[ok]] > 127
    return lit.reshape(CH, CW)


def _part_calib() -> int:
    r = Report("dmd-calib")
    M = true_transform()
    c = DmdCalibration(cam_to_dmd=np.linalg.inv(M), dmd_size=(DW, DH),
                       cam_size=(CW, CH))

    # ── ROI → mask, the thing the transform is for ──
    rs = RoiSet()
    rs.add(RectRoi(x=180.0, y=130.0, w=60.0, h=40.0))
    roi = rs.mask((CH, CW))
    frame = rs.dmd_frame(c)
    r.check(frame.shape == (DH, DW) and set(np.unique(frame)) <= {0, 255},
            "the ROI becomes a device-sized binary frame")
    r.check(frame.max() == ON, "…emitted at full-on, not scaled")
    lit = footprint(frame, M)
    hit = (lit & roi).sum() / max(1, roi.sum())
    spill = (lit & ~roi).sum() / max(1, lit.sum())
    r.check(hit > 0.85 and spill < 0.15,
            f"projected, the mask lands on the ROI ({100 * hit:.0f}% covered, "
            f"{100 * spill:.0f}% spill)")
    # Or the check above would pass on any mask at all.
    off = c.dmd_to_cam.copy()
    off[0, 2] += 40.0
    bad = DmdCalibration(cam_to_dmd=np.linalg.inv(off), dmd_size=c.dmd_size,
                         cam_size=c.cam_size)
    hit_bad = (footprint(rs.dmd_frame(bad), M) & roi).sum() / max(1, roi.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px error in the transform misses "
            f"({100 * hit_bad:.0f}% covered)")

    # ── it round-trips through JSON ──
    p = Path(tempfile.mkdtemp()) / "calib.json"
    c.save(p)
    back = DmdCalibration.load(p)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.dmd_size == c.dmd_size and back.cam_size == c.cam_size
            and back.model == c.model,
            "a calibration survives save/load")
    old = c.to_dict() | {"model": "affine-noshear+corners", "rms_px": 0.4,
                         "stripes": [[0, 0.0, 1.0, 2.0]],
                         "vignette": [1.0, 2.0, 3.0]}
    back = DmdCalibration.from_dict(old)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.model == "affine-noshear+corners",
            "a file from the old sweep/corner tools still loads (its fit "
            "statistics and vignette ignored)")
    return r.finish()


# ═══ reticle ════════════════════════════════════════════════════════════

def check_display_modes(r: Report) -> None:
    """All ON / Image / ROIs each load a different frame, and ROI needs a calib."""
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, MODE_ALL_ON,
                                            MODE_PATTERN, MODE_ROI,
                                            DmdSettings, MockDmdController)

    A = np.array([[4.0, 0.0, 200.0], [0.0, 4.0, 150.0], [0.0, 0.0, 1.0]])
    calib = DmdCalibration(cam_to_dmd=np.linalg.inv(A),
                           dmd_size=(DEFAULT_W, DEFAULT_H), cam_size=(900, 600))
    cpath = Path(tempfile.mkdtemp()) / "c.json"
    calib.save(cpath)
    # Keys exactly as RectRoi.to_dict() writes them (see check_roi_wiring).
    roi = {"kind": "rect", "name": "r1", "enabled": True, "x": 450.0,
           "y": 300.0, "w": 120.0, "h": 90.0, "angle_deg": 0.0}

    c = MockDmdController(DmdSettings(display_mode=MODE_ALL_ON))
    c.load_pattern()
    all_on = c.on_pixels
    r.check(all_on == DEFAULT_W * DEFAULT_H,
            f"All ON turns on every mirror ({all_on})")

    c = MockDmdController(DmdSettings(display_mode=MODE_ROI, rois=(roi,),
                                      calib_path=str(cpath)))
    c.load_pattern()
    n_roi = c.on_pixels
    r.check(0 < n_roi < all_on,
            f"ROI mode lights only the ROI's mirrors ({n_roi} of {all_on})")
    # 120x90 camera px at 4 px/mirror is about 30x22 mirrors.
    r.check(abs(n_roi - (120 / 4) * (90 / 4)) < 0.4 * (120 / 4) * (90 / 4),
            f"…and about the right number of them ({n_roi}, expected ~675)")

    c = MockDmdController(DmdSettings(display_mode=MODE_ROI, rois=(roi,)))
    c.load_pattern()
    r.check(c.on_pixels == 0,
            "control: ROI mode with no calibration projects nothing rather "
            "than guessing a transform")
    c = MockDmdController(DmdSettings(display_mode=MODE_ROI,
                                      calib_path=str(cpath)))
    c.load_pattern()
    r.check(c.on_pixels == 0, "control: …and with no ROIs, likewise")

    # A guard once reloaded only when a pattern FILE was set.
    c = MockDmdController(DmdSettings(display_mode=MODE_PATTERN))
    c.apply_settings(DmdSettings(display_mode=MODE_ALL_ON))
    r.check(c.on_pixels == all_on,
            "changing the mode reloads the frame, with no pattern file set")


def check_project_frame(r: Report) -> None:
    """`project_frame` must not go through `build_frame`: the panel's scale
    and rotation would move the reticle off the pose being steered."""
    from acqApp.acq.devices import RawProjector
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, DmdSettings,
                                            MockDmdController)

    # Every knob set to visibly move a pattern.
    s = DmdSettings(scale_pct=57.0, rotation_deg=23.0, offset_x=-90.0,
                    offset_y=45.0, fit=True, invert=True)
    c = MockDmdController(s)
    r.check(isinstance(c, RawProjector),
            "the mock controller satisfies RawProjector")

    pattern = np.zeros((DEFAULT_H, DEFAULT_W), np.uint8)
    pattern[300:340, 600:620] = ON
    c.project_frame(pattern)
    held = c._pattern
    r.check(np.array_equal(held, pattern),
            "project_frame holds the frame EXACTLY — no scale, rotation, "
            "offset, invert or fit")

    from acqApp.devices.dmd import alp
    built = alp.build_frame(pattern, DEFAULT_W, DEFAULT_H, scale_pct=s.scale_pct,
                            rotation_deg=s.rotation_deg, offset_x=s.offset_x,
                            offset_y=s.offset_y, invert=s.invert, fit=s.fit)
    r.check(not np.array_equal(built, pattern),
            "control: run through build_frame the same pattern IS transformed, "
            "so the check above is not vacuous")

    # Padding a mis-sized frame would silently register the wrong panel.
    try:
        c.project_frame(np.zeros((10, 10), np.uint8))
        r.check(False, "a mis-sized frame is refused")
    except ValueError as e:
        r.check("device is" in str(e),
                f"a mis-sized frame is refused, naming both shapes ({e})")

    # After a calibration, Display (apply_settings, then display) must show
    # the selection, not the last reticle, though no setting changed.
    from acqApp.devices.dmd.control import MODE_ALL_ON
    on = MockDmdController(DmdSettings(display_mode=MODE_ALL_ON))
    on.apply_settings(on._s)
    on.project_frame(pattern)
    on.stop()
    on.apply_settings(on._s)
    r.check(bool(np.all(on.frame == 255)),
            "a raw frame is replaced on the next apply_settings, unchanged or not")


def check_wiring(r: Report) -> None:
    """The Calibrate button reaches the adapter, which starts the camera,
    opens the reticle alignment on a fresh frame, ends dark, puts the camera
    back, and saves what was aligned."""
    isolate_user_state()
    app = qt_app()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")

    r.check(hasattr(dmd, "calibrate"),
            "the DMD adapter owns the calibrate path, not the panel — only it "
            "can reach both the controller and the camera")

    # Requiring Live view first would add nothing: the dialog's own button is
    # the actuation decision.
    seen = {"box": 0, "dialog": 0, "exec": 0, "live_during": None}
    from PyQt6.QtWidgets import QDialog, QFileDialog, QMessageBox
    real_info = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.__setitem__("box", seen["box"] + 1))
    import acqApp.devices.dmd.reticle_align as RA
    from acqApp.devices.dmd.calibration import ReticlePose
    real_dlg = RA.ReticleAlignDialog
    tmp = Path(tempfile.mkdtemp())
    real_save = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (str(tmp / "aligned.json"), ""))

    class FakeDialog:
        """Stands in for the reticle window — it must be constructed AND
        exec'd, so a wiring that builds it and forgets to show it fails."""

        def __init__(self, project, dmd_size, frame, *, live_source=None,
                     seed=None, stop=None, parent=None):
            seen["dialog"] += 1
            seen["live_during"] = win._btn_run.isChecked()
            seen["cam_size"] = (frame.shape[1], frame.shape[0])
            self._project, self._dmd, self._cam = project, dmd_size, seen["cam_size"]
            self.calibration = None

        def exec(self):
            seen["exec"] += 1
            w, h = self._dmd
            self._project(np.full((h, w), ON, np.uint8))
            self.calibration = ReticlePose.centred(
                self._dmd, self._cam).to_calibration(self._dmd, self._cam)
            return QDialog.DialogCode.Accepted

    RA.ReticleAlignDialog = FakeDialog
    try:
        r.check(not win._btn_run.isChecked(), "control: the camera is stopped")
        dmd.panel.calibrate_requested.emit()
        r.check(seen["dialog"] == 1 and seen["exec"] == 1 and seen["box"] == 0,
                "the reticle alignment opens with the camera stopped — it "
                "starts the camera itself rather than refusing")
        r.check(seen["live_during"] is True and not win._btn_run.isChecked(),
                "…live while it is open, stopped again after")
        r.check(not dmd.controller._raw and not dmd.controller.projecting,
                "after it the DMD is dark and the held frame is the selection "
                "again, not the reticle the card would show")
        saved = tmp / "aligned.json"
        r.check(saved.is_file()
                and dmd.panel.settings.calib_path == str(saved)
                and DmdCalibration.load(saved).cam_size == seen["cam_size"],
                "what was aligned is saved and adopted, in the camera px of "
                "the frame it was aligned on")

        was = win.set_live(True)
        pump_until(app, lambda: win.latest_frame("voltage_cam") is not None)
        r.check(was is False and win._btn_run.isChecked(),
                "set_live(True) starts the live view and reports it was off")
        r.check(win.latest_frame("voltage_cam") is not None,
                "…and frames really flow after it")
        r.check(win.set_live(True) is True,
                "control: calling it again reports it was already on, so a "
                "dialog cannot stop a camera the operator started")
        win.set_live(False)
        pump(app, 0.3)
        r.check(not win._btn_run.isChecked(), "set_live(False) stops it again")
    finally:
        QMessageBox.information = real_info
        QFileDialog.getSaveFileName = real_save
        RA.ReticleAlignDialog = real_dlg

    r.check(dmd.metadata()["dmd_calibration"] == "aligned.json",
            "the adopted calibration lands in the session metadata")

    win._btn_run.setChecked(False)
    pump(app, 0.3)
    win.close()
    pump(app, 0.1)


def check_reticle(r: Report) -> None:
    """Align to reticle: the pose round-trips through a matrix, the projected
    shapes land on the reticle under that pose, and the dialog steers and
    applies it."""
    _app = qt_app()
    import acqApp.devices.dmd.reticle_align as RA
    from acqApp.devices.dmd.calibration import (ReticlePose, apply_transform,
                                                reticle_frame)

    dmd, cam = (64, 48), (400, 300)
    for p in (ReticlePose(30.0, 20.0, 12.5, 0.12, 0.1),
              ReticlePose(33.0, 25.0, -170.0, 0.15, 0.11, mirrored=True)):
        back = ReticlePose.from_calibration(p.to_calibration(dmd, cam))
        r.check(np.allclose([back.cx, back.cy, back.angle_deg, back.sx, back.sy],
                            [p.cx, p.cy, p.angle_deg, p.sx, p.sy])
                and back.mirrored == p.mirrored,
                f"a pose survives matrix -> pose (mirrored={p.mirrored})")

    pose = ReticlePose(30.0, 22.0, 20.0, 0.12, 0.1)
    disc = reticle_frame("centre", pose, dmd, cam)
    ys, xs = np.nonzero(disc)
    r.check(disc.shape == (48, 64) and disc.dtype == np.uint8
            and abs(xs.mean() - 30) < 0.5 and abs(ys.mean() - 22) < 0.5,
            "the disc sits on the mirror under the camera centre")
    box = reticle_frame("size", pose, dmd, cam, box_frac=0.5)
    ys, xs = np.nonzero(box)
    seen = apply_transform(np.linalg.inv(pose.matrix(cam)),
                           np.column_stack([xs, ys]).astype(float))
    r.check(np.all(np.abs(seen[:, 0] - 199.5) <= 100)
            and np.all(np.abs(seen[:, 1] - 149.5) <= 75)
            and np.ptp(seen[:, 0]) > 180,
            "the box's lit mirrors fill the reticle box on the camera")
    flipped = ReticlePose(30.0, 22.0, 20.0, 0.12, 0.1, mirrored=True)
    r.check(not np.array_equal(reticle_frame("angle", pose, dmd, cam),
                               reticle_frame("angle", flipped, dmd, cam)),
            "the L tells a mirror from a rotation")

    projected: list = []
    stops: list = []
    frame = np.zeros((300, 400), np.uint16)
    dlg = RA.ReticleAlignDialog(projected.append, dmd, frame,
                                stop=lambda: stops.append(1))
    dlg._send()
    dlg.nudge(RA.Qt.Key.Key_Right)
    dlg._send()
    r.check(projected == [] and not dlg._btn_light.isChecked(),
            "the dialog opens dark: no frame is projected, even after an edit, "
            "until Light on")
    dlg._btn_light.setChecked(True)
    r.check(len(projected) == 1 and projected[0].shape == (48, 64)
            and projected[0].any(),
            "Light on projects a device-sized frame at once")
    dlg._btn_light.setChecked(False)
    dlg._send()
    r.check(stops == [1] and len(projected) == 1,
            "Light off darkens the DMD and edits stop projecting")
    dlg.set_lit(True)
    projected.clear()
    cx = dlg.pose().cx
    dlg.nudge(RA.Qt.Key.Key_Right, 10)
    r.check(abs(dlg.pose().cx - cx - 10) < 1e-6, "Shift+→ moves the centre 10 mirrors")
    dlg._steps.button(2).setChecked(True)
    sy = dlg.pose().sy
    dlg.nudge(RA.Qt.Key.Key_Up)
    r.check(dlg.kind == "size" and dlg.pose().sy > sy, "↑ on the size step grows Y")
    want = dlg.pose()
    dlg._apply()
    c = dlg.calibration
    r.check(c is not None and c.model == "manual-reticle"
            and np.allclose(c.cam_to_dmd, want.matrix(cam)) and c.cam_size == cam,
            "Use this returns the steered pose as a calibration")

    seed = want.to_calibration(dmd, cam)
    dlg2 = RA.ReticleAlignDialog(projected.append, dmd, frame, seed=seed)
    r.check(np.allclose(dlg2.pose().matrix(cam), seed.cam_to_dmd, atol=1e-3),
            "a loaded calibration seeds the guess")
    dlg2.reject()
    other = ReticlePose(1.0, 1.0).to_calibration(dmd, (200, 100))
    dlg3 = RA.ReticleAlignDialog(projected.append, dmd, frame, seed=other)
    corners = ReticlePose.centred(dmd, cam).to_calibration(
        dmd, cam).accessible_corners()
    r.check(np.allclose(dlg3.pose().matrix(cam),        # the spins round
                        ReticlePose.centred(dmd, cam).matrix(cam), atol=0.05)
            and np.ptp(corners[:, 0]) <= 0.6 * cam[0] + 1e-6
            and np.ptp(corners[:, 1]) <= 0.6 * cam[1] + 1e-6,
            "a seed for another camera size is ignored: the guess starts "
            "upright and centred, the panel inside 60 % of the frame")
    dlg3.reject()

    n = {"i": 0}

    def source():
        n["i"] += 1
        return None if n["i"] < 3 else np.zeros((4, 4))

    got = RA.fresh_frame(source, settle=1, timeout_s=1.0)
    r.check(got.shape == (4, 4) and n["i"] == 4,
            "fresh_frame skips the frame on hand and `settle` more; equal "
            "but distinct arrays count as new (identity, not ==)")
    stuck = np.zeros((4, 4))
    try:
        RA.fresh_frame(lambda: stuck, timeout_s=0.1)
        r.check(False, "a stalled camera raises")
    except TimeoutError as e:
        r.check("Live view" in str(e), f"a stalled camera raises, naming the "
                                       f"cause ({str(e)[:50]}…)")


def check_reticle_seed(r: Report) -> None:
    """An older file's homography seeds the pose of its Jacobian at the
    camera centre (not M/M22), and a failed calibration save is reported,
    not raised."""
    _app = qt_app()
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    from acqApp.adapters.dmd import DmdModule
    from acqApp.devices.dmd.calibration import ReticlePose

    dmd, cam = (64, 48), (400, 300)
    p0 = ReticlePose(30.0, 20.0, 25.0, 0.12, 0.1, mirrored=True)
    c = np.array([(cam[0] - 1) / 2.0, (cam[1] - 1) / 2.0])
    g = np.array([3e-4, -2e-4])
    T, Ti = np.eye(3), np.eye(3)
    T[:2, 2], Ti[:2, 2] = c, -c
    persp = np.eye(3)
    persp[2, :2] = g
    # Fixes the centre with an identity Jacobian there, so p0 is the answer.
    H = p0.matrix(cam) @ T @ persp @ Ti
    seed = p0.to_calibration(dmd, cam)
    seed.cam_to_dmd = 2.0 * H                    # a DLT's scale is arbitrary
    back = ReticlePose.from_calibration(seed)
    r.check(np.allclose([back.cx, back.cy, back.angle_deg, back.sx, back.sy],
                        [p0.cx, p0.cy, p0.angle_deg, p0.sx, p0.sy], atol=1e-6)
            and back.mirrored,
            f"a perspective seed's pose is its local scale at the centre ({back})")

    adopted, warned = [], []
    fake = types.SimpleNamespace(
        panel=types.SimpleNamespace(calib_path="", set_calib_path=adopted.append),
        win=types.SimpleNamespace(status=lambda _m: None))
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_calsave_"))
    (tmp / "a_file").write_text("x")
    bad, good = str(tmp / "a_file" / "calib.json"), str(tmp / "calib.json")
    yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    real_ask, real_warn, real_q = (QFileDialog.getSaveFileName,
                                   QMessageBox.warning, QMessageBox.question)
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[-1]))

    def run(paths: list, answers: list):
        """Save As returns `paths` in turn; Discard? answers `answers`."""
        asked = []
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (paths.pop(0), ""))
        QMessageBox.question = staticmethod(
            lambda *a, **k: (asked.append(1), answers.pop(0))[1])
        try:
            DmdModule._save_calibration(fake, seed)
            return True, asked
        except Exception:                   # noqa: BLE001
            return False, asked

    try:
        ok, asked = run([bad, ""], [yes])
        r.check(ok and warned and "Could not save" in warned[0] and not adopted
                and asked == [1],
                "an unwritable path is reported, Save As asks again, and only "
                "a confirmed Discard drops the alignment")
        ok, asked = run(["", good], [no])
        r.check(ok and asked == [1] and adopted == [good]
                and Path(good).is_file(),
                "cancelling Save As and answering No to Discard goes back to "
                "Save As, so the alignment is saved, not lost")
    finally:
        QFileDialog.getSaveFileName = real_ask
        QMessageBox.warning, QMessageBox.question = real_warn, real_q
        shutil.rmtree(tmp, ignore_errors=True)


def check_bend(r: Report) -> None:
    """The checkerboard bend: a `Warp` shifts camera px bilinearly between
    its nodes, `to_cam` undoes `to_dmd` through it, the board and ROIs follow
    it onto the DMD, it persists, and the dialog's step 4 steers it."""
    _app = qt_app()
    import acqApp.devices.dmd.reticle_align as RA
    from acqApp.devices.dmd.calibration import (ReticlePose, Warp,
                                                checker_frame, checker_lattice)
    from acqApp.devices.dmd.roi import RectRoi, RoiSet

    dmd, cam = (128, 96), (800, 600)
    pose = ReticlePose(63.0, 47.0, 8.0, 0.2, 0.18, mirrored=True)
    xs, ys = checker_lattice(cam, 4)
    r.check(len(xs) == 5 and len(ys) == 4 and np.isclose(xs[0], 159.5),
            f"the board fills the box with near-square cells ({len(xs)}×{len(ys)})")
    w = Warp.flat(xs, ys).moved(6, 12.0, -8.0)        # row 1, column 1
    sx, sy = w.shift(np.array([xs[1], (xs[1] + xs[2]) / 2, -50.0]),
                     np.array([ys[1], ys[1], ys[1]]))
    r.check(np.allclose(sx, [12.0, 6.0, 0.0]) and np.allclose(sy, [-8.0, -4.0, 0.0]),
            "a node's shift is exact there, halves midway, and holds the "
            "edge value beyond the board")
    gx, gy = w.shift(np.arange(0.0, 800.0, 7.0)[None, :],
                     np.arange(0.0, 600.0, 5.0)[:, None])
    X, Y = np.meshgrid(np.arange(0.0, 800.0, 7.0), np.arange(0.0, 600.0, 5.0))
    ex, ey = w.shift(X, Y)
    r.check(np.allclose(gx, ex) and np.allclose(gy, ey),
            "the row × column fast path equals the per-point shift")

    flat = pose.to_calibration(dmd, cam)
    bent = pose.to_calibration(dmd, cam, w)
    r.check(pose.to_calibration(dmd, cam, Warp.flat(xs, ys)).warp is None
            and bent.warp is w and "bent" in bent.describe(),
            "a flat bend is dropped; a real one is kept and described")
    p = np.random.default_rng(3).uniform([0, 0], cam, (500, 2))
    r.check(np.abs(bent.to_cam(bent.to_dmd(p)) - p).max() < 0.05,
            "to_cam undoes to_dmd through the bend")
    W, H = dmd
    u, v = bent.to_cam_xy(np.arange(W, dtype=float)[None, :],
                          np.arange(H, dtype=float)[:, None])
    MX, MY = np.meshgrid(np.arange(W, dtype=float), np.arange(H, dtype=float))
    ue, ve = bent.to_cam_xy(MX, MY)
    mirror = 1.0 / np.sqrt(pose.sx * pose.sy)
    err = max(np.abs(u - ue).max(), np.abs(v - ve).max())
    r.check(err < 0.5 * mirror,
            f"the coarse panel unbend is within half a mirror ({err:.2f} px)")

    board = checker_frame(flat, xs, ys)
    lit_cam = flat.to_cam(np.column_stack(np.nonzero(board)[::-1]).astype(float))
    i = np.searchsorted(xs, lit_cam[:, 0], side="right") - 1
    j = np.searchsorted(ys, lit_cam[:, 1], side="right") - 1
    r.check(board.shape == (H, W) and board.any()
            and np.all((i + j) % 2 == 0) and i.min() >= 0 and i.max() < 4
            and j.min() >= 0 and j.max() < 3,
            "every lit mirror lands on a light square of the board")
    slide = Warp(xs, ys, np.full((4, 5), 10.0), np.zeros((4, 5)))
    slid = checker_frame(pose.to_calibration(dmd, cam, slide), xs, ys)
    moved = checker_frame(flat, xs + 10.0, ys)
    r.check(np.mean(slid != moved) < 0.005,
            "a uniform bend slides the board by its shift")

    rs = RoiSet()
    rs.add(RectRoi(x=330.0, y=250.0, w=120.0, h=90.0))
    lit = rs.dmd_frame(pose.to_calibration(dmd, cam, slide))
    rs2 = RoiSet()
    rs2.add(RectRoi(x=340.0, y=250.0, w=120.0, h=90.0))
    r.check(lit.any() and np.mean(lit != rs2.dmd_frame(flat)) < 0.005,
            "ROIs follow the bend onto the DMD")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_bend_"))
    try:
        back = type(bent).load(bent.save(tmp / "bent.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    r.check(back.warp is not None and np.allclose(back.warp.dx, w.dx)
            and np.allclose(back.to_dmd(p), bent.to_dmd(p)),
            "a bent calibration survives save -> load")

    projected: list = []
    frame = np.zeros((cam[1], cam[0]), np.uint16)
    dlg = RA.ReticleAlignDialog(projected.append, dmd, frame,
                                seed=pose.to_calibration(dmd, cam))
    dlg._steps.button(3).setChecked(True)
    dlg.set_lit(True)
    r.check(dlg.kind == "bend" and dlg.warp.is_flat
            and all(n.isVisible() for n in dlg._nodes)
            and not dlg._outline.isVisible()
            and projected and projected[-1].shape == (H, W)
            and projected[-1].any(),
            "step 4 shows the board's dots and projects the checkerboard")
    k = len(dlg.warp.xs) + 1
    dlg._pick(k)
    dlg.nudge(RA.Qt.Key.Key_Right)
    dlg.nudge(RA.Qt.Key.Key_Up, 10)
    m = 1.0 / np.sqrt(dlg.pose().sx * dlg.pose().sy)
    r.check(np.isclose(dlg.warp.dx.flat[k], m)
            and np.isclose(dlg.warp.dy.flat[k], -10 * m)
            and np.count_nonzero(dlg.warp.dx) == 1,
            "arrows move the picked dot one mirror (Shift ten), and only it")
    dlg._apply()
    got = dlg.calibration
    r.check(got is not None and got.warp is not None
            and np.isclose(got.warp.dx.flat[k], m),
            "Use this returns the bend with the pose")
    cols = int(dlg._spn_cols.value())
    dlg._spn_cols.setValue(cols + 2)
    r.check(len(dlg.warp.xs) == cols + 3 and not dlg.warp.is_flat,
            "a finer board keeps the bend")
    box = dlg._spn_box.value()
    for b in (box - 20, box - 10, box):
        dlg._spn_box.setValue(b)
    dlg._spn_cols.setValue(cols)
    r.check(np.allclose(dlg.warp.dx, got.warp.dx)
            and np.allclose(dlg.warp.dy, got.warp.dy),
            "Box/Board changes resample the original bend: back on its own "
            "board, the bend is exactly what was set, not degraded")
    dlg2 = RA.ReticleAlignDialog(projected.append, dmd, frame, seed=got)
    r.check(int(dlg2._spn_cols.value()) == cols
            and np.allclose(dlg2.warp.dx, got.warp.dx)
            and np.allclose(dlg2.warp.xs, got.warp.xs),
            "a bent seed restores its board and bend")
    dlg2._set_warp(None)
    r.check(dlg2.warp.is_flat, "Flatten undoes the bend")
    dlg2._reset()
    r.check(np.allclose(dlg2.warp.dx, got.warp.dx), "Reset restores the seed's bend")
    dlg2.reject()


def _part_reticle() -> int:
    r = Report("dmd-reticle")
    check_reticle(r)
    check_reticle_seed(r)
    check_bend(r)
    check_display_modes(r)
    check_project_frame(r)
    check_wiring(r)
    return r.finish()


# ═══ roi ════════════════════════════════════════════════════════════════
# Shares DW, DH, CW, CH with the calib part.


def calib() -> DmdCalibration:
    """A DMD sitting rotated and offset inside a slightly larger camera FOV."""
    return DmdCalibration(cam_to_dmd=np.linalg.inv(true_transform()),
                          dmd_size=(DW, DH), cam_size=(CW, CH),
                          created="2026-08-18T00:00:00")


def _reach_unbounded(rset, calib, max_side: int = 512) -> float:
    """`reach_fraction` with no bounding (every ROI over the whole grid): the
    reference the bounded one must reproduce bit for bit."""
    w, h = calib.cam_size
    step = max(1, int(np.ceil(max(int(w), int(h)) / max(1, max_side))))
    xs = np.arange(0, int(w), step, dtype=np.float64)
    ys = np.arange(0, int(h), step, dtype=np.float64)
    want = np.zeros((ys.size, xs.size), bool)
    for r in rset.rois:
        if r.enabled:
            want |= r.mask_at(xs, ys)
    iy, ix = np.nonzero(want)
    if not iy.size:
        return 1.0
    return float(calib.accessible(np.column_stack((xs[ix], ys[iy]))).mean())


def _set(*rois) -> RoiSet:
    """A RoiSet of the given ROIs — `add()` names them as it goes."""
    st = RoiSet()
    for roi in rois:
        st.add(roi)
    return st


def _part_roi() -> int:
    r = Report("dmd-roi")
    c = calib()

    # ── 1. shapes ────────────────────────────────────────────────────────────
    rect = RectRoi(x=100, y=80, w=40, h=20)
    m = rect.mask((CH, CW))
    r.check(m.sum() > 0 and abs(m.sum() - 41 * 21) < 60,
            f"rect mask covers about w*h px ({m.sum()} vs {41*21})")
    ys, xs = np.nonzero(m)
    r.check(abs(xs.mean() - 100) < 0.6 and abs(ys.mean() - 80) < 0.6,
            f"…centred on (x, y) ({xs.mean():.1f}, {ys.mean():.1f})")

    turned = RectRoi(x=100, y=80, w=40, h=20, angle_deg=90).mask((CH, CW))
    r.check(abs(turned.sum() - m.sum()) < 60,
            "a rotated rect keeps its area")
    r.check((turned != m).sum() > 0.5 * m.sum(),
            "control: rotating by 90 deg really moves the covered pixels")

    circ = CircleRoi(x=160, y=120, r=25)
    cm = circ.mask((CH, CW))
    r.check(abs(cm.sum() - np.pi * 25 ** 2) / (np.pi * 25 ** 2) < 0.03,
            f"circle mask is pi*r^2 within 3% ({cm.sum()})")

    # ── free-form outlines ───────────────────────────────────────────────────
    tri = PolyRoi(points=[[100, 50], [200, 50], [150, 150]])        # area 5000
    tm = tri.mask((CH, CW))
    r.check(abs(tm.sum() - 5000) / 5000 < 0.03,
            f"a triangle's mask is its area within 3% ({tm.sum()} vs 5000)")
    r.check(tri.contains(np.array([150.0, 110.0, 10.0]),
                         np.array([80.0, 140.0, 10.0])).tolist()
            == [True, False, False],
            "contains() agrees: inside, outside beside the slanted edge, far away")
    ell = PolyRoi(points=[[0, 0], [100, 0], [100, 40], [40, 40], [40, 100],
                          [0, 100]])                                 # an L, 6400
    r.check(abs(ell.mask((CH, CW)).sum() - 6400) / 6400 < 0.03
            and not ell.contains(np.array([70.0]), np.array([70.0]))[0]
            and ell.contains(np.array([20.0]), np.array([70.0]))[0],
            "a concave outline is filled even-odd: the notch stays dark")
    gx, gy = np.arange(0, 300.0), np.arange(0, 200.0)
    # Half-pixel offsets: a point exactly ON an edge is a convention, not a fact.
    by_grid = tri.mask_at(gx + 0.5, gy + 0.5)
    by_points = tri.contains((gx + 0.5)[None, :], (gy + 0.5)[:, None])
    r.check((by_grid != by_points).sum() < 0.002 * by_grid.sum(),
            "the grid test and the scattered-point test agree "
            f"({(by_grid != by_points).sum()} px differ)")
    r.check((tri.x, tri.y, tri.w, tri.h) == (150.0, 100.0, 100.0, 100.0),
            "x, y, w, h are the bounding box")
    sq = PolyRoi(points=[[0, 0], [40, 0], [40, 20], [0, 20]])
    sq.x, sq.y = 100.0, 80.0
    sq.w = 80.0
    r.check(abs(sq.x - 100) < 1e-9 and abs(sq.y - 80) < 1e-9
            and abs(sq.w - 80) < 1e-9 and abs(sq.h - 20) < 1e-9,
            "moving then widening scales about the centre: "
            f"({sq.x:g}, {sq.y:g}) {sq.w:g}x{sq.h:g}")
    back = roi_from_dict(tri.to_dict())
    r.check(isinstance(back, PolyRoi) and back.points == tri.points,
            "a free-form ROI survives to_dict/roi_from_dict")
    tt = np.linspace(0, 2 * np.pi, 600, endpoint=False)
    noisy = np.column_stack((300 + 80 * np.cos(tt), 240 + 60 * np.sin(tt))) \
        + np.random.default_rng(3).normal(0, 0.3, (600, 2))
    thin = simplify_polygon(noisy, tol=1.5)
    thin_area = PolyRoi(points=thin.tolist()).mask((480, 640)).sum()
    r.check(3 <= len(thin) <= 60
            and abs(thin_area - np.pi * 80 * 60) / (np.pi * 80 * 60) < 0.05,
            f"a 600-point trace thins to {len(thin)} corners and keeps its "
            f"area ({thin_area} vs {np.pi * 80 * 60:.0f})")
    fc = c.accessible_corners().mean(axis=0)
    box = PolyRoi(points=[[fc[0] - 30, fc[1] - 30], [fc[0] + 30, fc[1] - 30],
                          [fc[0] + 30, fc[1] + 30], [fc[0] - 30, fc[1] + 30]])
    f_poly = _set(box).dmd_frame(c)
    f_rect = _set(RectRoi(x=fc[0], y=fc[1], w=60, h=60)).dmd_frame(c)
    on_p, on_r = int((f_poly == f_poly.max()).sum()), int((f_rect == f_rect.max()).sum())
    r.check(on_r > 0 and abs(on_p - on_r) / on_r < 0.03,
            f"projected to mirrors, a polygon square lights what the rectangle "
            f"does ({on_p} vs {on_r} mirrors)")

    # ── 2. the set ───────────────────────────────────────────────────────────
    s = RoiSet()
    s.add(RectRoi(x=100, y=80, w=40, h=20))
    s.add(CircleRoi(x=160, y=120, r=25))
    s.add(RectRoi(x=100, y=80, w=40, h=20))
    r.check([x.name for x in s] == ["rect1", "circle1", "rect2"],
            f"auto-named without collisions ({[x.name for x in s]})")
    r.check(s.mask((CH, CW)).sum() == (m | cm).sum(),
            "the set's mask is the union of its ROIs")

    s[1].enabled = False
    r.check(s.mask((CH, CW)).sum() == m.sum(),
            "disabling one drops it from the union")
    s[1].enabled = True

    again = RoiSet.from_list(s.to_list())
    r.check(len(again) == 3
            and np.array_equal(again.mask((CH, CW)), s.mask((CH, CW))),
            "a set survives to_list/from_list unchanged")
    r.check(isinstance(roi_from_dict({"kind": "circle", "x": 1, "y": 2, "r": 3}),
                       CircleRoi),
            "roi_from_dict rebuilds the right class")

    # ── 3. the accessible area ───────────────────────────────────────────────
    reach = c.accessible_mask((CH, CW))
    r.check(0.2 < reach.mean() < 0.95,
            f"the DMD reaches part but not all of the camera "
            f"({100 * reach.mean():.0f}%)")
    # The editor rebuilds this on every drag; whole-grid versions cost ~800 ms
    # and ~1 GB per drag at ORCA full frame.
    BH, BW = 1200, 1600
    grid = 2 * BH * BW * 8                      # what np.mgrid alone would cost

    def mask_peak(rows: int) -> int:
        """Peak allocation for one accessible_mask, on a fresh calibration so
        the cache cannot hide the work."""
        cal = DmdCalibration(cam_to_dmd=c.cam_to_dmd, dmd_size=(DW, DH),
                             cam_size=(BW, rows), model="homography")
        tracemalloc.start()
        cal.accessible_mask((rows, BW))
        _cur, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return pk

    # Banded: doubling the height adds only the extra output rows, not the
    # two int64 grids a whole-grid version would.
    grew = mask_peak(2 * BH) - mask_peak(BH)
    r.check(grew < 4 * BH * BW,
            f"accessible_mask is banded: doubling the height added "
            f"{grew / 2**20:.1f} MB, against {BH * BW / 2**20:.1f} MB of extra "
            f"output (a coordinate grid would add {grid / 2**20:.1f})")

    big = DmdCalibration(cam_to_dmd=c.cam_to_dmd, dmd_size=(DW, DH),
                         cam_size=(BW, BH), model="homography")
    m1 = big.accessible_mask((BH, BW))
    r.check(big.accessible_mask((BH, BW)) is m1 and not m1.flags.writeable,
            "…and is cached per shape, handed out read-only")

    tracemalloc.start()
    RectRoi(x=800, y=600, w=200, h=150).mask((BH, BW))
    _cur, peak_roi = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    r.check(peak_roi < 0.5 * grid,
            f"an ROI mask broadcasts rather than gridding: peak "
            f"{peak_roi / 2**20:.1f} MB")
    tracemalloc.start()
    _yy, _xx = np.mgrid[:BH, :BW]
    _cur, peak_grid = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del _yy, _xx
    r.check(peak_grid >= 0.5 * grid,
            f"control: np.mgrid alone costs {peak_grid / 2**20:.1f} MB, over "
            f"the budget both checks above pass")

    corners = c.accessible_corners()
    # Nudged 2 px toward the centroid: the exact corner is the boundary, and
    # whether it rounds in or out is not what this is checking.
    inset = corners + 2.0 * (corners.mean(axis=0) - corners) / np.linalg.norm(
        corners.mean(axis=0) - corners, axis=1, keepdims=True)
    r.check(bool(c.accessible(inset).all()),
            f"the field's own corners are inside the accessible mask "
            f"({c.accessible(inset)})")
    outset = corners - 6.0 * (corners.mean(axis=0) - corners) / np.linalg.norm(
        corners.mean(axis=0) - corners, axis=1, keepdims=True)
    r.check(not c.accessible(outset).any(),
            f"control: just outside the field is not accessible "
            f"({c.accessible(outset)})")

    far = RoiSet()
    far.add(CircleRoi(x=CW - 5, y=CH - 5, r=20))     # deliberately off the field
    r.check(far.outside(c) == ["circle1"],
            f"an ROI off the DMD field is reported ({far.outside(c)})")
    _, kept = far.clipped_mask(c)
    r.check(kept < 0.9, f"…and its unreachable part is not counted ({kept:.2f})")

    # outside() is geometric: a raster test sees no pixels off the IMAGE edge,
    # so it judged only the visible part and called it fine.
    off = RoiSet()
    off.add(CircleRoi(x=float(corners[:, 0].mean()), y=-6.0, r=30))
    r.check(off.outside(c) == ["circle1"],
            f"an ROI hanging off the image edge is reported, not judged on the "
            f"part that happens to be visible ({off.outside(c)})")
    inn = RoiSet()
    inn.add(CircleRoi(x=float(corners[:, 0].mean()),
                      y=float(corners[:, 1].mean()), r=30))
    r.check(inn.outside(c) == [],
            f"control: the same circle inside the field is not flagged "
            f"({inn.outside(c)})")

    for st in (far, off, inn):
        _, exact = st.clipped_mask(c)
        r.check(abs(st.reach_fraction(c) - exact) < 0.05,
                f"reach_fraction tracks clipped_mask "
                f"({st.reach_fraction(c):.3f} vs {exact:.3f})")
    # reach_fraction bounds each ROI to its bbox. A bbox one cell tight stays
    # plausible and passed the 5 % check above, so demand exact agreement
    # with the unbounded algorithm.
    for name, st in (
            ("two overlapping circles", _set(CircleRoi(x=40.0, y=60.0, r=34),
                                             CircleRoi(x=58.0, y=60.0, r=34))),
            ("two disjoint, far apart", _set(CircleRoi(x=38.0, y=40.0, r=26),
                                             CircleRoi(x=CW * 0.7, y=CH * 0.6,
                                                       r=26))),
            ("rotated rect", _set(RectRoi(x=44.0, y=90.0, w=90, h=30,
                                          angle_deg=37.0))),
            ("straddling the image edge", _set(RectRoi(x=4.0, y=CH * 0.5,
                                                       w=60, h=40))),
            ("one disabled of two", _set(CircleRoi(x=42.0, y=150.0, r=30),
                                         CircleRoi(x=CW * 0.6, y=CH * 0.5, r=30,
                                                   enabled=False))),
    ):
        ref = _reach_unbounded(st, c)
        got = st.reach_fraction(c)
        # Partial, or the two agree only because both saturated.
        r.check(0.02 < ref < 0.98 and got == ref,
                f"reach_fraction bounds correctly — {name} "
                f"({got!r} vs unbounded {ref!r})")

    # If off- and on-field sets read the same, the checks above prove nothing.
    lo = _set(CircleRoi(x=CW - 5, y=CH - 5, r=20)).reach_fraction(c)
    hi = _set(CircleRoi(x=float(corners[:, 0].mean()),
                        y=float(corners[:, 1].mean()), r=20)).reach_fraction(c)
    r.check(lo < 0.5 < hi,
            f"control: reach_fraction separates an off-field set from an "
            f"on-field one ({lo:.3f} vs {hi:.3f})")

    near = RoiSet()
    near.add(CircleRoi(x=float(corners[:, 0].mean()),
                       y=float(corners[:, 1].mean()), r=15))
    r.check(near.outside(c) == [],
            f"control: an ROI in the middle of the field is not flagged "
            f"({near.outside(c)})")

    # ── 4. the round trip that matters ───────────────────────────────────────
    want = near.mask((CH, CW))
    frame = near.dmd_frame(c)
    r.check(frame.shape == (DH, DW) and set(np.unique(frame)) <= {0, 255},
            f"the ROI becomes a device-sized binary frame {frame.shape}")

    # Project it back through the same optics (calib()'s) and see where it
    # lands.
    lit = footprint(frame, true_transform())
    hit = (lit & want).sum() / max(1, want.sum())
    spill = (lit & ~want).sum() / max(1, lit.sum())
    r.check(hit > 0.9 and spill < 0.12,
            f"projected, the mask lands on the ROI ({100*hit:.0f}% covered, "
            f"{100*spill:.0f}% spill)")

    bad_cal = DmdCalibration(cam_to_dmd=c.cam_to_dmd.copy(),
                             dmd_size=c.dmd_size, cam_size=c.cam_size)
    off = c.dmd_to_cam.copy()
    off[0, 2] += 40.0
    bad_cal.cam_to_dmd = np.linalg.inv(off)
    lit_bad = footprint(near.dmd_frame(bad_cal), true_transform())
    hit_bad = (lit_bad & want).sum() / max(1, want.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px registration error misses ({100*hit_bad:.0f}%)")


    # ── 6. the editor ────────────────────────────────────────────────────────
    isolate_user_state()
    app = qt_app()                      # assign it: an unreferenced one is GC'd
    from acqApp.devices.dmd.roi_panel import RoiEditor

    ed = RoiEditor(c)
    ed.set_image(np.random.default_rng(0).integers(0, 255, (CH, CW), dtype=np.uint8))
    r.check(len(ed.roi_set) == 0, "the editor starts empty")

    # ── contrast: an ORCA frame is 16-bit with hot pixels ───────────────────
    # autoLevels stretches to min/max, so two hot pixels blacken the image.
    rng16 = np.random.default_rng(1)
    frame16 = rng16.normal(1400, 60, (CH, CW)).astype(np.uint16)
    frame16[3, 4] = 65000                       # every sCMOS has a few
    ed.set_image(frame16)
    lo, hi = ed._img.getLevels()
    s1, s99 = np.percentile(frame16[::4, ::4], (1, 99))
    r.check(abs(lo - s1) < 1 and abs(hi - s99) < 1,
            f"levels come from the 1st/99th percentile ({lo:.0f}-{hi:.0f}), "
            f"not min/max")
    # A strided view, because percentile sorts (87 ms at ORCA full frame).
    f1, f99 = np.percentile(frame16, (1, 99))
    span = float(f99 - f1)
    r.check(abs(lo - f1) < 0.02 * span and abs(hi - f99) < 0.02 * span,
            f"…and 1/16 of the pixels give the same answer as all of them "
            f"({lo:.0f}-{hi:.0f} vs {f1:.0f}-{f99:.0f}, span {span:.0f})")
    r.check(hi < 0.1 * frame16.max(),
            f"control: a hot pixel at {frame16.max()} would have stretched the "
            f"range to it; the shown top is {hi:.0f}")
    ed.set_image(np.full((CH, CW), 700, np.uint16))
    flo, fhi = ed._img.getLevels()
    r.check(fhi >= flo, f"a flat frame still gives a usable range ({flo}-{fhi})")
    ed.set_image(frame16)

    # ── drawing: a drag places an ROI where you put it ──────────────────────
    before = len(ed.roi_set)
    ed._on_drawn((100.0, 80.0), (160.0, 130.0))
    if r.check(len(ed.roi_set) == before + 1, "a drag on the image adds an ROI"):
        roi = list(ed.roi_set)[-1]
        r.check(abs(roi.x - 130) < 1 and abs(roi.y - 105) < 1,
                f"…centred on the drag, not on the middle of the field "
                f"({roi.x:.0f}, {roi.y:.0f})")
        r.check(abs(roi.w - 60) < 1 and abs(roi.h - 50) < 1,
                f"…and sized by it ({roi.w:.0f}x{roi.h:.0f})")
    # The real drag path: mouseDragEvent maps local coordinates to the image.
    from PyQt6.QtCore import QPointF, Qt as _Qt
    ed._on_clear()
    r.check(ed._tool == "rectangle" and ed._tool_btns["rectangle"].isChecked(),
            "a drag draws from the start: no hidden Draw mode to arm")
    vb = ed._vb

    class _Ev:
        """Duck-types exactly what _DrawViewBox.mouseDragEvent calls."""

        def __init__(self, down, now, finish=True):
            self._d, self._p, self._f = QPointF(*down), QPointF(*now), finish
            self.accepted = False

        def button(self): return _Qt.MouseButton.LeftButton
        def buttonDownPos(self, *_a): return self._d
        def pos(self): return self._p
        def isFinish(self): return self._f
        def accept(self): self.accepted = True

    want_a, want_b = (200.0, 150.0), (400.0, 330.0)
    la, lb = vb.mapFromView(QPointF(*want_a)), vb.mapFromView(QPointF(*want_b))
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    if r.check(len(ed.roi_set) == 1, "a real drag event creates one ROI"):
        got = list(ed.roi_set)[0]
        r.check(abs(got.x - 300) < 0.5 and abs(got.y - 240) < 0.5
                and abs(got.w - 200) < 0.5 and abs(got.h - 180) < 0.5,
                f"…exactly where it was dragged: centre ({got.x:.1f}, "
                f"{got.y:.1f}) {got.w:.0f}x{got.h:.0f}, want (300, 240) 200x180")

    ed._on_clear()
    mid = _Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False)
    vb.mouseDragEvent(mid)
    r.check(vb._rect.isVisible() and len(ed.roi_set) == 0,
            "mid-drag shows the band and commits nothing")
    band = vb._rect.rect()
    r.check(abs(band.width() - 200) < 0.5 and abs(band.height() - 180) < 0.5,
            f"…the size being dragged ({band.width():.0f}x{band.height():.0f})")
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    r.check(not vb._rect.isVisible(), "…and it clears on release")

    ed._on_clear()
    ed.set_tool("circle")
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False))
    er = vb._ellipse.rect()
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    made = list(ed.roi_set)[0]
    r.check(abs(er.width() / 2 - made.r) < 0.5,
            f"the circle band previews the radius it creates "
            f"({er.width() / 2:.1f} vs {made.r:.1f})")
    want_r = float(np.hypot(want_b[0] - want_a[0], want_b[1] - want_a[1]))
    r.check(abs(made.x - want_a[0]) < 0.5 and abs(made.y - want_a[1]) < 0.5
            and abs(made.r - want_r) < 0.5,
            f"a circle is drawn from its centre (the press) out to the pointer: "
            f"centre ({made.x:.0f}, {made.y:.0f}) r={made.r:.0f}, "
            f"want {want_a} r={want_r:.0f}")
    ed._on_clear()
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False))
    ed.set_tool("pan")
    r.check(not vb._rect.isVisible() and not vb._ellipse.isVisible(),
            "switching to Pan clears any band left on screen")
    r.check(not vb._draw and vb.tool == "pan",
            "…and with Pan armed a left-drag is left to pyqtgraph's panning")
    ed.set_tool("rectangle")
    r.check(vb._draw and vb.tool == "rectangle",
            "…and picking a shape tool arms drawing again")
    ed._on_clear()

    n = len(ed.roi_set)
    ed._on_drawn((200.0, 200.0), (200.5, 200.5))
    r.check(len(ed.roi_set) == n,
            "control: a click (not a drag) adds nothing")
    ed._on_clear()

    seen: list = []
    ed.rois_changed.connect(seen.append)
    fc = c.accessible_corners().mean(axis=0)
    fx, fy = float(fc[0]), float(fc[1])
    ed.set_tool("rectangle")
    ed._on_drawn((fx - 50, fy - 40), (fx + 50, fy + 40))
    ed.set_tool("circle")
    ed._on_drawn((fx, fy), (fx + 40, fy))
    r.check(len(ed.roi_set) == 2 and len(seen) == 2,
            f"adding one of each emits and lands in the set "
            f"({len(ed.roi_set)} rois, {len(seen)} signals)")
    sizes = lambda: (ed._table.maximumHeight(), ed._hint.maximumHeight(),   # noqa: E731
                     ed._legend.maximumHeight(), ed._status.maximumHeight())
    h2 = sizes()
    for _ in range(7):
        ed._on_drawn((fx - 50, fy - 40), (fx + 50, fy + 40))
    h9 = sizes()
    ed.set_tool("free")
    ed.set_tool("pan")
    hp = sizes()
    ed.set_tool("rectangle")
    ed._on_clear()
    h0 = sizes()
    r.check(h0 == h2 == h9 == hp and ed._table.rowCount() == 0,
            f"the table, hint and status never change size as ROIs are added "
            f"or the tool changes (empty {h0}, 2 rois {h2}, 9 rois {h9})")
    five = (ed._table.verticalHeader().defaultSectionSize() * 5
            + ed._table.horizontalHeader().sizeHint().height())
    r.check(five <= ed._table.maximumHeight() <= five + 12,
            f"…and the table is five rows tall ({ed._table.maximumHeight()} px "
            f"for {five})")
    r.check(ed._table.verticalScrollBarPolicy()
            != _Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            "…and scrolls once there are more")
    ed.set_tool("rectangle")
    ed._on_drawn((fx - 50, fy - 40), (fx + 50, fy + 40))
    ed.set_tool("circle")
    ed._on_drawn((fx, fy), (fx + 40, fy))
    seen.clear()
    kinds = [x.kind for x in ed.roi_set]
    r.check(kinds == ["rect", "circle"], f"both shapes are creatable ({kinds})")

    r.check(ed.roi_set.outside(c) == [],
            f"a freshly added ROI is inside the DMD field "
            f"({ed.roi_set.outside(c)})")

    before = (ed.roi_set[0].x, ed.roi_set[0].y)
    ed._items[0].setPos([ed._items[0].pos()[0] + 12,
                         ed._items[0].pos()[1] + 7])
    ed._on_item_changed()
    after = (ed.roi_set[0].x, ed.roi_set[0].y)
    r.check(abs(after[0] - before[0] - 12) < 0.6
            and abs(after[1] - before[1] - 7) < 0.6,
            f"moving the handle moves the model {before} -> {after}")

    # ── selection from the image, typed numbers, the Delete key ──────────────
    keys_ = [f[0] for f in ed._FIELDS]

    def cell(i, key):
        return ed._table.item(i, ed._NUM0 + keys_.index(key))

    def edit(i, key, text):
        cell(i, key).setText(text)      # what committing a typed cell does

    r.check([ed._table.horizontalHeaderItem(c).text()
             for c in range(ed._table.columnCount())]
            == ["Name", "Shape", "X", "Y", "W", "H", "Radius", "Angle (°)"],
            "the ROI table has one labelled column per parameter")
    ed._select_item(ed._items[1])
    r.check(ed._table.currentRow() == 1,
            "clicking an ROI on the image selects its table row")
    r.check(cell(1, "w").text() == "—" and cell(1, "r").text() != "—"
            and not cell(1, "w").flags() & _Qt.ItemFlag.ItemIsEditable,
            "a circle's W/H/Angle cells read '—' and can't be edited")
    r.check(cell(0, "r").text() == "—" and cell(0, "w").text() != "—",
            "…and a rectangle's Radius cell does")
    edit(1, "r", "55")
    r.check(abs(ed.roi_set[1].r - 55.0) < 0.1
            and abs(ed._items[1].size()[0] / 2 - 55.0) < 0.1,
            "typing a radius resizes the circle on the image and in the model")
    edit(1, "r", "abc")
    r.check(abs(ed.roi_set[1].r - 55.0) < 0.1 and cell(1, "r").text() == "55.0",
            "…text that isn't a number is put back")
    ed._table.setCurrentCell(0, 0)
    y0 = ed.roi_set[0].y
    edit(0, "x", "400")
    edit(0, "angle_deg", "30")
    got = ed.roi_set[0]
    r.check(abs(got.x - 400.0) < 0.1 and abs(got.y - y0) < 0.1
            and abs(got.angle_deg - 30.0) < 0.1,
            f"typing x and an angle turns the rectangle about its centre "
            f"({got.x:.1f}, {got.y:.1f}) {got.angle_deg:.0f} deg")
    ed.load(ed.roi_set)
    back = ed.roi_set[0]
    r.check(abs(back.x - got.x) < 0.1 and abs(back.y - got.y) < 0.1
            and abs(back.angle_deg - 30.0) < 0.1,
            f"a rotated ROI survives a reload (was dropped to 0 deg before): "
            f"{back.angle_deg:.0f} deg")
    # ── handles say what they do ──────────────────────────────────────────────
    roles = lambda i: sorted(h["item"]._role for h in ed._items[i].handles)  # noqa: E731
    r.check(roles(0) == ["resize", "rotate"] and roles(1) == ["resize"],
            f"a rectangle has a resize and a rotate handle, a circle only "
            f"resize ({roles(0)}, {roles(1)})")
    curs = {h["item"]._role: h["item"].cursor().shape()
            for h in ed._items[0].handles}
    r.check(curs["resize"] == _Qt.CursorShape.SizeFDiagCursor
            and curs["rotate"] == _Qt.CursorShape.BitmapCursor,
            "…each with its own cursor: a resize arrow, a drawn rotate arrow")
    ed._add(PolyRoi(points=[[fx - 20, fy - 20], [fx + 20, fy - 20], [fx, fy + 20]]))
    r.check(ed._items[-1].handles == [],
            "a free-form outline has no handles: it moves as one piece")
    ed._table.setCurrentCell(len(ed.roi_set) - 1, 0)
    ed._on_delete()
    r.check("rotate" in ed._legend.text() and "resize" in ed._legend.text(),
            "the legend under the hint names the handles")

    # ── free-form: trace, reshape, add and remove corners ─────────────────────
    n0 = len(ed.roi_set)
    ed.set_tool("free")
    ring = [(300.0 + 80 * np.cos(t), 240.0 + 60 * np.sin(t))
            for t in np.linspace(0, 2 * np.pi, 120, endpoint=False)]
    p0 = vb.mapFromView(QPointF(*ring[0]))
    for pt in ring[1:-1]:
        q = vb.mapFromView(QPointF(*pt))
        vb.mouseDragEvent(_Ev((p0.x(), p0.y()), (q.x(), q.y()), finish=False))
    r.check(vb._stroke.isVisible() and len(ed.roi_set) == n0,
            "mid-trace the outline shows and commits nothing")
    q = vb.mapFromView(QPointF(*ring[-1]))
    vb.mouseDragEvent(_Ev((p0.x(), p0.y()), (q.x(), q.y())))
    poly_ok = r.check(len(ed.roi_set) == n0 + 1
                      and ed.roi_set[n0].kind == "poly",
                      "letting go closes the trace into a free-form ROI")
    r.check(not vb._stroke.isVisible(), "…and the trace outline clears")
    if poly_ok:
        pr = ed.roi_set[n0]
        pa = PolyRoi(points=pr.points).mask((480, 640)).sum()
        r.check(3 <= len(pr.points) <= 60
                and abs(pa - np.pi * 80 * 60) / (np.pi * 80 * 60) < 0.06
                and abs(pr.x - 300) < 1.5 and abs(pr.y - 240) < 1.5,
                f"a 120-sample trace keeps {len(pr.points)} corners, its area "
                f"and its place ({pa} px, centre {pr.x:.0f}, {pr.y:.0f})")
        r.check(ed._table.item(n0, 1).text() == "Free-form"
                and cell(n0, "r").text() == "—" and cell(n0, "angle_deg").text() == "—"
                and cell(n0, "w").text() != "—",
                "its table row says Free-form, with W/H but no Radius or Angle")
        before = [list(p) for p in pr.points]
        ed._on_item_changed()
        r.check(np.allclose(ed.roi_set[n0].points, before, atol=1e-6),
                "reading the corners back off the image is lossless")
        edit(n0, "w", "100")
        r.check(abs(ed.roi_set[n0].w - 100) < 0.01
                and abs(ed.roi_set[n0].x - 300) < 1.5
                and len(ed.roi_set[n0].points) == len(before),
                "typing a width rescales it on the image and in the model "
                f"({ed.roi_set[n0].w:.1f})")
        it_p = ed._items[n0]
        mid_before = (ed.roi_set[n0].x, ed.roi_set[n0].y)
        it_p.setPos([it_p.pos()[0] + 25, it_p.pos()[1] - 10])   # a drag moves the item
        ed._on_item_changed()
        r.check(abs(ed.roi_set[n0].x - mid_before[0] - 25) < 0.01
                and abs(ed.roi_set[n0].y - mid_before[1] + 10) < 0.01,
                "dragging the outline moves every point of the model with it")
        edit(n0, "y", "300")
        r.check(abs(ed.roi_set[n0].y - 300) < 0.01 and it_p.pos()[1] == 0
                and abs(ed.roi_set[n0].x - mid_before[0] - 25) < 0.01,
                "…and a typed position after a drag is still exact")
        r.check(it_p.shape().contains(QPointF(*it_p.local_points()[0]))
                or it_p.shape().boundingRect().contains(
                    QPointF(*np.mean(it_p.local_points(), axis=0))),
                "the outline is hit-testable, so a drag inside it picks it up")
    ed._table.setCurrentCell(n0, 0)
    ed._on_delete()
    r.check(len(ed.roi_set) == n0, "free-form ROIs delete like the others")
    ed.set_tool("rectangle")

    from PyQt6.QtGui import QShortcut
    keys = ed.findChildren(QShortcut)
    r.check(len(keys) == 2, f"Delete and Backspace are both bound ({len(keys)})")
    ed._table.setCurrentCell(0, 0)
    keys[0].activated.emit()
    r.check(len(ed.roi_set) == 1, "the Delete key removes the selected ROI")
    r.check("Delete" not in ed._hint.text(),
            "…and the hint stops offering Delete once nothing is selected")
    ed.set_tool("pan")
    r.check("pan" in ed._hint.text().lower(), "the hint follows the tool")
    ed.set_tool("rectangle")
    ed._on_drawn((fx - 50, fy - 40), (fx + 50, fy + 40))
    ed._table.setCurrentCell(0, 0)
    ed._on_delete()
    r.check(len(ed.roi_set) == 1, "delete removes the selected ROI")
    ed._on_clear()
    r.check(len(ed.roi_set) == 0, "clear empties the set")

    ed2 = RoiEditor(None)
    r.check("calibration" in ed2._status.text().lower(),
            f"with no calibration the editor says so ({ed2._status.text()!r})")
    ed2._on_drawn((50.0, 50.0), (90.0, 80.0))
    r.check(len(ed2.roi_set) == 1,
            "…but still allows drawing, so ROIs can be prepared beforehand")

    # ── offset: a cropped preset's frame origin isn't the sensor's ─────────
    # The calibration is fit full-frame, so the model stores absolute sensor
    # coordinates while the screen stays display-local.
    ox, oy = 37.0, 82.0
    ed3 = RoiEditor(c, offset=(ox, oy))
    ed3.set_image(np.zeros((CH, CW), np.uint8))
    ed3._on_drawn((100.0, 80.0), (160.0, 130.0))
    roi3 = list(ed3.roi_set)[-1]
    r.check(abs(roi3.x - (130 + ox)) < 1 and abs(roi3.y - (105 + oy)) < 1,
            f"a drag on a cropped preset's frame is stored in absolute "
            f"sensor coordinates, shifted by the preset offset "
            f"({roi3.x:.0f}, {roi3.y:.0f})")

    it3 = ed3._items[-1]
    r.check(abs(it3.pos()[0] - 100.0) < 1 and abs(it3.pos()[1] - 80.0) < 1,
            f"…but the on-screen item stays at the display-local drag "
            f"position ({it3.pos()[0]:.0f}, {it3.pos()[1]:.0f})")

    # Copied first: `roi3` IS the set's entry, and the move mutates it.
    x3, y3 = roi3.x, roi3.y
    before3 = it3.pos()
    it3.setPos([before3[0] + 15, before3[1] - 9])
    ed3._on_item_changed()
    moved3 = list(ed3.roi_set)[-1]
    r.check(abs(moved3.x - x3 - 15) < 0.6 and abs(moved3.y - y3 + 9) < 0.6,
            "moving the on-screen item still writes an absolute-coordinate "
            "model position")

    border = c.accessible_outline()
    xdata, ydata = ed3._field.getData()
    r.check(np.allclose(xdata, border[:, 0] - ox)
            and np.allclose(ydata, border[:, 1] - oy),
            "the reachable-field outline is drawn display-local too")

    ed4 = RoiEditor(c)
    ed4._on_drawn((100.0, 80.0), (160.0, 130.0))
    roi4 = list(ed4.roi_set)[-1]
    r.check(abs(roi4.x - 130) < 1 and abs(roi4.y - 105) < 1,
            "control: zero offset behaves exactly as before")

    # ── scale: a binned frame covers more sensor than it has pixels ─────────
    # The calibration is unbinned; a 2x2 frame drawn at its own pixel count
    # would cover a quarter of its real area.
    ed5 = RoiEditor(c, offset=(ox, oy), scale=2.0)
    ed5.set_image(np.zeros((CH // 2, CW // 2), np.uint8))
    box5 = ed5._img.mapRectToView(ed5._img.boundingRect())
    r.check(abs(box5.width() - CW) < 1 and abs(box5.height() - CH) < 1,
            f"a 2x2-binned frame is drawn across the full sensor area it "
            f"covers, not its own pixel count "
            f"({box5.width():.0f}x{box5.height():.0f})")

    # Scale belongs to the image alone; the view stays in sensor px.
    ed5._on_drawn((100.0, 80.0), (160.0, 130.0))
    roi5 = list(ed5.roi_set)[-1]
    r.check(abs(roi5.x - (130 + ox)) < 1 and abs(roi5.y - (105 + oy)) < 1,
            f"binning doesn't move where a drag lands in the model "
            f"({roi5.x:.0f}, {roi5.y:.0f})")

    ed6 = RoiEditor(c, offset=(ox, oy))
    ed6.set_image(np.zeros((CH // 2, CW // 2), np.uint8))
    box6 = ed6._img.mapRectToView(ed6._img.boundingRect())
    r.check(abs(box6.width() - CW / 2) < 1 and abs(box6.height() - CH / 2) < 1,
            "control: at 1x1 the frame spans exactly its own pixels")

    # ── 7. saved ROI sets: session/archive storage, the picker, routines ────
    from acqApp.devices.dmd import roi_store
    from acqApp.routines.settings import pattern_label

    saved = _set(RectRoi(x=10, y=10, w=4, h=4), CircleRoi(x=50, y=50, r=6))
    p1 = roi_store.save("column A", saved)
    r.check(p1.name.endswith(".roi.json") and p1.parent == roi_store.SESSION_DIR,
            f"save() writes into the session folder ({p1})")
    back = roi_store.load(p1)
    r.check(len(back) == 2 and np.array_equal(back.mask((CH, CW)),
                                              saved.mask((CH, CW))),
            "a saved set survives roi_store save/load unchanged")

    p2 = roi_store.save("column A", saved)      # same name, twice
    r.check(p2 != p1 and p2.exists(),
            f"saving under a taken name does not clobber the first ({p1.name}, "
            f"{p2.name})")

    listed = roi_store.list_session()
    r.check({s.path for s in listed} == {p1, p2},
            f"list_session sees both ({[s.path.name for s in listed]})")
    r.check(roi_store.is_roi_file(p1) and not roi_store.is_roi_file("frame.png"),
            "is_roi_file distinguishes a saved set from a raw pattern file")

    roi_store._rotated = False          # a second run
    moved = roi_store.list_archive()
    r.check({s.path.name for s in moved} == {p1.name, p2.name},
            f"rotation moves the previous run's sets into archive "
            f"({[s.path.name for s in moved]})")
    r.check(roi_store.list_session() == [],
            "…and leaves the session folder empty for the new run")

    r.check(pattern_label(str(p1)) == "ROI: column A",
            f"pattern_label names a saved ROI set ({pattern_label(str(p1))!r})")
    r.check(pattern_label("frame.png") == "frame.png",
            "control: a raw pattern file's label is untouched")

    # ── the panel adopts a routine-chosen ROI set the way it adopts a file ──
    from acqApp.devices.dmd.panel import SettingsPanel

    panel = SettingsPanel()
    emitted: list = []
    panel.settings_changed.connect(emitted.append)
    panel.set_roi_pattern("column A", saved.to_list())
    r.check(panel.mode == "roi" and len(panel.settings.rois) == 2,
            f"set_roi_pattern switches to ROI mode with the loaded set "
            f"({panel.mode}, {len(panel.settings.rois)} ROI(s))")
    r.check(len(emitted) >= 1, "…and emits settings_changed once switched")
    r.check('"column A"' in panel._lbl_rois.text(),
            f"the loaded set's name is shown ({panel._lbl_rois.text()!r})")

    check_roi_store_corrupt(r)
    return r.finish()


def check_roi_store_corrupt(r) -> None:
    """A damaged .roi.json is skipped by the list and refused with ValueError
    on load (what the editor and the DMD tab's Browse catch), never a crash."""
    import acqApp.devices.dmd.panel as P
    from acqApp.devices.dmd import roi_store

    tmp = Path(tempfile.mkdtemp(prefix="acqapp_roibad_"))
    good = tmp / "good.roi.json"
    good.write_text('{"name": "ok", "rois": []}', encoding="utf-8")
    bad = {"binary": b"\xff\xfe\x00garbage", "list": b"[1, 2]",
           "entry": b'{"rois": [7]}', "keys": b'{"rois": [{"kind": "rect", "zz": 1}]}'}
    for stem, data in bad.items():
        (tmp / f"{stem}.roi.json").write_bytes(data)
    try:
        listed = roi_store.list_folder(tmp)
        crashed = ""
    except Exception as e:                  # noqa: BLE001
        listed, crashed = [], type(e).__name__
    r.check(not crashed and {s.name for s in listed} >= {"ok"}
            and all(s.path.stem != "binary.roi" for s in listed)
            and all(s.path.stem != "list.roi" for s in listed),
            f"the picker's list skips unreadable sets ({crashed or len(listed)})")
    wrong = []
    for stem in bad:
        try:
            roi_store.load_named(tmp / f"{stem}.roi.json")
            wrong.append(f"{stem}: loaded")
        except ValueError:
            pass
        except Exception as e:              # noqa: BLE001
            wrong.append(f"{stem}: {type(e).__name__}")
    r.check(not wrong, f"loading a damaged set raises ValueError ({wrong})")
    (tmp / "num.roi.json").write_text('{"name": 7, "rois": []}', encoding="utf-8")
    names = [roi_store.load_named(tmp / "num.roi.json")[0]]
    names += [s.name for s in roi_store.list_folder(tmp) if s.path.name == "num.roi.json"]
    r.check(names == ["num.roi", "num.roi"],
            f"a non-text name falls back to the file stem, never reaches .strip() ({names})")

    panel = P.SettingsPanel()
    panel._rb["roi"].setChecked(True)
    warned: list = []
    real_open, real_warn = P.QFileDialog.getOpenFileName, P.QMessageBox.warning
    P.QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (str(tmp / "list.roi.json"), ""))
    P.QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[2]))
    try:
        panel._browse()
        raised = ""
    except Exception as e:                  # noqa: BLE001
        raised = type(e).__name__
    finally:
        P.QFileDialog.getOpenFileName, P.QMessageBox.warning = real_open, real_warn
    r.check(not raised and len(warned) == 1 and panel.rois == (),
            f"the DMD tab's Browse warns about a damaged set ({raised or warned})")
    shutil.rmtree(tmp, ignore_errors=True)

    import acqApp.devices.dmd.roi_panel as RP
    ed = RP.RoiEditor(None)
    ed._add(RectRoi(x=10, y=10, w=5, h=5))
    real_text, real_save = RP.QInputDialog.getText, RP.roi_store.save
    RP.QInputDialog.getText = staticmethod(lambda *a, **k: ("set", True))

    def bad_save(*_a, **_k):
        raise PermissionError("read-only")
    RP.roi_store.save = bad_save
    try:
        ed._on_save_roi()
        raised = ""
    except Exception as e:                  # noqa: BLE001
        raised = type(e).__name__
    finally:
        RP.QInputDialog.getText, RP.roi_store.save = real_text, real_save
    r.check(not raised and "read-only" in ed._status.text(),
            f"the editor's Save reports a write failure ({raised or ed._status.text()!r})")


# ═══ pair ═══════════════════════════════════════════════════════════════

def _part_pair() -> int:
    """Save FOV -> draw its ROI set, light it, save it as <base>_roi."""
    r = Report("dmd-pair")
    isolate_user_state()
    app = qt_app()
    from acqApp.devices.dmd import roi_store
    from acqApp.devices.dmd.pair_dialog import PairRoiDialog
    from acqApp.devices.dmd.roi_panel import RoiEditor

    snap = np.random.default_rng(0).integers(0, 255, (CH, CW), dtype=np.uint8)
    live = [snap.copy()]
    lit: list = []
    live_calls: list = []

    def make(calibration):
        ed = RoiEditor(calibration)
        ed.set_image(snap)
        return ed, PairRoiDialog(
            ed, "cellA_roi", live_source=lambda: (live[0], (0.0, 0.0), 1.0),
            illuminate=lambda rois: lit.append(None if rois is None
                                               else len(rois)),
            set_live=lambda on: live_calls.append(on) or False)

    ed, dlg = make(calib())
    r.check(not dlg._btn_save.isEnabled(), "Save waits for an ROI")
    ed._add(RectRoi(x=100, y=80, w=40, h=20))
    r.check(dlg._btn_save.isEnabled(), "…and is offered once one is drawn")
    r.check(lit == [] and live_calls == [], "nothing lights up on its own")
    dlg.show()
    pump(app, 0.1)
    r.check(live_calls == [True] and lit == [],
            f"opening starts Live view for the view under the drawing, and "
            f"lights nothing ({live_calls}, {lit})")

    dlg._btn_light.setChecked(True)
    r.check(lit == [1], f"Illuminate projects the drawn set ({lit})")
    live[0] = snap[::-1].copy()
    pump(app, 0.15)
    r.check(dlg._live._frame is live[0] and ed._image is snap,
            "the live view follows the camera; the drawing keeps the snapshot")
    ed._add(CircleRoi(x=50, y=50, r=6))
    pump(app, 0.3)
    r.check(lit[-1] == 2, f"an edit while lit re-projects ({lit})")
    xs, _ys = dlg._live._inside.getData()
    r.check(xs is not None and int(np.isnan(xs).sum()) == 2
            and dlg._live._outside.getData()[0].size == 0,
            "…and the live view outlines both, inside the capture area")
    dlg.reject()                        # Skip, still lit
    r.check(lit[-1] is None, f"closing turns the light off ({lit})")
    r.check(live_calls[-1] is False,
            f"…and puts Live view back how it was ({live_calls})")
    r.check(dlg.path is None and roi_store.list_session() == [],
            "Skip saves nothing")

    # ── the live view on a cropped, binned band ─────────────────────────────
    from acqApp.devices.dmd.roi_live import RoiLiveView

    band = snap[:40]                       # 2x binned: sensor rows 100..180
    view = RoiLiveView(lambda: (band, (0.0, 100.0), 2.0))
    view.set_rois(RoiSet([RectRoi(name="in", x=100, y=140, w=40, h=20),
                          CircleRoi(name="out", x=60, y=90, r=15)]))
    view.refresh()
    r.check(view.area() == (0.0, 100.0, 2.0 * CW, 80.0),
            f"the capture area is the band, in sensor px ({view.area()})")
    xs_in, ys_in = view._inside.getData()
    xs_out, ys_out = view._outside.getData()
    r.check(abs(np.nanmin(ys_in) - 30.0) < 1e-6
            and abs(np.nanmin(ys_out) - (-25.0)) < 1e-6,
            "an ROI inside it is outlined normally, one past its edge in "
            "amber, both at band-local rows")
    r.check("out" in view._lbl.text() and "in," not in view._lbl.text(),
            f"…and the status names only the one beyond ({view._lbl.text()!r})")

    lit.clear()
    ed, dlg = make(calib())
    ed._add(RectRoi(x=100, y=80, w=40, h=20))
    dlg._save()
    r.check(dlg.path is not None
            and roi_store.load_named(dlg.path)[0] == "cellA_roi",
            f"Save writes the set under the paired name ({dlg.path})")
    r.check(lit == [], "control: never lit, so nothing to turn off")

    ed, dlg = make(None)
    r.check(not dlg._btn_light.isEnabled(),
            "no calibration: Illuminate is offered disabled")
    dlg.reject()

    # ── every shape through the pair flow, on a cropped, binned preset ───────
    from PyQt6.QtCore import Qt as _Q
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QAbstractItemView, QLineEdit

    from acqApp.routines.table import _roi_icon

    ox, oy = 37.0, 82.0
    cal = calib()
    ed = RoiEditor(cal, offset=(ox, oy), scale=2.0)
    ed.set_image(snap)
    dlg = PairRoiDialog(ed, "cellB_roi", live_source=lambda: None,
                        illuminate=lambda rois: None, set_live=lambda on: False)
    ed.set_tool("rectangle")
    ed._on_drawn((10.0, 10.0), (60.0, 50.0))
    ed.set_tool("circle")
    ed._on_drawn((100.0, 40.0), (120.0, 40.0))
    ed.set_tool("free")
    ed._on_free([(150, 20), (200, 25), (210, 70), (160, 60), (150, 20)])
    r.check([x.kind for x in ed.roi_set] == ["rect", "circle", "poly"],
            "the dialog's editor draws all three shapes")
    ed._table.setCurrentCell(0, 0)
    QTest.keyClick(ed._table, _Q.Key.Key_Return)
    r.check(ed._table.state() == QAbstractItemView.State.EditingState
            and dlg.result() == 0 and dlg.path is None,
            "Enter on a selected row starts editing it; it doesn't press Save")
    QTest.keyClick(ed._table.findChild(QLineEdit), _Q.Key.Key_Escape)
    r.check(ed._table.state() != QAbstractItemView.State.EditingState
            and dlg.path is None, "…and Escape ends the edit, not the dialog")
    dlg._save()
    saved = roi_store.load(dlg.path) if dlg.path else None
    if r.check(saved is not None and [x.kind for x in saved]
               == ["rect", "circle", "poly"], "all three shapes save and reload"):
        r.check(abs(saved[0].x - (35 + ox)) < 0.01
                and abs(saved[1].x - (100 + ox)) < 0.01
                and abs(saved[2].points[0][0] - (150 + ox)) < 0.01
                and abs(saved[2].points[0][1] - (20 + oy)) < 0.01,
                "…in absolute sensor coordinates, offset applied to each kind")
        f = saved.dmd_frame(cal)
        r.check(int((f == f.max()).sum()) > 0
                and int((f == f.max()).sum()) < f.size // 2,
                "…and project to a lit frame that isn't all or nothing")
        r.check(not _roi_icon(str(dlg.path)).isNull(),
                "…and Routines draws their thumbnail")

    # ── the whole flow, from the Stage tab's button ─────────────────────────
    from types import SimpleNamespace

    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QInputDialog

    from acqApp.devices.stage import fov_store
    from acqApp.routines import pairs

    isolate_user_state()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd", "stage", "routines"})
    dmd = next(m for m in win._modules if m.key == "dmd")
    stage = next(m for m in win._modules if m.key == "stage")
    routines = next(m for m in win._modules if m.key == "routines")
    win._btn_run.setChecked(True)
    pump_until(app, lambda: win.latest_frame("voltage_cam") is not None)

    seen: list = []

    def fake_exec(self):
        seen.append(self.windowTitle())
        seen.append(self._ed._btn_save.isHidden())
        self._ed._add(RectRoi(x=100, y=80, w=40, h=20))
        self._ed.set_tool("free")
        self._ed._on_free([(150, 20), (200, 25), (210, 70), (160, 60), (150, 20)])
        self._save()
        return 1

    real_exec, real_text = PairRoiDialog.exec, QInputDialog.getText
    PairRoiDialog.exec = fake_exec
    QInputDialog.getText = staticmethod(lambda *a, **k: ("cellA", True))
    try:
        stage.save_fov()
    finally:
        PairRoiDialog.exec, QInputDialog.getText = real_exec, real_text
    fovs = fov_store.list_session()
    r.check([f.name for f in fovs] == ["cellA_fov"],
            f"the FOV is saved as <name>_fov ({[f.name for f in fovs]})")
    r.check(seen == ["DMD ROIs for cellA_roi", True],
            f"…and the ROI prompt follows at once, with no second name "
            f"prompt (the editor's own Save… hidden) ({seen})")
    roi = pairs.roi_for_fov(fovs[0]) if fovs else None
    r.check(roi is not None and roi_store.load_named(roi)[0] == "cellA_roi",
            f"…saved as <name>_roi, found from the FOV ({roi})")
    png = roi_store.image_for(roi) if roi is not None else None
    r.check(png is not None and png.is_file()
            and not QPixmap(str(png)).isNull(),
            f"…with its snapshot + shapes image beside it ({png})")

    steps = routines.panel.settings.steps
    step = steps[-1] if steps else None
    general = routines.panel.settings
    r.check(len(steps) == 1 and step.kind == "fov" and step.fov == "cellA_fov"
            and step.pattern == str(roi) and step.x_um == fovs[0].x_um
            and (step.length, step.unit, step.wait_s)
            == (general.capture_length, general.capture_unit, general.wait_s),
            f"…and the pair is already a routine step, on the general "
            f"capture/wait ({steps})")

    routines._engine = SimpleNamespace(running=True)
    routines.add_fov_step(fovs[0].path, roi)
    routines._engine = None
    r.check(len(routines.panel.settings.steps) == 1,
            "control: a running routine's locked protocol takes no new step")
    PairRoiDialog.exec = lambda self: 0
    QInputDialog.getText = staticmethod(lambda *a, **k: ("cellB", True))
    try:
        stage.save_fov()
    finally:
        PairRoiDialog.exec, QInputDialog.getText = real_exec, real_text
    r.check(len(routines.panel.settings.steps) == 1,
            "control: an FOV whose ROI drawing was cancelled adds no step")

    # Right-click on the map square -> the viewer shows that image.
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QLabel, QMenu
    real_menu_exec = QMenu.exec
    QMenu.exec = lambda self, *a: self.actions()[-1].trigger()
    shown: list = []
    real_preview = stage.preview_fov
    stage.panel.fov_preview_requested.disconnect()
    stage.panel.fov_preview_requested.connect(
        lambda f: shown.append(real_preview(f)))
    try:
        stage.panel._on_fov_menu(0, stage.panel.mapToGlobal(QPoint(0, 0)))
    finally:
        QMenu.exec = real_menu_exec
    dlg = shown[0] if shown else None
    r.check(dlg is not None and dlg.has_image
            and "as drawn on the snapshot" in " ".join(
                lb.text() for lb in dlg.findChildren(QLabel)),
            "right-click on the FOV opens its snapshot + ROIs")
    if dlg is not None:
        dlg.close()
    r.check(dmd.panel.mode == "roi" and len(dmd.panel.settings.rois) == 2,
            "the DMD tab adopts the saved set")
    adopted = RoiSet.from_list(list(dmd.panel.settings.rois))
    r.check([x.kind for x in adopted] == ["rect", "poly"]
            and len(adopted[1].points) >= 3,
            "…free-form outline included")

    win._btn_run.setChecked(False)
    pump(app, 0.3)
    win.close()
    pump(app, 0.1)
    check_pair_faults(r)
    check_roi_editor_live(r)
    return r.finish()


def check_pair_faults(r: Report) -> None:
    """A projector fault inside the pair dialog's slots is shown, not raised
    (that aborts the app), and the light is still asked off; a failed save
    keeps the dialog open."""
    import acqApp.devices.dmd.pair_dialog as PD
    from acqApp.devices.dmd.roi_panel import RoiEditor

    _app = qt_app()
    calls: list = []
    alp_gone = [True]

    def illuminate(rois):
        calls.append(rois is not None)
        if alp_gone[0]:
            raise RuntimeError("ALP gone")

    ed = RoiEditor(calib())
    ed.set_image(np.zeros((CH, CW), np.uint8))
    ed._add(RectRoi(x=100, y=80, w=40, h=20))
    dlg = PD.PairRoiDialog(ed, "cellC_roi", live_source=lambda: None,
                           illuminate=illuminate)
    try:
        dlg._btn_light.setChecked(True)
        raised = False
    except RuntimeError:
        raised = True
    r.check(not raised and calls == [True, False]
            and not dlg._btn_light.isChecked() and "ALP gone" in dlg._lbl_err.text(),
            f"a failing Illuminate is reported, unticked and asked dark ({calls})")
    alp_gone[0] = False
    dlg._btn_light.setChecked(True)
    r.check(dlg._btn_light.isChecked() and dlg._lbl_err.text() == "",
            f"a retry that lights clears the old error ({dlg._lbl_err.text()!r})")
    dlg._btn_light.setChecked(False)

    real_save = PD.roi_store.save

    def bad_save(*_a, **_k):
        raise PermissionError("read-only")
    PD.roi_store.save = bad_save
    try:
        dlg._save()
        raised = False
    except OSError:
        raised = True
    finally:
        PD.roi_store.save = real_save
    r.check(not raised and dlg.path is None and dlg.result() == 0
            and "read-only" in dlg._lbl_err.text(),
            "a failed save is reported and the dialog stays open")
    dlg.reject()


def check_roi_editor_live(r: Report) -> None:
    """Edit ROIs… projects each edit while Illuminate is on (on from the
    start if the DMD was lighting ROIs), and on close the DMD shows what it
    did before, with the edits only if saved."""
    isolate_user_state()
    app = qt_app()
    import acqApp.devices.dmd.pair_dialog as PD
    from acqApp.devices.dmd.calibration import ReticlePose
    from acqApp.devices.dmd.control import FRAME_STOP
    from acqApp.devices.voltage_cam.presets import SENSOR_H, SENSOR_W

    sys.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")
    win._btn_run.setChecked(True)
    pump_until(app, lambda: win.latest_frame("voltage_cam") is not None)
    size, sensor = dmd.controller.resolution, (SENSOR_W, SENSOR_H)
    cal = ReticlePose.centred(size, sensor).to_calibration(size, sensor)
    path = Path(tempfile.mkdtemp(prefix="acqapp_roilive_")) / "cal.json"
    cal.save(path)
    dmd.panel.set_calib_path(str(path))
    first = RectRoi(name="a", x=SENSOR_W / 2, y=SENSOR_H / 2, w=300, h=200)
    dmd.panel.set_roi_pattern("old", [first.to_dict()])
    old_frame = RoiSet([first]).dmd_frame(cal)

    seen: dict = {}
    real_exec = PD.RoiLightDialog.exec

    def drive(dlg, save: bool, off_at_end: bool = False):
        seen["lit_at_open"] = dlg._btn_light.isChecked()
        ed = dlg._ed
        dlg._btn_light.setChecked(True)
        ed._add(CircleRoi(x=SENSOR_W / 2 - 900, y=SENSOR_H / 2, r=150))
        pump(app, 0.3)
        seen["projected"] = (dmd.controller.projecting and np.array_equal(
            dmd.controller.frame, ed.roi_set.dmd_frame(cal)))
        seen["n"] = len(ed.roi_set)
        if off_at_end:
            dlg._btn_light.setChecked(False)
        events.clear()
        dlg._save() if save else dlg.reject()
        return dlg.result()

    events: list[int] = []
    dmd.controller.set_sink(events.append)

    try:
        PD.RoiLightDialog.exec = lambda dlg: drive(dlg, True)
        dmd.edit_rois()
        r.check(seen["lit_at_open"] is False,
                "with the DMD dark, the editor opens with Illuminate off")
        r.check(seen["projected"],
                "with it on, a new ROI is on the DMD within the debounce")
        r.check(len(dmd.panel.rois) == seen["n"] == 2
                and not dmd.controller.projecting,
                "Save adopts the edits, and the DMD is dark again as before")

        dmd.panel.set_roi_pattern("old", [first.to_dict()])
        dmd.display()
        PD.RoiLightDialog.exec = lambda dlg: drive(dlg, False)
        dmd.edit_rois()
        r.check(seen["lit_at_open"] is True,
                "with the DMD lighting ROIs, Illuminate starts on: edits show "
                "at once")
        r.check(dmd.controller.projecting and len(dmd.panel.rois) == 1
                and np.array_equal(dmd.controller.frame, old_frame),
                "Cancel drops the edits and the old ROIs are lit again")
        r.check(FRAME_STOP not in events,
                f"…with no dark gap on close: no stop logged to /dmd ({events})")

        PD.RoiLightDialog.exec = lambda dlg: drive(dlg, False, off_at_end=True)
        dmd.edit_rois()
        r.check(seen["lit_at_open"] is True and not dmd.controller.projecting,
                "switching Illuminate off in the editor keeps the DMD dark on "
                "close, even though it was lit before")
    finally:
        PD.RoiLightDialog.exec = real_exec
        dmd.stop_display()
        win._btn_run.setChecked(False)
        pump(app, 0.3)
        win.close()
        pump(app, 0.1)


PARTS = {
    "dmd": _part_dmd,
    "calib": _part_calib,
    "reticle": _part_reticle,
    "roi": _part_roi,
    "pair": _part_pair,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
