"""DMD: frame building and controllers, calibration fit, the sweep wiring, ROIs.

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
                      make_window, pump, qt_app, run_parts)
from acqApp.devices.dmd.calibration import (ON, STRIPE_OFFSETS,
                                            CalibrationError, DmdCalibration,
                                            apply_transform, calibrate,
                                            deshear, fit_axes, flip_x, flip_y,
                                            holdout_error, offset_stripe,
                                            stripe_sweep, with_corners,
                                            with_vignette, without_vignette)
from acqApp.devices.dmd.roi import CircleRoi, RectRoi, RoiSet, roi_from_dict
from acqApp.devices.dmd.sweep import FreshGrabber, sweep_exposures


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

    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=True))
    c.display()
    r.check(dev.last("SetTiming")[1] is None and dev.last("Run")[1] is True,
            f"static hold leaves the timing at the device default and loops "
            f"(got illumination={dev.last('SetTiming')[1]}, "
            f"loop={dev.last('Run')[1]})")
    c.stop()

    # The cycling path below: the panel never offers it, and the dataclass
    # default follows the panel, so static_hold=False is explicit.
    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=False,
                                 on_time_ms=20.0, n_repeats=3))
    c.display()
    r.check(dev.last("Run")[1] is False, "a repeat count stops looping")
    r.check(("SeqControl", 2100, 3) in dev.calls,
            f"…and is sent as ALP_SEQ_REPEAT (calls {dev.calls[-4:]})")
    c.stop()

    c.apply_settings(DmdSettings(pattern_path=pat, static_hold=False,
                                 on_time_ms=30_000.0))
    c.display()
    r.check(dev.last("SetTiming")[1] == alp.MAX_PICTURE_US,
            f"an on-time past the ALP's limit is clamped, not passed through "
            f"(got {dev.last('SetTiming')[1]})")
    c.stop()

    # Unrebuilt, Display would project the old alignment under the new panel.
    c.apply_settings(DmdSettings(pattern_path=pat, scale_pct=50.0))
    r.check(c.on_pixels == 50 * 50,
            f"a geometry change re-renders the pattern ({c.on_pixels} mirrors)")

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

    shutil.rmtree(tmp, ignore_errors=True)

    check_roi_wiring(r)
    check_mode_switch_and_cache(r)
    check_sub_sampling(r)
    return r.finish()


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
    pump(app, 1.2)

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

# Not calibration.py's STRIPE_WIDTH/STRIPE_CROSS: those are tuned per rig, and
# the synthetic camera below was sized against these.
TEST_THICK_FRAC = 0.025
TEST_CROSS_FRAC = 0.25


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


def make_camera(M, rng, *, vignette=True):
    """A camera that images `pattern` through `M`, vignetted and noisy."""
    yy, xx = np.mgrid[:CH, :CW].astype(np.float64)
    v = (0.25 + 0.75 * np.exp(-(((xx - CW * 0.35) ** 2 + (yy - CH * 0.45) ** 2)
                                / (2 * (0.45 * CW) ** 2)))
         if vignette else np.ones((CH, CW)))
    base = 300.0 * v

    def image(pattern):
        img = base * np.where(footprint(pattern, M), 3.0, 1.0)
        return img + rng.normal(0, 4.0, img.shape)
    return image


def run(image, dmd_size=(DW, DH), **kw):
    """Drive `calibrate` against a synthetic camera."""
    held = {"f": np.zeros((DH, DW), np.uint8)}
    kw.setdefault("thick_frac", TEST_THICK_FRAC)
    kw.setdefault("cross_frac", TEST_CROSS_FRAC)
    return calibrate(lambda f: held.__setitem__("f", f),
                     lambda: image(held["f"]), dmd_size,
                     log=lambda _s: None, **kw)


def _part_calib() -> int:
    r = Report("dmd-calib")
    rng = np.random.default_rng(7)
    M = true_transform()

    # ── 1. the stripe ────────────────────────────────────────────────────────
    s = offset_stripe(DW, DH, 0, 40.0)
    r.check(s.shape == (DH, DW) and s.dtype == np.uint8
            and set(np.unique(s)) <= {0, 255},
            f"a stripe is a device-sized binary frame {s.shape}")
    box = np.nonzero(s.any(axis=0))[0]
    r.check(abs((box.mean()) - ((DW - 1) / 2 + 40.0)) < 1.0,
            f"…centred on its offset (at x={box.mean():.1f}, "
            f"want {(DW - 1) / 2 + 40.0:.1f})")
    r.check(len(box) <= 0.08 * DW,
            f"…and narrow: {len(box)} of {DW} columns. Fine patterns do not "
            f"survive this relay, so nothing here may project one")

    # ── 2. the fit recovers the transform ────────────────────────────────────
    c = run(make_camera(M, rng))
    r.check(c.rms_px < 2.0,
            f"the stripe centroids fall on a straight line "
            f"(rms {c.rms_px:.2f} px over {c.n_points} stripes)")
    pts = np.array([[DW / 2, DH / 2], [DW / 4, DH / 4],
                    [3 * DW / 4, 2 * DH / 3]], float)
    err = float(np.abs(apply_transform(np.linalg.inv(c.cam_to_dmd), pts)
                       - apply_transform(M, pts)).max())
    r.check(err < 4.0,
            f"…and it agrees with the transform we projected through "
            f"(max {err:.2f} px)")
    r.check(c.dmd_size == (DW, DH) and c.cam_size == (CW, CH),
            f"…recording both sizes it was measured at {c.dmd_size} -> "
            f"{c.cam_size}")

    # A mirrored registration aims every ROI wrongly while looking well
    # fitted; the signed offsets are what tell the two apart.
    flip = np.array([[-1.05, 0.10, 300.0], [0.08, 1.02, 22.0], [0.0, 0.0, 1.0]])
    cf = run(make_camera(flip, rng))
    errf = float(np.abs(apply_transform(np.linalg.inv(cf.cam_to_dmd), pts)
                        - apply_transform(flip, pts)).max())
    r.check(errf < 4.0, f"a mirrored relay is recovered mirrored ({errf:.2f} px)")
    d = (apply_transform(flip, np.array([[DW - 1, DH / 2]], float))
         - apply_transform(flip, np.array([[0, DH / 2]], float)))[0]
    r.check(d[0] < 0,
            f"control: that transform really does run DMD +x towards camera -x "
            f"({d[0]:+.0f} px), so a sign error would have been caught")

    # ── 2b. the manual Y-flip (DmdSettings.roi_flip_y) mirrors DMD rows only ──
    cf = flip_y(c)
    r.check(cf.dmd_size == c.dmd_size and cf.cam_size == c.cam_size,
            "flip_y keeps both recorded sizes — only the mapping's sense "
            "changes")
    probe = np.array([[10.0, 20.0], [DW - 5.0, DH - 5.0], [DW / 2, DH / 4]])
    mirrored = probe.copy()
    mirrored[:, 1] = (DH - 1) - probe[:, 1]
    got = apply_transform(cf.dmd_to_cam, probe)
    want = apply_transform(c.dmd_to_cam, mirrored)
    r.check(float(np.abs(got - want).max()) < 1e-6,
            "flip_y(calib) at DMD row y lands where calib itself lands at "
            "row (h-1-y)")
    r.check(float(np.abs(flip_y(cf).cam_to_dmd - c.cam_to_dmd).max()) < 1e-9,
            "control: flipping twice is the identity — a pure mirror, not a "
            "shift hiding as one")

    # ── 2c. flip_x is flip_y's twin, across columns instead of rows ─────────
    cx = flip_x(c)
    r.check(cx.dmd_size == c.dmd_size and cx.cam_size == c.cam_size,
            "flip_x keeps both recorded sizes too")
    mirrored_x = probe.copy()
    mirrored_x[:, 0] = (DW - 1) - probe[:, 0]
    got_x = apply_transform(cx.dmd_to_cam, probe)
    want_x = apply_transform(c.dmd_to_cam, mirrored_x)
    r.check(float(np.abs(got_x - want_x).max()) < 1e-6,
            "flip_x(calib) at DMD column x lands where calib itself lands at "
            "column (w-1-x)")
    r.check(float(np.abs(flip_x(cx).cam_to_dmd - c.cam_to_dmd).max()) < 1e-9,
            "control: flipping X twice is the identity too")
    r.check(float(np.abs(flip_x(cf).cam_to_dmd
                        - flip_y(cx).cam_to_dmd).max()) < 1e-9,
            "flip_x and flip_y commute — each mirrors its own axis "
            "independently of the other")

    # Vignetting ate the previous method.
    c_flat = run(make_camera(M, rng, vignette=False))
    moved = float(np.abs(c.cam_to_dmd - c_flat.cam_to_dmd).max())
    r.check(moved < 0.05,
            f"control: vignetting barely moves the fit ({moved:.4f} in the "
            f"matrix) — a stripe's centroid is local")

    # ── 3. where the camera's view lands on the panel ────────────────────────
    x0, y0, x1, y1 = c.visible_mirrors()
    want = apply_transform(np.linalg.inv(M),
                           np.array([[0, 0], [CW - 1, 0], [CW - 1, CH - 1],
                                     [0, CH - 1]], float))
    r.check(abs(x0 - max(0, want[:, 0].min())) < 6
            and abs(x1 - min(DW, want[:, 0].max())) < 6,
            f"the camera sees mirrors x {x0}..{x1}, against a true "
            f"{max(0, want[:, 0].min()):.0f}..{min(DW, want[:, 0].max()):.0f}")
    r.check(0 <= x0 < x1 <= DW and 0 <= y0 < y1 <= DH,
            f"…clipped to the panel ({x0}, {y0}, {x1}, {y1}), so it is the "
            f"usable region and not an extrapolation")

    # ── 4. ROI → mask, the thing the transform is for ────────────────────────
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
    bad = DmdCalibration(cam_to_dmd=c.cam_to_dmd.copy(), dmd_size=c.dmd_size,
                         cam_size=c.cam_size)
    off = c.dmd_to_cam.copy()
    off[0, 2] += 40.0
    bad.cam_to_dmd = np.linalg.inv(off)
    hit_bad = (footprint(rs.dmd_frame(bad), M) & roi).sum() / max(1, roi.sum())
    r.check(hit_bad < 0.6,
            f"control: a 40 px error in the transform misses "
            f"({100 * hit_bad:.0f}% covered)")

    # ── 4b. knowing when not to trust it ─────────────────────────────────────
    # >= 0: a clean synthetic fit can average its noise to (near) exact.
    r.check(0 <= c.holdout_px < 4.0,
            f"a stripe left OUT of the fit is predicted to {c.holdout_px:.2f} px")
    # The residual is optimistic by construction; hold-out must not be it.
    r.check(c.holdout_px != c.rms_px,
            f"…and it is a different number from the residual "
            f"({c.holdout_px:.2f} vs {c.rms_px:.2f})")
    # A wandering stripe the fit absorbs must still show in the hold-out.
    good = {0: [(d, 100 + 2 * d, 50.0) for d in (-80, -40, 0, 40, 80)],
            1: [(d, 100.0, 50 + 2 * d) for d in (-80, -40, 0, 40, 80)]}
    clean = holdout_error(good)
    bent = dict(good)
    bent[0] = [(d, 100 + 2 * d + (25 if d == 0 else 0), 50.0)
               for d in (-80, -40, 0, 40, 80)]
    r.check(clean is not None and clean < 0.01,
            f"control: a perfectly linear sweep holds out to {clean:.3f} px")
    r.check(holdout_error(bent) > 20,
            f"…and one stripe 25 px off its line shows as "
            f"{holdout_error(bent):.0f} px of hold-out error (the WORST axis, "
            f"since averaging it against a good axis would hide it)")

    wild = {0: [(d, 100 + 2 * d + (300 if d == 40 else 0), 50.0)
                for d in (-80, -40, 0, 40, 80)],
            1: [(d, 100.0, 50 + 2 * d) for d in (-80, -40, 0, 40, 80)]}
    out = fit_axes(wild)
    r.check(out is not None and out[4] == 9,
            f"a 300 px outlier is rejected, leaving {out[4]} of 10 stripes")
    r.check(out[3] < 1.0,
            f"…so the residual reflects the good stripes ({out[3]:.2f} px)")
    # Rejection must not keep trimming until it "fits".
    noisy = {0: [(d, 100 + 2 * d + (3 if i % 2 else -3), 50.0)
                 for i, d in enumerate((-80, -40, 0, 40, 80))],
             1: [(d, 100.0, 50 + 2 * d) for d in (-80, -40, 0, 40, 80)]}
    r.check(fit_axes(noisy)[4] == 10,
            "control: ordinary scatter is kept — the worst of a good set is "
            "not an outlier")

    # ── 5. refusing to guess ─────────────────────────────────────────────────
    try:
        run(lambda _p: np.full((CH, CW), 50.0))
        r.check(False, "a dark rig raises rather than fitting noise")
    except CalibrationError as e:
        r.check("usable stripe" in str(e),
                f"a dark rig raises, naming the axis and the count "
                f"({str(e)[:46]}…)")
    r.check(fit_axes({0: [(0.0, 1.0, 2.0)], 1: [(0.0, 1.0, 2.0)]}) is None,
            "one stripe per axis is refused — a fit with no residual cannot be "
            "judged")

    r.check(c.model == "affine-noshear" and "shear" in c.notes,
            f"shear is off by default and the measured value is kept in the "
            f"notes ({c.model})")
    A = c.dmd_to_cam[:2, :2]
    gap = abs(np.degrees(np.arctan2(A[1, 1], A[0, 1]))
              - np.degrees(np.arctan2(A[1, 0], A[0, 0])))
    gap = min(gap, 360 - gap)
    r.check(abs(gap - 90.0) < 0.01,
            f"…so the two axes come out exactly perpendicular ({gap:.3f}deg)")
    withshear = run(make_camera(M, rng), allow_shear=True)
    r.check(withshear.model == "affine",
            "…and allow_shear=True keeps it, for a relay where it is real")
    vx = np.array([3.0, 1.0])
    vy = np.array([-0.6, 2.0])
    ox, oy = deshear(vx, vy)
    r.check(abs(np.hypot(*ox) - np.hypot(*vx)) < 1e-9
            and abs(np.hypot(*oy) - np.hypot(*vy)) < 1e-9,
            "deshear keeps each axis's measured scale")
    r.check(abs(float(ox @ oy)) < 1e-9,
            "…makes them perpendicular")
    r.check(np.sign(vx[0] * vy[1] - vx[1] * vy[0])
            == np.sign(ox[0] * oy[1] - ox[1] * oy[0]),
            "…and preserves handedness, so it cannot mirror the registration")

    # n_points is after outlier rejection, hence >=.
    r.check(len(c.stripes) >= c.n_points and len(c.stripes[0]) == 4,
            f"the {len(c.stripes)} raw stripe measurements are stored "
            f"[axis, offset, cam_x, cam_y] ({c.n_points} kept after outlier "
            f"rejection)")

    # Off-frame stripes are dropped: the rig's DMD field is ~1.9x the camera's.
    seen = stripe_sweep(lambda _f: None, lambda: np.full((CH, CW), 50.0),
                        (DW, DH), log=lambda _s: None)
    r.check(seen[0] == [] and seen[1] == [],
            "an invisible stripe is dropped rather than contributing a NaN")

    big = np.array([[6.0, 0.0, -700.0], [0.0, 6.0, -500.0], [0.0, 0.0, 1.0]])
    img = make_camera(big, rng)
    held = {"f": np.zeros((DH, DW), np.uint8)}
    seen = stripe_sweep(lambda f: held.__setitem__("f", f),
                        lambda: img(held["f"]), (DW, DH), log=lambda _s: None,
                        thick_frac=TEST_THICK_FRAC, cross_frac=TEST_CROSS_FRAC)
    kept = len(seen[0]) + len(seen[1])
    r.check(kept < 2 * len(STRIPE_OFFSETS),
            f"…and with the field {big[0, 0]:.0f}x the panel, only {kept} of "
            f"{2 * len(STRIPE_OFFSETS)} stripes stay on the frame")

    # ── 6b. a rig too tilted for affine — the homography model ───────────────
    # Real perspective (bottom row not [0, 0, 1]): scale varies across the
    # panel, which no affine fit can represent.
    tilt = np.array([[1.3, 0.05, 40.0], [0.02, 1.1, 20.0],
                     [0.0015, 0.0005, 1.0]])
    d0 = 0.0015 * (DW - 1) + 0.0005 * (DH - 1) + 1.0
    r.check(d0 > 0.0 and (0.0005 * (DH - 1) + 1.0) > 0.0,
            "control: the synthetic tilt's denominator stays positive across "
            "the whole panel, so it is a well-posed homography, not one with "
            "a vanishing line crossing the DMD")
    img_tilt = make_camera(tilt, rng, vignette=False)
    probe = np.array([[DW / 4, DH / 4], [3 * DW / 4, DH / 4],
                      [DW / 4, 3 * DH / 4], [3 * DW / 4, 3 * DH / 4]], float)

    # Otherwise "homography does better" would prove nothing.
    c_aff = run(img_tilt, model="affine")
    err_aff = float(np.abs(apply_transform(np.linalg.inv(c_aff.cam_to_dmd), probe)
                           - apply_transform(tilt, probe)).max())
    r.check(err_aff > 5.0,
            f"control: an affine fit measurably mis-registers real "
            f"perspective ({err_aff:.2f} px)")

    c_h = run(img_tilt, model="homography")
    r.check(c_h.model == "homography", "the result records which model fit it")
    err_h = float(np.abs(apply_transform(np.linalg.inv(c_h.cam_to_dmd), probe)
                         - apply_transform(tilt, probe)).max())
    r.check(err_h < 2.0,
            f"…while the homography fit recovers it (max {err_h:.2f} px, vs "
            f"{err_aff:.2f} px for the affine control on the same data)")
    r.check(c_h.holdout_px >= 0.0, "hold-out error is computed for this model too")

    # 8 DOF, not 6: three offsets are too few.
    try:
        run(img_tilt, model="homography", offsets=(-100, 0, 100))
        r.check(False, "too few points for a homography should raise")
    except CalibrationError as e:
        r.check("homography" in str(e),
                f"…naming the model that needed more of them ({str(e)[:50]}…)")

    try:
        run(img_tilt, model="projective")
        r.check(False, "an unrecognized model name should raise")
    except CalibrationError as e:
        r.check("projective" in str(e), f"…naming the bad value ({e})")

    wide = offset_stripe(DW, DH, 0, 40.0, cross_frac=0.25)
    narrow = offset_stripe(DW, DH, 0, 40.0, cross_frac=0.05)
    r.check(0 < int(narrow.sum()) < int(wide.sum()),
            f"a smaller cross_frac makes a smaller stripe ({int(narrow.sum())} "
            f"vs {int(wide.sum())} lit px), which is what keeps a magnified "
            f"footprint from clipping the frame edge on a tilted rig")

    # ── 6. it round-trips through JSON ───────────────────────────────────────
    p = Path(tempfile.mkdtemp()) / "calib.json"
    c.save(p)
    back = DmdCalibration.load(p)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.dmd_size == c.dmd_size and back.rms_px == c.rms_px
            and back.stripes == c.stripes,
            "a calibration survives save/load with its provenance and its "
            "raw stripes")
    r.check(back.vignette is None,
            "…and a calibration with no vignette marked stays that way")

    # ── 7. manual corner adjustment (with_corners) ───────────────────────────
    # Only the sizes need be right: with_corners replaces the mapping outright.
    wrong = DmdCalibration(
        cam_to_dmd=np.linalg.inv(np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                                           [0.0, 0.0, 1.0]])),
        dmd_size=(DW, DH), cam_size=(CW, CH), model="affine-noshear",
        notes="a starting point with the wrong scale entirely")
    dmd_corners = np.array([[0, 0], [DW - 1, 0], [DW - 1, DH - 1],
                            [0, DH - 1]], float)
    true_cam_corners = apply_transform(M, dmd_corners)
    fixed = with_corners(wrong, true_cam_corners)
    r.check(np.allclose(fixed.accessible_corners(), true_cam_corners, atol=1e-6),
            "with_corners lands the panel's own corners exactly on the ones "
            "handed to it")
    probe2 = np.array([[DW / 2, DH / 2], [DW / 3, 2 * DH / 3]], float)
    err2 = float(np.abs(apply_transform(fixed.dmd_to_cam, probe2)
                        - apply_transform(M, probe2)).max())
    r.check(err2 < 1e-4,
            f"…and since the true relay IS affine (a homography's special "
            f"case), the 4 corners alone reconstruct it everywhere else too "
            f"(max {err2:.2e} px)")
    r.check(fixed.rms_px == 0.0 and fixed.holdout_px == 0.0,
            "an exact 4-point fit has no residual left to report")
    r.check(fixed.model == "affine-noshear+corners"
            and "corners manually adjusted" in fixed.notes,
            f"the correction is recorded, not silent ({fixed.model!r})")
    twice = with_corners(fixed, true_cam_corners)
    r.check(twice.model == "affine-noshear+corners",
            f"…and adjusting twice doesn't pile up the same tag "
            f"({twice.model!r})")
    r.check(fixed.dmd_size == wrong.dmd_size and fixed.cam_size == wrong.cam_size,
            "the sizes travel through unchanged — only the mapping is replaced")

    try:
        with_corners(wrong, np.array([[0, 0], [10, 0.001], [20, 0.002],
                                      [30, 0.003]], float))
        r.check(False, "near-collinear corners are refused")
    except CalibrationError as e:
        r.check("collinear" in str(e), f"…naming why ({e})")

    # ── 8. marking the optical vignette (with_vignette / well_lit) ──────────
    r.check(np.all(fixed.well_lit(probe2)),
            "well_lit says everything is fine when no vignette was ever marked")
    vig = with_vignette(fixed, 100.0, 80.0, 50.0)
    r.check(vig.vignette == (100.0, 80.0, 50.0),
            f"with_vignette records the circle as given ({vig.vignette})")
    r.check(np.array_equal(vig.cam_to_dmd, fixed.cam_to_dmd),
            "…and touches nothing about the geometric mapping — vignette and "
            "registration are independent corrections")
    inside = np.array([[100.0, 80.0], [130.0, 80.0]])       # centre, +30 (< r)
    outside = np.array([[100.0, 200.0], [400.0, 400.0]])    # well past r=50
    r.check(bool(vig.well_lit(inside).all()) and not vig.well_lit(outside).any(),
            "well_lit tells inside the marked circle from outside it")
    r.check(vig.well_lit(np.array([[150.0, 80.0]]))[0]
            and not vig.well_lit(np.array([[150.01, 80.0]]))[0],
            "…right at the boundary (r=50 from (100, 80))")
    back_vig = without_vignette(vig)
    r.check(back_vig.vignette is None and np.all(back_vig.well_lit(outside)),
            "without_vignette un-marks it — the points outside the old "
            "circle read as fine again")
    try:
        with_vignette(fixed, 0.0, 0.0, 0.0)
        r.check(False, "a non-positive radius is refused")
    except ValueError as e:
        r.check("radius" in str(e), f"…naming why ({e})")

    p2 = Path(tempfile.mkdtemp()) / "calib_vig.json"
    vig.save(p2)
    back2 = DmdCalibration.load(p2)
    r.check(back2.vignette == vig.vignette,
            "a marked vignette survives save/load too")

    return r.finish()


# ═══ sweep ══════════════════════════════════════════════════════════════

class LaggingRig:
    """A projector and a camera whose next `latency` frames still carry the
    previous pattern, as the real one's do — without that lag
    `FreshGrabber`'s settle count would go untested."""

    def __init__(self, M, rng, *, latency: int = 1, tick: int = 3):
        self.image = make_camera(M, rng)
        self.latency, self.tick = latency, tick
        self.pattern = np.zeros((DH, DW), np.uint8)
        self._queue: list = []              # patterns still in flight
        self._frame = np.zeros((CH, CW))    # what the display last showed
        self.n_pumps = 0
        self.stalled = False

    def project(self, frame) -> None:
        self.pattern = np.asarray(frame)

    def pump(self) -> None:
        """One display tick. Publishes a NEW array object, as the adapter does."""
        self.n_pumps += 1
        if self.stalled or self.n_pumps % self.tick:
            return
        self._queue.append(self.pattern)
        if len(self._queue) > self.latency:
            shown = self._queue.pop(0)
            self._frame = self.image(shown)

    def latest(self):
        return self._frame


