"""Pupil camera: EyeLoop seam, tracking, the eye region, clip replay.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil.py [-q] [--part NAME]
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from _harness import (Report, isolate_user_state, make_window, npoints, pump,
                      qt_app, run_parts)
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking import PupilTracking
from acqApp.devices.pupil_cam.track_worker import PupilTrackWorker
from PyQt6.QtCore import QPointF, Qt


# ═══ eyeloop (was test_pupil_eyeloop.py) ════════════════════════════════

CLIPS = {
    "pAce": (Path(r"E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi"), (850, 490)),
    "State": (Path(r"E:\State\VF182.6B\20260709\FOV1_T1\FOV1_T1_Pupil.avi"), (900, 490)),
}


def synthetic_eye(w=400, h=400, centre=(200, 200), radius=55, glint=None):
    """A dark disc on mid-grey, optionally with a saturated reflection in it."""
    img = np.full((h, w), 150, np.uint8)
    yy, xx = np.ogrid[:h, :w]
    img[(xx - centre[0]) ** 2 + (yy - centre[1]) ** 2 <= radius ** 2] = 20
    if glint is not None:
        gx, gy, gr = glint
        img[(xx - gx) ** 2 + (yy - gy) ** 2 <= gr ** 2] = 235
    return img


def frame_with_eye(eye, at=(850, 490), shape=(1208, 1928)):
    """Drop a synthetic eye into a full-size dark frame at `at`."""
    full = np.full(shape, 12, np.uint8)
    h, w = eye.shape
    x0, y0 = int(at[0] - w // 2), int(at[1] - h // 2)
    full[y0:y0 + h, x0:x0 + w] = eye
    return full


def _part_eyeloop() -> int:
    isolate_user_state()
    r = Report("pupil-eyeloop")

    try:
        from acqApp.devices.pupil_cam.eyeloop_tracker import (
            EYELOOP_DIR, GlintRemoval, Pin, remove_glints)
    except ImportError as e:
        print(f"[pupil-eyeloop] cannot import the wrapper: {e}")
        return 1

    have_clone = (EYELOOP_DIR / "eyeloop").is_dir()

    # ── the contract that must hold with NO clone ────────────────────────────
    st_off = PupilSettings(track=False, limit_x0=50, limit_y0=50,
                           limit_x1=350, limit_y1=350)
    pt_off = PupilTracking()
    r.check(pt_off.track(synthetic_eye(), st_off) is None,
            "tracking off returns None regardless of anything else")

    st_noregion = PupilSettings(track=True)
    r.check(PupilTracking().track(synthetic_eye(), st_noregion) is None,
            "no eye region means no tracking (the crop is not optional)")

    if not have_clone:
        print(f"[pupil-eyeloop] no EyeLoop clone at {EYELOOP_DIR} — "
              f"skipping the tracking checks (see docs/EYELOOP.md)")
        return r.finish()

    # ── it tracks, and the crop is what makes it work ───────────────────────
    eye = synthetic_eye(glint=(180, 215, 9))
    full = frame_with_eye(eye)
    st = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                       limit_x0=650, limit_y0=290, limit_x1=1050, limit_y1=690)

    pt = PupilTracking()
    fit = pt.track(full, st)
    r.check(fit is not None, "a synthetic eye is tracked through the app path")
    if fit is not None:
        r.check(abs(fit.center_x - 850) < 12 and abs(fit.center_y - 490) < 12,
                f"the fit is in FULL-FRAME pixels ({fit.center_x:.0f},"
                f"{fit.center_y:.0f}) not crop pixels")
        r.check(abs(fit.radius - 55) < 8,
                f"radius {fit.radius:.1f} recovers the synthetic 55")
        r.check(fit.axis_ratio > 0.9,
                f"a round pupil fits round (ratio {fit.axis_ratio:.2f})")

    # CONTROL: EyeLoop cannot fit a full rig frame; hence the region.
    st_full = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                            limit_x0=364, limit_y0=4, limit_x1=1564, limit_y1=1204)
    ctl = PupilTracking().track(full, st_full)
    r.check(ctl is None or abs(ctl.radius - 55) > 15,
            "control: a region covering the whole frame does NOT recover it")

    # ── a failure must read as a failure, not as the last good frame ────────
    pt2 = PupilTracking()
    good = pt2.track(full, st)
    rng = np.random.default_rng(0)
    noise = frame_with_eye(rng.integers(90, 150, (400, 400), dtype=np.uint8))
    misses = [pt2.track(noise, st) for _ in range(8)]
    r.check(good is not None and all(m is None for m in misses),
            "a frame with no pupil returns None, not the previous fit")
    # CONTROL: EyeLoop's own state is where the stale answer lived.
    r.check(pt2._tracker is not None
            and pt2._tracker._shape.fit_model.params is None,
            "control: params really was nulled, not merely re-read")

    # ── settings changes must not silently kill every frame ─────────────────
    # Floats in the walk radius make np.clip raise inside a bare except.
    st_moved = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                             limit_x0=730, limit_y0=370, limit_x1=970, limit_y1=610)
    pt3 = PupilTracking()
    pt3.track(full, st)
    box_a = pt3._box
    pt3.track(full, st_moved)
    r.check(box_a != pt3._box, "resizing the eye region re-arms the tracker")
    r.check(pt3.track(full, st_moved) is not None,
            "and it still tracks after the re-arm")

    # A model change must re-arm too: EyeLoop bakes it in at arm(), and the
    # operator found it could not be changed without reopening.
    st_circle = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                              limit_x0=650, limit_y0=290, limit_x1=1050,
                              limit_y1=690, track_model="circular")
    pt4b = PupilTracking()
    pt4b.track(full, st)                # armed with the default "ellipsoid"
    tracker_before = pt4b._tracker
    r.check(pt4b._model == "ellipsoid", f"…and remembers which ({pt4b._model!r})")
    pt4b.track(full, st_circle)         # same box, model only
    r.check(pt4b._model == "circular",
            f"a model change alone re-arms — the box did not move "
            f"({pt4b._model!r})")
    r.check(pt4b._tracker is not tracker_before,
            "…a genuinely new EyeLoopTracker, not the old one relabelled")
    r.check(pt4b.track(full, st_circle) is not None,
            "…and it still tracks after switching models")

    # ── reflection removal: it removes, and it stays inside the pupil ────────
    glinty = synthetic_eye(glint=(180, 215, 9))
    cfg = GlintRemoval(enabled=True, threshold=120, pad=4, ring=6,
                       search_scale=0.85)
    cleaned, mask = remove_glints(glinty, (200, 200), 55, cfg, (55, 55, 0))
    r.check(mask.sum() > 0, "the reflection is found")
    r.check(cleaned[215, 180] < 120,
            f"and blanked to {cleaned[215, 180]} — it reads as pupil again")
    # CONTROL: masking the rim erases the edge and inflates the radius
    # (+3.5 px, docs/EYELOOP.md).
    yy, xx = np.ogrid[:400, :400]
    d = np.hypot(xx - 200, yy - 200)
    r.check(not mask[d > 55 * 0.95].any(),
            "control: nothing outside 0.95 r is masked")

    # a pin reaches what the automatic pass will not
    far = synthetic_eye(glint=(200, 250, 10))     # at ~0.91 r, outside reach
    tight = GlintRemoval(enabled=True, threshold=120, search_scale=0.55)
    _, m_auto = remove_glints(far, (200, 200), 55, tight, (55, 55, 0))
    _, m_pin = remove_glints(far, (200, 200), 55,
                             GlintRemoval(enabled=True, threshold=120,
                                          search_scale=0.55,
                                          pins=(Pin(200, 250, 14),)),
                             (55, 55, 0))
    r.check(m_auto.sum() == 0, "control: the automatic pass cannot reach it")
    r.check(m_pin.sum() > 0, "a pin can — pins are exempt from reach")

    # pins are stored in full-frame pixels and must survive a region move
    st_pin = PupilSettings(track=True, track_threshold=60,
                           limit_x0=650, limit_y0=290, limit_x1=1050, limit_y1=690,
                           cr_pins=[(830.0, 505.0, 12.0)])
    box = st_pin.crop_box(full.shape)
    r.check(box == (650, 290, 1050, 690), f"crop box from the region: {box}")
    pt4 = PupilTracking()
    r.check(pt4.track(full, st_pin) is not None,
            "a pinned reflection does not break tracking")

    # ── the rig clips, where the numbers came from ──────────────────────────
    from acqApp.devices.pupil_cam.avi import AviReader
    for name, (path, (ex, ey)) in CLIPS.items():
        if not path.exists():
            r.note(f"SKIP {name}: clip not on this machine")
            continue
        rd = AviReader(str(path))
        s = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                          limit_x0=ex - 200, limit_y0=ey - 200,
                          limit_x1=ex + 200, limit_y1=ey + 200)
        p = PupilTracking()
        fits = [p.track(rd.luma(i), s) for i in range(len(rd))]
        ok = [f for f in fits if f is not None]
        r.check(len(ok) == len(fits),
                f"{name}: {len(ok)}/{len(fits)} frames fit")
        rad = np.array([f.radius for f in ok])
        r.check(rad.std() < 4.0,
                f"{name}: radius is steady across the clip "
                f"({rad.mean():.1f} +- {rad.std():.2f} px)")

    return r.finish()


# ═══ track (was test_pupil_track.py) ════════════════════════════════════

REGION = dict(limit_x0=60.0, limit_y0=20.0, limit_x1=260.0, limit_y1=220.0)


def eye_frame(w=320, h=240, r=40) -> np.ndarray:
    """The mock camera's frame: a dark disc on mid-grey, with a glint in it."""
    img = np.full((h, w), 180, np.uint8)
    yy, xx = np.ogrid[:h, :w]
    img[(xx - w // 2) ** 2 + (yy - h // 2) ** 2 <= r ** 2] = 20
    img[(xx - (w // 2 + 12)) ** 2 + (yy - (h // 2 - 10)) ** 2 <= 25] = 245
    return img


class FakeRec:
    """Stands in for the Recorder: remembers what was offered, and to which
    stream."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, float, float | None]] = []

    def put(self, stream, data, at=None) -> None:
        self.puts.append((stream, float(data), at))


def run_worker(app, r: Report, st: PupilSettings, *, frames: int = 6,
               reconfigure: PupilSettings | None = None):
    """Drive a worker over `frames` synthetic frames. Returns what it saw."""
    frame = eye_frame()
    # `gate` holds the source at half the frames until the reconfigure, or the
    # worker serves every frame before the edit.
    served = {"n": 0, "thread": None,
              "gate": frames if reconfigure is None else frames // 2}

    def source():
        served["thread"] = threading.get_ident()
        if served["n"] >= served["gate"]:
            return None
        served["n"] += 1
        return frame

    w = PupilTrackWorker(source, st)
    sink: list[tuple] = []
    w.set_fit_sink(lambda fit, is_blink, at: sink.append((fit, is_blink, at)))
    seen: list = []
    crashed: list[str] = []
    w.error.connect(crashed.append)     # the Qt signal, NOT `track_error`

    w.start()
    deadline = time.perf_counter() + 4.0
    while time.perf_counter() < deadline:
        pump(app, 0.05)
        tr = w.get_latest()
        if tr is not None:
            seen.append(tr)
        if reconfigure is not None and served["n"] >= served["gate"]:
            w.configure(reconfigure)
            reconfigure = None
            served["gate"] = frames
            deadline = time.perf_counter() + 4.0
        if served["n"] >= frames and reconfigure is None:
            break
    radii = [radius for radius, _blink in w.take_tracked()]
    w.stop()
    r.check(not w.isRunning(), "the worker stops when told to")
    r.check(crashed == [], f"…without an exception escaping its thread ({crashed})")
    return w, served, seen, sink, radii


def _part_track() -> int:
    r = Report("pupil-track")
    isolate_user_state()
    app = qt_app()

    main_thread = threading.get_ident()

    # ── 1. it runs somewhere else ────────────────────────────────────────────
    st_on = PupilSettings(track=True, **REGION)
    w, served, seen, sink, radii = run_worker(app, r, st_on)
    r.check(served["n"] > 0, f"the worker pulled frames ({served['n']})")
    r.check(served["thread"] not in (None, main_thread),
            "frames are pulled on the worker's thread, not the GUI's")
    # CONTROL: the comparison is against something that can differ.
    inline = threading.get_ident()
    r.check(inline == main_thread,
            "control: the same reading taken inline is the GUI thread")
    r.info(f"EyeLoop: {w.track_error or 'available'}")

    # ── 2. the frame and its fit travel together ─────────────────────────────
    r.check(seen and all(t.frame is not None for t in seen),
            f"every published item carries its frame ({len(seen)} seen)")
    r.check(all(t.fit is None or t.box is not None for t in seen),
            "a fit always comes with the crop it was made in")

    # ── 3. every tracked frame reaches the recorder ──────────────────────────
    r.check(len(sink) == w.frames_seen,
            f"one sink call per tracked frame ({len(sink)} vs {w.frames_seen})")
    r.check(len(radii) == w.frames_seen,
            f"…and one trace point per tracked frame ({len(radii)})")
    r.check(all(isinstance(at, float) and at > 0 for _f, _b, at in sink),
            "each carries the time the frame was pulled")
    r.check(w.fits == sum(1 for f, _b, _at in sink if f is not None),
            f"the fit counter matches the fits ({w.fits}/{w.frames_seen})")

    # CONTROL: tracking off records nothing, while frames still flow.
    st_off = PupilSettings(track=False, **REGION)
    w2, served2, seen2, sink2, radii2 = run_worker(app, r, st_off)
    r.check(served2["n"] > 0 and seen2, "control: frames still flow with tracking off")
    r.check(sink2 == [] and radii2 == [],
            f"control: …but nothing is recorded or traced ({len(sink2)}, "
            f"{len(radii2)})")
    r.check(all(t.fit is None for t in seen2),
            "control: …and no frame carries a fit")

    # ── 4. a settings edit reaches the running worker ────────────────────────
    w3, _s3, _v3, sink3, _r3 = run_worker(app, r, PupilSettings(track=False,
                                                                **REGION),
                                          frames=10, reconfigure=st_on)
    r.check(len(sink3) > 0,
            f"turning tracking on mid-run starts the trace ({len(sink3)} frames)")
    r.check(len(sink3) < w3.frames_seen,
            f"…and only from the edit onwards ({len(sink3)} of "
            f"{w3.frames_seen} frames)")

    # ── 5. `error` is still the Qt signal that carries a crash out ───────────
    r.check(hasattr(w.error, "connect"),
            "`error` is the PullWorker signal, not shadowed by a property")
    r.check(w.track_error is None or isinstance(w.track_error, str),
            f"`track_error` is the message ({w.track_error!r})")

    # ── 5b. the fit smoother — a rolling mean, with a circular angle mean ────
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.track_worker import _FitSmoother

    sm = _FitSmoother()
    f1 = sm.apply(PupilFit(100.0, 100.0, 40.0, 30.0, 10.0), window=1)
    r.check(f1 == PupilFit(100.0, 100.0, 40.0, 30.0, 10.0),
            "window=1 is a no-op — the raw fit, unchanged")

    sm = _FitSmoother()
    fits = [PupilFit(100.0 + d, 100.0, 40.0, 30.0, 10.0) for d in (0.0, 10.0, 20.0)]
    out = [sm.apply(f, window=3) for f in fits]
    r.check(abs(out[0].center_x - 100.0) < 1e-9,
            f"the first fit in a run is unaveraged, one value in ({out[0].center_x})")
    r.check(abs(out[1].center_x - 105.0) < 1e-9,
            f"two fits in, the mean of both ({out[1].center_x})")
    r.check(abs(out[2].center_x - 110.0) < 1e-9,
            f"three fits in (=window), the mean of all three ({out[2].center_x})")

    sm = _FitSmoother()
    for f in fits:
        sm.apply(f, window=3)
    f4 = sm.apply(PupilFit(140.0, 100.0, 40.0, 30.0, 10.0), window=3)
    r.check(abs(f4.center_x - (110.0 + 120.0 + 140.0) / 3.0) < 1e-9,
            f"a fourth fit drops the oldest — mean of the last 3 (110,120,140), "
            f"not all 4 ({f4.center_x})")

    # CONTROL: a lost frame clears the buffer rather than being skipped.
    sm = _FitSmoother()
    for f in fits:
        sm.apply(f, window=3)
    r.check(sm.apply(None, window=3) is None,
            "a lost frame reports no fit, same as unsmoothed")
    f_after = sm.apply(PupilFit(500.0, 500.0, 40.0, 30.0, 10.0), window=3)
    r.check(f_after.center_x == 500.0,
            f"control: the fit right after a loss is raw, not blended with "
            f"pre-loss history ({f_after.center_x})")

    # 179 and 1 deg are 2 deg apart; a plain mean gets the wrap wrong.
    sm = _FitSmoother()
    sm.apply(PupilFit(0.0, 0.0, 40.0, 30.0, 179.0), window=2)
    wrapped = sm.apply(PupilFit(0.0, 0.0, 40.0, 30.0, 1.0), window=2)
    r.check(wrapped.angle_deg < 5.0 or wrapped.angle_deg > 175.0,
            f"the angle mean wraps at 180 deg, not through the middle "
            f"({wrapped.angle_deg:.1f})")

    # ── 5c. the blink detector — a sudden drop against a rolling baseline ────
    from acqApp.devices.pupil_cam.track_worker import _BlinkDetector

    bd = _BlinkDetector()
    steady = [bd.check(30.0, drop_frac=0.35, window=10) for _ in range(6)]
    r.check(not any(steady),
            f"a steady radius never flags, warm-up included ({steady})")

    bd = _BlinkDetector()
    for _ in range(6):
        bd.check(30.0, drop_frac=0.35, window=10)
    r.check(not bd.check(25.0, drop_frac=0.35, window=10),
            "a 17% dip under a 35% threshold does not flag")
    r.check(bd.check(15.0, drop_frac=0.35, window=10),
            "a 50% drop under the same threshold does")
    r.check(bd.check(14.0, drop_frac=0.35, window=10),
            "…and stays flagged while the radius stays down")
    r.check(not bd.check(29.0, drop_frac=0.35, window=10),
            "…and clears once the radius recovers")

    # CONTROL: or a long enough blink would become the baseline.
    bd = _BlinkDetector()
    for _ in range(6):
        bd.check(30.0, drop_frac=0.35, window=10)
    for _ in range(20):                 # a long blink, well past `window`
        bd.check(10.0, drop_frac=0.35, window=10)
    r.check(bd.check(28.0, drop_frac=0.35, window=10) is False,
            "control: the baseline held at ~30 through a long blink, so the "
            "eventual recovery reads as recovery, not as a fresh baseline")

    r.check(bd.check(None, drop_frac=0.35, window=10) is False,
            "no radius (no fit) is never itself a flagged blink")

    # ══ the app half ═════════════════════════════════════════════════════════
    sys.argv = ["main.py", "--mock"]

    win = make_window({"pupil_cam"})
    mod = win._modules[0]
    panel = mod.panel

    # ── 6. what the recorder is offered, stream by stream ────────────────────
    all_streams = list(mod.FIT_STREAMS) + [mod.BLINK_STREAM]
    rec = FakeRec()
    mod._record_fit(rec, PupilFit(101.0, 202.0, 30.0, 20.0, 45.0), False, 1.5)
    r.check([p[0] for p in rec.puts] == all_streams,
            f"a fit writes the five ellipse streams plus the blink flag "
            f"({[p[0] for p in rec.puts]})")
    r.check([p[1] for p in rec.puts] == [101.0, 202.0, 30.0, 20.0, 45.0, 0.0],
            f"…with the ellipse in them, and 0.0 = not flagged "
            f"({[p[1] for p in rec.puts]})")
    r.check(all(p[2] == 1.5 for p in rec.puts),
            "…all stamped at the frame's own time, not the write's")

    rec1b = FakeRec()
    mod._record_fit(rec1b, PupilFit(0.0, 0.0, 10.0, 10.0, 0.0), True, 1.6)
    r.check(rec1b.puts[-1][:2] == (mod.BLINK_STREAM, 1.0),
            f"a flagged frame records 1.0, not just True ({rec1b.puts[-1]})")

    rec2 = FakeRec()
    mod._record_fit(rec2, None, False, 2.5)
    r.check([p[0] for p in rec2.puts] == all_streams,
            "a LOST frame writes the same six streams")
    r.check(all(math.isnan(p[1]) for p in rec2.puts),
            f"…all NaN including the blink flag — a blink cannot be judged "
            f"without a radius ({[p[1] for p in rec2.puts]})")

    # ── 7. the settings behind the number are recorded ───────────────────────
    panel.tracking._chk_track.setChecked(True)
    panel.tracking._spn_thr.setValue(57)
    panel.set_pins([(11.0, 22.0, 3.0), (44.0, 55.0, 6.0)])
    md = mod.metadata()
    r.check(md.get("pupil_track_threshold") == 57,
            f"the threshold is in the metadata ({md.get('pupil_track_threshold')})")
    r.check(md.get("pupil_tracker") == "eyeloop",
            f"…and which tracker produced it ({md.get('pupil_tracker')!r})")
    panel.tracking._chk_smooth.setChecked(True)
    panel.tracking._spn_smooth_win.setValue(9)
    md = mod.metadata()
    r.check(md.get("pupil_smooth") is True and md.get("pupil_smooth_window") == 9,
            f"stabilization travels with the trace, like threshold does "
            f"({md.get('pupil_smooth')}, {md.get('pupil_smooth_window')})")
    panel.tracking._chk_smooth.setChecked(False)
    r.check(md.get("pupil_cr_pins") == [11.0, 22.0, 3.0, 44.0, 55.0, 6.0],
            f"…and the pins, flattened for HDF5 ({md.get('pupil_cr_pins')})")
    r.check(mod.final_metadata() == {},
            "a session-less adapter invents no frame counts")

    # ── 8. the preview draws the fit, and clears it ──────────────────────────
    r.check(npoints(mod._fit_curve) == 0, "nothing tracked yet: no outline")
    mod._draw_fit(PupilFit(160.0, 120.0, 40.0, 30.0, 0.0))
    r.check(npoints(mod._fit_curve) > 8,
            f"a fit is drawn ({npoints(mod._fit_curve)} points)")
    xs, ys = mod._fit_curve.getData()
    r.check(abs(0.5 * (xs.min() + xs.max()) - 160.0) < 1.0
            and abs(0.5 * (xs.max() - xs.min()) - 40.0) < 1.0
            and abs(0.5 * (ys.max() - ys.min()) - 30.0) < 1.0,
            f"…as the ellipse it was given (centre {0.5*(xs.min()+xs.max()):.0f}, "
            f"semi-axes {0.5*(xs.max()-xs.min()):.0f}/"
            f"{0.5*(ys.max()-ys.min()):.0f})")
    mod._draw_fit(PupilFit(160.0, 120.0, 40.0, 30.0, 90.0))
    xs, ys = mod._fit_curve.getData()
    r.check(abs(0.5 * (xs.max() - xs.min()) - 30.0) < 1.0
            and abs(0.5 * (ys.max() - ys.min()) - 40.0) < 1.0,
            f"control: turning it 90° swaps the axes "
            f"({0.5*(xs.max()-xs.min()):.0f}/{0.5*(ys.max()-ys.min()):.0f})")
    # The display half of EyeLoop's stale-fit trap.
    mod._draw_fit(None)
    r.check(npoints(mod._fit_curve) == 0,
            "a frame with no fit clears the outline rather than leaving a stale one")

    # ── 8b. blink runs are shaded on the radius plot ──────────────────────────
    blink = [False, False, True, True, True, False, False, True, False]
    mod._trace = [(0.0, b) for b in blink]
    mod._update_blink_overlay()
    r.check(sum(reg.isVisible() for reg in mod._blink_regions) == 2,
            f"two separate runs of True become two shaded regions "
            f"({sum(reg.isVisible() for reg in mod._blink_regions)})")
    spans = sorted(reg.getRegion() for reg in mod._blink_regions if reg.isVisible())
    r.check(abs(spans[0][0] - 1.5) < 1e-9 and abs(spans[0][1] - 4.5) < 1e-9,
            f"the first run (indices 2-4) spans (1.5, 4.5) ({spans[0]})")
    r.check(abs(spans[1][0] - 6.5) < 1e-9 and abs(spans[1][1] - 7.5) < 1e-9,
            f"the second, one-frame run (index 7) spans (6.5, 7.5) ({spans[1]})")

    pool_after_two = len(mod._blink_regions)
    mod._trace = [(0.0, False) for _ in mod._trace]      # the blink passes
    mod._update_blink_overlay()
    r.check(all(not reg.isVisible() for reg in mod._blink_regions),
            "no runs left: every region is hidden")
    r.check(len(mod._blink_regions) == pool_after_two,
            "…but the pool is kept, not torn down and rebuilt next time")

    mod._trace = [(0.0, True)] * 5
    mod._update_blink_overlay()
    r.check(len(mod._blink_regions) == pool_after_two,
            "one run reuses a pooled region rather than growing the pool")
    r.check(sum(reg.isVisible() for reg in mod._blink_regions) == 1,
            "…and exactly one of them is shown")

    # ── 9. pins go on and come off, on the preview ───────────────────────────
    win._btn_run.setChecked(True)
    pump(app, 1.0)
    for _ in range(4):
        win._display_tick()
        pump(app, 0.05)
    r.check(mod._img.image is not None, "frames still reach the preview")
    r.check(mod._last_frame is not None, "…and the newest one is kept for pinning")

    mod._gv.resize(400, 300)
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.05)

    class _Ev:
        def __init__(self, pt): self._p = pt
        def scenePos(self): return self._p

    panel.clear_pins()
    rect = mod._vb.sceneBoundingRect()
    at = rect.center()
    at_view = mod._vb.mapSceneToView(at)

    # CONTROL first: with the tool off, a click on the preview pins nothing.
    mod._on_click(_Ev(at))
    r.check(panel.settings.cr_pins == [],
            f"control: with pin mode off, a click pins nothing "
            f"({panel.settings.cr_pins})")

    mod._btn_pin.setChecked(True)
    r.check("pin or unpin" in mod._lbl_limit.text(),
            f"arming says what to do next ({mod._lbl_limit.text()!r})")
    mod._on_click(_Ev(at))
    pins = panel.settings.cr_pins
    r.check(len(pins) == 1, f"a click pins one reflection ({pins})")
    r.check(abs(pins[0][0] - at_view.x()) < 1 and abs(pins[0][1] - at_view.y()) < 1,
            "…where it was clicked, in frame pixels")
    r.check(pins[0][2] > 0, f"…with an extent, so it can be seen and hit ({pins[0][2]})")
    r.check(npoints(mod._pin_curve) > 8,
            f"…and it is drawn ({npoints(mod._pin_curve)} points)")

    mod._on_click(_Ev(at))              # the same place again
    r.check(panel.settings.cr_pins == [],
            f"clicking a pinned reflection unpins it ({panel.settings.cr_pins})")
    r.check(npoints(mod._pin_curve) == 0, "…and it stops being drawn")

    # Exclusive, or the next click means two things at once.
    mod._btn_pin.setChecked(True)
    mod._btn_limit.setChecked(True)
    r.check(not mod._btn_pin.isChecked(),
            "arming the region tool disarms pinning")
    mod._btn_pin.setChecked(True)
    r.check(not mod._btn_limit.isChecked(), "…and the other way round")
    mod._btn_pin.setChecked(False)

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)
    return r.finish()


# ═══ limit (was test_pupil_limit.py) ════════════════════════════════════

class _DragEv:
    """pyqtgraph's MouseDragEvent, delivered to the ViewBox so its own
    armed/unarmed gate is exercised, not just `_on_limit_drag`."""

    def __init__(self, start, pos, finish: bool):
        self._start = start
        self._pos = pos
        self._finish = finish

    def button(self):
        return Qt.MouseButton.LeftButton

    def buttonDownScenePos(self):
        return self._start

    def scenePos(self):
        return self._pos

    def isFinish(self) -> bool:
        return self._finish

    def accept(self) -> None:
        pass


def _part_limit() -> int:
    r = Report("pupil-limit")

    # ── 1. the settings model ────────────────────────────────────────────────
    r.check(PupilSettings().search_limit() is None,
            "shipped default is no region")
    s = PupilSettings(limit_x0=530.0, limit_y0=190.0, limit_x1=750.0, limit_y1=410.0)
    r.check(s.search_limit() == (530.0, 190.0, 750.0, 410.0),
            "a set region reads back as one tuple")
    r.check(PupilSettings(limit_x0=530.0, limit_y0=190.0, limit_x1=530.0,
                          limit_y1=410.0).search_limit() is None,
            "control: a collapsed box (X1<=X0) is 'no region' whatever Y says")

    # ══ the app half ═════════════════════════════════════════════════════════
    app = qt_app()
    isolate_user_state()
    sys.argv = ["main.py", "--mock"]

    win = make_window({"pupil_cam"})
    mod = win._modules[0]
    panel = mod.panel

    # ── 8. the rectangle is drawn whenever it is in force ────────────────────
    r.check(npoints(mod._limit_curve) == 0, "no limit set: nothing is drawn")
    panel.set_limit(530.0, 190.0, 750.0, 410.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(npoints(mod._limit_curve) == 5,
            f"setting a limit outlines it on the preview as a closed rectangle "
            f"({npoints(mod._limit_curve)} points)")
    ys = mod._limit_curve.getData()[1]
    r.check(abs(xs.min() - 530.0) < 1.0 and abs(xs.max() - 750.0) < 1.0
            and abs(ys.min() - 190.0) < 1.0 and abs(ys.max() - 410.0) < 1.0,
            f"…at the box it was given ({xs.min():.0f},{ys.min():.0f})-"
            f"({xs.max():.0f},{ys.max():.0f})")
    r.check(panel.settings.limit_x1 == 750.0, "the panel carries X1")
    md = mod.metadata()
    r.check(md.get("pupil_limit_x0") == 530.0 and md.get("pupil_limit_x1") == 750.0,
            f"the session metadata records the limit ({md.get('pupil_limit_x0')}, "
            f"{md.get('pupil_limit_y0')}, {md.get('pupil_limit_x1')}, "
            f"{md.get('pupil_limit_y1')})")

    # ── 9. one settings change per placement, not four ───────────────────────
    seen: list = []
    panel.settings_changed.connect(lambda s: seen.append(s))
    panel.set_limit(400.0, 150.0, 600.0, 350.0)
    r.check(len(seen) == 1, f"a placement writes back as one settings change "
                            f"({len(seen)})")
    seen.clear()
    # The region is a check box now: off removes it, on brings it back.
    panel.tracking._chk_region.setChecked(False)
    r.check(len(seen) == 1 and panel.settings.search_limit() is None,
            "unticking Eye region removes it, as one change")
    panel.tracking._chk_region.setChecked(True)
    r.check(len(seen) == 2 and panel.settings.search_limit()
            == (400.0, 150.0, 600.0, 350.0),
            "ticking it again restores the same box")

    # ── 10. clearing, from either place ──────────────────────────────────────
    r.check(panel.tracking._chk_region.isChecked(), "Eye region is ticked while one is set")
    r.check(mod._btn_limit_off.isEnabled(), "…on the preview bar too")
    mod._btn_limit_off.click()
    pump(app, 0.05)
    r.check(panel.settings.search_limit() is None, "…and clearing removes it")
    r.check(npoints(mod._limit_curve) == 0, "…and un-draws the rectangle")
    r.check(not panel.tracking._chk_region.isChecked() and
            not mod._btn_limit_off.isEnabled(),
            "control: with no region there is nothing to clear")

    # ── 11. the Eye region check box draws and un-draws the rectangle ────────
    panel.set_limit(150.0, 200.0, 500.0, 320.0)
    pump(app, 0.05)
    panel.tracking._chk_region.setChecked(False)
    pump(app, 0.05)
    r.check(npoints(mod._limit_curve) == 0, "unticked: the rectangle goes")
    panel.tracking._chk_region.setChecked(True)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(xs is not None and abs(xs.min() - 150.0) < 1.0
            and abs(xs.max() - 500.0) < 1.0,
            f"ticked: it comes back where it was ({xs.min():.0f}-{xs.max():.0f})")
    r.check("150" in mod._lbl_limit.text() and "500" in mod._lbl_limit.text(),
            f"…and the preview bar reads it back ({mod._lbl_limit.text()!r})")
    panel.clear_limit()
    pump(app, 0.05)

    # ── 12. placing it on the preview: press-drag, release commits + disarms ─
    win._btn_run.setChecked(True)
    pump(app, 1.0)                       # frames flow

    # Never shown, the view has no size or range: the drag would span 0 px.
    mod._gv.resize(400, 300)
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.05)

    rect = mod._vb.sceneBoundingRect()
    start = QPointF(rect.center().x() - 0.2 * rect.width(),
                    rect.center().y() - 0.2 * rect.height())
    end = QPointF(rect.center().x() + 0.2 * rect.width(),
                  rect.center().y() + 0.2 * rect.height())
    s_view = mod._vb.mapSceneToView(start)
    e_view = mod._vb.mapSceneToView(end)
    want_x0, want_x1 = sorted((s_view.x(), e_view.x()))
    want_y0, want_y1 = sorted((s_view.y(), e_view.y()))
    if not r.check(want_x1 - want_x0 > 5.0 and want_y1 - want_y0 > 5.0,
                   f"fixture: the drag spans ({want_x1-want_x0:.1f}, "
                   f"{want_y1-want_y0:.1f}) px in the frame — without this the "
                   f"placement checks are vacuous"):
        return r.finish()

    r.check(not mod._btn_limit.isChecked(), "the region tool starts off")
    mod._btn_limit.setChecked(True)
    pump(app, 0.05)
    r.check("drag from one corner" in mod._lbl_limit.text(),
            f"arming says what to do next ({mod._lbl_limit.text()!r})")

    mod._vb.mouseDragEvent(_DragEv(start, start, finish=False))
    r.check(npoints(mod._limit_ghost) == 5,
            f"the rectangle follows the cursor before it is committed "
            f"({npoints(mod._limit_ghost)} points)")
    r.check(panel.settings.search_limit() is None,
            "…and commits nothing yet — mid-drag is not a region")

    mod._vb.mouseDragEvent(_DragEv(start, end, finish=True))
    pump(app, 0.05)
    s1 = panel.settings
    r.check(abs(s1.limit_x0 - want_x0) < 1 and abs(s1.limit_y0 - want_y0) < 1
            and abs(s1.limit_x1 - want_x1) < 1 and abs(s1.limit_y1 - want_y1) < 1,
            f"release sets the box ({s1.limit_x0:.0f}, {s1.limit_y0:.0f})-"
            f"({s1.limit_x1:.0f}, {s1.limit_y1:.0f}); wanted "
            f"({want_x0:.0f}, {want_y0:.0f})-({want_x1:.0f}, {want_y1:.0f})")
    r.check(not mod._btn_limit.isChecked(),
            "…and disarms itself — no mode left switched on")
    r.check(npoints(mod._limit_ghost) == 0, "…and the rubber band is cleared")

    # CONTROL: the flag `mouseDragEvent` gates on; a stub cannot replay the
    # real pan it falls through to.
    r.check(mod._vb._draw is False,
            "control: disarmed, the ViewBox's own draw-mode flag is off — the "
            "next real drag falls through to pyqtgraph's own pan")

    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    return r.finish()


# ═══ video (was test_pupil_video.py) ════════════════════════════════════

# ── building an AVI, so the test owns its own input ──────────────────────────

def _chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" * (len(payload) & 1))


def write_avi(path: Path, frames: list[bytes], w: int, h: int,
              fourcc: bytes, bits: int, us: int = 50000) -> Path:
    """A minimal but real RIFF AVI: hdrl(avih, strl(strh, strf)) + movi."""
    avih = struct.pack("<10I", us, 0, 0, 0, len(frames), 0, 1, 0, w, h) + b"\0" * 16
    strh = (b"vids" + fourcc + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, us and 1,
                                           0, len(frames), 0, 0, 0)
            + b"\0" * 8)
    strf = struct.pack("<IiiHH4sIiiII", 40, w, h, 1, bits, fourcc,
                       w * h * bits // 8, 0, 0, 0, 0)
    strl = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh) + _chunk(b"strf", strf))
    hdrl = _chunk(b"LIST", b"hdrl" + _chunk(b"avih", avih) + strl)
    movi = _chunk(b"LIST", b"movi" + b"".join(_chunk(b"00db", f) for f in frames))
    body = b"AVI " + hdrl + movi
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def i420(y: np.ndarray) -> bytes:
    """Y plane plus mid-grey 4:2:0 chroma, which the reader must ignore."""
    h, w = y.shape
    uv = np.full((h // 2, w // 2), 128, np.uint8)
    return y.tobytes() + uv.tobytes() + uv.tobytes()


def dib_rows(img: np.ndarray, px: int) -> bytes:
    """`img` as DIB scanlines, each padded to a 4-byte boundary (packed rows
    are the one case a stride-ignoring reader gets right)."""
    h, w = img.shape[:2]
    stride = ((w * px + 3) // 4) * 4
    row = img.reshape(h, w * px) if img.ndim == 3 else img
    pad = b"\0" * (stride - w * px)
    return b"".join(bytes(row[y]) + pad for y in range(h))


def video_eye_frame(h: int, w: int, cx: int, cy: int, r: int) -> np.ndarray:
    """A dark disc on a bright field, with a glint — the mock's shape."""
    Y, X = np.ogrid[:h, :w]
    f = np.full((h, w), 190, np.uint8)
    f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = 20
    f[(X - cx - r // 3) ** 2 + (Y - cy) ** 2 < max(2, r // 6) ** 2] = 250
    return f


def _part_video() -> int:  # noqa: PLR0915 — one linear scenario, split only by section
    r = Report("pupil-video")
    tmp = Path(tempfile.mkdtemp(prefix="pupil_video_"))
    H, W = 64, 96

    from acqApp.devices.pupil_cam.avi import AviReader

    # ── 1. the reader, per layout ────────────────────────────────────────────
    y0, y1 = video_eye_frame(H, W, 40, 30, 12), video_eye_frame(H, W, 52, 34, 15)

    p = write_avi(tmp / "planar.avi", [i420(y0), i420(y1)], W, H, b"IYUV", 24)
    rd = AviReader(p)
    r.check((rd.width, rd.height) == (W, H),
            f"IYUV: geometry read from strf ({rd.width}x{rd.height})")
    r.check(len(rd) == 2, f"IYUV: both frames indexed (got {len(rd)})")
    r.check(np.array_equal(rd.luma(0), y0) and np.array_equal(rd.luma(1), y1),
            "IYUV: the Y plane comes back exactly, chroma ignored")
    r.check(abs(rd.hz - 20.0) < 1e-6, f"IYUV: hz from avih ({rd.hz:.2f})")
    # CONTROL: or returning one buffer twice would pass.
    r.check(not np.array_equal(y0, y1),
            "control: the two source frames are not identical")

    p = write_avi(tmp / "gray.avi", [y0.tobytes(), y1.tobytes()], W, H,
                  b"Y800", 8)
    rd = AviReader(p)
    r.check(np.array_equal(rd.luma(0), y0),
            "Y800: 8-bit luma passes through unflipped")

    # BI_RGB is bottom-up, so a correct reader must flip it back.
    bgr = np.repeat(y0[:, :, None], 3, axis=2)[::-1]
    p = write_avi(tmp / "dib.avi", [bgr.tobytes()], W, H, b"\0\0\0\0", 24)
    rd = AviReader(p)
    got = rd.luma(0)
    r.check(got.shape == (H, W) and int(np.abs(got.astype(int)
                                               - y0.astype(int)).max()) <= 1,
            "BI_RGB 24-bit: flipped upright and converted to luma")
    # CONTROL: without the flip the top row would be the source's bottom row.
    r.check(not np.array_equal(got, bgr[:, :, 0]),
            "control: the BI_RGB flip actually happened")

    # Rows NOT 4-aligned: a reader assuming width*px shears each row further.
    # W=96 above is aligned, which is how this went unnoticed.
    W2 = 97
    r.check((W2 * 3) % 4 != 0 and (W2 * 1) % 4 != 0,
            f"control: {W2}px rows are unaligned at both 8- and 24-bit, so "
            f"these two cases can actually fail")
    y2 = video_eye_frame(H, W2, 44, 30, 12)
    p = write_avi(tmp / "pad8.avi", [dib_rows(y2[::-1], 1)], W2, H,
                  b"\0\0\0\0", 8)
    r.check(np.array_equal(AviReader(p).luma(0), y2),
            "BI_RGB 8-bit: padded scanlines read back unsheared")
    bgr2 = np.repeat(y2[:, :, None], 3, axis=2)[::-1]
    p = write_avi(tmp / "pad24.avi", [dib_rows(bgr2, 3)], W2, H,
                  b"\0\0\0\0", 24)
    got2 = AviReader(p).luma(0)
    r.check(got2.shape == (H, W2) and int(np.abs(got2.astype(int)
                                                 - y2.astype(int)).max()) <= 1,
            "BI_RGB 24-bit: ditto, and still flipped upright")

    # ── 2. a compressed clip must say so, not guess ──────────────────────────
    p = write_avi(tmp / "mjpg.avi", [b"\xff\xd8" + b"\0" * (W * H)], W, H,
                  b"MJPG", 24)
    try:
        AviReader(p)
        r.check(False, "MJPG: raises rather than returning garbage")
    except ValueError as e:
        r.check("MJPG" in str(e) and "decoder" in str(e).lower(),
                f"MJPG: refused, naming the codec and the missing decoder")

    # ── 3. the worker ────────────────────────────────────────────────────────
    app = qt_app()                     # a real QThread needs a real app
    from acqApp.devices.pupil_cam.video import VideoFileCameraWorker

    clip = write_avi(tmp / "clip.avi", [i420(video_eye_frame(H, W, 30 + 4 * i, 30, 12))
                                        for i in range(5)], W, H, b"IYUV", 24)
    wk = VideoFileCameraWorker(clip, rate_hz=60.0)
    r.check(wk.frame_shape == (H, W), f"worker: frame_shape {wk.frame_shape}")
    r.check(wk.n_frames == 5, f"worker: n_frames {wk.n_frames}")
    seen: list[np.ndarray] = []
    wk.set_sink(seen.append)           # the sink sees every frame
    wk.start()
    pump(app, 0.6)
    wk.stop()
    r.check(len(seen) > 5,
            f"worker: loops past the end of the clip ({len(seen)} frames of 5)")
    r.check(all(f.shape == (H, W) and f.dtype == np.uint8 for f in seen),
            "worker: every published frame is (H, W) uint8")
    r.check(np.array_equal(seen[0], seen[5]) if len(seen) > 5 else False,
            "worker: frame 5 is frame 0 again — the loop wraps in order")
    r.check(not np.shares_memory(seen[0], seen[1]),
            "worker: frames are copies, so the sink can keep them")

    wk2 = VideoFileCameraWorker(clip, rate_hz=20.0, loop=False)
    wk2.start()
    pump(app, 0.8)
    r.check(wk2.isFinished() or not wk2.isRunning(),
            "worker: loop=False stops itself at the end of the clip")
    wk2.stop()

    # ── 4. the adapter's choice, and what lands in the file ──────────────────
    isolate_user_state()               # the panel persists on every edit
    from acqApp.adapters.pupil_cam import PupilCamModule
    from acqApp.devices.pupil_cam.acquisition import MockPupilCameraWorker

    class FakeWin:
        """Only what build_session touches."""
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

    def built(video: str, emulate: bool):
        win = FakeWin()
        m = PupilCamModule(win)
        m.panel = type("P", (), {
            "settings": PupilSettings(video_path=video, rate_hz=30.0),
            "set_measured_rate": lambda self, *a, **kw: None,
            "set_led": lambda self, *a, **kw: None,
        })()
        m.build_session(emulate)
        return m, win

    m, _ = built(str(clip), True)
    r.check(isinstance(m.worker, VideoFileCameraWorker),
            "adapter: video_path wins over the mock in emulate mode")
    m.stop()

    m, _ = built("", True)
    r.check(isinstance(m.worker, MockPupilCameraWorker),
            "control: no video_path still gives the mock")
    m.stop()

    m, win = built(str(tmp / "nope.avi"), True)
    r.check(isinstance(m.worker, MockPupilCameraWorker),
            "adapter: a missing clip falls back instead of killing the session")
    r.check(any("video" in x for x in win.messages),
            f"adapter: and says so in the status bar ({win.messages})")
    m.stop()

    m, _ = built(str(clip), True)
    md = m.metadata()
    r.check(md.get("pupil_video") == str(clip),
            "metadata: the clip is recorded, so replayed data cannot pass as rig data")
    r.check(md.get("pupil_rate_hz") == 30.0 and "pupil_edge_select" not in md,
            f"metadata: camera settings are filed and the archived tracking "
            f"parameters are not ({sorted(md)})")
    m.stop()
    # CONTROL: a live session files an empty string, not a stale path.
    m, _ = built("", True)
    r.check(m.metadata().get("pupil_video") == "",
            "control: a camera session files pupil_video as empty")
    m.stop()

    return r.finish()


# ═══ offline review ═════════════════════════════════════════════════════

def _part_review() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-review")
    tmp = Path(tempfile.mkdtemp(prefix="pupil_review_"))
    H, W, N = 120, 160, 12
    radii = [14 + (i % 3) for i in range(N)]
    frames = [video_eye_frame(H, W, 80, 60, rad) for rad in radii]
    frames[7] = np.full((H, W), 190, np.uint8)       # nothing to fit
    clip = write_avi(tmp / "eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)

    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths

    class DiscTracking:
        """Stands in for EyeLoop (needs cv2 and a clone): the dark pixels
        under `track_threshold` inside the region are the pupil."""

        error = None
        available = True

        def track(self, frame, st):
            x0, y0, x1, y1 = st.crop_box(frame.shape)
            ys, xs = np.nonzero(frame[y0:y1, x0:x1] < st.track_threshold)
            if xs.size < 20:
                return None
            rad = float(np.sqrt(xs.size / np.pi))
            return PupilFit(xs.mean() + x0, ys.mean() + y0, rad, rad, 0.0)

    review_mod.PupilTracking = DiscTracking

    st = PupilSettings(limit_x0=40, limit_y0=30, limit_x1=120, limit_y1=90,
                       cr_remove=False)
    rev = PupilReview(clip, st)
    seen: list[int] = []
    done = rev.track_all(lambda i, n: seen.append(i))
    r.check(done and seen[-1] == N, f"tracked every frame ({seen[-1:]})")
    r.check(rev.tracked, "marked tracked")
    good = [i for i in range(N) if i != 7]
    r.check(all(not np.isnan(rev.auto[i, 0]) for i in good),
            "every real frame got a fit")
    r.check(np.isnan(rev.auto[7, 0]), "the blank frame has no fit")
    r.check(all(abs(rev.radius()[i] - radii[i]) < 4 for i in good),
            f"radius follows the disc ({np.round(rev.radius(), 1)})")
    r.check(7 in rev.suspects(), "a no-fit frame is a suspect")

    # ── hand edits ──
    before = rev.table().copy()
    fix = PupilFit(82.0, 58.0, 25.0, 25.0, 0.0)
    rev.set_manual(3, fix)
    r.check(rev.is_edited(3) and not rev.is_edited(4), "only frame 3 is edited")
    r.check(abs(rev.radius()[3] - 25.0) < 1e-9, "the reported radius is the edit")
    r.check(np.array_equal(rev.auto[3], before[3]),
            "the auto fit under the edit is untouched")
    r.check(np.array_equal(rev.table()[4:], before[4:], equal_nan=True),
            "other frames unchanged")
    rev.set_manual(7, fix)
    r.check(rev.fit_at(7) == fix, "a frame with no fit can be given one")
    r.check(7 not in rev.suspects(), "an edited frame stops being a suspect")
    # CONTROL: or a table that always returned manual would pass.
    rev.clear_manual(3)
    r.check(np.array_equal(rev.table()[3], before[3]),
            "control: clearing the edit restores the auto fit")
    rev.set_manual(3, fix)

    # ── re-track keeps edits; new parameters take effect ──
    rev.settings = PupilSettings(**{**vars(rev.settings), "track_threshold": 5})
    rev.track_all()
    r.check(rev.is_edited(3) and rev.is_edited(7),
            "re-tracking keeps hand edits")
    r.check(np.isnan(rev.auto[:, 0]).all(),
            "a threshold that selects nothing re-tracks to no fits")
    rev.settings = PupilSettings(**{**vars(rev.settings), "track_threshold": 45})
    rev.track_all()

    # ── sidecar ──
    js, npz = rev.save()
    r.check(js == sidecar_paths(clip)[0] and js.is_file() and npz.is_file(),
            "sidecar files sit beside the clip")
    back = PupilReview.load(clip)
    r.check(np.array_equal(back.table(), rev.table(), equal_nan=True),
            "the final table round-trips")
    r.check(np.array_equal(back.edited, rev.edited), "edited flags round-trip")
    r.check(back.settings.track_threshold == 45
            and back.settings.limit_x1 == 120, "parameters round-trip")
    z = np.load(npz)
    r.check(z["final"].shape == (N, 5) and z["radius"].shape == (N,),
            "npz carries the final table and radius")
    other = write_avi(tmp / "short.avi", [f.tobytes() for f in frames[:5]],
                      W, H, b"Y800", 8)
    for src, dst in zip(sidecar_paths(clip), sidecar_paths(other)):
        dst.write_bytes(src.read_bytes())
    r.check(not PupilReview.load(other).edited.any(),
            "a sidecar for a different frame count is ignored")

    # ── the dialog ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    clip2 = write_avi(tmp / "eye2.avi", [f.tobytes() for f in frames],
                      W, H, b"Y800", 8)
    live = [True]
    dlg = PupilReviewDialog(str(clip2), busy=lambda: live[0])
    r.check(dlg.review is not None and len(dlg.review) == N,
            "dialog opens the clip")
    r.check(dlg.review.settings.search_limit() is not None,
            "a clip with no region gets a default one")
    # Fit the frames: a default region is the central half, which holds the disc.
    r.check(dlg._btn_track.text() == "Apply to all frames", "idle button label")
    dlg._track_all()
    r.check(dlg._worker is None and "Live tracking" in dlg._prog.text(),
            "tracking refused while live tracking runs, and says why")
    live[0] = False
    dlg._track_all()
    for _ in range(100):
        pump(app, 0.1)
        if dlg._worker is None:
            break
    r.check(dlg._worker is None and dlg.review.tracked, "tracked in the worker")
    dlg.goto(5)
    fit = dlg.review.fit_at(5)
    r.check(fit is not None, "frame 5 has a fit to edit")
    # The handle must reproduce the fit it was built from.
    dlg._make_roi(PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    got = dlg._roi_fit()
    r.check(all(abs(a - b) < 1e-6 for a, b in
                zip((got.center_x, got.center_y, got.semi_major,
                     got.semi_minor, got.angle_deg), (70, 50, 20, 10, 30))),
            "ellipse handle round-trips centre, axes and angle")
    dlg.edit_frame(5, PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    r.check(dlg.review.is_edited(5) and abs(dlg.review.radius()[5] - 15) < 1e-9,
            "a dialog edit lands in the data")
    dlg._next_suspect()
    r.check(dlg._frame == 7, f"Next suspect jumps to the no-fit frame ({dlg._frame})")
    dlg._reset_current()
    dlg.goto(5)
    dlg._reset_current()
    r.check(not dlg.review.is_edited(5), "Reset returns the frame to auto")
    # ── playback ──
    dlg.goto(0)
    dlg._spn_rate.setValue(200.0)
    dlg.toggle_play()
    r.check(dlg.playing and dlg._btn_play.text() == "Pause" and dlg._roi is None,
            "Play starts the timer and drops the edit handle")
    for _ in range(60):
        pump(app, 0.05)
        if not dlg.playing:
            break
    r.check(not dlg.playing and dlg._frame == N - 1,
            f"playback runs to the last frame and stops ({dlg._frame})")
    r.check(dlg._btn_play.text() == "Play", "the button reads Play again")
    dlg.toggle_play()                       # from the end: starts over
    r.check(dlg._frame <= 2, f"Play at the end restarts from the top ({dlg._frame})")
    dlg.pause()
    dlg.goto(0)
    dlg._chk_loop.setChecked(True)
    dlg.toggle_play()
    wraps, prev = 0, dlg._frame
    for _ in range(40):
        pump(app, 0.05)
        wraps += dlg._frame < prev
        prev = dlg._frame
    r.check(dlg.playing and wraps >= 1, f"Loop wraps to the top and keeps going ({wraps})")
    dlg.edit_frame(2, PupilFit(70.0, 50.0, 20.0, 10.0, 30.0))
    r.check(not dlg.playing, "editing a frame stops playback")
    dlg.review.clear_manual(2)
    dlg._chk_loop.setChecked(False)

    # ── display levels (display only) ──
    dlg.goto(0, force=True)
    auto = tuple(dlg._img.levels)
    dlg._chk_auto.setChecked(False)
    dlg._hist.item.setLevels(60, 130)
    pump(app, 0.05)
    r.check(tuple(dlg._img.levels) == (60, 130),
            f"Auto off: the LUT bar's handles set the image levels ({tuple(dlg._img.levels)})")
    before = dlg.review.table().copy()
    dlg._chk_auto.setChecked(True)
    r.check(tuple(dlg._img.levels) == auto,
            "Auto on again re-derives the levels from the frame")
    r.check(np.array_equal(dlg.review.table(), before, equal_nan=True),
            "changing the display never touches the data")
    dlg.save()
    r.check(not dlg._dirty, "saving clears the unsaved flag")
    dlg._dirty = False
    dlg.close()
    return r.finish()


# ═══ the standalone launcher ═══════════════════════════════════════════

def _part_launcher() -> int:
    r = Report("pupil-launcher")
    import ast
    import subprocess
    from _harness import APP_DIR

    launcher = APP_DIR / "run_pupil_review.py"
    reqs = (APP_DIR / "requirements-pupil.txt").read_text(encoding="utf-8")
    want = {ln.split("#")[0].strip().lower() for ln in reqs.splitlines()}
    want.discard("")

    # The tool's third-party imports must all be installable from its own
    # requirements, or a fresh machine dies at the first import.
    stdlib = set(sys.stdlib_module_names)
    third, seen, todo = set(), set(), ["devices/pupil_cam/review_app.py"]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for n in ast.walk(ast.parse((APP_DIR / rel).read_text(encoding="utf-8"))):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                    else [n.module] if isinstance(n, ast.ImportFrom) and n.module
                    else [])
            for m in mods:
                top = m.split(".")[0]
                if top == "acqApp":
                    parts = m.split(".")[1:]
                    for cand in (parts, parts[:-1]):
                        if cand and APP_DIR.joinpath(*cand).with_suffix(".py").is_file():
                            todo.append("/".join(cand) + ".py")
                elif top not in stdlib:
                    third.add(top)
    pip_name = {"cv2": "opencv-python", "yaml": "pyyaml"}
    need = {pip_name.get(m, m).lower() for m in third - {"eyeloop"}}  # the clone, not pip
    r.check(need <= want, f"requirements-pupil.txt covers the tool's imports "
                          f"(missing {sorted(need - want)})")
    # EyeLoop is imported lazily from a clone, but its own cv2/yaml need listing.
    r.check({"opencv-python", "pyyaml"} <= want, "EyeLoop's cv2 and yaml are listed")
    r.check("nidaqmx" not in want and "pypylon" not in want,
            "no hardware package in the tool's requirements")

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, str(launcher), "--check"],
                         capture_output=True, text=True, timeout=180, env=env)
    r.check(out.returncode == 0,
            f"the launcher builds the review window headless ({out.stderr[-300:]})")
    return r.finish()


# ═══ Auto (suggested parameters) and the review's safety fixes ══════════

def face_frame(h=300, w=420, cx=210, cy=150, r=30, pupil=20, iris=26,
               glint=True, seed=0) -> np.ndarray:
    """A dim eye like the rig's: pupil a few levels under the iris, an eye
    opening, bright fur around, sensor noise and a reflection at the rim."""
    rng = np.random.default_rng(seed)
    Y, X = np.ogrid[:h, :w]
    f = np.full((h, w), 120.0)
    f[((X - cx) / (2.6 * r)) ** 2 + ((Y - cy) / (1.6 * r)) ** 2 < 1] = iris
    f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = pupil
    if glint:
        f[(X - cx + int(r * 0.8)) ** 2 + (Y - cy - int(r * 0.8)) ** 2 < 16] = 235
    f += rng.normal(0, 1.0, f.shape)
    return np.clip(f, 0, 255).astype(np.uint8)


def _part_autotune() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-autotune")
    from acqApp.devices.pupil_cam.autotune import autotune

    frames = [face_frame(cx=200 + 3 * i, seed=i) for i in range(8)]
    a = autotune(frames, (100, 60, 320, 240))
    r.check(a is not None, "a dim synthetic eye gets a suggestion")
    if a is not None:
        r.check(20 <= a.threshold < 26,
                f"threshold lands between pupil (20) and iris (26): {a.threshold}")
        r.check(a.blur in (1, 3, 5), f"blur is a sane odd kernel ({a.blur})")
        r.check(a.cr_remove and a.cr_threshold is not None
                and 26 < a.cr_threshold < 235,
                f"a rim reflection turns removal on, threshold between iris and "
                f"glint ({a.cr_threshold})")
        r.check(a.region is None, "a given region is left alone")
        st = a.apply(PupilSettings(limit_x0=100, limit_y0=60, limit_x1=320,
                                   limit_y1=240, track_threshold=99))
        r.check(st.track_threshold == a.threshold and st.limit_x1 == 320,
                "apply() sets the knobs and keeps the drawn region")

    nog = autotune([face_frame(glint=False, seed=i) for i in range(6)],
                   (100, 60, 320, 240))
    r.check(nog is not None and not nog.cr_remove,
            "control: no reflection leaves removal off")

    big = [np.pad(face_frame(seed=i), ((300, 300), (500, 500)),
                  constant_values=150) for i in range(6)]
    b = autotune(big, None)
    r.check(b is not None and b.region is not None,
            "no region: one is estimated on a larger frame")
    if b is not None and b.region is not None:
        x0, y0, x1, y1 = b.region
        r.check(x0 < 710 < x1 and y0 < 450 < y1,
                f"the estimated region holds the pupil ({b.region})")
        r.check((x1 - x0) < 600, f"and is a box around the eye, not the frame "
                                 f"({x1 - x0} px wide)")
        r.check(20 <= b.threshold < 26, f"threshold still right ({b.threshold})")
    r.check(autotune([np.full((200, 200), 128, np.uint8)] * 4, None) is None,
            "a blank frame gives None, not a guess")
    r.check(autotune([], None) is None, "no frames gives None")

    # Real rig clip, when this machine has it.
    real = Path(r"C:\Users\pinkh008\Downloads\VF215.4LL_20260924_FOV1_T16_Pupil"
                r"\VF215.4LL_20260924_FOV1_T16_Pupil.avi")
    if real.is_file():
        from acqApp.devices.pupil_cam.avi import AviReader
        rd = AviReader(real)
        got = autotune([rd.luma(i) for i in range(0, len(rd), 8)], None)
        r.check(got is not None and got.threshold == 21 and got.blur == 3,
                f"real clip: matches the hand-swept best (21, blur 3): "
                f"{got and (got.threshold, got.blur)}")
    else:
        r.info("real clip absent: skipped")

    # ── the live tab: panel Auto -> adapter gathers frames -> one change ──
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    changes: list = []
    m.panel.settings_changed.connect(changes.append)
    m._on_auto_requested()
    r.check(m._auto_frames is None and any("Live view" in x for x in win.messages),
            "Auto with no live feed says to start Live view")
    m.build_session(True)
    m.start()
    pump(app, 0.2)
    m._on_auto_requested()
    r.check(m._auto_frames == [] and not m.panel.tracking._btn_auto.isEnabled(),
            "Auto starts gathering and greys the button")
    for i in range(m._AUTO_FRAMES * m._AUTO_EVERY):
        m._gather_auto(face_frame(cx=200 + i % 5, seed=i))
    for _ in range(100):
        pump(app, 0.05)
        if m._auto_worker is None:
            break
    st = m.panel.settings
    r.check(20 <= st.track_threshold < 26 and st.search_limit() is not None,
            f"the suggestion lands in the panel (threshold {st.track_threshold}, "
            f"region {st.search_limit()})")
    r.check(len(changes) == 1, f"as ONE settings change ({len(changes)})")
    r.check(m.panel.tracking._btn_auto.isEnabled()
            and any("pupil Auto:" in x and "check" in x for x in win.messages),
            "the button comes back and the status says what was chosen")
    m.stop()
    return r.finish()


def _part_review_safety() -> int:  # noqa: PLR0915 — one linear scenario
    r = Report("pupil-review-safety")
    import json
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths

    class DiscTracking:
        error = None
        available = True

        def track(self, frame, st):
            x0, y0, x1, y1 = st.crop_box(frame.shape)
            ys, xs = np.nonzero(frame[y0:y1, x0:x1] < st.track_threshold)
            if xs.size < 20:
                return None
            rad = float(np.sqrt(xs.size / np.pi))
            return PupilFit(xs.mean() + x0, ys.mean() + y0, rad, rad, 0.0)

    review_mod.PupilTracking = DiscTracking
    tmp = Path(tempfile.mkdtemp(prefix="pupil_safety_"))
    H, W, N = 120, 160, 10
    frames = [video_eye_frame(H, W, 80, 60, 14).tobytes() for _ in range(N)]
    clip = write_avi(tmp / "a.avi", frames, W, H, b"Y800", 8)
    st = PupilSettings(limit_x0=40, limit_y0=30, limit_x1=120, limit_y1=90)

    rev = PupilReview(clip, st)
    rev.track_all()
    good = rev.auto.copy()
    r.check(not rev.stale and not PupilReview.fitting(),
            "fresh fit: not stale, not fitting")
    # A stopped re-track keeps the previous fits.
    rev.settings = PupilSettings(**{**vars(st), "track_threshold": 5})
    r.check(rev.stale, "changed parameters make the fits stale")
    calls: list = []
    r.check(rev.track_all(should_stop=lambda: calls.append(1) or len(calls) > 3)
            is False, "a stop returns False")
    r.check(np.array_equal(rev.auto, good), "and the previous fits are kept")

    class Broken(DiscTracking):
        def track(self, frame, st):
            raise ValueError("bad frame")

    review_mod.PupilTracking = Broken
    try:
        rev.track_all()
        r.check(False, "a failing tracker raises")
    except ValueError:
        r.check(np.array_equal(rev.auto, good) and not PupilReview.fitting(),
                "a failed re-track keeps the fits and clears the fitting flag")
    review_mod.PupilTracking = DiscTracking

    # Saved while stale: reopened, it still says stale.
    rev.save()
    back = PupilReview.load(clip)
    r.check(back.stale and back.tracked_with.track_threshold == st.track_threshold,
            "the settings the fits came from are saved; stale survives a reload")

    # Another clip under the same name, same frame count: not applied.
    rev.set_manual(2, PupilFit(80, 60, 30, 30, 0))
    rev.save()
    js, npz = sidecar_paths(clip)
    meta = json.loads(js.read_text())
    meta["video_bytes"] += 1
    js.write_text(json.dumps(meta))
    other = PupilReview.load(clip)
    r.check(not other.edited.any() and bool(other.sidecar_note)
            and "changed" in other.sidecar_note,
            f"a sidecar for a changed clip is not applied ({other.sidecar_note})")
    other.save()
    r.check((tmp / "a.avi.pupil.json.old").is_file()
            and (tmp / "a.avi.pupil.npz.old").is_file(),
            "and is kept as *.old when the new one is saved")

    # A truncated npz: ignored, not a crash.
    npz.write_bytes(npz.read_bytes()[:50])
    r.check(PupilReview.load(clip).sidecar_note is not None,
            "a truncated sidecar is ignored with a note, not raised")
    js.write_text("[1, 2]")
    r.check(PupilReview.load(clip).sidecar_note is not None,
            "a json that isn't an object is ignored too")
    r.check(not list(tmp.glob("*.tmp")), "no temp files left behind")

    # ── the dialog ──
    app = qt_app()
    isolate_user_state()
    from PyQt6.QtWidgets import QMessageBox
    from acqApp.devices.pupil_cam import review_dialog as rd
    for p in sidecar_paths(clip):
        p.unlink(missing_ok=True)
    answers: list = []
    real_q, real_warn = QMessageBox.question, QMessageBox.warning
    warned: list = []
    QMessageBox.question = staticmethod(lambda *a, **k: answers.pop(0))
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    real_save = PupilReview.save
    try:
        dlg = rd.PupilReviewDialog(str(clip))
        dlg.show()
        dlg.edit_frame(1, PupilFit(80, 60, 20, 20, 0))
        answers[:] = [QMessageBox.StandardButton.Cancel]
        dlg.reject()                                    # Esc
        r.check(dlg.isVisible() and not answers,
                "Esc asks about unsaved edits, and Cancel keeps the window")
        dlg._ctl._spn_thr.setValue(200)
        dlg._auto()
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(dlg._ctl._spn_thr.value() < 190 and "Auto:" in dlg._prog.text(),
                f"Auto in the review window sets the threshold "
                f"({dlg._ctl._spn_thr.value()}): {dlg._prog.text()[:70]}")
        r.check(dlg.review.settings.track_threshold == dlg._ctl._spn_thr.value(),
                "and the review's settings follow")
        dlg._worker = object()
        r.check(not dlg.open_video(str(clip)) and "Stop" in dlg._prog.text(),
                "opening a clip mid-job is refused")
        dlg._worker = None

        def ro(self):
            raise PermissionError("read-only folder")

        PupilReview.save = ro
        r.check(dlg.save() is False and bool(warned) and dlg._dirty,
                "a failed save warns and leaves the edits unsaved")
        answers[:] = [QMessageBox.StandardButton.Save]
        dlg.close()
        r.check(dlg.isVisible(), "a failed save on close keeps the window open")
        PupilReview.save = real_save
        answers[:] = [QMessageBox.StandardButton.Discard]
        dlg.close()
        r.check(not dlg.isVisible(), "Discard closes")
    finally:
        QMessageBox.question, QMessageBox.warning = real_q, real_warn
        PupilReview.save = real_save
    return r.finish()

def _part_mirror() -> int:  # noqa: PLR0915 — one linear scenario
    """The review window shows the live tab's own tracking controls, the
    region box and the numbers stay in step, suspects are drawn, and the
    sidecar survives case, half-writes and odd values."""
    r = Report("pupil-mirror")
    import json
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    from acqApp.devices.pupil_cam.review import PupilReview, sidecar_paths
    from acqApp.devices.pupil_cam.tracking_panel import TrackingControls

    class DiscTracking:
        error = None
        available = True

        def track(self, frame, st):
            x0, y0, x1, y1 = st.crop_box(frame.shape)
            ys, xs = np.nonzero(frame[y0:y1, x0:x1] < st.track_threshold)
            if xs.size < 20:
                return None
            rad = float(np.sqrt(xs.size / np.pi))
            return PupilFit(xs.mean() + x0, ys.mean() + y0, rad, rad, 0.0)

    review_mod.PupilTracking = DiscTracking
    tmp = Path(tempfile.mkdtemp(prefix="pupil_mirror_"))
    H, W, N = 120, 160, 20
    frames = [video_eye_frame(H, W, 80, 60, 14) for _ in range(N)]
    for i in (6, 7, 15):
        frames[i] = np.full((H, W), 190, np.uint8)      # nothing to fit
    clip = write_avi(tmp / "Eye.avi", [f.tobytes() for f in frames], W, H,
                     b"Y800", 8)

    # ── persistence ──
    rev = PupilReview(clip, PupilSettings(limit_x0=40, limit_y0=30,
                                          limit_x1=120, limit_y1=90))
    rev.track_all()
    rev.set_manual(3, PupilFit(80, 60, 20, 20, 0))
    rev.save()
    lower = tmp / "eye.avi"
    back = PupilReview.load(lower)
    r.check(back.sidecar_note is None and back.is_edited(3),
            f"the same clip opened in another case keeps its results "
            f"({back.sidecar_note})")
    js, npz = sidecar_paths(clip)
    meta = json.loads(js.read_text())
    meta["settings"]["track_threshold"] = 21.0
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.settings.track_threshold == 21
            and isinstance(back.settings.track_threshold, int),
            "a float written for an int setting is read as an int")
    meta["settings"]["cr_pins"] = [[1, "a", 3]]
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.sidecar_note is not None and not back.edited.any(),
            f"a sidecar with unusable values is set aside, not raised "
            f"({back.sidecar_note})")
    rev.save()
    meta = json.loads(js.read_text())
    meta["generation"] = "someone-else"
    js.write_text(json.dumps(meta))
    back = PupilReview.load(clip)
    r.check(back.stale and back.sidecar_note and "re-track" in back.sidecar_note,
            "a half-written pair (ids differ) loads as stale, with a note")
    for i in range(2):                      # two set-asides, two backups
        meta = json.loads(js.read_text())
        meta["video_bytes"] = 1
        js.write_text(json.dumps(meta))
        PupilReview.load(clip).save()
    olds = sorted(p.name for p in tmp.glob("Eye.avi.pupil.json.old*"))
    r.check(len(olds) == 2, f"an earlier backup is never overwritten ({olds})")

    # ── the window mirrors the live tab ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.panel import SettingsPanel
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    for p in list(sidecar_paths(clip)) + list(tmp.glob("*.old*")):
        p.unlink(missing_ok=True)
    panel = SettingsPanel(PupilSettings())
    dlg = PupilReviewDialog(str(clip))
    r.check(isinstance(panel.tracking, TrackingControls)
            and isinstance(dlg._ctl, TrackingControls),
            "both use the one TrackingControls widget")

    def titles(w):
        from PyQt6.QtWidgets import QGroupBox
        return [b._base_title if hasattr(b, "_base_title") else b.title()
                for b in w.findChildren(QGroupBox)]

    r.check(titles(panel.tracking) == titles(dlg._ctl),
            f"same sections in the same order ({titles(dlg._ctl)})")
    r.check(not dlg._ctl._chk_track.isVisibleTo(dlg)
            and dlg._ctl._spn_smooth_win.isEnabled(),
            "review hides 'Track the pupil'; smoothing is usable")

    # Region: box dragged -> numbers; numbers typed -> box.
    dlg._region.setPos((30, 20))
    dlg._region.setSize((90, 70))           # finish=True fires the drag slot
    r.check(dlg._ctl.region() == (30.0, 20.0, 120.0, 90.0),
            f"dragging the box updates X0..Y1 ({dlg._ctl.region()})")
    dlg._ctl.set_limit(35, 20, 120, 90)
    p = dlg._region.pos()
    r.check(abs(p.x() - 35) < 1e-6, f"a set region moves the box ({p.x()})")
    r.check(dlg.review.settings.limit_x0 == 35.0, "and the review follows")
    dlg._ctl._chk_region.setChecked(False)
    r.check(dlg._region is None and dlg.review.settings.search_limit() is None,
            "unticking Eye region removes the box")
    dlg._ctl._chk_region.setChecked(True)
    r.check(dlg._region is not None and abs(dlg._region.pos().x() - 35) < 1e-6,
            "ticking it restores the box")

    # Display row and the LUT's own Auto box are one setting, as live.
    dlg._chk_auto.setChecked(False)
    r.check(not dlg._chk_auto_contrast.isChecked(),
            "the box over the LUT and 'Auto contrast' move together")
    dlg._chk_lut.setChecked(False)
    r.check(not dlg._hist.isVisibleTo(dlg), "Show LUT hides the bar")
    dlg._chk_auto.setChecked(True)
    dlg._chk_lut.setChecked(True)

    # ── suspects are highlighted ──
    r.check(not dlg._suspects and dlg._lbl_sus.text() == "",
            "nothing is flagged before the first track")
    dlg._ctl.set_limit(40, 30, 120, 90)
    dlg._track_all()
    for _ in range(100):
        pump(app, 0.05)
        if dlg._worker is None:
            break
    r.check(dlg._suspects == [6, 7, 15], f"suspects found ({dlg._suspects})")
    shown = [b.getRegion() for b in dlg._sus_bands if b.isVisible()]
    r.check(shown == [(5.5, 7.5), (14.5, 15.5)],
            f"one red band per run of suspect frames ({shown})")
    r.check(len(dlg._sus_pts.data) == 3, "and a mark on each")
    r.check("3 to check" in dlg._lbl_sus.text(), dlg._lbl_sus.text())
    # ── Stabilize: auto fits only, edits kept as drawn ──
    rev2 = dlg.review
    rev2.auto[4:6, 2:4] += 6.0                  # a jumpy pair of fits
    rev2.set_manual(5, PupilFit(80, 60, 30, 30, 0))
    before = rev2.radius().copy()
    dlg._ctl._chk_smooth.setChecked(True)
    dlg._ctl._spn_smooth_win.setValue(5)
    after = rev2.radius()
    r.check(abs(after[4] - before[4]) > 0.5 and after[5] == 30.0,
            f"Stabilize smooths auto fits ({before[4]:.1f}->{after[4]:.1f}) and "
            f"leaves a hand edit alone ({after[5]})")
    r.check(not rev2.stale, "and needs no re-track")
    dlg._ctl._chk_smooth.setChecked(False)
    r.check(np.allclose(rev2.radius(), before, equal_nan=True), "off restores")
    rev2.clear_manual(5)
    rev2.auto[4:6, 2:4] -= 6.0

    # ── pins in review ──
    dlg._btn_pin_cr.setChecked(True)
    dlg.toggle_pin(90.0, 55.0)
    pins = dlg.review.settings.cr_pins
    r.check(len(pins) == 1 and dlg.review.stale
            and dlg._ctl._btn_pins_clear.isEnabled()
            and npoints(dlg._pin_curve) > 0,
            f"Pin reflection pins one, draws it, and asks for a re-track ({pins})")
    dlg.toggle_pin(90.0, 55.0)
    r.check(not dlg.review.settings.cr_pins and npoints(dlg._pin_curve) == 0,
            "clicking it again unpins")
    dlg._btn_pin_cr.setChecked(False)

    dlg.goto(10)
    dlg._jump_suspect(-1)
    r.check(dlg._frame == 7, f"◀ Suspect goes back ({dlg._frame})")
    dlg._next_suspect()
    r.check(dlg._frame == 15, f"Suspect ▶ goes forward ({dlg._frame})")
    dlg._place_new()
    shown = [b.getRegion() for b in dlg._sus_bands if b.isVisible()]
    r.check(15 not in dlg._suspects and shown == [(5.5, 7.5)],
            f"fixing a frame clears its band ({shown})")
    dlg._dirty = False
    dlg.close()
    return r.finish()

def _part_help() -> int:
    """Help shows on a section's title only: one tooltip per section naming
    each control, none on the controls themselves."""
    r = Report("pupil-help")
    app = qt_app()
    isolate_user_state()
    from PyQt6.QtCore import QEvent, QPoint
    from PyQt6.QtGui import QHelpEvent
    from PyQt6.QtWidgets import QGroupBox, QToolTip, QWidget
    from acqApp import widgets as W
    from acqApp.devices.pupil_cam.panel import SettingsPanel
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog

    panel = SettingsPanel(PupilSettings())
    W.collapsible_groups(panel, "help-test")        # as dialogs.py does
    panel.resize(380, 1200)
    panel.show()
    pump(app, 0.05)
    boxes = {getattr(b, "_base_title", b.title()): b
             for b in panel.findChildren(QGroupBox)}
    tipped = [w for w in panel.findChildren(QWidget) if w.toolTip()
              and not isinstance(w, QGroupBox)]
    r.check(not tipped, f"no control carries its own tooltip "
                        f"({[type(w).__name__ for w in tipped][:5]})")
    r.check(all(not b.toolTip() for b in boxes.values()),
            "nor does a box body (Qt's own tooltip would show anywhere on it)")
    track = boxes["Pupil tracking"]._help_text
    r.check("<b>Threshold</b>" in track and "<b>Auto</b>" in track
            and "<b>Shape</b>" in track and "Darker than this" in track,
            "the section's help names each control with its explanation")
    r.check("<b>Search out to</b>" in boxes["Reflections"]._help_text,
            "reflections section too")
    r.check("<b>Link</b>" in boxes["Camera"]._help_text,
            "camera section too")

    shown: list = []
    real_show, real_hide = QToolTip.showText, QToolTip.hideText
    QToolTip.showText = staticmethod(lambda *a, **k: shown.append(a[1]))
    QToolTip.hideText = staticmethod(lambda: shown.append(None))
    try:
        box = boxes["Pupil tracking"]

        def hover(pt):
            ev = QHelpEvent(QEvent.Type.ToolTip, pt, box.mapToGlobal(pt))
            app.sendEvent(box, ev)

        hover(QPoint(40, 6))                       # on the title
        r.check(shown and shown[-1] == track, "hovering the title shows it")
        hover(QPoint(40, box.height() - 6))        # inside the section
        r.check(shown[-1] is None, "hovering inside the section shows nothing")
    finally:
        QToolTip.showText, QToolTip.hideText = real_show, real_hide

    # Re-applying is harmless: help is kept, not wiped by the cleared tips.
    W.section_help(boxes["Pupil tracking"])
    r.check(boxes["Pupil tracking"]._help_text == track,
            "applying twice keeps the help")

    # The review window: same help on the same sections.
    tmp = Path(tempfile.mkdtemp(prefix="pupil_help_"))
    clip = write_avi(tmp / "c.avi", [video_eye_frame(64, 96, 40, 30, 10).tobytes()]
                     * 3, 96, 64, b"Y800", 8)
    dlg = PupilReviewDialog(str(clip))
    rboxes = {getattr(b, "_base_title", b.title()): b
              for b in dlg.findChildren(QGroupBox)}
    r.check("<b>Threshold</b>" in rboxes["Pupil tracking"]._help_text
            and not dlg._ctl._spn_thr.toolTip(),
            "review: help moved to the titles as well")
    r.check("Auto contrast" in rboxes["Recording"]._help_text,
            "review: the Recording section has its help")
    dlg._dirty = False
    dlg.close()
    return r.finish()

def _part_seed() -> int:  # noqa: PLR0915 — one linear scenario
    """When Auto is unsure, the user marks the pupil on a few frames."""
    r = Report("pupil-seed")
    from acqApp.devices.pupil_cam.autotune import autotune

    # Auto pins a reflection that stays put while the eye moves, not one
    # that moves with it.
    def glinted(i, moving):
        f = face_frame(cx=200 + 2 * i, seed=i, glint=False)
        Y, X = np.ogrid[:f.shape[0], :f.shape[1]]
        gx = (175 + 2 * i) if moving else 178
        f[(X - gx) ** 2 + (Y - 125) ** 2 < 16] = 235
        return f
    fixed = autotune([glinted(i, False) for i in range(8)], (100, 60, 320, 240))
    r.check(fixed is not None and len(fixed.pins) == 1
            and abs(fixed.pins[0][0] - 178) < 3 and abs(fixed.pins[0][1] - 125) < 3,
            f"a fixed reflection is pinned ({fixed and fixed.pins})")
    st = fixed.apply(PupilSettings(cr_pins=[(1.0, 2.0, 3.0)]))
    r.check(list(st.cr_pins) == [tuple(fixed.pins[0])], "apply() sets the pins")
    moving = autotune([glinted(i, True) for i in range(8)], (100, 60, 320, 240))
    r.check(moving is not None and not moving.pins
            and moving.apply(PupilSettings(cr_pins=[(1.0, 2.0, 3.0)])).cr_pins
            == [(1.0, 2.0, 3.0)],
            "one that moves is not, and hand-placed pins are kept")

    # A dark distractor (a round shadow, darker than the pupil) wins unaided.
    def tricky(seed):
        f = face_frame(seed=seed)
        Y, X = np.ogrid[:f.shape[0], :f.shape[1]]
        f[(X - 360) ** 2 + (Y - 60) ** 2 < 18 ** 2] = 8
        return f

    frames = [tricky(i) for i in range(5)]
    alone = autotune(frames, (0, 0, 420, 300))
    r.info(f"unaided: {alone and (alone.threshold, alone.notes)}")
    clicked = autotune(frames, (0, 0, 420, 300), [(210, 150, None)] * 5)
    r.check(clicked is not None and 20 <= clicked.threshold < 26,
            f"a clicked centre steers it to the pupil "
            f"({clicked and clicked.threshold})")
    drawn = autotune(frames, None, [(210, 150, 30.0)] * 5)
    r.check(drawn is not None and 20 <= drawn.threshold < 26
            and drawn.region is not None,
            f"a drawn circle sets the threshold and the region around it "
            f"({drawn and (drawn.threshold, drawn.region)})")
    if drawn is not None and drawn.region is not None:
        x0, y0, x1, y1 = drawn.region
        r.check(x0 < 210 < x1 and y0 < 150 < y1 and x1 - x0 < 300,
                f"…a box around the marks ({drawn.region})")
    skipped = autotune(frames, (0, 0, 420, 300),
                       [None, (210, 150, None), None, (210, 150, None), None])
    r.check(skipped is not None and 20 <= skipped.threshold < 26,
            "skipped frames (None) are left out")

    # ── the review window asks, the user marks, Auto applies ──
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam import review_dialog as rd
    tmp = Path(tempfile.mkdtemp(prefix="pupil_seed_"))
    clip = write_avi(tmp / "s.avi", [tricky(i).tobytes() for i in range(10)],
                     420, 300, b"Y800", 8)
    rd.NEEDS_HELP = 2.0                 # any unaided answer counts as unsure
    try:
        dlg = rd.PupilReviewDialog(str(clip))
        dlg.show()
        dlg._ctl._spn_thr.setValue(200)
        dlg._auto()
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(dlg.seeding and dlg._seed_bar.isVisible()
                and "click" in dlg._lbl_seed.text(),
                f"an unsure Auto asks for marks ({dlg._lbl_seed.text()[:40]})")
        r.check(dlg._frame == dlg._seed_frames[0] and dlg._roi is None,
                "it shows the first frame to mark, without the edit handle")
        r.check(not dlg._btn_seed_next.isEnabled(), "Next waits for a click")

        class Click:
            def __init__(self, x, y):
                self._p = dlg._vb.mapViewToScene(QPointF(x, y))

            def button(self):
                return Qt.MouseButton.LeftButton

            def scenePos(self):
                return self._p

            def accept(self):
                pass

        frames_seen = []
        for k in range(len(dlg._seed_frames)):
            frames_seen.append(dlg._frame)
            if k == 1:
                dlg._seed_next(skip=True)
                continue
            dlg._on_image_click(Click(210, 150))
            if k == 2:      # draw this one: resize the circle
                dlg._seed_roi.setSize((60, 60))
            r.check(dlg._btn_seed_next.isEnabled() or k == 1, f"click {k} placed")
            dlg._seed_next()
        r.check(frames_seen == dlg._seed_frames,
                f"each marked frame is shown in turn ({frames_seen})")
        r.check(dlg._seed_marks[1] is None and dlg._seed_marks[0][2] is None
                and dlg._seed_marks[2][2] is not None,
                "a skip, a click and a drawn circle are told apart")
        for _ in range(100):
            pump(app, 0.05)
            if dlg._auto_worker is None:
                break
        r.check(20 <= dlg._ctl._spn_thr.value() < 26 and not dlg.seeding
                and "Auto:" in dlg._prog.text(),
                f"the marks give a threshold ({dlg._ctl._spn_thr.value()}): "
                f"{dlg._prog.text()[:50]}")
        r.check(dlg._ctl._btn_auto.text() == "Auto"
                and dlg._ctl._btn_auto.isEnabled(), "Auto is back")
        # Cancel leaves everything as it was.
        thr = dlg._ctl._spn_thr.value()
        dlg._seed_start()
        dlg._seed_cancel()
        r.check(not dlg.seeding and dlg._ctl._spn_thr.value() == thr
                and dlg._btn_open.isEnabled(), "Cancel changes nothing")
        dlg._dirty = False
        dlg.close()
    finally:
        rd.NEEDS_HELP = 0.25

    # ── the live tab: an unsure answer asks for clicks; Auto again cancels ──
    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    m._on_auto_done(None)
    r.check(m._seed_clicks == [] and "click" in win.messages[-1]
            and m.panel.tracking._btn_auto.text() == "click pupil",
            f"live: no answer asks for clicks ({win.messages[-1][:50]})")
    m._on_auto_requested()
    r.check(m._seed_clicks is None and m.panel.tracking._btn_auto.text() == "Auto",
            "live: Auto again cancels")
    return r.finish()

def _part_apply() -> int:
    """A changed setting shows on the current frame at once; Apply to all
    frames keeps it; Revert goes back to the previous trace."""
    r = Report("pupil-apply")
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit

    class DiscTracking:
        error = None
        available = True

        def track(self, frame, st):
            x0, y0, x1, y1 = st.crop_box(frame.shape)
            ys, xs = np.nonzero(frame[y0:y1, x0:x1] < st.track_threshold)
            if xs.size < 20:
                return None
            rad = float(np.sqrt(xs.size / np.pi))
            return PupilFit(xs.mean() + x0, ys.mean() + y0, rad, rad, 0.0)

    review_mod.PupilTracking = DiscTracking
    app = qt_app()
    isolate_user_state()
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    tmp = Path(tempfile.mkdtemp(prefix="pupil_apply_"))
    # A soft-edged disc, so the threshold moves the radius.
    H, W = 120, 160
    Y, X = np.ogrid[:H, :W]
    d = np.sqrt((X - 80) ** 2 + (Y - 60) ** 2)
    frame = np.clip(20 + np.maximum(0, d - 10) * 6, 0, 190).astype(np.uint8)
    clip = write_avi(tmp / "a.avi", [frame.tobytes()] * 8, W, H, b"Y800", 8)
    dlg = PupilReviewDialog(str(clip))
    dlg._ctl.set_limit(40, 30, 120, 90)
    dlg._ctl._spn_thr.setValue(60)

    def settle():
        for _ in range(40):
            pump(app, 0.05)
            if dlg._worker is None and not dlg._preview_timer.isActive():
                return

    settle()
    r.check(dlg._preview is not None and dlg._preview[0] == dlg._frame
            and "preview" in dlg._lbl_state.text(),
            f"before any Apply, the shown frame is fitted ({dlg._lbl_state.text()})")
    dlg._track_all()
    settle()
    r1 = float(dlg.review.radius()[3])
    r.check(dlg._preview is None and not dlg._btn_revert.isEnabled(),
            "after Apply: no preview, nothing to revert yet")
    dlg.goto(3)
    dlg._ctl._spn_thr.setValue(100)
    settle()
    prev = dlg._preview
    r.check(prev is not None and prev[0] == 3 and prev[1].radius > r1 + 1,
            f"a new threshold shows on the current frame at once "
            f"({r1:.1f} -> {prev and prev[1].radius:.1f})")
    r.check(float(dlg.review.radius()[3]) == r1,
            "…without touching the saved trace")
    dlg.goto(5)
    settle()
    r.check(dlg._preview is not None and dlg._preview[0] == 5,
            "moving to another frame previews that one")
    r.check(dlg._btn_track.text() == "Apply to all frames", "the button's name")
    dlg._track_all()
    settle()
    r2 = float(dlg.review.radius()[3])
    r.check(r2 > r1 + 1 and dlg._btn_revert.isEnabled(),
            f"Apply keeps it for every frame ({r2:.1f}); Revert is offered")
    # The live view's overlays: removed reflections in red, and the views.
    class MaskTracking(DiscTracking):
        def __init__(self):
            self.last_mask = np.ones((60, 80), bool)
            self.last_box = (40, 30, 120, 90)
    review_mod.PupilTracking = MaskTracking
    dlg._ctl._chk_cr.setChecked(False)      # on by default: toggle to redraw
    dlg._ctl._chk_cr.setChecked(True)
    settle()
    r.check(dlg._mask is not None and dlg._mask_img.image is not None,
            "with removal on, what it removed is painted on the clip")
    r.check(not hasattr(dlg._ctl, "_chk_cr_mask"), "…with no check box for it")
    # Playing: every frame gets its overlay, not just where it stops.
    dlg._spn_rate.setValue(200.0)
    dlg.goto(0)
    painted = []
    real_draw = dlg._draw_mask
    dlg._draw_mask = lambda: (painted.append(dlg._frame), real_draw())[1]
    dlg.toggle_play()
    for _ in range(40):
        pump(app, 0.05)
        if not dlg.playing:
            break
    dlg._draw_mask = real_draw
    r.check(set(range(1, 8)) <= set(painted),
            f"playing overlays every frame ({sorted(set(painted))})")
    dlg._ctl._chk_cr.setChecked(False)
    settle()
    r.check(dlg._mask is None and dlg._mask_img.image is None,
            "removal off: nothing painted")
    review_mod.PupilTracking = DiscTracking
    dlg._cmb_view.setCurrentIndex(dlg._cmb_view.findData("crop"))
    rect = dlg._img.mapRectToParent(dlg._img.boundingRect())
    r.check(abs(rect.x() - 40) < 1 and abs(rect.width() - 80) < 1,
            f"Cropped to region shows just the region ({rect})")
    dlg._cmb_view.setCurrentIndex(dlg._cmb_view.findData("bare"))
    r.check(not dlg._fit_curve.isVisible(), "Full, no overlay hides the fit")
    dlg._cmb_view.setCurrentIndex(0)
    dlg._ctl._chk_cr.setChecked(False)
    settle()

    dlg._revert()
    r.check(float(dlg.review.radius()[3]) == r1
            and dlg._ctl._spn_thr.value() == 60 and not dlg.review.stale,
            "Revert restores the previous trace and its settings")
    r.check(not dlg._btn_revert.isEnabled(), "…once; then there is nothing older")
    dlg._dirty = False
    dlg.close()
    return r.finish()

def _part_mode() -> int:
    """Live and Review share the Pupil tab, switched by a segmented control."""
    r = Report("pupil-mode")
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule
    from acqApp.devices.pupil_cam.review_dialog import ReviewWidget
    from acqApp.widgets import SegmentedSwitch

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.docks: list = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

        def register_pg_view(self, _v) -> None:
            pass

        def add_dock(self, title, widget, *_a, **_k) -> None:
            self.docks.append((title, widget))

        def is_recording(self) -> bool:
            return False

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    m.build_views()
    r.check(isinstance(m.panel.mode, SegmentedSwitch)
            and m.panel.mode.value() == "live" and m.mode() == "live",
            "the tab opens in Live, with a Live | Review switch")
    m.panel.mode.button("review").click()
    r.check(m.mode() == "review" and isinstance(m._review, ReviewWidget)
            and m.panel._pages.currentWidget() is m.panel.review_page
            and m._view_stack.currentWidget() is m._review.view_widget,
            "Review shows the clip's controls in the panel and the clip in "
            "the same dock")
    r.check(m._review.side_widget.parent() is not None
            and m._review._ctl.isVisibleTo(m.panel.review_page),
            "…the same tracking controls, now for the clip")
    m.panel.mode.button("live").click()
    r.check(m.mode() == "live" and m.panel._pages.currentIndex() == 0,
            "back to Live")
    review = m._review
    m.panel.mode.button("review").click()
    r.check(m._review is review, "the review (and its clip) is kept between visits")

    # Live view takes the tab back; Review is refused while the camera runs.
    m.build_session(True)
    m.start()
    pump(app, 0.2)
    r.check(m.mode() == "live", "starting Live view returns to Live")
    m.panel.mode.button("review").click()
    r.check(m.mode() == "live" and m.panel.mode.value() == "live"
            and any("stop Live view" in x for x in win.messages),
            "Review is refused while the camera runs, and says why")
    m.stop()
    m.panel.mode.button("review").click()
    r.check(m.mode() == "review", "…and allowed once it stops")
    r.check(m.busy_reason() == "", "nothing fitting: the module set may change")
    m.close_controller()
    return r.finish()

def _part_recorded() -> int:
    """Review reads what acqApp records (the session folder's .avi, or the
    .tiff older sessions have) and opens the newest recording by itself."""
    r = Report("pupil-recorded")
    from acqApp.acq.writer import SessionWriter
    from acqApp.devices.pupil_cam.clip import open_clip, recorded_clip

    tmp = Path(tempfile.mkdtemp(prefix="pupil_rec_"))
    frames = [video_eye_frame(64, 96, 40 + i, 30, 10) for i in range(6)]

    sess = tmp / "sess"
    w = SessionWriter({"pupil_cam": "avi"})
    w.open(sess, {})
    for i, f in enumerate(frames):
        w.write("pupil_cam", i / 30.0, f)
    w.write("wheel", 0.0, 1.0)
    w.close()
    clip_avi = recorded_clip(sess)
    rd = open_clip(clip_avi)
    r.check(clip_avi is not None and clip_avi.name == "sess_pupil_cam.avi"
            and len(rd) == 6 and abs(rd.hz - 30.0) < 0.5,
            f"a session's pupil .avi is found and reads ({rd.describe()})")
    r.check(np.array_equal(rd.luma(3), frames[3]), "frames come back exact")

    split = tmp / "sess2"                       # an older session: TIFF
    w = SessionWriter()
    w.open(split, {})
    for i, f in enumerate(frames):
        w.write("pupil_cam", i / 30.0, f)
    w.close()
    clip = recorded_clip(split)
    r.check(clip is not None and clip.name == "sess2_pupil_cam.tiff",
            f"an older session's pupil TIFF is found ({clip})")
    rd = open_clip(clip)
    r.check(len(rd) == 6 and np.array_equal(rd.luma(5), frames[5]),
            "and reads")
    r.check(recorded_clip(None) is None and recorded_clip(tmp / "nope") is None,
            "recorded_clip edge cases")

    # The module remembers the recording and Review opens it.
    app = qt_app()
    isolate_user_state()
    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.path = None

        def status(self, msg): self.messages.append(msg)
        def on_worker_error(self, _m): pass
        def register_pg_view(self, _v): pass
        def add_dock(self, *_a, **_k): pass
        def is_recording(self): return False
        def recording_path(self): return self.path

    class FakeRec:
        def put(self, *_a, **_k): pass

    win = FakeWin()
    m = PupilCamModule(win)
    m.build_panel()
    m.build_views()
    m.build_session(True)
    win.path = sess
    m.attach_sink(FakeRec())
    m.detach_sink()
    m.stop()
    r.check(m._last_recording == sess, "recording with the pupil camera is remembered")
    m.panel.mode.button("review").click()
    rv = m._review
    r.check(rv.review is not None and rv.review.video == clip_avi
            and any("newest recording" in x for x in win.messages),
            "Review opens it by itself")
    rv._dirty = True
    m._last_recording = split
    m.panel.mode.button("live").click()
    m.panel.mode.button("review").click()
    r.check(rv.review.video == clip_avi and "save or discard" in win.messages[-1],
            "unsaved edits on the open clip are not thrown away for it")
    rv._dirty = False
    m.panel.mode.button("live").click()
    m.panel.mode.button("review").click()
    r.check(rv.review.video == clip, "…and once saved/discarded the newest opens")
    m.close_controller()
    return r.finish()


PARTS = {
    "eyeloop": _part_eyeloop,
    "track": _part_track,
    "limit": _part_limit,
    "video": _part_video,
    "review": _part_review,
    "launcher": _part_launcher,
    "autotune": _part_autotune,
    "safety": _part_review_safety,
    "mirror": _part_mirror,
    "help": _part_help,
    "seed": _part_seed,
    "apply": _part_apply,
    "mode": _part_mode,
    "recorded": _part_recorded,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
