"""Running the calibration sweep: the fresh-frame grab, and the whole chain.

`test_dmd_calibration.py` holds the pure half to a transform we chose. This
covers the half that touches the rig, and the two defects that half can have:

  * **a stale grab.** `latest_frame` hands back the frame the camera last
    *displayed*, which is older than the pattern just projected. Decode forty
    planes from the frame before each and the fit comes back with a plausible
    rms on nonsense — the failure that cannot be seen in the result.
  * **a transformed pattern.** `build_frame`'s scale/rotation/offset, and `fit`
    which overrides all three, would warp the geometry being measured.

It also runs `run_calibration` end to end for the first time. Until 2026-08-24
nothing executed it: the pure test imports the pieces, not the orchestrator, so
the one function the whole feature turns on had never been called.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_dmd_sweep.py
"""
from __future__ import annotations

import sys

import numpy as np

from _harness import Report

# The simulated rig, imported rather than copied: a second camera model would
# be a second set of assumptions to keep in step with this one. It is a plain
# module — its own checks run only under __main__.
from test_dmd_calibration import (CH, CW, DH, DW, TEST_CROSS_FRAC,
                                  TEST_THICK_FRAC, make_camera, true_transform)

from acqApp.devices.dmd.calibration import (STRIPE_OFFSETS, CalibrationError,
                                            apply_transform, calibrate)
from acqApp.devices.dmd.sweep import FreshGrabber, sweep_exposures


# ── a rig where the camera lags the projector, as the real one does ───────────

class LaggingRig:
    """A projector and a camera that does NOT show the new pattern at once.

    `latency` frames after a projection still carry the previous pattern —
    exposed across the mirror flip. This is the whole reason `FreshGrabber` has
    a settle count, so the model has to have it or the test is vacuous.
    """

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

    # A pattern whose brightness names it, so a returned frame can be traced
    # back to the pattern that was up when it was exposed.
    def marked(v):
        return np.full((DH, DW), np.uint8(v))

    def which(frame):
        """Which pattern this frame came from: lit -> 255, dark -> 0.

        Against a midpoint, not against the dark frame's own mean: an all-off
        pattern gives `sample`, a full-on one `3 * sample`, and comparing two
        dark frames to each other is a coin flip on the noise.
        """
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

    # CONTROL: with no settle the in-flight frame gets through, so the check
    # above is testing the settle count and not the rig's own timing.
    rig2 = LaggingRig(M, rng, latency=1)
    rig2.project(marked(0))
    for _ in range(20):
        rig2.pump()
    g0 = FreshGrabber(rig2.latest, settle=0, timeout_s=2.0, pump=rig2.pump)
    rig2.project(marked(255))
    r.check(which(g0.grab()) == 0,
            "control: settle=0 returns the STALE frame — the lag is real, and "
            "the settle count is what defeats it")

    # A frame object that never changes must not be handed back as new.
    stuck = np.zeros((CH, CW))
    frozen = FreshGrabber(lambda: stuck, settle=0, timeout_s=0.15,
                          pump=lambda: None)
    try:
        frozen.grab()
        r.check(False, "a stalled camera raises")
    except CalibrationError as e:
        r.check("RUNNING" in str(e) or "running" in str(e),
                f"a stalled camera raises, naming the cause ({str(e)[:60]}…)")

    # …and identity is the test, not equality: two equal-but-distinct frames
    # are two real exposures of an unchanging sample.
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
    """calibrate() through a camera that LAGS — the two halves meeting.

    Neither half proves this on its own: the maths is tested against an
    instant camera, and `FreshGrabber` is tested against a rig with no
    geometry. A wrong pairing of project and grab only shows up here.
    """
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
    import tempfile
    from pathlib import Path

    from acqApp.devices.dmd.calibration import DmdCalibration
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, MODE_ALL_ON,
                                            MODE_PATTERN, MODE_ROI,
                                            DmdSettings, MockDmdController)

    # A calibration whose panel matches the mock, so the mask is device-sized.
    A = np.array([[4.0, 0.0, 200.0], [0.0, 4.0, 150.0], [0.0, 0.0, 1.0]])
    calib = DmdCalibration(cam_to_dmd=np.linalg.inv(A),
                           dmd_size=(DEFAULT_W, DEFAULT_H), cam_size=(900, 600))
    cpath = Path(tempfile.mkdtemp()) / "c.json"
    calib.save(cpath)
    # Keys as `RectRoi.to_dict()` really writes them — x/y/angle_deg, not
    # cx/cy/angle. The panel round-trips these through `roi_from_dict`, so a
    # near-miss raises rather than being ignored.
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
    # It must be the RIGHT mirrors: a 120x90 camera-px ROI at 4 px/mirror is
    # about 30x22 mirrors.
    r.check(abs(n_roi - (120 / 4) * (90 / 4)) < 0.4 * (120 / 4) * (90 / 4),
            f"…and about the right number of them ({n_roi}, expected ~675)")

    # CONTROL: without a calibration there is no way to map camera px to
    # mirrors, and guessing would aim light at the wrong place.
    c = MockDmdController(DmdSettings(display_mode=MODE_ROI, rois=(roi,)))
    c.load_pattern()
    r.check(c.on_pixels == 0,
            "control: ROI mode with no calibration projects nothing rather "
            "than guessing a transform")
    c = MockDmdController(DmdSettings(display_mode=MODE_ROI,
                                      calib_path=str(cpath)))
    c.load_pattern()
    r.check(c.on_pixels == 0, "control: …and with no ROIs, likewise")

    # Switching mode reloads: the old guard only reloaded when a pattern FILE
    # was set, so all-on and ROI modes would have stayed stale.
    c = MockDmdController(DmdSettings(display_mode=MODE_PATTERN))
    c.apply_settings(DmdSettings(display_mode=MODE_ALL_ON))
    r.check(c.on_pixels == all_on,
            "changing the mode reloads the frame, with no pattern file set")