def check_fresh_grabber(r: Report) -> None:
    rng = np.random.default_rng(3)
    M = true_transform()

    def marked(v):
        return np.full((DH, DW), np.uint8(v))

    def which(frame):
        """Which pattern this frame came from: lit -> 255, dark -> 0. Against
        a midpoint: two dark frames compared directly is a coin flip."""
        return 255 if float(np.mean(frame)) > 1.5 * float(np.mean(dark_ref)) else 0

    rig = LaggingRig(M, rng, latency=1)
    rig.project(marked(0))
    for _ in range(20):
        rig.pump()
    dark_ref = rig.latest().copy()

    g = FreshGrabber(rig.latest, settle=1, timeout_s=2.0, pump=rig.pump)
    rig.project(marked(255))
    got = g.grab()
    r.check(which(got) == 255,
            "grab() returns a frame exposed AFTER the projection")

    rig2 = LaggingRig(M, rng, latency=1)
    rig2.project(marked(0))
    for _ in range(20):
        rig2.pump()
    g0 = FreshGrabber(rig2.latest, settle=0, timeout_s=2.0, pump=rig2.pump)
    rig2.project(marked(255))
    r.check(which(g0.grab()) == 0,
            "control: settle=0 returns the STALE frame — the lag is real, and "
            "the settle count is what defeats it")

    stuck = np.zeros((CH, CW))
    frozen = FreshGrabber(lambda: stuck, settle=0, timeout_s=0.15,
                          pump=lambda: None)
    try:
        frozen.grab()
        r.check(False, "a stalled camera raises")
    except CalibrationError as e:
        r.check("RUNNING" in str(e) or "running" in str(e),
                f"a stalled camera raises, naming the cause ({str(e)[:60]}…)")

    seq = [np.zeros((4, 4)), np.zeros((4, 4)), np.zeros((4, 4))]
    box = {"i": 0}

    def next_equal():
        box["i"] = min(box["i"] + 1, len(seq) - 1)
        return seq[box["i"]]

    g2 = FreshGrabber(lambda: seq[box["i"]], settle=0, timeout_s=1.0,
                      pump=next_equal)
    ok = True
    try:
        g2.grab()
    except CalibrationError:
        ok = False
    r.check(ok, "equal-but-distinct frames count as new (identity, not ==) — "
                "an unchanging sample still yields real exposures")


