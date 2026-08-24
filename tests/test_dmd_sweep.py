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
from test_dmd_calibration import (CH, CW, DH, DW, image_through, sample_field,
                                  true_transform)

from acqApp.devices.dmd.calibration import PROBE_FRACS

from acqApp.devices.dmd.calibration import (CalibrationError, apply_transform,
                                            axis_angle_deg, axis_scale,
                                            centre_out_probe,
                                            coarse_calibration, gray_planes,
                                            probe_verdict, run_calibration)
from acqApp.devices.dmd.sweep import FreshGrabber, sweep_exposures


# ── a rig where the camera lags the projector, as the real one does ───────────

class LaggingRig:
    """A projector and a camera that does NOT show the new pattern at once.

    `latency` frames after a projection still carry the previous pattern —
    exposed across the mirror flip. This is the whole reason `FreshGrabber` has
    a settle count, so the model has to have it or the test is vacuous.
    """

    def __init__(self, M, sample, rng, *, latency: int = 1, tick: int = 3):
        self.M, self.sample, self.rng = M, sample, rng
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
            self._frame = image_through(shown, self.M, self.sample, self.rng)

    def latest(self):
        return self._frame


def check_fresh_grabber(r: Report) -> None:
    rng = np.random.default_rng(3)
    M, sample = true_transform(), sample_field(rng)

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

    rig = LaggingRig(M, sample, rng, latency=1)
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
    rig2 = LaggingRig(M, sample, rng, latency=1)
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


def check_probe(r: Report) -> None:
    """The axis-wise probe measures what the transform actually does."""
    rng = np.random.default_rng(7)
    M, sample = true_transform(), sample_field(rng)
    held = {"f": None}

    def project(f):
        held["f"] = f

    def grab():
        return image_through(held["f"], M, sample, rng)

    steps = centre_out_probe(project, grab, (DW, DH), log=lambda _s: None)
    r.check(len(steps) == sweep_exposures((DW, DH), full=False) - 1,
            f"{len(steps)} probe rows, one per exposure after the dark "
            f"reference")
    n_f = len(PROBE_FRACS)
    r.check(all(s.axis == 0 for s in steps[1:1 + n_f])
            and all(s.axis == 1 for s in steps[1 + n_f:]),
            "x is swept to completion before y — each axis is established on "
            "its own")

    # Truth is the transform's Jacobian at the panel centre, NOT the 1.05/7deg
    # it was written with: the keystone makes the local scale 1.006/1.030 and
    # the local angle 6.03deg. Measuring against the parameters instead of the
    # behaviour is how a correct probe gets called broken.
    c = np.array([[DW / 2, DH / 2]], float)
    truth = {}
    for axis in (0, 1):
        d = np.zeros((1, 2))
        d[0, axis] = 1e-3
        j = (apply_transform(M, c + d) - apply_transform(M, c - d))[0] / 2e-3
        truth[axis] = (float(np.hypot(*j)),
                       float(np.degrees(np.arctan2(j[1], j[0]))))

    for axis, name in ((0, "x"), (1, "y")):
        k = axis_scale(steps, axis)
        want = truth[axis][0]
        r.check(k is not None and abs(k - want) < 0.03 * want,
                f"{name} scale {k:.3f} px/mirror against the Jacobian's "
                f"{want:.3f}")
    ang = axis_angle_deg(steps, 0)
    r.check(ang is not None and abs(ang - truth[0][1]) < 1.0,
            f"the rotation falls out of the x sweep alone: {ang:+.2f}deg "
            f"against {truth[0][1]:+.2f}")

    # An anisotropic relay is the case a disc CANNOT separate — its
    # equivalent-area radius averages the two axes into one number. This is the
    # reason the probe grows one axis at a time.
    A = np.diag([1.6, 0.8, 1.0]).astype(float)
    A[0, 2], A[1, 2] = 30.0, 40.0
    aniso = {"f": None}

    def grab_a():
        return image_through(aniso["f"], A, sample, rng)

    st = centre_out_probe(lambda f: aniso.__setitem__("f", f), grab_a,
                          (DW, DH), log=lambda _s: None)
    kx, ky = axis_scale(st, 0), axis_scale(st, 1)
    r.check(kx is not None and ky is not None
            and abs(kx - 1.6) < 0.08 and abs(ky - 0.8) < 0.08,
            f"an anisotropic relay reads as two different scales "
            f"({kx:.2f} and {ky:.2f} against 1.6 and 0.8)")
    r.check("anisotropic" in probe_verdict(st, st[0].cam_shape, (DW, DH)),
            "…and the verdict says so, rather than reporting one average scale")

    # CONTROL: a projector the camera cannot see must be named as such, not
    # fitted. This is the diagnosis the operator gets at a misaimed rig.
    def grab_dark():
        return sample + rng.normal(0, 4.0, sample.shape)

    dark_steps = centre_out_probe(project, grab_dark, (DW, DH),
                                  log=lambda _s: None)
    v = probe_verdict(dark_steps, dark_steps[0].cam_shape, (DW, DH))
    r.check(all(s.lit_px == 0 for s in dark_steps)
            and "does not modulate" in v,
            f"control: with no projector the probe reports it ({v[:50]}…)")