def check_project_frame(r: Report) -> None:
    """`project_frame` must not go through `build_frame`. PLAN §6 names this
    trap twice, and it is invisible in the result: a warped calibration
    pattern still decodes, into the wrong geometry."""
    from acqApp.acq.devices import RawProjector
    from acqApp.devices.dmd.calibration import offset_stripe
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, DmdSettings,
                                            MockDmdController)

    # Hostile geometry: every knob set to something that would visibly move a
    # pattern, and `fit` on, which overrides the other three.
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

    # CONTROL: those settings are not inert — build_frame really would move it.
    from acqApp.devices.dmd import alp
    built = alp.build_frame(pattern, DEFAULT_W, DEFAULT_H, scale_pct=s.scale_pct,
                            rotation_deg=s.rotation_deg, offset_x=s.offset_x,
                            offset_y=s.offset_y, invert=s.invert, fit=s.fit)
    r.check(not np.array_equal(built, pattern),
            "control: run through build_frame the same pattern IS transformed, "
            "so the check above is not vacuous")

    # A frame that is not device-sized is a bug in the caller, not something to
    # pad: it would silently register the wrong panel.
    try:
        c.project_frame(np.zeros((10, 10), np.uint8))
        r.check(False, "a mis-sized frame is refused")
    except ValueError as e:
        r.check("device is" in str(e),
                f"a mis-sized frame is refused, naming both shapes ({e})")


def check_wiring(r: Report) -> None:
    """The button reaches the adapter, and the adapter refuses without a camera."""
    import sys as _s

    from _harness import isolate_user_state, make_window, pump, qt_app
    isolate_user_state()
    app = qt_app()
    _s.argv = ["main.py", "--mock"]
    win = make_window({"voltage_cam", "dmd"})
    dmd = next(m for m in win._modules if m.key == "dmd")

    r.check(hasattr(dmd, "calibrate"),
            "the DMD adapter owns the calibrate path, not the panel — only it "
            "can reach both the controller and the camera")

    # The dialog must open with the camera STOPPED: it starts the camera
    # itself and puts it back, so requiring Live view first would be friction
    # with no safety value — the actuation decision is the dialog's button.
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

        # set_live really drives the window, and reports the PREVIOUS state so
        # the dialog can restore it.
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

    # The measured calibration must reach the panel, or the ROI editor keeps
    # drawing the old field.
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
    """The Model/cross-length controls (added for a rig whose camera is
    tilted enough that the affine fit measurably mis-registers it — see
    test_dmd_calibration.py's homography checks) seed from this rig's
    profile, stay editable, and the operator's choice — not the seed — is
    what reaches calibrate()."""
    from _harness import qt_app
    _app = qt_app()          # kept alive for the function's duration — an
    # unreferenced QApplication is garbage-collected immediately, taking the
    # C++ singleton with it, and every widget built after that segfaults with
    # no Python traceback at all.
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
    from _harness import qt_app
    _app = qt_app()          # kept alive — see check_geometry_controls
    import acqApp.devices.dmd.sweep as SW
    from acqApp.devices.dmd.calibration import ON, DmdCalibration

    projected: list = []

    class FakeProjector:
        resolution = (64, 48)

        def project_frame(self, f):
            projected.append(np.asarray(f).copy())

        def stop(self):
            pass

    # A fresh array object every call — FreshGrabber keys on IDENTITY, exactly
    # as the real display tick does (a new object per frame), so a source
    # that reused one array would make every grab() wait for the timeout.
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

    # CANCEL: the fake corner-adjust dialog reports Rejected, so _calib and
    # every button's enabled state must come back exactly as they went in.
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

        # APPLY: exec -> Accepted with a result must adopt it.
        FakeCornerDialog.result = adjusted
        dlg._adjust_corners()
        r.check(dlg._calib is adjusted,
                "Apply (exec -> Accepted) adopts the corner-adjusted calibration")
    finally:
        CE.CornerAdjustDialog = real_corner_dlg


def check_corner_editor(r: Report) -> None:
    """The real `CornerAdjustDialog`: corners AND the vignette circle reach
    Apply's result, independently of each other, and Cancel discards both."""
    from _harness import qt_app
    _app = qt_app()          # kept alive — see check_geometry_controls
    from acqApp.devices.dmd.calibration import DmdCalibration
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

    # CANCEL must discard everything, not just leave the corners alone.
    dlg2 = CornerAdjustDialog(calib, frame)
    dlg2.reject()
    r.check(dlg2.calibration is None, "Cancel produces no result at all")

    # Re-opening on an ALREADY-marked calibration must show it pre-checked
    # and pre-positioned, not force the operator to remark it from scratch.
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

    # Unchecking the box on Apply must un-mark it, not just hide the widget.
    dlg3._chk_vignette.setChecked(False)
    dlg3._apply()
    r.check(dlg3.calibration.vignette is None,
            "unchecking before Apply clears a previously marked vignette")


def main() -> int:
    r = Report("dmd-sweep")
    check_fresh_grabber(r)
    check_end_to_end(r)
    check_display_modes(r)
    check_project_frame(r)
    check_wiring(r)
    check_geometry_controls(r)
    check_corner_adjust(r)
    check_corner_editor(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