def check_end_to_end(r: Report) -> None:
    """calibrate() through a camera that LAGS: a wrong pairing of project and
    grab shows only here, not in either half's own tests."""
    rng = np.random.default_rng(11)
    M = true_transform()
    rig = LaggingRig(M, rng, latency=1, tick=2)
    g = FreshGrabber(rig.latest, settle=1, timeout_s=2.0, pump=rig.pump)
    n = {"n": 0}

    def project(f):
        n["n"] += 1
        rig.project(f)

    c = calibrate(project, g.grab, (DW, DH), log=lambda _s: None,
                 thick_frac=TEST_THICK_FRAC, cross_frac=TEST_CROSS_FRAC)
    r.check(c.rms_px < 2.0,
            f"a calibration comes back through a lagging camera "
            f"(rms {c.rms_px:.2f} px over {c.n_points} stripes)")
    pts = np.array([[DW / 2, DH / 2], [DW / 4, DH / 4]], float)
    err = float(np.abs(apply_transform(np.linalg.inv(c.cam_to_dmd), pts)
                       - apply_transform(M, pts)).max())
    r.check(err < 4.0,
            f"…and agrees with the transform we projected through ({err:.2f} px)")
    r.check(n["n"] == sweep_exposures() == 1 + 2 * len(STRIPE_OFFSETS),
            f"sweep_exposures() is what the run really costs "
            f"({sweep_exposures()} quoted, {n['n']} run) — the operator is "
            f"shown that number before any light is emitted")


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
    """`project_frame` must not go through `build_frame`: a warped calibration
    pattern still decodes, into the wrong geometry."""
    from acqApp.acq.devices import RawProjector
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, DmdSettings,
                                            MockDmdController)

    # Every knob set to visibly move a pattern.
    s = DmdSettings(scale_pct=57.0, rotation_deg=23.0, offset_x=-90.0,
                    offset_y=45.0, fit=True, invert=True)
    c = MockDmdController(s)
    r.check(isinstance(c, RawProjector),
            "the mock controller satisfies RawProjector")

    pattern = offset_stripe(DEFAULT_W, DEFAULT_H, 0, 200.0)
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