def check_run_calibration(r: Report) -> None:
    """The orchestrator, end to end, through a lagging camera.

    Nothing had ever called `run_calibration` — the pure test imports the
    pieces. Running it through `FreshGrabber` and a camera that lags is the
    only way the two halves are shown to fit together.
    """
    rng = np.random.default_rng(11)
    M, sample = true_transform(), sample_field(rng)
    rig = LaggingRig(M, sample, rng, latency=1, tick=2)
    g = FreshGrabber(rig.latest, settle=1, timeout_s=2.0, pump=rig.pump)

    n_projected = {"n": 0}

    def project(f):
        n_projected["n"] += 1
        rig.project(f)

    calib = run_calibration(project, g.grab, (DW, DH), step=4,
                            log=lambda _s: None)
    r.check(calib.rms_px < 1.0,
            f"a calibration comes back through a lagging camera "
            f"(rms {calib.rms_px:.3f} px over {calib.n_points} points)")
    r.check(calib.dmd_size == (DW, DH) and calib.cam_size == (CW, CH),
            f"…knowing both sizes it was measured at {calib.dmd_size} -> "
            f"{calib.cam_size}")
    r.check("probe:" in calib.notes and "px/mirror" in calib.notes,
            "…and carrying the probe's verdict, the only record of what the "
            "fields looked like before the fit")

    # The transform has to agree with the one we projected through.
    probe_pts = np.array([[20, 20], [DW - 20, 20], [DW // 2, DH // 2]], float)
    back = apply_transform(np.linalg.inv(calib.cam_to_dmd), probe_pts)
    want = apply_transform(M, probe_pts)
    r.check(np.abs(back - want).max() < 2.0,
            f"…and it agrees with the true transform (max "
            f"{np.abs(back - want).max():.2f} px)")

    # The dialog's warning is a count of exposures. If it drifts from what the
    # sweep really does, the operator is told a number about light emission
    # that is not true.
    r.check(n_projected["n"] <= sweep_exposures((DW, DH)),
            f"sweep_exposures() bounds the real count "
            f"({sweep_exposures((DW, DH))} quoted, {n_projected['n']} run)")
    # …and it is a TIGHT bound, not a safe over-estimate that tells the operator
    # nothing: the gap is only the planes a coarser code saved.
    r.check(n_projected["n"] >= 0.6 * sweep_exposures((DW, DH)),
            f"…tightly ({n_projected['n']} of {sweep_exposures((DW, DH))})")
    # 12 probe + 2 checkerboard + 4 resolution + 2x20 Gray planes. Worth
    # pinning: this is the number the dialog shows before any light is emitted.
    n_real = sweep_exposures((1024, 768))
    r.check(len(gray_planes(1024, 768)[0]) == 20 and n_real == 64,
            f"…and on the rig's own 1024x768 panel it is at most {n_real}")

    # A sweep that cannot be registered must say why, not return a bad matrix.
    try:
        run_calibration(project, lambda: sample + rng.normal(0, 4.0, sample.shape),
                        (DW, DH), log=lambda _s: None)
        r.check(False, "an unlit sweep raises rather than fitting noise")
    except CalibrationError as e:
        r.check("probe saw nothing" in str(e),
                f"control: an unlit sweep raises at the PROBE, before the "
                f"full-panel exposures ({str(e)[:45]}…)")


def check_unresolved_planes(r: Report) -> None:
    """The rig's 2026-08-24 failure: probe fine, checkerboard fine, decode 0.0 %.

    At the measured 4.56 camera px per mirror a 1-mirror Gray stripe is 4.6 px,
    and a relay that blurs even slightly averages it away. `decode` requires
    EVERY plane, so one unresolved plane invalidated all 10.5 Mpx.

    Simulated by imaging through a magnifying transform and blurring — which is
    what the optics do — rather than by mocking `decode` into failing.
    """
    from acqApp.devices.dmd.calibration import (decode, gray_planes,
                                                plane_coverage,
                                                resolve_gray_step)

    # A stand-in for the rig: the DMD magnified ~4.5x onto the camera, through a
    # relay whose point spread is about TWO mirror widths. That blur is the
    # premise, not a tuned number — it is what the rig's own data implies, since
    # a relay that resolved single mirrors would not have returned 0.0 %.
    dw, dh = 64, 48
    k, psf = 5, 9              # px per mirror (integer); PSF half-width, px
    cw, ch = dw * k, dh * k

    def image(pattern):
        cam = np.repeat(np.repeat(pattern.astype(np.float32), k, axis=0),
                        k, axis=1)
        assert cam.shape == (ch, cw), cam.shape
        pad = np.pad(cam, psf, mode="edge")
        blur = sum(pad[i:i + ch, j:j + cw]
                   for i in range(2 * psf + 1)
                   for j in range(2 * psf + 1)) / (2 * psf + 1) ** 2
        return blur * 3.0 + 40.0          # a sample under it, plus an offset

    def sweep(gstep):
        planes, nbx, nby = gray_planes(dw, dh, step=gstep)
        on = [image(p) for p in planes]
        off = [image((255 - p).astype(np.uint8)) for p in planes]
        _dx, _dy, valid = decode(on, off, nbx, nby)
        return float(valid.mean()), plane_coverage(on, off), nbx, len(planes)

    got, cov, nbx, n = sweep(1)
    r.check(got < 0.05,
            f"reproduced: at 1 mirror per code the sweep decodes "
            f"{100 * got:.1f}% of the frame")
    early = max(v for _t, v in cov[:max(1, nbx - 3)])
    r.check(early > 0.5 and cov[-1][1] < 0.05,
            f"…and the coverage table localises it — {100 * early:.0f}% still "
            f"valid through the coarse planes, {100 * cov[-1][1]:.1f}% at the "
            f"end. The FINE planes, not a dead field")

    # The step is MEASURED, by projecting each candidate's finest stripe.
    held = {}
    field = 1.0                          # this stand-in fills the frame
    gstep = resolve_gray_step(lambda f: held.__setitem__("f", f),
                              lambda: image(held["f"]), (dw, dh),
                              field=field, log=lambda _s: None)
    r.check(gstep > 1, f"resolve_gray_step measured its way to {gstep} "
                       f"mirror(s) per code, {gstep * k:.0f} camera px")
    fixed, _cov, _nbx, n2 = sweep(gstep)
    # Not "most of the frame" — under a PSF two mirrors wide only the stripe
    # CENTRES survive every plane's intersection. Enough to register from is
    # the bar that matters, and 0 % was the failure.
    r.check(fixed > 0.2,
            f"…and at that step it decodes {100 * fixed:.0f}% — thousands of "
            f"correspondences, against none")
    r.check(n2 < n, f"…on fewer planes too ({n2} against {n})")

    # CONTROL: coarsening must NOT rescue a rig that sees nothing, or it would
    # paper over a dark projector instead of a blurry one.
    planes, nbx2, nby2 = gray_planes(dw, dh, step=gstep)
    dark = [np.full((ch, cw), 50.0, np.float32) for _ in planes]
    _dx, _dy, v_dark = decode(dark, dark, nbx2, nby2)
    r.check(v_dark.mean() == 0.0,
            "control: with no modulation at all, coarsening still decodes "
            "nothing — it fixes unresolved stripes, not a dark rig")
    dead = resolve_gray_step(lambda f: None,
                             lambda: np.full((ch, cw), 50.0, np.float32),
                             (dw, dh), field=field, log=lambda _s: None)
    r.check(dead == 16,
            f"control: against a dead rig it exhausts every candidate ({dead}) "
            f"rather than reporting one as resolved")

    planes, nbx3, _nby3 = gray_planes(dw, dh, step=4)
    r.check(nbx3 == 4 and len(planes) == 4 + 4,
            f"a 64x48 panel at step 4 needs {nbx3} x-bits, not 6 "
            f"({len(planes)} planes total)")


def check_coarse_calibration(r: Report) -> None:
    """Rectangles alone give a usable affine — the rig's answer when Gray fails.

    The operator's question (2026-08-24): why not just run the rectangles and
    read the edges and the tilt? Because the probe already measures every
    parameter an affine has. The one thing it cannot measure is which WAY each
    axis runs, and that is what the off-centre bars are for.
    """
    rng = np.random.default_rng(5)
    sample = sample_field(rng)

    for name, M in (("rotated + keystone", true_transform()),
                    ("mirrored in x", np.array([[-1.1, 0.10, 250.0],
                                                [0.08, 1.05, 22.0],
                                                [0.0, 0.0, 1.0]]))):
        held = {"f": None}
        c = coarse_calibration(lambda f: held.__setitem__("f", f),
                               lambda: image_through(held["f"], M, sample, rng),
                               (DW, DH), log=lambda _s: None)
        r.check(c.model == "affine-coarse" and "COARSE" in c.notes,
                f"[{name}] the file says it is coarse, so it cannot be "
                f"mistaken for a Gray-coded fit later")
        # It must land where the transform really puts the mirrors.
        pts = np.array([[DW / 2, DH / 2], [DW / 4, DH / 4],
                        [3 * DW / 4, 2 * DH / 3]], float)
        got = apply_transform(np.linalg.inv(c.cam_to_dmd), pts)
        want = apply_transform(M, pts)
        err = float(np.abs(got - want).max())
        r.check(err < 12.0,
                f"[{name}] rectangles alone place the panel to {err:.1f} px "
                f"(rms {c.rms_px:.2f} over {c.n_points} bar measurements)")

    # CONTROL: the mirrored case must actually be mirrored, or the check above
    # passes for free and the handedness bars are proving nothing.
    flip = np.array([[-1.1, 0.10, 250.0], [0.08, 1.05, 22.0], [0.0, 0.0, 1.0]])
    d = (apply_transform(flip, np.array([[DW - 1, DH / 2]], float))
         - apply_transform(flip, np.array([[0, DH / 2]], float)))[0]
    r.check(d[0] < 0,
            f"control: that transform really does run DMD +x towards camera −x "
            f"({d[0]:+.0f} px), so a sign error would have been caught")


def check_project_frame(r: Report) -> None:
    """`project_frame` must not go through `build_frame`. PLAN §6 names this
    trap twice, and it is invisible in the result: a warped calibration
    pattern still decodes, into the wrong geometry."""
    from acqApp.acq.devices import RawProjector
    from acqApp.devices.dmd.calibration import axis_bar
    from acqApp.devices.dmd.control import (DEFAULT_H, DEFAULT_W, DmdSettings,
                                            MockDmdController)

    # Hostile geometry: every knob set to something that would visibly move a
    # pattern, and `fit` on, which overrides the other three.
    s = DmdSettings(scale_pct=57.0, rotation_deg=23.0, offset_x=-90.0,
                    offset_y=45.0, fit=True, invert=True)
    c = MockDmdController(s)
    r.check(isinstance(c, RawProjector),
            "the mock controller satisfies RawProjector")

    pattern = axis_bar(DEFAULT_W, DEFAULT_H, 0, 200.0)
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

    from _harness import isolate_user_state, pump, qt_app
    isolate_user_state()
    app = qt_app()
    import acqApp.main as M
    _s.argv = ["main.py", "--mock"]
    win = M.MainWindow(cam_info=None, mock=True,
                       enabled={"voltage_cam", "dmd"}, cam_handle=None)
    dmd = next(m for m in win._modules if m.key == "dmd")

    r.check(hasattr(dmd, "calibrate"),
            "the DMD adapter owns the calibrate path, not the panel — only it "
            "can reach both the controller and the camera")

    # No camera frame yet: the dialog must not open. Asserted by watching for
    # the message box rather than the dialog, since neither can be shown here.
    seen = {"box": 0, "dialog": 0}
    from PyQt6.QtWidgets import QMessageBox
    real_info = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: seen.__setitem__("box", seen["box"] + 1))
    import acqApp.devices.dmd.sweep as SW
    real_dlg = SW.CalibrationDialog

    class FakeDialog:
        """Stands in for the sweep window — it must be constructed AND exec'd,
        so a wiring that builds it and forgets to show it still fails."""

        def __init__(self, *_a, **_k):
            seen["dialog"] += 1

        def exec(self):
            seen["exec"] += 1
            return 0

    seen["exec"] = 0
    SW.CalibrationDialog = FakeDialog
    try:
        dmd.panel.calibrate_requested.emit()
        r.check(seen["box"] == 1 and seen["dialog"] == 0,
                "with no camera frame it explains instead of opening the "
                "sweep — a sweep with nothing to image would emit light for "
                "nothing")

        win._btn_run.setChecked(True)
        pump(app, 1.2)
        r.check(win.latest_frame("voltage_cam") is not None,
                "control: the camera is now delivering frames")
        dmd.panel.calibrate_requested.emit()
        r.check(seen["dialog"] == 1 and seen["exec"] == 1,
                "…and with one, the button opens the sweep dialog")
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


def main() -> int:
    r = Report("dmd-sweep")
    check_fresh_grabber(r)
    check_probe(r)
    check_run_calibration(r)
    check_unresolved_planes(r)
    check_coarse_calibration(r)
    check_project_frame(r)
    check_wiring(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
