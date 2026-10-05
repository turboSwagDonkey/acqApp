"""Pupil camera: EyeLoop seam, tracking, the eye region, clip replay,
the exposure bar, section help. Review is test_pupil_review.py, Auto
test_pupil_auto.py.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil.py [-v] [--part NAME]
"""
from __future__ import annotations

import math
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np
from _harness import (Report, isolate_user_state, make_window, npoints, pump,
                      qt_app, run_parts)
from _pupil_helpers import video_eye_frame, write_avi
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

    # ── a degenerate edge (collinear points) doesn't flood the console ───────
    # EyeLoop's ellipse fit casts its complex eigen-solution to float there;
    # numpy warned once per frame. The wrapper silences only EyeLoop's.
    import types
    import warnings
    if str(EYELOOP_DIR) not in sys.path:
        sys.path.insert(0, str(EYELOOP_DIR))
    import eyeloop.config as el_config
    el_config.arguments = types.SimpleNamespace(model="ellipsoid")
    el_config.engine = types.SimpleNamespace(dataout={}, width=200, height=200,
                                             angle=0)
    from eyeloop.engine.models.ellipsoid import Ellipse
    line = np.column_stack([np.linspace(0, 50, 30), np.linspace(0, 20, 30)])

    def complex_warnings(always: bool) -> int:
        with warnings.catch_warnings(record=True) as got:
            if always:
                warnings.simplefilter("always", np.exceptions.ComplexWarning)
            try:
                Ellipse(None).fit(line)
            except Exception:           # noqa: BLE001 — a bad fit is fine here
                pass
        return sum(issubclass(w.category, np.exceptions.ComplexWarning)
                   for w in got)

    r.check(complex_warnings(False) == 0,
            "a degenerate edge prints no ComplexWarning")
    r.check(complex_warnings(True) > 0,
            "control: without the filter that same fit does warn")

    # ── trackers take turns on the shared config, silently and safely ───────
    from acqApp.devices.pupil_cam.eyeloop_tracker import EyeLoopTracker
    big, small = EyeLoopTracker(), EyeLoopTracker()
    with warnings.catch_warnings(record=True) as got:
        warnings.simplefilter("always")
        big.arm(200, 100, (100.0, 50.0))
        small.arm(60, 40, (30.0, 20.0))
        stale = (el_config.engine.width, el_config.engine.height)
        big.reset((90.0, 45.0))
    r.check(stale == (60, 40),
            "control: the last tracker armed owns the shared frame size")
    r.check(big._shape.standard_corners == [(0, 0), (200, 100)],
            f"a tracker re-seeded after another was armed walks its own frame "
            f"({big._shape.standard_corners})")
    r.check(not [w for w in got if issubclass(w.category, RuntimeWarning)],
            "…and taking turns prints nothing")

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

    # A fit is recorded only with its frame: one the camera published before
    # attach_sink was never recorded, and its fit made fits outnumber frames.
    class _Cam:
        f = None

        def get_latest(self):
            f, self.f = self.f, None
            return f

    cam, nowhere = _Cam(), types.SimpleNamespace(put=lambda *a, **k: None)
    cam.f = np.zeros((4, 4), np.uint8)              # published, never recorded
    mod._pull_frame(cam)
    early = FakeRec()
    mod._record_fit(early, None, False, 0.5)
    r.check(early.puts == [], "a frame published before recording started "
            "gets no fit rows")
    frame = np.zeros((4, 4), np.uint8)
    mod._record_frame(nowhere, frame)                # recorded, then published
    cam.f = frame
    mod._pull_frame(cam)
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
        def button(self): return Qt.MouseButton.LeftButton

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

    r.check(not mod._btn_pin.isChecked(), "…and pinning disarms itself")
    mod._btn_pin.setChecked(True)
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
    # ── 10. always on: no switch, no Clear, an empty box changes nothing ─────
    r.check(not hasattr(panel.tracking, "_chk_region")
            and not hasattr(mod, "_btn_limit_off"),
            "no Eye region check box and no Clear: the region is always on")
    panel.set_limit(0.0, 0.0, 0.0, 0.0)
    r.check(not seen and panel.settings.search_limit()
            == (400.0, 150.0, 600.0, 350.0),
            "an empty box is ignored, the region stays")

    # ── 11. the rectangle is drawn and read back ─────────────────────────────
    panel.set_limit(150.0, 200.0, 500.0, 320.0)
    pump(app, 0.05)
    xs = mod._limit_curve.getData()[0]
    r.check(xs is not None and abs(xs.min() - 150.0) < 1.0
            and abs(xs.max() - 500.0) < 1.0,
            f"the rectangle is drawn where it is ({xs.min():.0f}-{xs.max():.0f})")
    r.check("150" in mod._lbl_limit.text() and "500" in mod._lbl_limit.text(),
            f"…and the preview bar reads it back ({mod._lbl_limit.text()!r})")

    # ── 12. none saved: the first frame gives it the middle half ─────────────
    panel.tracking._set_region((0.0, 0.0, 0.0, 0.0))    # as a fresh install
    r.check(panel.settings.search_limit() is None, "fixture: no region saved")
    win._btn_run.setChecked(True)
    pump(app, 1.0)                       # frames flow
    h, w = mod._last_frame.shape[:2]
    default = (w * 0.25, h * 0.25, w * 0.75, h * 0.75)
    r.check(panel.settings.search_limit() == default,
            f"the first frame sets the middle half ({panel.settings.search_limit()})")

    # ── 13. placing it on the preview: press-drag, release commits + disarms ─

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
    r.check(panel.settings.search_limit() == default,
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


# ═══ the exposure bar and section help ══════════════════════════════

def _part_exposure() -> int:
    """Rate is typed; Exposure is a bar from the camera's minimum to 1/rate."""
    r = Report("pupil-exposure")
    app = qt_app()      # held: a collected QApplication takes the process down
    isolate_user_state()
    from acqApp.devices.pupil_cam.panel import SettingsPanel
    from acqApp.widgets import RangeBar

    p = SettingsPanel(PupilSettings(exposure_us=8000.0, rate_hz=20.0))
    sent, saved = [], []
    p.exposure_changed.connect(sent.append)
    p.settings_changed.connect(lambda s: saved.append(s.exposure_us))
    r.check(isinstance(p._exp, RangeBar) and abs(p._exp.maximum() - 50_000) < 1e-6
            and p._exp.value() == 8000.0,
            f"exposure is a bar ending at 1/rate ({p._exp.maximum():g} µs at 20 Hz)")
    p._spn_hz.setValue(200.0)
    r.check(abs(p._exp.maximum() - 5000) < 1e-6 and p._exp.value() == 5000.0
            and sent[-1] == 5000.0 and saved[-1] == 5000.0,
            "a faster rate shortens the bar and pulls a longer exposure in, "
            "telling the camera and the settings")
    p._spn_hz.setValue(10.0)
    r.check(abs(p._exp.maximum() - 100_000) < 1e-6 and p._exp.value() == 5000.0,
            "a slower rate lengthens the bar without moving the exposure")
    p.set_exposure_min(35.0)
    r.check(p._exp.minimum() == 35.0, "the short end is the camera's own minimum")
    n = len(saved)
    p._exp._sld.setSliderDown(True)
    for pos in (100, 300, 500):
        p._exp._sld.setValue(pos)
    r.check(len(sent) >= 3 and len(saved) == n,
            "dragging updates the camera live but saves nothing yet")
    p._exp._sld.setSliderDown(False)       # Qt emits sliderReleased
    r.check(len(saved) == n + 1 and 35.0 < p._exp.value() < 100_000,
            f"releasing saves once ({p._exp._lbl.text()})")
    r.check(not hasattr(p, "_chk_hz_link"), "no Link box: the bar is the link")
    app.processEvents()
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
    r.check("<b>Exposure</b>" in boxes["Camera"]._help_text,
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


# ═══ real mouse events through the viewport (pins, lock) ════════════════
# QTest on the view's viewport, so pyqtgraph's own dispatch runs.

L, R, M = (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton,
           Qt.MouseButton.MiddleButton)
_NONE = Qt.KeyboardModifier.NoModifier


def at(gv, vb, x, y):
    """Image px -> viewport px."""
    return gv.mapFromScene(vb.mapViewToScene(QPointF(x, y)))


def click(gv, pt, btn=L):
    from PyQt6.QtTest import QTest
    app = qt_app()
    QTest.mouseClick(gv.viewport(), btn, _NONE, pt)
    pump(app, 0.05)
    for w in app.topLevelWidgets():          # a right click's menu
        if w.isVisible() and w.metaObject().className() == "QMenu":
            w.close()


def _send(vp, kind, p, button, buttons):
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    p = QPointF(p)
    qt_app().sendEvent(vp, QMouseEvent(getattr(QEvent.Type, kind), p,
                                       QPointF(vp.mapToGlobal(p)), button,
                                       buttons, _NONE))


def dclick(gv, pt):
    """As the OS sends it: press, release, double-click, release.
    (QTest.mouseDClick sends the double-click alone.)"""
    from PyQt6.QtTest import QTest
    vp = gv.viewport()
    QTest.mousePress(vp, L, _NONE, pt)
    QTest.mouseRelease(vp, L, _NONE, pt)
    _send(vp, "MouseButtonDblClick", pt, L, L)
    QTest.mouseRelease(vp, L, _NONE, pt)
    pump(qt_app(), 0.05)


def drag(gv, a, b):
    from PyQt6.QtTest import QTest
    vp = gv.viewport()
    QTest.mousePress(vp, L, _NONE, a)
    for k in range(1, 9):
        _send(vp, "MouseMove", a + (b - a) * k / 8, Qt.MouseButton.NoButton, L)
        pump(qt_app(), 0.01)
    QTest.mouseRelease(vp, L, _NONE, b)
    pump(qt_app(), 0.05)


def _live_view(app):
    """The pupil module running on the mock, its view showing 320x240."""
    isolate_user_state()
    sys.argv = ["main.py", "--mock"]
    win = make_window({"pupil_cam"})
    win.show()
    mod = win._modules[0]
    win._btn_run.setChecked(True)
    pump(app, 1.0)
    for _ in range(4):
        win._display_tick()
        pump(app, 0.05)
    pump(app, 0.2)                           # let the dock lay the view out
    mod._vb.setRange(xRange=(0, 320), yRange=(0, 240), padding=0)
    pump(app, 0.1)
    return win, mod


def _review_view(app, tag, **st):
    """A Pupil review window on a 160x120 clip, shown whole."""
    from _pupil_helpers import DiscTracking
    from acqApp.devices.pupil_cam import review as review_mod
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    review_mod.PupilTracking = DiscTracking
    tmp = Path(tempfile.mkdtemp(prefix=f"pupil_{tag}_"))
    H, W = 120, 160
    clip = write_avi(tmp / "eye.avi",
                     [video_eye_frame(H, W, 80, 60, 14).tobytes()] * 6, W, H,
                     b"Y800", 8)
    dlg = PupilReviewDialog(str(clip), settings=PupilSettings(**st))
    dlg.resize(1000, 700)
    dlg.show()
    pump(app, 0.3)
    dlg._vb.setRange(xRange=(0, W), yRange=(0, H), padding=0)
    pump(app, 0.1)
    return dlg, clip


def _part_pins() -> int:  # noqa: PLR0915 — one linear scenario
    """Stray pins: Pin reflection stayed armed, so every later click pinned;
    right and middle clicks pinned too, and a double click pinned then
    unpinned."""
    r = Report("pupil-pins")
    app = qt_app()

    def stray(name, arm, count, clear, act):
        """Armed, `act` must not pin."""
        clear()
        arm(True)
        act()
        r.check(count() == 0, f"{name}: no pin ({count()})")
        arm(False)

    # ── Live ──
    win, mod = _live_view(app)
    panel = mod.panel
    gv, vb, btn = mod._gv, mod._vb, mod._btn_pin
    count = lambda: len(panel.settings.cr_pins)         # noqa: E731
    panel.clear_pins()

    click(gv, at(gv, vb, 100, 100))
    r.check(count() == 0, f"live control: unarmed, a click pins nothing ({count()})")
    btn.setChecked(True)
    click(gv, at(gv, vb, 100, 100))
    r.check(count() == 1, f"live control: armed, a real click pins one ({count()})")
    r.check(not btn.isChecked(), "live: one pin disarms Pin reflection")
    click(gv, at(gv, vb, 200, 150))
    r.check(count() == 1, f"live: a later click pins nothing more ({count()}); "
                          f"it was 2 before the fix")
    btn.setChecked(True)
    click(gv, at(gv, vb, 100, 100))
    r.check(count() == 0 and not btn.isChecked(),
            f"live: re-armed, clicking the pin removes it, and disarms ({count()})")
    for name, act in (
            ("live right click", lambda: click(gv, at(gv, vb, 150, 120), R)),
            ("live middle click", lambda: click(gv, at(gv, vb, 150, 120), M)),
            ("live drag", lambda: drag(gv, at(gv, vb, 50, 50), at(gv, vb, 120, 120)))):
        stray(name, btn.setChecked, count, panel.clear_pins, act)
    panel.clear_pins()
    btn.setChecked(True)
    dclick(gv, at(gv, vb, 60, 60))
    r.check(count() == 1, f"live: a double click pins once, not on-then-off ({count()})")
    panel.clear_pins()
    mod._seed_clicks = []                   # Auto asking for the pupil
    click(gv, at(gv, vb, 150, 120), R)
    r.check(mod._seed_clicks == [], "live: a right click is not an Auto seed")
    click(gv, at(gv, vb, 150, 120))
    r.check(len(mod._seed_clicks) == 1, "live control: a left click is")
    mod._seed_clicks = None
    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    # ── Review ──
    dlg, clip = _review_view(app, "pins", limit_x0=40, limit_y0=30,
                             limit_x1=120, limit_y1=90)
    gv, vb, btn = dlg._gv, dlg._vb, dlg._btn_pin_cr
    count = lambda: len(dlg._read_settings().cr_pins)   # noqa: E731
    clear = dlg._ctl.clear_pins

    click(gv, at(gv, vb, 20, 20))
    r.check(count() == 0, f"review control: unarmed, a click pins nothing ({count()})")
    btn.setChecked(True)
    click(gv, at(gv, vb, 20, 20))
    r.check(count() == 1 and not btn.isChecked(),
            f"review control: armed, a real click pins one and disarms ({count()})")
    click(gv, at(gv, vb, 140, 100))
    r.check(count() == 1, f"review: a later click pins nothing more ({count()})")
    for name, act in (
            ("review right click", lambda: click(gv, at(gv, vb, 140, 20), R)),
            ("review drag", lambda: drag(gv, at(gv, vb, 10, 10), at(gv, vb, 30, 30)))):
        stray(name, btn.setChecked, count, clear, act)
    clear()
    btn.setChecked(True)
    dclick(gv, at(gv, vb, 20, 100))
    r.check(count() == 1, f"review: a double click pins once ({count()})")
    clear()
    btn.setChecked(True)
    r.check("pin or unpin" in dlg._prog.text(), "review: arming says what to do")
    dlg._prog.setText("saved eye.avi.pupil.json")
    click(gv, at(gv, vb, 20, 20))
    r.check(dlg._prog.text() == "saved eye.avi.pupil.json",
            f"review: disarming keeps a newer status ({dlg._prog.text()!r})")
    clear()
    btn.setChecked(True)
    dlg.open_video(str(clip))
    r.check(not btn.isChecked(), "review: opening a clip disarms Pin reflection")
    dlg._dirty = False
    dlg.close()
    return r.finish()


def _part_lock() -> int:  # noqa: PLR0915 — one linear scenario
    """A locked eye region: Set eye region, a drag and Auto can't move it,
    in Live and Review; saved with the settings. Each blocked move has an
    unlocked control that does move it."""
    r = Report("pupil-lock")
    from PyQt6.QtTest import QTest
    from acqApp.devices.pupil_cam.autotune import AutoTune
    app = qt_app()
    box = (100.0, 80.0, 220.0, 180.0)

    def auto(region):           # what Auto suggests: a new threshold, a region
        return AutoTune(33, 3, False, None, region, 0.9, "test")

    # ── settings ──
    r.check(PupilSettings().limit_locked is False, "shipped default: unlocked")
    st = PupilSettings(limit_x0=1, limit_y0=1, limit_x1=9, limit_y1=9)
    r.check(auto((0, 0, 50, 50)).apply(st).search_limit() == (0, 0, 50, 50),
            "control: Auto's region replaces an unlocked one")
    st.limit_locked = True
    moved = auto((0, 0, 50, 50)).apply(st)
    r.check(moved.search_limit() == (1, 1, 9, 9) and moved.track_threshold == 33,
            "Auto leaves a locked region alone, and still sets the threshold")

    # ── Live ──
    win, mod = _live_view(app)
    panel = mod.panel
    gv, vb = mod._gv, mod._vb
    panel.set_limit(*box)
    r.check(mod._btn_lock.isEnabled() and not mod._btn_lock.isChecked(),
            "live: Lock is offered once there is a region")

    def draw(x0, y0, x1, y1):
        """Set eye region as the user does it: press the button, drag."""
        QTest.mouseClick(mod._btn_limit, L)
        pump(app, 0.1)                       # the dock settles its layout
        drag(gv, at(gv, vb, x0, y0), at(gv, vb, x1, y1))

    draw(40, 40, 150, 120)
    lim = panel.settings.search_limit()
    r.check(lim is not None and abs(lim[0] - 40) < 3 and abs(lim[2] - 150) < 3,
            f"live control: unlocked, Set eye region moves it ({lim})")
    panel.set_limit(*box)

    QTest.mouseClick(mod._btn_lock, L)
    r.check(panel.settings.limit_locked and mod._btn_lock.isChecked(),
            "live: Lock locks it, in the settings")
    r.check(not mod._btn_limit.isEnabled() and "locked" in mod._lbl_limit.text(),
            f"live: Set eye region is greyed out, the bar says so "
            f"({mod._lbl_limit.text()!r})")
    draw(40, 40, 150, 120)
    r.check(not mod._btn_limit.isChecked()
            and panel.settings.search_limit() == box,
            f"live: pressing it and dragging moves nothing "
            f"({panel.settings.search_limit()})")
    vb.set_draw_mode(True)                   # even if the drag got through
    drag(gv, at(gv, vb, 40, 40), at(gv, vb, 150, 120))
    vb.set_draw_mode(False)
    r.check(panel.settings.search_limit() == box,
            "live: a drag that reaches the panel is refused too")
    panel.set_limit(0, 0, 60, 60)
    r.check(panel.settings.search_limit() == box, "live: so is set_limit")
    mod._on_auto_done(auto((0, 0, 50, 50)))
    r.check(panel.settings.search_limit() == box
            and panel.tracking._spn_thr.value() == 33,
            f"live: Auto sets the threshold, not the region "
            f"({panel.settings.search_limit()})")
    from acqApp import config
    saved = config.load_settings("pupil_cam") or {}
    r.check(saved.get("limit_locked") is True, "live: the lock is saved")

    QTest.mouseClick(mod._btn_lock, L)
    r.check(not panel.settings.limit_locked and mod._btn_limit.isEnabled(),
            "live: Lock again unlocks")
    mod._on_auto_done(auto((10.0, 10.0, 90.0, 70.0)))
    r.check(panel.settings.search_limit() == (10.0, 10.0, 90.0, 70.0),
            f"live control: unlocked, Auto's region is taken "
            f"({panel.settings.search_limit()})")
    win._btn_run.setChecked(False)
    pump(app, 0.2)
    win.close()
    pump(app, 0.1)

    # ── Review ──
    rbox = (40.0, 30.0, 120.0, 90.0)
    dlg, clip = _review_view(app, "lock", limit_x0=40, limit_y0=30,
                             limit_x1=120, limit_y1=90)
    gv, vb = dlg._gv, dlg._vb
    region = lambda: dlg._read_settings().search_limit()    # noqa: E731

    drag(gv, at(gv, vb, 80, 60), at(gv, vb, 100, 70))
    moved = region()
    r.check(moved is not None and abs(moved[0] - 60) < 3,
            f"review control: unlocked, dragging the box moves it ({moved})")
    dlg._ctl.set_limit(*rbox)
    dlg._sync_region_box()

    QTest.mouseClick(dlg._btn_lock, L)
    r.check(dlg._read_settings().limit_locked and dlg._btn_lock.isChecked(),
            "review: Lock region locks it")
    r.check(not dlg._region.translatable and not dlg._region.getHandles(),
            "review: the box can't be dragged and has no handles")
    drag(gv, at(gv, vb, 80, 60), at(gv, vb, 100, 70))
    drag(gv, at(gv, vb, 120, 90), at(gv, vb, 150, 110))     # where the handle was
    r.check(region() == rbox, f"review: dragging moves nothing ({region()})")
    dlg._on_auto(auto((0, 0, 50, 50)))
    r.check(region() == rbox and dlg._ctl._spn_thr.value() == 33,
            f"review: Auto sets the threshold, not the region ({region()})")
    r.check(dlg.save(), "review: saved")
    dlg._dirty = False
    dlg.close()
    pump(app, 0.1)

    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog
    dlg = PupilReviewDialog(str(clip))
    r.check(dlg._btn_lock.isChecked() and region() == rbox
            and not dlg._region.translatable,
            "review: reopened, the region is still locked")
    QTest.mouseClick(dlg._btn_lock, L)
    r.check(dlg._region.translatable and dlg._region.getHandles(),
            "review: unlocked, the box drags again")
    dlg._on_auto(auto((10.0, 10.0, 90.0, 70.0)))
    r.check(region() == (10.0, 10.0, 90.0, 70.0),
            f"review control: unlocked, Auto's region is taken ({region()})")
    dlg._dirty = False
    dlg.close()

    # Opened with no region: the default is Auto's to move until locked.
    dlg, _ = _review_view(app, "lock2")
    r.check(dlg._region_default, "fixture: a clip with no saved region")
    QTest.mouseClick(dlg._btn_lock, L)
    r.check(not dlg._region_default,
            "review: locking the default makes it the user's, so Auto keeps to it")
    QTest.mouseClick(dlg._btn_lock, L)
    r.check(dlg._region_default,
            "review: unlocked again, the default is Auto's to move once more")

    # Revert brings back the older trace, not the older region.
    def track(box_):
        dlg._ctl.set_limit(*box_)
        dlg._track_all()
        for _ in range(100):
            pump(app, 0.05)
            if dlg._worker is None:
                break

    a, b = (40.0, 30.0, 120.0, 90.0), (30.0, 20.0, 130.0, 100.0)
    track(a)
    track(b)
    dlg._revert()
    r.check(region() == a, f"review control: unlocked, Revert restores the "
                           f"trace's region ({region()})")
    track(b)
    QTest.mouseClick(dlg._btn_lock, L)
    dlg._revert()
    r.check(region() == b and dlg._read_settings().limit_locked
            and dlg._btn_lock.isChecked() and dlg.review.stale
            and "locked" in dlg._prog.text(),
            f"review: locked, Revert keeps the region, says so, and the trace "
            f"is marked for re-tracking ({region()})")
    dlg._dirty = False
    dlg.close()

    empty = PupilReviewDialog()
    r.check(not empty._btn_lock.isEnabled(),
            "review: no clip, nothing to lock (the button is off)")
    empty.close()
    return r.finish()


PARTS = {
    "eyeloop": _part_eyeloop,
    "track": _part_track,
    "pins": _part_pins,
    "lock": _part_lock,
    "limit": _part_limit,
    "video": _part_video,
    "exposure": _part_exposure,
    "help": _part_help,
}

if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