def check_wiring(r: Report) -> None:
    """The Calibrate button reaches the adapter, which opens the dialog with
    the camera stopped and hands it set_live."""
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
    seen = {"box": 0, "dialog": 0, "exec": 0, "live": []}
    from PyQt6.QtWidgets import QMessageBox
    real_info = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.__setitem__("box", seen["box"] + 1))
    import acqApp.devices.dmd.sweep as SW
    real_dlg = SW.CalibrationDialog

    class FakeDialog:
        """Stands in for the sweep window — it must be constructed AND exec'd,
        so a wiring that builds it and forgets to show it still fails."""

        def __init__(self, *_a, **kw):
            seen["dialog"] += 1
            seen["live"].append(kw.get("set_live"))

        def exec(self):
            seen["exec"] += 1
            return 0

    SW.CalibrationDialog = FakeDialog
    try:
        r.check(not win._btn_run.isChecked(), "control: the camera is stopped")
        dmd.panel.calibrate_requested.emit()
        r.check(seen["dialog"] == 1 and seen["exec"] == 1 and seen["box"] == 0,
                "the dialog opens with the camera stopped — it starts the "
                "camera itself rather than refusing")
        r.check(callable(seen["live"][0]),
                "…and is handed set_live, so it can start the camera and put "
                "it back")

        was = win.set_live(True)
        pump(app, 1.0)
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
        SW.CalibrationDialog = real_dlg

    # Else the ROI editor keeps drawing the old field.
    dmd._adopt_calibration("C:/nowhere/dmd_calib_test.json")
    r.check(dmd.panel.settings.calib_path.endswith("dmd_calib_test.json"),
            "a saved calibration is adopted by the panel straight away")
    r.check(dmd.metadata()["dmd_calibration"] == "dmd_calib_test.json",
            "…and lands in the session metadata")

    win._btn_run.setChecked(False)
    pump(app, 0.3)
    win.close()
    pump(app, 0.1)


def check_geometry_controls(r: Report) -> None:
    """The Model/cross-length controls seed from the rig profile, and the
    operator's choice, not the seed, reaches calibrate()."""
    # Held: an unreferenced QApplication is GC'd, and every widget built
    # after that segfaults with no traceback.
    _app = qt_app()
    import acqApp.devices.dmd.sweep as SW

    class FakeProjector:
        resolution = (64, 48)

        def project_frame(self, _f):
            pass

        def stop(self):
            pass

    real_seed = SW.config.rig_dmd_calibration
    SW.config.rig_dmd_calibration = lambda: {"model": "homography",
                                             "cross_frac": 0.06}
    try:
        dlg = SW.CalibrationDialog(FakeProjector(), lambda: None, real=True)
        r.check(dlg._cmb_model.currentData() == "homography",
                "the model combo seeds from the rig profile")
        r.check(abs(dlg._spn_cross.value() - 6.0) < 1e-6,
                "…and so does the cross-length spinbox (as a percent)")

        captured: dict = {}

        def fake_calibrate(_project, _grab, _size, **kw):
            captured.update(kw)
            raise SW.CalibrationError("stub — nothing to fit")

        real_calibrate = SW.calibrate
        SW.calibrate = fake_calibrate
        try:
            dlg._cmb_model.setCurrentIndex(0)      # override the seed: affine
            dlg._spn_cross.setValue(12.5)
            dlg._run()
        finally:
            SW.calibrate = real_calibrate
        r.check(captured.get("model") == "affine",
                "the dialog's own selection reaches calibrate(), not the "
                "rig-profile seed it started from")
        r.check(abs(captured.get("cross_frac", 0.0) - 0.125) < 1e-6,
                "…and the percent spinbox arrives as a fraction")
    finally:
        SW.config.rig_dmd_calibration = real_seed


def check_corner_adjust(r: Report) -> None:
    """"Adjust corners…" is disabled until a fit exists, projects ALL-ON (not
    a stripe) for the operator to align against, and only replaces `_calib`
    on Apply — never on Cancel."""
    _app = qt_app()          # kept alive — see check_geometry_controls
    import acqApp.devices.dmd.sweep as SW

    projected: list = []

    class FakeProjector:
        resolution = (64, 48)

        def project_frame(self, f):
            projected.append(np.asarray(f).copy())

        def stop(self):
            pass

    # A fresh array every call: FreshGrabber keys on identity, and a reused
    # one would make every grab() wait out its timeout.
    counter = {"n": 0}

    def source():
        counter["n"] += 1
        return np.full((10, 10), counter["n"] % 256, np.uint8)

    dlg = SW.CalibrationDialog(FakeProjector(), source, real=True)
    r.check(not dlg._btn_adjust.isEnabled(),
            "corner adjustment is unavailable before any fit exists")

    stub_calib = DmdCalibration(
        cam_to_dmd=np.eye(3), dmd_size=(64, 48), cam_size=(10, 10))
    dlg._calib = stub_calib
    dlg._btn_save.setEnabled(True)
    dlg._btn_adjust.setEnabled(True)

    # Cancel first: the fake reports Rejected until `result` is set.
    seen: dict = {"built": 0, "exec": 0}
    adjusted = DmdCalibration(
        cam_to_dmd=2 * np.eye(3), dmd_size=(64, 48), cam_size=(10, 10),
        notes="corners manually adjusted")

    class FakeCornerDialog:
        result = None

        def __init__(self, calib, frame, *, parent=None):
            seen["built"] += 1
            seen["calib_in"] = calib
            seen["frame_shape"] = np.asarray(frame).shape

        def exec(self):
            seen["exec"] += 1
            return SW.QDialog.DialogCode.Accepted if self.result is not None \
                else SW.QDialog.DialogCode.Rejected

        @property
        def calibration(self):
            return self.result

    import acqApp.devices.dmd.corner_editor as CE
    real_corner_dlg = CE.CornerAdjustDialog
    CE.CornerAdjustDialog = FakeCornerDialog
    try:
        dlg._adjust_corners()
        r.check(seen["built"] == 1 and seen["exec"] == 1,
                "the corner-adjust dialog is built AND exec'd on click")
        r.check(len(projected) == 1 and bool(np.all(projected[0] == ON)),
                "it projects ALL-ON, not a stripe — that's what the operator "
                "needs to see to align the corners against")
        r.check(seen["calib_in"] is stub_calib,
                "…handed the fit that's currently in force")
        r.check(seen["frame_shape"] == (10, 10),
                "…and the frame it just grabbed")
        r.check(dlg._calib is stub_calib,
                "Cancel (exec -> Rejected) leaves the calibration untouched")
        for b in (dlg._btn_run, dlg._btn_adjust, dlg._btn_save, dlg._btn_close):
            r.check(b.isEnabled(),
                    "…and every button is left enabled again, not stuck mid-run")

        FakeCornerDialog.result = adjusted
        dlg._adjust_corners()
        r.check(dlg._calib is adjusted,
                "Apply (exec -> Accepted) adopts the corner-adjusted calibration")
    finally:
        CE.CornerAdjustDialog = real_corner_dlg


def check_corner_editor(r: Report) -> None:
    """The real `CornerAdjustDialog`: corners AND the vignette circle reach
    Apply's result, independently of each other, and Cancel discards both."""
    _app = qt_app()          # kept alive — see check_geometry_controls
    from acqApp.devices.dmd.corner_editor import CornerAdjustDialog

    calib = DmdCalibration(cam_to_dmd=np.linalg.inv(
        np.array([[4.0, 0.0, 100.0], [0.0, 4.0, 60.0], [0.0, 0.0, 1.0]])),
        dmd_size=(64, 48), cam_size=(400, 300))
    frame = np.zeros((300, 400), np.uint8)

    dlg = CornerAdjustDialog(calib, frame)
    r.check(len(dlg._targets) == 4,
            "one draggable corner per DMD corner")
    r.check(not dlg._chk_vignette.isChecked() and not dlg._vignette_roi.isVisible(),
            "the vignette circle starts unmarked and hidden when the "
            "calibration never had one")

    dlg._chk_vignette.setChecked(True)
    r.check(dlg._vignette_roi.isVisible(),
            "checking the box shows it, for dragging")
    dlg._vignette_roi.setPos([150.0, 110.0])
    dlg._vignette_roi.setSize([80.0, 80.0])    # r=40, centre (190, 150)
    dlg._targets[0].setPos(5.0, 5.0)           # move the (0, 0) corner too

    dlg._apply()
    got = dlg.calibration
    r.check(got is not None, "Apply with a checked box produces a result")
    r.check(got.vignette is not None
            and abs(got.vignette[0] - 190.0) < 1e-6
            and abs(got.vignette[1] - 150.0) < 1e-6
            and abs(got.vignette[2] - 40.0) < 1e-6,
            f"…recording the circle exactly as dragged ({got.vignette})")
    r.check(np.allclose(got.accessible_corners()[0], [5.0, 5.0]),
            "…and the dragged corner too — both reach the same result "
            "independently")

    dlg2 = CornerAdjustDialog(calib, frame)
    dlg2.reject()
    r.check(dlg2.calibration is None, "Cancel produces no result at all")

    marked = DmdCalibration(cam_to_dmd=calib.cam_to_dmd, dmd_size=calib.dmd_size,
                            cam_size=calib.cam_size, vignette=(200.0, 150.0, 90.0))
    dlg3 = CornerAdjustDialog(marked, frame)
    r.check(dlg3._chk_vignette.isChecked() and dlg3._vignette_roi.isVisible(),
            "a previously marked vignette shows pre-checked and visible")
    pos, size = dlg3._vignette_roi.pos(), dlg3._vignette_roi.size()
    r.check(abs(float(pos[0]) + float(size[0]) / 2 - 200.0) < 1e-6
            and abs(float(pos[1]) + float(size[1]) / 2 - 150.0) < 1e-6
            and abs(float(size[0]) / 2 - 90.0) < 1e-6,
            "…seeded at the calibration's own circle, not a fresh guess")

    dlg3._chk_vignette.setChecked(False)
    dlg3._apply()
    r.check(dlg3.calibration.vignette is None,
            "unchecking before Apply clears a previously marked vignette")


def check_manual_live(r: Report) -> None:
    """Manual (live): works with no sweep, seeds sanely, projects all-on and
    ends dark whatever happens, the editor image follows the camera, and only
    Apply replaces `_calib`."""
    _app = qt_app()          # kept alive — see check_geometry_controls
    import acqApp.devices.dmd.corner_editor as CE
    import acqApp.devices.dmd.sweep as SW
    from acqApp.devices.dmd.calibration import manual_seed

    seed = manual_seed((64, 48), (400, 300))
    c = seed.accessible_corners()
    r.check(seed.model == "manual+corners", f"seed is labelled manual ({seed.model})")
    r.check(c[:, 0].min() > 0 and c[:, 0].max() < 399
            and c[:, 1].min() > 0 and c[:, 1].max() < 299,
            "the seed rectangle sits inside the camera frame")
    r.check(abs((c[1, 0] - c[0, 0]) / (c[3, 1] - c[0, 1]) - 64 / 48) < 1e-6,
            "…at the panel's aspect ratio")

    projected: list = []
    events: list = []

    class FakeProjector:
        resolution = (64, 48)

        def project_frame(self, f):
            projected.append(np.asarray(f).copy())

        def stop(self):
            events.append("dark")

    counter = {"n": 0}

    def source():
        counter["n"] += 1
        return np.full((30, 40), counter["n"] % 256, np.uint8)

    seen: dict = {}

    class FakeEditor:
        result = None

        def __init__(self, calib, frame, *, live_source=None, start_label="",
                     parent=None):
            seen.update(calib=calib, live=live_source, label=start_label)

        def exec(self):
            events.append("exec")
            return SW.QDialog.DialogCode.Accepted if self.result is not None \
                else SW.QDialog.DialogCode.Rejected

        @property
        def calibration(self):
            return self.result

    dlg = SW.CalibrationDialog(FakeProjector(), source, real=True)
    r.check(dlg._btn_manual.isEnabled() and dlg._calib is None,
            "Manual is available with no sweep done")
    real_editor = CE.CornerAdjustDialog
    CE.CornerAdjustDialog = FakeEditor
    try:
        dlg._cancel = True               # as a stopped sweep leaves it
        dlg._manual_live()
        r.check(len(projected) == 1 and bool(np.all(projected[0] == ON)),
                "it projects ALL-ON")
        r.check(seen["calib"].model == "manual+corners" and seen["live"] is source
                and seen["label"] == "starting rectangle",
                "with no sweep the editor starts from the seed, fed by the camera")
        r.check(events == ["exec", "dark"], f"light is off after the editor ({events})")
        r.check(dlg._calib is None, "Cancel leaves the calibration unset")
        r.check(not dlg._btn_save.isEnabled() and not dlg._btn_adjust.isEnabled(),
                "…and Save / Adjust stay off")

        FakeEditor.result = manual_seed((64, 48), (40, 30))
        events.clear()
        dlg._manual_live()
        r.check(dlg._calib is FakeEditor.result and dlg._btn_save.isEnabled()
                and dlg._btn_adjust.isEnabled(),
                "Apply adopts the calibration and enables Save / Adjust")

        dlg._manual_live()
        r.check(seen["calib"] is FakeEditor.result and seen["label"] == "sweep fit",
                "with a fit in hand the editor starts from it")

        class Boom(FakeEditor):
            def exec(self):
                raise RuntimeError("boom")
        CE.CornerAdjustDialog = Boom
        events.clear()
        dlg._manual_live()
        r.check(events == ["dark"] and dlg._btn_run.isEnabled()
                and dlg._btn_manual.isEnabled(),
                "an editor failure still ends dark and re-enables the buttons")
    finally:
        CE.CornerAdjustDialog = real_editor

    # The real editor: a new frame object reaches the image; the same one doesn't.
    calib = manual_seed((64, 48), (400, 300))
    frames = [np.zeros((300, 400), np.uint8)]
    ed = CE.CornerAdjustDialog(calib, frames[0], live_source=lambda: frames[-1])
    r.check(ed._timer.isActive(), "a live editor polls the camera")
    bright = np.full((300, 400), 200, np.uint8)
    frames.append(bright)
    ed._refresh()
    r.check(ed._img.image.max() == 200, "a newer frame replaces the image")
    ed.reject()
    r.check(not ed._timer.isActive(), "closing the editor stops the polling")
    still = CE.CornerAdjustDialog(calib, frames[0])
    r.check(not still._timer.isActive(), "a still editor never polls")


def _part_sweep() -> int:
    r = Report("dmd-sweep")
    check_manual_live(r)
    check_fresh_grabber(r)
    check_end_to_end(r)
    check_display_modes(r)
    check_project_frame(r)
    check_wiring(r)
    check_geometry_controls(r)
    check_corner_adjust(r)
    check_corner_editor(r)
    return r.finish()


# ═══ roi ════════════════════════════════════════════════════════════════
# Shares DW, DH, CW, CH with the calib part.


def calib() -> DmdCalibration:
    """A DMD sitting rotated and offset inside a slightly larger camera FOV."""
    return DmdCalibration(cam_to_dmd=np.linalg.inv(true_transform()),
                          dmd_size=(DW, DH), cam_size=(CW, CH),
                          model="homography", rms_px=0.31, n_points=4096,
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

    # dim() is outside()'s twin for the marked vignette.
    vcx, vcy = float(corners[:, 0].mean()), float(corners[:, 1].mean())
    cv = with_vignette(c, vcx, vcy, 25.0)
    r.check(inn.dim(cv) == ["circle1"],
            f"an ROI past the marked vignette is reported, even though it's "
            f"fully reachable by the DMD ({inn.dim(cv)})")
    r.check(inn.outside(cv) == [],
            "control: it's still geometrically fine — dim() and outside() "
            "answer different questions")
    r.check(inn.dim(c) == [],
            "…and with no vignette ever marked, dim() has nothing to report")
    small = RoiSet()
    small.add(CircleRoi(x=vcx, y=vcy, r=5))
    r.check(small.dim(cv) == [],
            f"control: well inside the marked circle is not flagged "
            f"({small.dim(cv)})")

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

    # ── 4b. Flip X/Y mirror the PROJECTED PIXELS and nothing else ───────────
    # What the panel tooltip promises (2026-09-24): the drawing stays put,
    # only the lit mirrors flip.
    r.check(np.array_equal(near.dmd_frame(flip_y(c)), frame[::-1, :]),
            "Flip Y mirrors the mask's rows on the panel, exactly")
    r.check(np.array_equal(near.dmd_frame(flip_x(c)), frame[:, ::-1]),
            "Flip X mirrors its columns, exactly")
    r.check(np.allclose(np.sort(flip_y(c).accessible_corners(), axis=0),
                        np.sort(c.accessible_corners(), axis=0)),
            "…while the reachable field stays exactly where it was")
    probe = np.array([[CW / 2, CH / 2], [-50.0, -50.0], [CW + 50.0, CH + 50.0]])
    r.check(np.array_equal(flip_y(c).accessible(probe), c.accessible(probe))
            and np.array_equal(flip_x(c).accessible(probe), c.accessible(probe)),
            "…and so does every \"can the DMD reach this?\" answer")

    # ── 5. persistence of the calibration itself ─────────────────────────────
    tmp = Path(tempfile.mkdtemp(prefix="dmd_calib_")) / "cal.json"
    c.save(tmp)
    back = DmdCalibration.load(tmp)
    r.check(np.allclose(back.cam_to_dmd, c.cam_to_dmd)
            and back.dmd_size == c.dmd_size and back.cam_size == c.cam_size,
            "the calibration survives save/load")
    r.check(back.rms_px == c.rms_px and back.n_points == c.n_points,
            "…including the provenance that says whether to trust it")

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
    ed._btn_draw.setChecked(True)
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
    ed._cmb.setCurrentText("circle")
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y()), finish=False))
    er = vb._ellipse.rect()
    vb.mouseDragEvent(_Ev((la.x(), la.y()), (lb.x(), lb.y())))
    made = list(ed.roi_set)[0]
    r.check(abs(er.width() / 2 - made.r) < 0.5,
            f"the circle band previews the radius it creates "
            f"({er.width() / 2:.1f} vs {made.r:.1f})")
    ed._cmb.setCurrentText("rectangle")
    ed._btn_draw.setChecked(False)
    r.check(not vb._rect.isVisible() and not vb._ellipse.isVisible(),
            "disarming Draw clears any band left on screen")
    ed._on_clear()

    n = len(ed.roi_set)
    ed._on_drawn((200.0, 200.0), (200.5, 200.5))
    r.check(len(ed.roi_set) == n,
            "control: a click (not a drag) adds nothing")
    ed._on_clear()

    seen: list = []
    ed.rois_changed.connect(seen.append)
    ed._cmb.setCurrentText("rectangle")
    ed._on_add()
    ed._cmb.setCurrentText("circle")
    ed._on_add()
    r.check(len(ed.roi_set) == 2 and len(seen) == 2,
            f"adding one of each emits and lands in the set "
            f"({len(ed.roi_set)} rois, {len(seen)} signals)")
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

    ed._list.setCurrentRow(0)
    ed._on_delete()
    r.check(len(ed.roi_set) == 1, "delete removes the selected ROI")
    ed._on_clear()
    r.check(len(ed.roi_set) == 0, "clear empties the set")

    ed2 = RoiEditor(None)
    r.check("calibration" in ed2._status.text().lower(),
            f"with no calibration the editor says so ({ed2._status.text()!r})")
    ed2._on_add()
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

    corners = c.accessible_corners()
    xdata, ydata = ed3._field.getData()
    r.check(np.allclose(xdata[:-1], corners[:, 0] - ox)
            and np.allclose(ydata[:-1], corners[:, 1] - oy),
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

    return r.finish()


PARTS = {
    "dmd": _part_dmd,
    "calib": _part_calib,
    "sweep": _part_sweep,
    "roi": _part_roi,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
